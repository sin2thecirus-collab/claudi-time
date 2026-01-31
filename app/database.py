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


async def init_db() -> None:
    """Initialisiert die Datenbankverbindung und führt Migrationen aus."""
    async with engine.begin() as conn:
        # Verbindung testen
        await conn.run_sync(lambda _: None)

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
