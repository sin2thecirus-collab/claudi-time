"""Datenbank-Konfiguration und Session-Management."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

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
    """Initialisiert die Datenbankverbindung (für Startup-Check)."""
    async with engine.begin() as conn:
        # Verbindung testen
        await conn.run_sync(lambda _: None)
