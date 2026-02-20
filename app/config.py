"""Konfiguration für das Matching-Tool."""

import os
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
    TIMEOUT_OPENAI: int = int(os.getenv("OPENAI_TIMEOUT", "90"))
    TIMEOUT_GEOCODING: int = 10
    # Matching
    DEFAULT_RADIUS_KM: int = 25
    ACTIVE_CANDIDATE_DAYS: int = 30


class Settings(BaseSettings):
    """Umgebungsvariablen und Einstellungen."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
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

    # Sicherheit
    secret_key: str = Field(
        default="development-secret-key-change-in-production",
        description="Geheimer Schlüssel für Sessions",
    )
    cron_secret: str = Field(
        default="",
        description="Secret für Cron-Job-Authentifizierung",
    )

    # Cloudflare R2 Object Storage
    r2_access_key_id: str = Field(
        default="",
        description="Cloudflare R2 Access Key ID",
    )
    r2_secret_access_key: str = Field(
        default="",
        description="Cloudflare R2 Secret Access Key",
    )
    r2_endpoint_url: str = Field(
        default="",
        description="Cloudflare R2 S3-kompatibler Endpoint",
    )
    r2_bucket_name: str = Field(
        default="pulspoint-cvs",
        description="Cloudflare R2 Bucket-Name",
    )

    # n8n Integration
    n8n_webhook_url: str = Field(
        default="",
        description="n8n Webhook-Basis-URL (z.B. https://n8n-production-aa9c.up.railway.app)",
    )
    n8n_api_token: str = Field(
        default="",
        description="API-Token fuer n8n-Inbound-Webhooks",
    )

    # Authentifizierung
    admin_email: str = Field(
        default="",
        description="E-Mail-Adresse des Admin-Users",
    )
    admin_password: str = Field(
        default="",
        description="Passwort des Admin-Users (wird gehasht gespeichert)",
    )
    api_access_key: str = Field(
        default="",
        description="API-Key fuer programmatischen Zugriff (X-API-Key Header)",
    )
    session_expire_hours: int = Field(
        default=24,
        description="Session-Gueltigkeitsdauer in Stunden",
    )

    # Microsoft Graph API (Email-Versand via Outlook/M365)
    microsoft_tenant_id: str = Field(
        default="",
        description="Azure AD Tenant ID fuer Microsoft Graph API",
    )
    microsoft_client_id: str = Field(
        default="",
        description="Azure AD App Registration Client ID",
    )
    microsoft_client_secret: str = Field(
        default="",
        description="Azure AD App Registration Client Secret",
    )
    microsoft_sender_email: str = Field(
        default="",
        description="Absender-Email fuer automatische Emails (z.B. hamdard@sincirus.com)",
    )

    # Google Maps
    google_maps_api_key: str = Field(
        default="",
        description="Google Maps API-Schlüssel für Distance Matrix API (Fahrzeit)",
    )

    # Telegram Bot
    telegram_bot_token: str = Field(
        default="",
        description="Telegram Bot Token (@Sincirusbot)",
    )
    telegram_chat_id: str = Field(
        default="",
        description="Telegram Chat ID (Milad)",
    )
    telegram_webhook_secret: str = Field(
        default="",
        description="Secret fuer Telegram Webhook Verifizierung",
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
