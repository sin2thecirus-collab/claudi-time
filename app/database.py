"""Datenbank-Konfiguration und Session-Management."""

import logging
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)

from app.config import settings


class Base(DeclarativeBase):
    """Basis-Klasse für alle SQLAlchemy Models."""

    pass


# Async Engine erstellen
engine = create_async_engine(
    settings.database_url,
    echo=settings.is_development,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

# Session Factory
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency für FastAPI-Endpoints.

    Liefert eine Datenbank-Session und räumt nach dem Request auf.
    """
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def _ensure_company_tables() -> None:
    """Erstellt Company-Tabellen falls sie nicht existieren (Railway-Fallback)."""

    # --- Schritt 1: Pruefen ob Tabellen existieren ---
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'companies'"
            )
        )
        tables_exist = result.fetchone() is not None

    if not tables_exist:
        logger.info("Company-Tabellen werden erstellt...")

        async with engine.begin() as conn:
            # Enum-Typen erstellen
            await conn.execute(text(
                "DO $$ BEGIN "
                "CREATE TYPE companystatus AS ENUM ('active', 'blacklist', 'laufende_prozesse'); "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            ))
            await conn.execute(text(
                "DO $$ BEGIN "
                "CREATE TYPE correspondencedirection AS ENUM ('inbound', 'outbound'); "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            ))

            # companies Tabelle
            await conn.execute(text("""
                CREATE TABLE companies (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name VARCHAR(255) NOT NULL UNIQUE,
                    domain VARCHAR(255),
                    street VARCHAR(255),
                    house_number VARCHAR(20),
                    postal_code VARCHAR(10),
                    city VARCHAR(100),
                    employee_count VARCHAR(50),
                    status companystatus DEFAULT 'active',
                    notes TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))

            # PostGIS Koordinaten-Spalte
            await conn.execute(text("""
                DO $$ BEGIN
                    PERFORM AddGeographyColumn('public', 'companies', 'location_coords', 4326, 'POINT', 2);
                EXCEPTION WHEN OTHERS THEN
                    BEGIN
                        ALTER TABLE companies ADD COLUMN location_coords TEXT;
                    EXCEPTION WHEN duplicate_column THEN
                        NULL;
                    END;
                END $$
            """))

            # Indizes
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_companies_name ON companies (name)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_companies_city ON companies (city)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_companies_status ON companies (status)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_companies_created_at ON companies (created_at)"))

            # company_contacts Tabelle
            await conn.execute(text("""
                CREATE TABLE company_contacts (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    salutation VARCHAR(20),
                    first_name VARCHAR(100),
                    last_name VARCHAR(100),
                    position VARCHAR(255),
                    email VARCHAR(500),
                    phone VARCHAR(100),
                    notes TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_company_contacts_company_id ON company_contacts (company_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_company_contacts_last_name ON company_contacts (last_name)"))

            # company_correspondence Tabelle
            await conn.execute(text("""
                CREATE TABLE company_correspondence (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    contact_id UUID REFERENCES company_contacts(id) ON DELETE SET NULL,
                    direction correspondencedirection DEFAULT 'outbound',
                    subject VARCHAR(500) NOT NULL,
                    body TEXT,
                    sent_at TIMESTAMPTZ DEFAULT NOW(),
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_company_correspondence_company_id ON company_correspondence (company_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_company_correspondence_sent_at ON company_correspondence (sent_at)"))

            # jobs.company_id FK hinzufuegen (falls noch nicht vorhanden)
            result = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'jobs' AND column_name = 'company_id'"
                )
            )
            if not result.fetchone():
                await conn.execute(text("ALTER TABLE jobs ADD COLUMN company_id UUID"))
                await conn.execute(text(
                    "ALTER TABLE jobs ADD CONSTRAINT fk_jobs_company_id "
                    "FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE SET NULL"
                ))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_company_id ON jobs (company_id)"))

            # Datenmigration: bestehende company_name → Company Records
            await conn.execute(text("""
                INSERT INTO companies (id, name, city, postal_code, street, created_at, updated_at)
                SELECT
                    gen_random_uuid(),
                    sub.company_name,
                    sub.city,
                    sub.postal_code,
                    sub.street_address,
                    NOW(),
                    NOW()
                FROM (
                    SELECT DISTINCT ON (TRIM(company_name))
                        TRIM(company_name) AS company_name,
                        city,
                        postal_code,
                        street_address
                    FROM jobs
                    WHERE company_name IS NOT NULL
                        AND TRIM(company_name) != ''
                        AND deleted_at IS NULL
                    ORDER BY TRIM(company_name), created_at DESC
                ) sub
                WHERE NOT EXISTS (
                    SELECT 1 FROM companies c
                    WHERE LOWER(c.name) = LOWER(sub.company_name)
                )
            """))

            # Jobs mit company_id verknuepfen
            await conn.execute(text("""
                UPDATE jobs j
                SET company_id = c.id
                FROM companies c
                WHERE LOWER(TRIM(j.company_name)) = LOWER(c.name)
                    AND j.company_id IS NULL
            """))

            logger.info("Company-Tabellen erfolgreich erstellt und Daten migriert.")
    else:
        logger.info("Company-Tabellen existieren bereits.")

    # --- Schritt 2: Sicherstellen dass location_coords existiert (immer) ---
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                DO $$ BEGIN
                    ALTER TABLE companies ADD COLUMN location_coords TEXT;
                EXCEPTION WHEN duplicate_column THEN
                    NULL;
                END $$
            """))
    except Exception as e:
        logger.warning(f"location_coords Check uebersprungen: {e}")


async def init_db() -> None:
    """Initialisiert die Datenbankverbindung und führt Migrationen aus."""
    async with engine.begin() as conn:
        # Verbindung testen
        await conn.run_sync(lambda _: None)

    # ── Tabellen-Erstellung (fuer neue Tabellen die nicht via Alembic laufen) ──
    await _ensure_company_tables()

    # ── pgvector Extension NICHT noetig — Embeddings werden als JSONB gespeichert ──
    # Railway Standard-PostgreSQL hat kein pgvector vorinstalliert.
    # Similarity-Suche laeuft stattdessen in Python (performant genug fuer ~2000 Kandidaten).

    # Automatische Migrationen für neue Spalten
    # Lock-Timeout setzen damit ALTER TABLE nicht ewig blockiert
    # Format: (table, column_name, column_type)
    migrations = [
        # Candidates: bestehende Felder
        ("candidates", "languages", "JSONB"),
        ("candidates", "it_skills", "VARCHAR[]"),
        ("candidates", "further_education", "JSONB"),
        ("candidates", "cv_parse_failed", "BOOLEAN DEFAULT FALSE"),
        ("candidates", "deleted_at", "TIMESTAMPTZ"),
        ("candidates", "manual_overrides", "JSONB"),
        # Candidates: Hotlist-Felder
        ("candidates", "hotlist_category", "VARCHAR(50)"),
        ("candidates", "hotlist_city", "VARCHAR(255)"),
        ("candidates", "hotlist_job_title", "VARCHAR(255)"),
        ("candidates", "hotlist_job_titles", "VARCHAR[]"),
        ("candidates", "categorized_at", "TIMESTAMPTZ"),
        ("candidates", "classification_data", "JSONB"),
        ("candidates", "cv_stored_path", "TEXT"),
        # Jobs: Hotlist-Felder
        ("jobs", "hotlist_category", "VARCHAR(50)"),
        ("jobs", "hotlist_city", "VARCHAR(255)"),
        ("jobs", "hotlist_job_title", "VARCHAR(255)"),
        ("jobs", "hotlist_job_titles", "VARCHAR[]"),
        ("jobs", "categorized_at", "TIMESTAMPTZ"),
        # Matches: DeepMatch-Felder
        ("matches", "pre_score", "FLOAT"),
        ("matches", "user_feedback", "VARCHAR(50)"),
        ("matches", "feedback_note", "TEXT"),
        ("matches", "feedback_at", "TIMESTAMPTZ"),
        # Matches: Stale-Tracking (Pipeline)
        ("matches", "stale", "BOOLEAN DEFAULT FALSE"),
        ("matches", "stale_reason", "VARCHAR(255)"),
        ("matches", "stale_since", "TIMESTAMPTZ"),
        # Jobs: Company FK
        ("jobs", "company_id", "UUID"),
        # Jobs: Import-Tracking
        ("jobs", "imported_at", "TIMESTAMPTZ DEFAULT NOW()"),
        ("jobs", "last_updated_at", "TIMESTAMPTZ"),
        # Embeddings (als JSONB-Array gespeichert — kein pgvector noetig)
        ("candidates", "embedding", "JSONB"),
        ("jobs", "embedding", "JSONB"),
        # Match Center: Matching-Methode (pre_match, deep_match, smart_match, manual)
        ("matches", "matching_method", "VARCHAR(50)"),
        # Manuelle Jobtitel-Zuweisung (Kandidaten: ARRAY, Jobs: einzeln)
        ("candidates", "manual_job_titles", "VARCHAR[]"),
        ("candidates", "manual_job_titles_set_at", "TIMESTAMPTZ"),
        ("jobs", "manual_job_title", "VARCHAR(255)"),
        ("jobs", "manual_job_title_set_at", "TIMESTAMPTZ"),
    ]
    for table_name, col_name, col_type in migrations:
        try:
            async with engine.begin() as conn:
                # Lock-Timeout auf 5 Sekunden setzen
                await conn.execute(text("SET lock_timeout = '5s'"))
                result = await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :table AND column_name = :col"
                    ),
                    {"table": table_name, "col": col_name},
                )
                if not result.fetchone():
                    logger.info(f"Migration: Füge '{table_name}.{col_name}' Spalte hinzu...")
                    await conn.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
                    )
                    logger.info(f"Migration: '{table_name}.{col_name}' Spalte hinzugefügt.")
        except Exception as e:
            logger.warning(f"Migration für '{table_name}.{col_name}' übersprungen: {e}")

    # Backfill: imported_at = created_at fuer bestehende Jobs ohne imported_at
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text("UPDATE jobs SET imported_at = created_at WHERE imported_at IS NULL")
            )
            if result.rowcount > 0:
                logger.info(f"Backfill: {result.rowcount} Jobs imported_at = created_at gesetzt")
    except Exception as e:
        logger.warning(f"Backfill imported_at übersprungen: {e}")

    # ── Match-FK-Migration: CASCADE → SET NULL ──
    # Matches duerfen NICHT geloescht werden wenn Jobs/Kandidaten entfernt werden.
    # Die Lerndaten muessen dauerhaft erhalten bleiben.
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET lock_timeout = '5s'"))
            for fk_name, col, ref in [
                ("matches_job_id_fkey", "job_id", "jobs(id)"),
                ("matches_candidate_id_fkey", "candidate_id", "candidates(id)"),
            ]:
                await conn.execute(text(f"""
                    DO $$ BEGIN
                        ALTER TABLE matches DROP CONSTRAINT IF EXISTS {fk_name};
                        ALTER TABLE matches ALTER COLUMN {col} DROP NOT NULL;
                        ALTER TABLE matches ADD CONSTRAINT {fk_name}
                            FOREIGN KEY ({col}) REFERENCES {ref} ON DELETE SET NULL;
                    EXCEPTION WHEN OTHERS THEN NULL;
                    END $$
                """))
            logger.info("Migration: matches FK CASCADE → SET NULL erfolgreich.")
    except Exception as e:
        logger.warning(f"FK-Migration uebersprungen: {e}")

    # ── Backfill matching_method fuer bestehende Matches ──
    try:
        async with engine.begin() as conn:
            # Matches mit AI-Bewertung → smart_match (letzter grosser Lauf war Smart Match)
            result1 = await conn.execute(text("""
                UPDATE matches SET matching_method = 'smart_match'
                WHERE matching_method IS NULL AND ai_checked_at IS NOT NULL
            """))
            # Matches ohne AI-Bewertung → pre_match (alter regelbasierter Lauf)
            result2 = await conn.execute(text("""
                UPDATE matches SET matching_method = 'pre_match'
                WHERE matching_method IS NULL AND ai_checked_at IS NULL
            """))
            total = (result1.rowcount or 0) + (result2.rowcount or 0)
            if total > 0:
                logger.info(f"Backfill matching_method: {total} Matches getaggt "
                            f"(smart_match={result1.rowcount}, pre_match={result2.rowcount})")
    except Exception as e:
        logger.warning(f"Backfill matching_method uebersprungen: {e}")

    # ── Index fuer matching_method ──
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_matches_matching_method ON matches (matching_method)"
            ))
    except Exception as e:
        logger.warning(f"Index ix_matches_matching_method uebersprungen: {e}")

    # ── MT Lern-Tabellen erstellen ──
    try:
        async with engine.begin() as conn:
            # mt_training_data — Lern-Daten aus manuellen Jobtitel-Zuweisungen
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS mt_training_data (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    entity_type VARCHAR(20) NOT NULL,
                    entity_id UUID NOT NULL,
                    input_text TEXT,
                    predicted_titles JSONB,
                    assigned_titles JSONB NOT NULL,
                    was_correct BOOLEAN,
                    reasoning TEXT,
                    embedding JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_mt_training_entity "
                "ON mt_training_data (entity_type, entity_id)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_mt_training_created "
                "ON mt_training_data (created_at)"
            ))

            # mt_match_memory — Gedaechtnis fuer Match-Entscheidungen
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS mt_match_memory (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    candidate_id UUID REFERENCES candidates(id) ON DELETE SET NULL,
                    job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,
                    company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
                    action VARCHAR(50) NOT NULL,
                    rejection_reason TEXT,
                    never_again_company BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_mt_memory_candidate "
                "ON mt_match_memory (candidate_id)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_mt_memory_company "
                "ON mt_match_memory (company_id)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_mt_memory_never_again "
                "ON mt_match_memory (candidate_id, company_id) WHERE never_again_company = TRUE"
            ))
            logger.info("MT Lern-Tabellen erstellt/geprueft.")
    except Exception as e:
        logger.warning(f"MT Lern-Tabellen Erstellung uebersprungen: {e}")

    # ── Embedding-Indexes nicht noetig (JSONB, kein pgvector) ──
    # Similarity-Suche laeuft in Python, nicht in SQL.
