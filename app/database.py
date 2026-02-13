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
    pool_recycle=300,       # Connections nach 5 Min recyceln
    pool_timeout=30,        # Max 30s auf freie Connection warten
    connect_args={
        "server_settings": {
            "statement_timeout": "15000",     # 15s max pro Statement
            "lock_timeout": "5000",           # 5s max auf Lock warten
            "idle_in_transaction_session_timeout": "30000",  # 30s max idle in TX
        }
    },
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

    # --- Schritt 3: Sicherstellen dass phone existiert (immer) ---
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                DO $$ BEGIN
                    ALTER TABLE companies ADD COLUMN phone VARCHAR(100);
                EXCEPTION WHEN duplicate_column THEN
                    NULL;
                END $$
            """))
    except Exception as e:
        logger.warning(f"phone Check uebersprungen: {e}")


async def _ensure_ats_tables() -> None:
    """Erstellt ATS-Tabellen falls sie nicht existieren."""

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'ats_jobs'"
            )
        )
        tables_exist = result.fetchone() is not None

    if tables_exist:
        logger.info("ATS-Tabellen existieren bereits.")
        return

    logger.info("ATS-Tabellen werden erstellt...")

    async with engine.begin() as conn:
        # Enum-Typen erstellen
        for enum_name, enum_values in [
            ("atsjobpriority", "'low', 'medium', 'high', 'urgent'"),
            ("atsjobstatus", "'open', 'paused', 'filled', 'cancelled'"),
            ("pipelinestage", "'matched', 'sent', 'feedback', 'interview_1', 'interview_2', 'interview_3', 'offer', 'placed', 'rejected'"),
            ("calltype", "'acquisition', 'qualification', 'followup', 'candidate_call'"),
            ("todostatus", "'open', 'in_progress', 'done', 'cancelled'"),
            ("todopriority", "'low', 'normal', 'high', 'urgent'"),
            ("activitytype", "'stage_changed', 'note_added', 'todo_created', 'todo_completed', 'email_sent', 'email_received', 'call_logged', 'candidate_added', 'candidate_removed', 'job_created', 'job_status_changed'"),
        ]:
            await conn.execute(text(
                f"DO $$ BEGIN "
                f"CREATE TYPE {enum_name} AS ENUM ({enum_values}); "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            ))

        # ats_jobs Tabelle
        await conn.execute(text("""
            CREATE TABLE ats_jobs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
                contact_id UUID REFERENCES company_contacts(id) ON DELETE SET NULL,
                title VARCHAR(255) NOT NULL,
                description TEXT,
                requirements TEXT,
                location_city VARCHAR(100),
                salary_min INTEGER,
                salary_max INTEGER,
                employment_type VARCHAR(50),
                priority atsjobpriority DEFAULT 'medium',
                status atsjobstatus DEFAULT 'open',
                source VARCHAR(100),
                notes TEXT,
                filled_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))

        # PostGIS Koordinaten-Spalte fuer ats_jobs
        await conn.execute(text("""
            DO $$ BEGIN
                PERFORM AddGeographyColumn('public', 'ats_jobs', 'location_coords', 4326, 'POINT', 2);
            EXCEPTION WHEN OTHERS THEN
                BEGIN
                    ALTER TABLE ats_jobs ADD COLUMN location_coords TEXT;
                EXCEPTION WHEN duplicate_column THEN
                    NULL;
                END;
            END $$
        """))

        # ats_jobs Indizes
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_jobs_company_id ON ats_jobs (company_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_jobs_status ON ats_jobs (status)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_jobs_priority ON ats_jobs (priority)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_jobs_created_at ON ats_jobs (created_at)"))

        # ats_pipeline_entries Tabelle
        await conn.execute(text("""
            CREATE TABLE ats_pipeline_entries (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                ats_job_id UUID NOT NULL REFERENCES ats_jobs(id) ON DELETE CASCADE,
                candidate_id UUID REFERENCES candidates(id) ON DELETE SET NULL,
                stage pipelinestage DEFAULT 'matched',
                stage_changed_at TIMESTAMPTZ DEFAULT NOW(),
                rejection_reason TEXT,
                notes TEXT,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_ats_pipeline_job_candidate UNIQUE (ats_job_id, candidate_id)
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_pipeline_ats_job_id ON ats_pipeline_entries (ats_job_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_pipeline_candidate_id ON ats_pipeline_entries (candidate_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_pipeline_stage ON ats_pipeline_entries (stage)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_pipeline_created_at ON ats_pipeline_entries (created_at)"))

        # ats_call_notes Tabelle
        await conn.execute(text("""
            CREATE TABLE ats_call_notes (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                ats_job_id UUID REFERENCES ats_jobs(id) ON DELETE SET NULL,
                company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
                candidate_id UUID REFERENCES candidates(id) ON DELETE SET NULL,
                contact_id UUID REFERENCES company_contacts(id) ON DELETE SET NULL,
                call_type calltype NOT NULL,
                summary TEXT NOT NULL,
                raw_notes TEXT,
                action_items JSONB,
                duration_minutes INTEGER,
                called_at TIMESTAMPTZ DEFAULT NOW(),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_call_notes_company_id ON ats_call_notes (company_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_call_notes_candidate_id ON ats_call_notes (candidate_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_call_notes_ats_job_id ON ats_call_notes (ats_job_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_call_notes_call_type ON ats_call_notes (call_type)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_call_notes_called_at ON ats_call_notes (called_at)"))

        # ats_todos Tabelle
        await conn.execute(text("""
            CREATE TABLE ats_todos (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                title VARCHAR(500) NOT NULL,
                description TEXT,
                status todostatus DEFAULT 'open',
                priority todopriority DEFAULT 'normal',
                due_date DATE,
                completed_at TIMESTAMPTZ,
                company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
                candidate_id UUID REFERENCES candidates(id) ON DELETE SET NULL,
                ats_job_id UUID REFERENCES ats_jobs(id) ON DELETE SET NULL,
                call_note_id UUID REFERENCES ats_call_notes(id) ON DELETE SET NULL,
                pipeline_entry_id UUID REFERENCES ats_pipeline_entries(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_todos_status ON ats_todos (status)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_todos_priority ON ats_todos (priority)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_todos_due_date ON ats_todos (due_date)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_todos_company_id ON ats_todos (company_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_todos_candidate_id ON ats_todos (candidate_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_todos_ats_job_id ON ats_todos (ats_job_id)"))

        # ats_activities Tabelle
        await conn.execute(text("""
            CREATE TABLE ats_activities (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                activity_type activitytype NOT NULL,
                description VARCHAR(500) NOT NULL,
                metadata JSONB,
                ats_job_id UUID REFERENCES ats_jobs(id) ON DELETE SET NULL,
                pipeline_entry_id UUID REFERENCES ats_pipeline_entries(id) ON DELETE SET NULL,
                company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
                candidate_id UUID REFERENCES candidates(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_activities_ats_job_id ON ats_activities (ats_job_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_activities_pipeline_entry_id ON ats_activities (pipeline_entry_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_activities_company_id ON ats_activities (company_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ats_activities_created_at ON ats_activities (created_at)"))

        # ats_email_templates Tabelle
        await conn.execute(text("""
            CREATE TABLE ats_email_templates (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name VARCHAR(255) NOT NULL UNIQUE,
                subject VARCHAR(500) NOT NULL,
                body TEXT NOT NULL,
                category VARCHAR(100),
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))

        logger.info("ATS-Tabellen erfolgreich erstellt.")


async def _ensure_matching_v2_tables() -> None:
    """Erstellt Matching Engine v2 Tabellen falls sie nicht existieren."""

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'match_v2_training_data'"
            )
        )
        if result.fetchone() is not None:
            logger.info("Matching v2 Tabellen existieren bereits.")
            return

    logger.info("Matching v2 Tabellen werden erstellt...")

    async with engine.begin() as conn:
        # match_v2_training_data — Feedback-Daten fuer ML-Training
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS match_v2_training_data (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                match_id UUID REFERENCES matches(id) ON DELETE SET NULL,
                job_id UUID,
                candidate_id UUID,
                features JSONB,
                outcome VARCHAR(20),
                outcome_source VARCHAR(20),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_v2_training_match_id ON match_v2_training_data (match_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_v2_training_outcome ON match_v2_training_data (outcome)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_v2_training_created_at ON match_v2_training_data (created_at)"))

        # match_v2_learned_rules — Automatisch entdeckte Matching-Regeln
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS match_v2_learned_rules (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                rule_type VARCHAR(30) NOT NULL,
                rule_json JSONB NOT NULL,
                confidence FLOAT,
                support_count INTEGER,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_v2_rules_type ON match_v2_learned_rules (rule_type)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_v2_rules_active ON match_v2_learned_rules (active)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_v2_rules_confidence ON match_v2_learned_rules (confidence)"))

        # match_v2_scoring_weights — Lernbare Gewichte fuer Score-Komponenten
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS match_v2_scoring_weights (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                component VARCHAR(50) NOT NULL UNIQUE,
                weight FLOAT NOT NULL,
                default_weight FLOAT NOT NULL,
                adjustment_count INTEGER DEFAULT 0,
                last_adjusted_at TIMESTAMPTZ
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_v2_weights_component ON match_v2_scoring_weights (component)"))

        # Default-Gewichte: Entfernung ist jetzt Hard Filter, nicht mehr Soft-Score
        # Alte Weights (distance, city_metro) durch neue ersetzen
        await conn.execute(text("""
            DELETE FROM match_v2_scoring_weights
            WHERE component IN ('distance', 'city_metro')
        """))
        # Aktualisiere bestehende Weights auf neue Werte
        await conn.execute(text("""
            UPDATE match_v2_scoring_weights SET weight = 35.0, default_weight = 35.0 WHERE component = 'skill_overlap'
        """))
        await conn.execute(text("""
            UPDATE match_v2_scoring_weights SET weight = 10.0, default_weight = 10.0 WHERE component = 'software_match'
        """))
        await conn.execute(text("""
            INSERT INTO match_v2_scoring_weights (id, component, weight, default_weight, adjustment_count)
            SELECT gen_random_uuid(), v.component, v.weight, v.weight, 0
            FROM (VALUES
                ('skill_overlap', 35.0),
                ('seniority_fit', 20.0),
                ('embedding_sim', 20.0),
                ('career_fit', 10.0),
                ('software_match', 10.0),
                ('location_bonus', 5.0)
            ) AS v(component, weight)
            WHERE NOT EXISTS (
                SELECT 1 FROM match_v2_scoring_weights WHERE component = v.component
            )
        """))

        logger.info("Matching v2 Tabellen erfolgreich erstellt.")


async def _ensure_unassigned_calls_table() -> None:
    """Erstellt unassigned_calls Tabelle (Zwischenspeicher fuer unzugeordnete Anrufe)."""

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'unassigned_calls'"
            )
        )
        if result.fetchone() is not None:
            logger.info("unassigned_calls Tabelle existiert bereits.")
            return

    logger.info("unassigned_calls Tabelle wird erstellt...")

    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS unassigned_calls (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                phone_number VARCHAR(100),
                direction VARCHAR(20),
                call_date TIMESTAMPTZ,
                duration_seconds INTEGER,
                transcript TEXT,
                call_summary TEXT,
                extracted_data JSONB,
                recording_topic VARCHAR(500),
                webex_recording_id VARCHAR(255),
                mt_payload JSONB,
                assigned BOOLEAN DEFAULT FALSE,
                assigned_to_type VARCHAR(20),
                assigned_to_id UUID,
                assigned_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_unassigned_calls_assigned ON unassigned_calls (assigned)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_unassigned_calls_phone ON unassigned_calls (phone_number)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_unassigned_calls_created ON unassigned_calls (created_at)"
        ))

        logger.info("unassigned_calls Tabelle erfolgreich erstellt.")


async def _ensure_candidate_notes_table() -> None:
    """Erstellt candidate_notes Tabelle + Backfill aus altem Freitext-Feld."""

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'candidate_notes'"
            )
        )
        if result.fetchone() is not None:
            logger.info("candidate_notes Tabelle existiert bereits.")
            return

    logger.info("candidate_notes Tabelle wird erstellt...")

    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS candidate_notes (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                candidate_id UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
                title VARCHAR(500),
                content TEXT NOT NULL,
                source VARCHAR(50),
                note_date TIMESTAMPTZ DEFAULT NOW(),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_candidate_notes_candidate_id ON candidate_notes (candidate_id)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_candidate_notes_note_date ON candidate_notes (note_date)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_candidate_notes_created_at ON candidate_notes (created_at)"
        ))

        logger.info("candidate_notes Tabelle erfolgreich erstellt.")

    # ── Backfill: Bestehende candidate_notes Freitext → einzelne CandidateNote Eintraege ──
    try:
        async with engine.begin() as conn:
            # Alle Kandidaten mit Notizen-Text laden
            rows = await conn.execute(text(
                "SELECT id, candidate_notes FROM candidates "
                "WHERE candidate_notes IS NOT NULL AND candidate_notes != ''"
            ))
            candidates_with_notes = rows.fetchall()

            if not candidates_with_notes:
                logger.info("Backfill: Keine bestehenden Kandidaten-Notizen zum Migrieren.")
                return

            migrated = 0
            for row in candidates_with_notes:
                cand_id, notes_text = row[0], row[1]
                # Parse: Notizen sind getrennt durch "--- DATUM ---" oder einfach Freitext
                # Format: "--- 12.02.2026 16:07 | Qualifizierungsgespräch (KI) ---\nText..."
                import re
                blocks = re.split(r'\n*---\s*(.+?)\s*---\n*', notes_text)

                if len(blocks) <= 1:
                    # Kein Datumsformat erkannt → als eine einzelne manuelle Notiz migrieren
                    await conn.execute(text("""
                        INSERT INTO candidate_notes (id, candidate_id, title, content, source, note_date, created_at)
                        VALUES (gen_random_uuid(), :cid, 'Migrierte Notiz', :content, 'system', NOW(), NOW())
                    """), {"cid": str(cand_id), "content": notes_text.strip()})
                    migrated += 1
                else:
                    # blocks = ['', 'HEADER1', 'CONTENT1', 'HEADER2', 'CONTENT2', ...]
                    # Ungerade Indizes = Header, gerade Indizes (>0) = Content
                    # Block[0] kann Freitext vor dem ersten Separator sein
                    if blocks[0].strip():
                        await conn.execute(text("""
                            INSERT INTO candidate_notes (id, candidate_id, title, content, source, note_date, created_at)
                            VALUES (gen_random_uuid(), :cid, 'Migrierte Notiz', :content, 'system', NOW(), NOW())
                        """), {"cid": str(cand_id), "content": blocks[0].strip()})
                        migrated += 1

                    for i in range(1, len(blocks) - 1, 2):
                        header = blocks[i].strip()
                        content = blocks[i + 1].strip() if (i + 1) < len(blocks) else ""
                        if not content:
                            continue

                        # Versuche Datum aus Header zu parsen: "12.02.2026 16:07 | Qualifizierungsgespräch (KI)"
                        title = header
                        note_date_str = None
                        source = "system"
                        date_match = re.match(r'(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})\s*\|\s*(.*)', header)
                        if date_match:
                            note_date_str = date_match.group(1)
                            title = date_match.group(2).strip() or header
                            if "KI" in title.upper() or "Transkription" in title.lower():
                                source = "ki_transkription"

                        if note_date_str:
                            await conn.execute(text("""
                                INSERT INTO candidate_notes (id, candidate_id, title, content, source, note_date, created_at)
                                VALUES (gen_random_uuid(), :cid, :title, :content, :source,
                                        TO_TIMESTAMP(:ndate, 'DD.MM.YYYY HH24:MI'), NOW())
                            """), {
                                "cid": str(cand_id), "title": title, "content": content,
                                "source": source, "ndate": note_date_str,
                            })
                        else:
                            await conn.execute(text("""
                                INSERT INTO candidate_notes (id, candidate_id, title, content, source, note_date, created_at)
                                VALUES (gen_random_uuid(), :cid, :title, :content, :source, NOW(), NOW())
                            """), {
                                "cid": str(cand_id), "title": title, "content": content, "source": source,
                            })
                        migrated += 1

            if migrated > 0:
                logger.info(f"Backfill: {migrated} Kandidaten-Notizen in candidate_notes migriert.")
    except Exception as e:
        logger.warning(f"Backfill candidate_notes uebersprungen: {e}")


async def _ensure_users_table() -> None:
    """Erstellt users Tabelle + Admin-User aus ENV-Variablen."""

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'users'"
            )
        )
        tables_exist = result.fetchone() is not None

    if not tables_exist:
        logger.info("users Tabelle wird erstellt...")
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS users (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    email VARCHAR(255) NOT NULL UNIQUE,
                    hashed_password VARCHAR(255) NOT NULL,
                    role VARCHAR(20) DEFAULT 'admin',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
        logger.info("users Tabelle erfolgreich erstellt.")

    # Admin-User aus ENV erstellen/aktualisieren
    from app.config import settings
    if settings.admin_email and settings.admin_password:
        from passlib.context import CryptContext
        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        hashed = pwd_context.hash(settings.admin_password)
        admin_email = settings.admin_email.strip().lower()

        async with engine.begin() as conn:
            # Pruefen ob Admin existiert
            result = await conn.execute(
                text("SELECT id, hashed_password FROM users WHERE email = :email"),
                {"email": admin_email},
            )
            existing = result.fetchone()

            if existing:
                # Passwort aktualisieren falls ENV geaendert wurde
                if not pwd_context.verify(settings.admin_password, existing[1]):
                    await conn.execute(
                        text("UPDATE users SET hashed_password = :pw WHERE email = :email"),
                        {"pw": hashed, "email": admin_email},
                    )
                    logger.info(f"Admin-User {admin_email} Passwort aktualisiert.")
            else:
                await conn.execute(
                    text(
                        "INSERT INTO users (id, email, hashed_password, role) "
                        "VALUES (gen_random_uuid(), :email, :pw, 'admin')"
                    ),
                    {"email": admin_email, "pw": hashed},
                )
                logger.info(f"Admin-User {admin_email} erstellt.")


async def init_db() -> None:
    """Initialisiert die Datenbankverbindung und führt Migrationen aus."""
    # Schritt 0: Alle haengenden Transaktionen killen (von vorherigen Deployments)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE state = 'idle in transaction'
                  AND pid != pg_backend_pid()
                  AND query_start < NOW() - INTERVAL '30 seconds'
            """))
            logger.info("init_db: Haengende idle-in-transaction Connections gekillt.")
    except Exception as e:
        logger.warning(f"init_db: Konnte idle Transactions nicht killen: {e}")

    async with engine.begin() as conn:
        # Verbindung testen
        await conn.run_sync(lambda _: None)

    # ── Tabellen-Erstellung (fuer neue Tabellen die nicht via Alembic laufen) ──
    await _ensure_users_table()
    await _ensure_company_tables()
    await _ensure_ats_tables()
    await _ensure_matching_v2_tables()
    await _ensure_unassigned_calls_table()
    await _ensure_candidate_notes_table()

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
        # ATS Jobs: Pipeline-Uebersicht Flag
        ("ats_jobs", "in_pipeline", "BOOLEAN DEFAULT FALSE"),
        # Phase 5: Company address consolidation
        ("companies", "address", "TEXT"),
        # Phase 5: Contact city + mobile
        ("company_contacts", "city", "VARCHAR(255)"),
        ("company_contacts", "mobile", "VARCHAR(100)"),
        # Phase 5: Todo contact_id FK
        ("ats_todos", "contact_id", "UUID"),
        # ── Matching Engine v2: Kandidaten-Profil ──
        ("candidates", "v2_seniority_level", "INTEGER"),
        ("candidates", "v2_career_trajectory", "VARCHAR(20)"),
        ("candidates", "v2_years_experience", "INTEGER"),
        ("candidates", "v2_structured_skills", "JSONB"),
        ("candidates", "v2_current_role_summary", "TEXT"),
        ("candidates", "v2_profile_created_at", "TIMESTAMPTZ"),
        ("candidates", "v2_embedding_current", "JSONB"),
        ("candidates", "v2_embedding_full", "JSONB"),
        # ── Matching Engine v2: Job-Profil ──
        ("jobs", "v2_seniority_level", "INTEGER"),
        ("jobs", "v2_required_skills", "JSONB"),
        ("jobs", "v2_role_summary", "TEXT"),
        ("jobs", "v2_profile_created_at", "TIMESTAMPTZ"),
        ("jobs", "v2_embedding", "JSONB"),
        # ── Matching Engine v2: Match-Score ──
        ("matches", "v2_score", "FLOAT"),
        ("matches", "v2_score_breakdown", "JSONB"),
        ("matches", "v2_matched_at", "TIMESTAMPTZ"),
        # Kandidaten: Gehalt/Kuendigungsfrist/ERP
        ("candidates", "salary", "VARCHAR(100)"),
        ("candidates", "notice_period", "VARCHAR(100)"),
        ("candidates", "erp", "VARCHAR[]"),
        ("candidates", "rating", "INTEGER"),
        ("candidates", "rating_set_at", "TIMESTAMPTZ"),
        # ── Matching Engine v2.5: Phase 0e + Phase 1 ──
        ("jobs", "work_arrangement", "VARCHAR(20)"),  # "vor_ort" / "hybrid" / "remote"
        ("candidates", "v2_certifications", "JSONB"),  # z.B. ["Bilanzbuchhalter"]
        ("candidates", "v2_industries", "JSONB"),  # z.B. ["Maschinenbau", "Pharma"]
        # ── Phase 2: Recruiting-Daten (Quelle, Letzter Kontakt, Wechselbereitschaft, Notizen) ──
        ("candidates", "source", "VARCHAR(50)"),
        ("candidates", "last_contact", "TIMESTAMPTZ"),
        ("candidates", "willingness_to_change", "VARCHAR(20)"),
        ("candidates", "candidate_notes", "TEXT"),
        ("companies", "industry", "VARCHAR(100)"),
        ("companies", "erp_systems", "VARCHAR[]"),
        # ── Matching Learning System Upgrade ──
        ("matches", "rejection_reason", "VARCHAR(50)"),  # bad_distance, bad_skills, bad_seniority
        ("match_v2_training_data", "rejection_reason", "VARCHAR(50)"),
        ("match_v2_training_data", "job_category", "VARCHAR(100)"),
        ("match_v2_scoring_weights", "job_category", "VARCHAR(100)"),
        # ── Phase 2.1: Kandidaten-Antwort-System + Numerische ID ──
        ("candidates", "candidate_number", "INTEGER"),
        ("candidates", "presented_at_companies", "JSONB"),
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

    # ── Learning System: UNIQUE Constraint auf scoring_weights anpassen ──
    # Alt: component UNIQUE | Neu: (component, job_category) Paar
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET lock_timeout = '5s'"))
            # Alten UNIQUE Constraint entfernen (falls vorhanden)
            await conn.execute(text("""
                DO $$ BEGIN
                    ALTER TABLE match_v2_scoring_weights DROP CONSTRAINT IF EXISTS match_v2_scoring_weights_component_key;
                EXCEPTION WHEN OTHERS THEN NULL;
                END $$
            """))
            # Neuen UNIQUE Constraint erstellen: (component, job_category) — NULLS NOT DISTINCT
            await conn.execute(text("""
                DO $$ BEGIN
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_v2_weights_component_category
                    ON match_v2_scoring_weights (component, COALESCE(job_category, '__global__'));
                EXCEPTION WHEN OTHERS THEN NULL;
                END $$
            """))
            logger.info("Migration: scoring_weights UNIQUE Constraint auf (component, job_category) aktualisiert.")
    except Exception as e:
        logger.warning(f"scoring_weights UNIQUE Migration uebersprungen: {e}")

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

    # ── Todo Priority Enum Migration (5 Stufen) ──
    # ALTER TYPE ... ADD VALUE muss AUSSERHALB einer Transaktion laufen (Autocommit)
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            for new_val in ["unwichtig", "mittelmaessig", "wichtig", "dringend", "sehr_dringend"]:
                try:
                    await conn.execute(text(
                        f"ALTER TYPE todopriority ADD VALUE IF NOT EXISTS '{new_val}'"
                    ))
                except Exception:
                    pass  # Wert existiert bereits
            logger.info("Todo Priority Enum: 5 Stufen sichergestellt")
    except Exception as e:
        logger.warning(f"Todo Priority Enum Migration uebersprungen: {e}")

    # Bestehende Todos auf neue Werte migrieren + Default setzen
    try:
        async with engine.begin() as conn:
            for old_val, new_val in [
                ("low", "unwichtig"),
                ("normal", "mittelmaessig"),
                ("high", "wichtig"),
                ("urgent", "dringend"),
            ]:
                result = await conn.execute(text(f"""
                    UPDATE ats_todos SET priority = '{new_val}'
                    WHERE priority = '{old_val}'
                """))
                if result.rowcount and result.rowcount > 0:
                    logger.info(f"Todo Priority Migration: {result.rowcount}x {old_val} -> {new_val}")

            await conn.execute(text("""
                DO $$ BEGIN
                    ALTER TABLE ats_todos ALTER COLUMN priority SET DEFAULT 'wichtig';
                EXCEPTION WHEN OTHERS THEN NULL;
                END $$
            """))
    except Exception as e:
        logger.warning(f"Todo Priority Daten-Migration uebersprungen: {e}")

    # ── ActivityType Enum: candidate_response hinzufuegen ──
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            try:
                await conn.execute(text(
                    "ALTER TYPE activitytype ADD VALUE IF NOT EXISTS 'candidate_response'"
                ))
            except Exception:
                pass  # Wert existiert bereits
            logger.info("ActivityType Enum: candidate_response sichergestellt")
    except Exception as e:
        logger.warning(f"ActivityType Enum Migration uebersprungen: {e}")

    # ── Candidate Number: Sequence + Backfill + Unique Constraint ──
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET lock_timeout = '5s'"))
            # 1. Sequence erstellen (idempotent)
            await conn.execute(text("""
                DO $$ BEGIN
                    CREATE SEQUENCE IF NOT EXISTS candidates_candidate_number_seq;
                EXCEPTION WHEN OTHERS THEN NULL;
                END $$
            """))
            # 2. Default setzen (fuer neue Kandidaten)
            await conn.execute(text("""
                DO $$ BEGIN
                    ALTER TABLE candidates
                        ALTER COLUMN candidate_number
                        SET DEFAULT nextval('candidates_candidate_number_seq');
                EXCEPTION WHEN OTHERS THEN NULL;
                END $$
            """))
            # 3. Backfill: bestehende Kandidaten ohne candidate_number
            await conn.execute(text("""
                UPDATE candidates
                SET candidate_number = nextval('candidates_candidate_number_seq')
                WHERE candidate_number IS NULL
            """))
            # 4. Unique Index (idempotent)
            await conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS ix_candidates_candidate_number
                ON candidates (candidate_number)
            """))
            logger.info("Migration: candidates.candidate_number Sequence + Backfill erfolgreich.")
    except Exception as e:
        logger.warning(f"candidate_number Migration uebersprungen: {e}")

    # ── Email-Index fuer n8n by-email Lookup ──
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_candidates_email ON candidates (email)"
            ))
    except Exception as e:
        logger.warning(f"Email-Index uebersprungen: {e}")

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

    # ── Phase 5: company_documents Tabelle ──
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS company_documents (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    filename VARCHAR(500) NOT NULL,
                    file_path TEXT NOT NULL,
                    file_size INTEGER,
                    mime_type VARCHAR(100),
                    notes TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_company_documents_company_id "
                "ON company_documents (company_id)"
            ))
    except Exception as e:
        logger.warning(f"company_documents Tabelle uebersprungen: {e}")

    # ── Phase 5: ats_todos contact_id Spalte + FK hinzufuegen ──
    try:
        async with engine.begin() as conn:
            # Spalte hinzufuegen falls nicht vorhanden
            await conn.execute(text("""
                DO $$ BEGIN
                    ALTER TABLE ats_todos ADD COLUMN contact_id UUID;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """))
            # FK Constraint
            await conn.execute(text("""
                DO $$ BEGIN
                    ALTER TABLE ats_todos ADD CONSTRAINT fk_ats_todos_contact_id
                        FOREIGN KEY (contact_id) REFERENCES company_contacts(id) ON DELETE SET NULL;
                EXCEPTION WHEN duplicate_object THEN NULL;
                END $$
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_ats_todos_contact_id ON ats_todos (contact_id)"
            ))
    except Exception as e:
        logger.warning(f"ats_todos contact_id FK uebersprungen: {e}")

    # ── Phase 5: company_notes Tabelle ──
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS company_notes (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    contact_id UUID REFERENCES company_contacts(id) ON DELETE SET NULL,
                    title VARCHAR(500),
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_company_notes_company_id "
                "ON company_notes (company_id)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_company_notes_created_at "
                "ON company_notes (created_at)"
            ))
    except Exception as e:
        logger.warning(f"company_notes Tabelle uebersprungen: {e}")

    # ── CallDirection Enum + direction Spalte fuer ats_call_notes ──
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(
                "DO $$ BEGIN "
                "CREATE TYPE calldirection AS ENUM ('outbound', 'inbound'); "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            ))
            logger.info("CallDirection Enum erstellt/geprueft.")
    except Exception as e:
        logger.warning(f"CallDirection Enum uebersprungen: {e}")

    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                DO $$ BEGIN
                    ALTER TABLE ats_call_notes ADD COLUMN direction VARCHAR;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """))
    except Exception as e:
        logger.warning(f"direction Spalte uebersprungen: {e}")

    # ── Matching Engine v2: Indizes fuer Profil-Spalten ──
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_candidates_v2_seniority ON candidates (v2_seniority_level)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_candidates_v2_profile ON candidates (v2_profile_created_at)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_v2_seniority ON jobs (v2_seniority_level)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_v2_profile ON jobs (v2_profile_created_at)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_matches_v2_score ON matches (v2_score)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_matches_v2_matched_at ON matches (v2_matched_at)"))
    except Exception as e:
        logger.warning(f"Matching v2 Indizes uebersprungen: {e}")

    # ── Matching v2.5: Company Unique Constraint (name → name+city) ──
    # Multi-Standort-Firmen: "Allianz München" und "Allianz Hamburg" = 2 separate Companies
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET lock_timeout = '5s'"))
            await conn.execute(text("""
                DO $$ BEGIN
                    -- Alten name-only Constraint entfernen
                    ALTER TABLE companies DROP CONSTRAINT IF EXISTS companies_name_key;
                    -- Neuen Compound-Constraint erstellen (idempotent via IF NOT EXISTS)
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'uq_companies_name_city'
                    ) THEN
                        ALTER TABLE companies ADD CONSTRAINT uq_companies_name_city UNIQUE (name, city);
                    END IF;
                EXCEPTION WHEN OTHERS THEN
                    RAISE NOTICE 'Company Constraint Migration uebersprungen: %', SQLERRM;
                END $$
            """))
            logger.info("Migration: companies UNIQUE (name, city) Constraint gesetzt.")
    except Exception as e:
        logger.warning(f"Company Constraint Migration uebersprungen: {e}")

    # ── Matching v2.5: Scoring-Gewichte aktualisieren ──
    try:
        async with engine.begin() as conn:
            # Pruefen ob die neuen Dimensionen schon existieren
            result = await conn.execute(text(
                "SELECT component FROM match_v2_scoring_weights WHERE component = 'job_title_fit'"
            ))
            if not result.fetchone():
                # Bestehende Gewichte anpassen
                await conn.execute(text("UPDATE match_v2_scoring_weights SET weight = 27.0, default_weight = 27.0 WHERE component = 'skill_overlap'"))
                await conn.execute(text("UPDATE match_v2_scoring_weights SET weight = 20.0, default_weight = 20.0 WHERE component = 'seniority_fit'"))
                await conn.execute(text("UPDATE match_v2_scoring_weights SET weight = 15.0, default_weight = 15.0 WHERE component = 'embedding_sim'"))
                await conn.execute(text("UPDATE match_v2_scoring_weights SET weight = 7.0, default_weight = 7.0 WHERE component = 'career_fit'"))
                await conn.execute(text("UPDATE match_v2_scoring_weights SET weight = 5.0, default_weight = 5.0 WHERE component = 'software_match'"))
                # Location entfernen (ist jetzt Hard Filter)
                await conn.execute(text("DELETE FROM match_v2_scoring_weights WHERE component = 'location_bonus'"))
                # Neue Dimensionen
                await conn.execute(text("INSERT INTO match_v2_scoring_weights (component, weight, default_weight) VALUES ('job_title_fit', 18.0, 18.0)"))
                await conn.execute(text("INSERT INTO match_v2_scoring_weights (component, weight, default_weight) VALUES ('industry_fit', 8.0, 8.0)"))
                logger.info("Migration: Scoring-Gewichte auf v2.5 aktualisiert (7 Dimensionen, Summe=100)")
    except Exception as e:
        logger.warning(f"Scoring-Gewichte v2.5 Migration uebersprungen: {e}")

    # ── Embedding-Indexes nicht noetig (JSONB, kein pgvector) ──
    # Similarity-Suche laeuft in Python, nicht in SQL.
