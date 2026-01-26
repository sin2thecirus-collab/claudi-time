"""Konfiguration für das Matching-Tool."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Limits:
    """Konstanten für Limits im System."""

    # CSV-Import
    CSV_MAX_FILE_SIZE_MB: int = 50
    CSV_MAX_ROWS: int = 10_000

    # Batch-Operationen
    BATCH_DELETE_MAX: int = 100
    BATCH_HIDE_MAX: int = 100

    # KI-Check
    AI_CHECK_MAX_CANDIDATES: int = 50

    # Filter
    FILTER_MULTI_SELECT_MAX: int = 20
    FILTER_PRESETS_MAX: int = 20
    PRIORITY_CITIES_MAX: int = 10

    # Suche
    SEARCH_MIN_LENGTH: int = 2
    SEARCH_MAX_LENGTH: int = 100

    # Pagination
    PAGE_SIZE_DEFAULT: int = 20
    PAGE_SIZE_MAX: int = 100

    # Timeouts (in Sekunden)
    TIMEOUT_OPENAI: int = 30
    TIMEOUT_GEOCODING: int = 10
    TIMEOUT_CRM: int = 15

    # Matching
    DEFAULT_RADIUS_KM: int = 25
    ACTIVE_CANDIDATE_DAYS: int = 30


class Settings(BaseSettings):
    """Umgebungsvariablen und Einstellungen."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Datenbank
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/matching_tool",
        description="PostgreSQL-Verbindungs-URL",
    )

    # OpenAI
    openai_api_key: str = Field(
        default="",
        description="OpenAI API-Schlüssel",
    )

    # Recruit CRM
    recruit_crm_api_key: str = Field(
        default="",
        description="Recruit CRM API-Schlüssel",
    )
    recruit_crm_base_url: str = Field(
        default="https://api.recruitcrm.io/v1",
        description="Recruit CRM API-Basis-URL",
    )

    # Sicherheit
    secret_key: str = Field(
        default="development-secret-key-change-in-production",
        description="Geheimer Schlüssel für Sessions",
    )
    cron_secret: str = Field(
        default="",
        description="Secret für Cron-Job-Authentifizierung",
    )

    # Umgebung
    environment: str = Field(
        default="development",
        description="Umgebung (development, staging, production)",
    )

    @property
    def is_production(self) -> bool:
        """Prüft, ob die Anwendung in Produktion läuft."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Prüft, ob die Anwendung in Entwicklung läuft."""
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """Gibt die gecachten Einstellungen zurück."""
    return Settings()


# Globale Instanzen
settings = get_settings()
limits = Limits()
