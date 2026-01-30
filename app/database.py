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
    migrations = [
        ("languages", "JSONB"),
        ("it_skills", "VARCHAR[]"),
        ("further_education", "JSONB"),
        ("cv_parse_failed", "BOOLEAN DEFAULT FALSE"),
        ("deleted_at", "TIMESTAMPTZ"),
        ("manual_overrides", "JSONB"),
    ]
    for col_name, col_type in migrations:
        try:
            async with engine.begin() as conn:
                # Lock-Timeout auf 5 Sekunden setzen
                await conn.execute(text("SET lock_timeout = '5s'"))
                result = await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'candidates' AND column_name = :col"
                    ),
                    {"col": col_name},
                )
                if not result.fetchone():
                    logger.info(f"Migration: Füge '{col_name}' Spalte hinzu...")
                    await conn.execute(
                        text(f"ALTER TABLE candidates ADD COLUMN {col_name} {col_type}")
                    )
                    logger.info(f"Migration: '{col_name}' Spalte hinzugefügt.")
        except Exception as e:
            logger.warning(f"Migration für '{col_name}' übersprungen: {e}")
