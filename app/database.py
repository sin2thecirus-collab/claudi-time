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
    async with engine.begin() as conn:
        # Prüfe ob languages-Spalte existiert
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'candidates' AND column_name = 'languages'"
            )
        )
        if not result.fetchone():
            logger.info("Migration: Füge 'languages' Spalte hinzu...")
            await conn.execute(text("ALTER TABLE candidates ADD COLUMN languages JSONB"))
            logger.info("Migration: 'languages' Spalte hinzugefügt.")

        # Prüfe ob it_skills-Spalte existiert
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'candidates' AND column_name = 'it_skills'"
            )
        )
        if not result.fetchone():
            logger.info("Migration: Füge 'it_skills' Spalte hinzu...")
            await conn.execute(text("ALTER TABLE candidates ADD COLUMN it_skills VARCHAR[]"))
            logger.info("Migration: 'it_skills' Spalte hinzugefügt.")

        # Prüfe ob further_education-Spalte existiert
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'candidates' AND column_name = 'further_education'"
            )
        )
        if not result.fetchone():
            logger.info("Migration: Füge 'further_education' Spalte hinzu...")
            await conn.execute(text("ALTER TABLE candidates ADD COLUMN further_education JSONB"))
            logger.info("Migration: 'further_education' Spalte hinzugefügt.")
