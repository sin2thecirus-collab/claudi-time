"""Test-Konfiguration und Fixtures für das Matching-Tool."""

import os
import uuid
from datetime import date, datetime, timezone
from typing import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import Limits
from app.database import Base, get_db
from app.main import app
from app.models.alert import Alert, AlertPriority, AlertType
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus

# Test-Datenbank-URL: PostgreSQL für Geo-Tests, SQLite als Fallback
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost:5433/matching_tool_test"
)

# Prüfen ob PostgreSQL verfügbar
USE_POSTGRES = TEST_DATABASE_URL.startswith("postgresql")


@pytest.fixture
def anyio_backend():
    """Async Backend für Tests."""
    return "asyncio"


# Engine und Session für Tests
test_engine = create_async_engine(
    TEST_DATABASE_URL,
    echo=False,
)

TestSessionLocal = sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
    """Überschreibt die DB-Dependency für Tests."""
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def setup_database():
    """Erstellt die Datenbank-Tabellen vor jedem Test.

    HINWEIS: Nicht autouse=True, da Unit-Tests ohne DB laufen sollen.
    Für Integration-Tests muss dieses Fixture explizit verwendet werden.
    """
    async with test_engine.begin() as conn:
        # PostGIS Extension aktivieren (nur PostgreSQL)
        if USE_POSTGRES:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await conn.run_sync(Base.metadata.create_all)

    yield

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db_session(setup_database) -> AsyncGenerator[AsyncSession, None]:
    """Stellt eine Test-DB-Session bereit.

    Benötigt setup_database für die Datenbank-Initialisierung.
    """
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Async Test-Client mit überschriebener DB-Dependency."""
    app.dependency_overrides[get_db] = lambda: db_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ==================== GEO-KOORDINATEN ====================

# Echte Koordinaten für Geo-Tests
GEO_COORDS = {
    "hamburg_zentrum": (53.5511, 9.9937),      # Hamburg Rathaus
    "hamburg_altona": (53.5503, 9.9356),       # ~4km entfernt
    "hamburg_wandsbek": (53.5725, 10.0856),    # ~7km entfernt
    "luebeck": (53.8655, 10.6866),             # ~65km entfernt
    "bremen": (53.0793, 8.8017),               # ~95km entfernt
    "berlin": (52.5200, 13.4050),              # ~290km entfernt
}


def make_point_wkt(lat: float, lon: float) -> str:
    """Erstellt WKT POINT String für Geography."""
    return f"SRID=4326;POINT({lon} {lat})"


# ==================== FACTORIES ====================


class JobFactory:
    """Factory für Job-Testdaten."""

    @staticmethod
    def create(
        id: uuid.UUID | None = None,
        company_name: str = "Test GmbH",
        position: str = "Buchhalter/in",
        city: str = "Hamburg",
        work_location_city: str | None = None,
        postal_code: str = "20095",
        street_address: str = "Teststraße 1",
        job_text: str = "Wir suchen einen erfahrenen Buchhalter mit SAP und DATEV Kenntnissen.",
        employment_type: str = "Vollzeit",
        industry: str = "Buchhaltung",
        job_url: str = "https://example.com/job/1",
        expires_at: datetime | None = None,
        deleted_at: datetime | None = None,
        content_hash: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> Job:
        """Erstellt ein Job-Objekt für Tests."""
        job = Job(
            id=id or uuid.uuid4(),
            company_name=company_name,
            position=position,
            city=city,
            work_location_city=work_location_city,
            postal_code=postal_code,
            street_address=street_address,
            job_text=job_text,
            employment_type=employment_type,
            industry=industry,
            job_url=job_url,
            expires_at=expires_at,
            deleted_at=deleted_at,
            content_hash=content_hash or str(uuid.uuid4()),
        )
        # Geo-Location setzen wenn Koordinaten angegeben
        if latitude is not None and longitude is not None:
            job.latitude = latitude
            job.longitude = longitude
        return job


class CandidateFactory:
    """Factory für Candidate-Testdaten."""

    @staticmethod
    def create(
        id: uuid.UUID | None = None,
        crm_id: str | None = None,
        first_name: str = "Max",
        last_name: str = "Mustermann",
        email: str = "max@example.com",
        phone: str = "+49 40 12345678",
        birth_date: date | None = None,
        current_position: str = "Buchhalter",
        skills: list[str] | None = None,
        city: str = "Hamburg",
        postal_code: str = "20095",
        street_address: str = "Musterweg 5",
        hidden: bool = False,
        cv_text: str | None = None,
        work_history: dict | None = None,
        education: dict | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> Candidate:
        """Erstellt ein Candidate-Objekt für Tests."""
        candidate = Candidate(
            id=id or uuid.uuid4(),
            crm_id=crm_id or f"CRM-{uuid.uuid4().hex[:8]}",
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            birth_date=birth_date or date(1985, 5, 15),
            current_position=current_position,
            skills=skills or ["SAP", "DATEV", "Buchhaltung"],
            city=city,
            postal_code=postal_code,
            street_address=street_address,
            hidden=hidden,
            cv_text=cv_text,
            work_history=work_history or [],
            education=education or [],
        )
        # Geo-Location setzen wenn Koordinaten angegeben
        if latitude is not None and longitude is not None:
            candidate.latitude = latitude
            candidate.longitude = longitude
        return candidate


class MatchFactory:
    """Factory für Match-Testdaten."""

    @staticmethod
    def create(
        id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        candidate_id: uuid.UUID | None = None,
        distance_km: float = 5.0,
        keyword_score: float = 0.75,
        matched_keywords: list[str] | None = None,
        ai_score: float | None = None,
        ai_explanation: str | None = None,
        ai_strengths: list[str] | None = None,
        ai_weaknesses: list[str] | None = None,
        ai_checked_at: datetime | None = None,
        status: MatchStatus = MatchStatus.NEW,
        placed_at: datetime | None = None,
        placed_notes: str | None = None,
    ) -> Match:
        """Erstellt ein Match-Objekt für Tests."""
        return Match(
            id=id or uuid.uuid4(),
            job_id=job_id or uuid.uuid4(),
            candidate_id=candidate_id or uuid.uuid4(),
            distance_km=distance_km,
            keyword_score=keyword_score,
            matched_keywords=matched_keywords or ["SAP", "DATEV", "Buchhaltung"],
            ai_score=ai_score,
            ai_explanation=ai_explanation,
            ai_strengths=ai_strengths,
            ai_weaknesses=ai_weaknesses,
            ai_checked_at=ai_checked_at,
            status=status,
            placed_at=placed_at,
            placed_notes=placed_notes,
        )


class AlertFactory:
    """Factory für Alert-Testdaten."""

    @staticmethod
    def create(
        id: uuid.UUID | None = None,
        alert_type: AlertType = AlertType.EXCELLENT_MATCH,
        priority: AlertPriority = AlertPriority.HIGH,
        title: str = "Exzellenter Match gefunden",
        message: str = "Ein neuer Kandidat passt perfekt auf die Stelle.",
        job_id: uuid.UUID | None = None,
        candidate_id: uuid.UUID | None = None,
        match_id: uuid.UUID | None = None,
        is_read: bool = False,
        is_dismissed: bool = False,
    ) -> Alert:
        """Erstellt ein Alert-Objekt für Tests."""
        return Alert(
            id=id or uuid.uuid4(),
            alert_type=alert_type,
            priority=priority,
            title=title,
            message=message,
            job_id=job_id,
            candidate_id=candidate_id,
            match_id=match_id,
            is_read=is_read,
            is_dismissed=is_dismissed,
        )


# ==================== FIXTURES FÜR TESTDATEN ====================


@pytest.fixture
def sample_job() -> Job:
    """Erstellt einen Beispiel-Job."""
    return JobFactory.create()


@pytest.fixture
def sample_candidate() -> Candidate:
    """Erstellt einen Beispiel-Kandidaten."""
    return CandidateFactory.create()


@pytest.fixture
def sample_match(sample_job: Job, sample_candidate: Candidate) -> Match:
    """Erstellt ein Beispiel-Match."""
    return MatchFactory.create(
        job_id=sample_job.id,
        candidate_id=sample_candidate.id,
    )


@pytest.fixture
async def persisted_job(db_session: AsyncSession) -> Job:
    """Erstellt und speichert einen Job in der DB."""
    job = JobFactory.create()
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


@pytest.fixture
async def persisted_candidate(db_session: AsyncSession) -> Candidate:
    """Erstellt und speichert einen Kandidaten in der DB."""
    candidate = CandidateFactory.create()
    db_session.add(candidate)
    await db_session.commit()
    await db_session.refresh(candidate)
    return candidate


@pytest.fixture
async def persisted_match(
    db_session: AsyncSession,
    persisted_job: Job,
    persisted_candidate: Candidate,
) -> Match:
    """Erstellt und speichert ein Match in der DB."""
    match = MatchFactory.create(
        job_id=persisted_job.id,
        candidate_id=persisted_candidate.id,
    )
    db_session.add(match)
    await db_session.commit()
    await db_session.refresh(match)
    return match


# ==================== HILFSFUNKTIONEN ====================


def create_multiple_jobs(count: int = 5, **kwargs) -> list[Job]:
    """Erstellt mehrere Jobs für Tests."""
    jobs = []
    for i in range(count):
        job = JobFactory.create(
            company_name=f"Firma {i+1}",
            position=f"Position {i+1}",
            content_hash=str(uuid.uuid4()),
            **kwargs,
        )
        jobs.append(job)
    return jobs


def create_multiple_candidates(count: int = 5, **kwargs) -> list[Candidate]:
    """Erstellt mehrere Kandidaten für Tests."""
    candidates = []
    for i in range(count):
        candidate = CandidateFactory.create(
            first_name=f"Vorname{i+1}",
            last_name=f"Nachname{i+1}",
            crm_id=f"CRM-{uuid.uuid4().hex[:8]}",
            **kwargs,
        )
        candidates.append(candidate)
    return candidates


# ==================== KONSTANTEN FÜR TESTS ====================

# Beispiel-Job-Text für Keyword-Tests
SAMPLE_JOB_TEXT_ACCOUNTING = """
Wir suchen einen erfahrenen Finanzbuchhalter (m/w/d) für unser Team in Hamburg.

Ihre Aufgaben:
- Debitorenbuchhaltung und Kreditorenbuchhaltung
- Monatsabschluss und Jahresabschluss
- Kontenabstimmung und Mahnwesen
- Umsatzsteuervoranmeldung

Ihr Profil:
- Ausbildung als Steuerfachangestellte/r oder Bilanzbuchhalter
- Sehr gute Kenntnisse in SAP und DATEV
- Erfahrung mit HGB und IFRS
- Sicherer Umgang mit MS Office
"""

SAMPLE_JOB_TEXT_TECHNICAL = """
Elektriker (m/w/d) für Gebäudetechnik gesucht!

Ihre Aufgaben:
- Elektroinstallation in Neubauten und Bestandsgebäuden
- SPS-Programmierung und Steuerungstechnik
- Wartung von Brandmeldeanlagen (BMA)
- Installation von KNX und Bus-Systemen

Ihr Profil:
- Abgeschlossene Ausbildung als Elektroniker
- Erfahrung mit Simatic und SPS
- Führerschein Klasse B
- Bereitschaftsdienst
"""
