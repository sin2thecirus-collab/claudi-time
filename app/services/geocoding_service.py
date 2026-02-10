"""Geocoding Service für das Matching-Tool.

Verwendet OpenStreetMap/Nominatim für kostenlose Geokodierung.
"""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Tuple
from uuid import UUID

import httpx
from geoalchemy2.functions import ST_MakePoint, ST_SetSRID
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Limits, settings
from app.models import Candidate, Job
from app.models.company import Company

logger = logging.getLogger(__name__)

# Nominatim API Basis-URL
NOMINATIM_BASE_URL = "https://nominatim.openstreetmap.org/search"

# User-Agent für Nominatim (erforderlich)
USER_AGENT = f"MatchingTool/1.0 ({settings.environment})"

# Rate-Limiting: 1 Request pro Sekunde (Nominatim Nutzungsbedingungen)
RATE_LIMIT_SECONDS = 1.0


@dataclass
class GeocodingResult:
    """Ergebnis einer Geokodierung."""

    latitude: float
    longitude: float
    display_name: str | None = None


@dataclass
class ProcessResult:
    """Ergebnis einer Batch-Verarbeitung."""

    total: int
    successful: int
    failed: int
    skipped: int
    errors: list[dict]


class GeocodingService:
    """
    Service für Geokodierung von Adressen.

    Features:
    - Nominatim API (OpenStreetMap)
    - Rate-Limiting (1 Request/Sekunde)
    - In-Memory Cache für Session
    - Retry bei Timeout
    """

    def __init__(self, db: AsyncSession):
        """
        Initialisiert den Geocoding-Service.

        Args:
            db: AsyncSession für Datenbankzugriff
        """
        self.db = db
        self._cache: dict[str, GeocodingResult | None] = {}
        self._last_request_time: float = 0
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-Initialisierung des HTTP-Clients."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=Limits.TIMEOUT_GEOCODING,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    async def close(self) -> None:
        """Schließt den HTTP-Client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _normalize_address(self, address: str) -> str:
        """
        Normalisiert eine Adresse für bessere Geokodierung.

        Args:
            address: Rohe Adresse

        Returns:
            Normalisierte Adresse
        """
        # Leerzeichen normalisieren
        address = re.sub(r"\s+", " ", address.strip())

        # Häufige Abkürzungen expandieren
        replacements = {
            "Str.": "Straße",
            "str.": "straße",
            "Pl.": "Platz",
            "pl.": "platz",
        }
        for old, new in replacements.items():
            address = address.replace(old, new)

        return address

    def _hash_address(self, address: str) -> str:
        """
        Berechnet einen Hash für die Adresse (für Caching).

        Args:
            address: Adresse

        Returns:
            SHA-256 Hash
        """
        normalized = self._normalize_address(address.lower())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _build_address(
        self,
        street: str | None,
        postal_code: str | None,
        city: str | None,
    ) -> str | None:
        """
        Baut eine vollständige Adresse aus Komponenten.

        Args:
            street: Straße mit Hausnummer
            postal_code: Postleitzahl
            city: Stadt

        Returns:
            Vollständige Adresse oder None
        """
        parts = []

        if street:
            parts.append(street.strip())

        if postal_code and city:
            parts.append(f"{postal_code} {city}".strip())
        elif city:
            parts.append(city.strip())
        elif postal_code:
            parts.append(postal_code.strip())

        if not parts:
            return None

        # Deutschland hinzufügen für bessere Ergebnisse
        parts.append("Deutschland")

        return ", ".join(parts)

    async def _wait_for_rate_limit(self) -> None:
        """Wartet, um Rate-Limiting einzuhalten."""
        import time

        now = time.time()
        elapsed = now - self._last_request_time

        if elapsed < RATE_LIMIT_SECONDS:
            wait_time = RATE_LIMIT_SECONDS - elapsed
            logger.debug(f"Rate-Limit: Warte {wait_time:.2f} Sekunden")
            await asyncio.sleep(wait_time)

        self._last_request_time = time.time()

    async def geocode(
        self,
        address: str,
        retry: int = 2,
    ) -> GeocodingResult | None:
        """
        Geokodiert eine Adresse.

        Args:
            address: Vollständige Adresse
            retry: Anzahl Wiederholungsversuche

        Returns:
            GeocodingResult oder None bei Fehler
        """
        if not address:
            return None

        # Cache prüfen
        address_hash = self._hash_address(address)
        if address_hash in self._cache:
            logger.debug(f"Cache-Hit für Adresse: {address[:50]}...")
            return self._cache[address_hash]

        # Rate-Limiting
        await self._wait_for_rate_limit()

        client = await self._get_client()
        normalized_address = self._normalize_address(address)

        for attempt in range(retry + 1):
            try:
                response = await client.get(
                    NOMINATIM_BASE_URL,
                    params={
                        "q": normalized_address,
                        "format": "json",
                        "limit": 1,
                        "countrycodes": "de",  # Nur Deutschland
                    },
                )
                response.raise_for_status()

                data = response.json()

                if not data:
                    logger.debug(f"Keine Ergebnisse für: {address[:50]}...")
                    self._cache[address_hash] = None
                    return None

                result = GeocodingResult(
                    latitude=float(data[0]["lat"]),
                    longitude=float(data[0]["lon"]),
                    display_name=data[0].get("display_name"),
                )

                self._cache[address_hash] = result
                logger.debug(
                    f"Geokodiert: {address[:50]}... -> "
                    f"({result.latitude}, {result.longitude})"
                )
                return result

            except httpx.TimeoutException:
                logger.warning(
                    f"Timeout bei Geokodierung (Versuch {attempt + 1}/{retry + 1}): "
                    f"{address[:50]}..."
                )
                if attempt < retry:
                    await asyncio.sleep(1)  # Kurze Pause vor Retry
                continue

            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP-Fehler bei Geokodierung: {e}")
                break

            except Exception as e:
                logger.error(f"Unerwarteter Fehler bei Geokodierung: {e}")
                break

        self._cache[address_hash] = None
        return None

    async def geocode_job(self, job: Job) -> bool:
        """
        Geokodiert einen Job.

        Strategie (Geocode-Vererbung):
        1. Wenn das Unternehmen schon Koordinaten hat → erbe sie (kein API-Aufruf)
        2. Wenn nicht → geocode die Job-Adresse und speichere auch auf dem Unternehmen
        3. So wird ein Unternehmen nur 1x geocoded — alle zukuenftigen Jobs erben

        Args:
            job: Job-Objekt

        Returns:
            True bei Erfolg
        """
        # ── Strategie 1: Koordinaten vom Unternehmen erben ──
        if job.company_id:
            company = await self.db.get(Company, job.company_id)
            if company and company.location_coords is not None:
                job.location_coords = company.location_coords
                logger.debug(
                    f"Job {job.id}: Koordinaten von Unternehmen '{company.name}' geerbt"
                )
                return True

        # ── Strategie 2: Selbst geocoden (mit Fallback) ──
        # Versuche: 1) Volle Adresse, 2) PLZ + Stadt, 3) Nur Stadt
        # Falls Nominatim die Strasse nicht kennt (Tippfehler etc.)
        address_variants = []
        full_addr = self._build_address(
            street=job.street_address,
            postal_code=job.postal_code,
            city=job.city,
        )
        if full_addr:
            address_variants.append(full_addr)

        # Fallback ohne Strasse (PLZ + Stadt)
        if job.street_address:  # Nur Fallback wenn es eine Strasse GAB
            fallback = self._build_address(
                street=None,
                postal_code=job.postal_code,
                city=job.city,
            )
            if fallback and fallback != full_addr:
                address_variants.append(fallback)

        # Fallback: PLZ + Stadt bereinigt (ohne "OT ...", "bei ...", etc.)
        if job.city:
            clean_city = re.sub(r'\s*(?:OT|bei|Ortsteil)\s+\S+.*', '', job.city).strip()
            if clean_city != job.city:
                clean_fallback = self._build_address(
                    street=None, postal_code=job.postal_code, city=clean_city,
                )
                if clean_fallback and clean_fallback not in address_variants:
                    address_variants.append(clean_fallback)

        # Fallback: Nur PLZ (praeziser als Stadt — PLZ vor Stadt!)
        if job.postal_code:
            plz_only = f"{job.postal_code}, Deutschland"
            if plz_only not in address_variants:
                address_variants.append(plz_only)

        # Fallback: Nur Stadt (ungenauer, aber besser als nichts)
        if job.city:
            city_only = self._build_address(street=None, postal_code=None, city=job.city)
            if city_only and city_only not in address_variants:
                address_variants.append(city_only)

        # Fallback: work_location_city (immer als letzter Versuch)
        if job.work_location_city:
            wlc = f"{job.work_location_city}, Deutschland"
            if wlc not in address_variants:
                address_variants.append(wlc)

        if not address_variants:
            logger.debug(f"Job {job.id}: Keine Adresse vorhanden")
            return False

        result = None
        for addr in address_variants:
            result = await self.geocode(addr)
            if result:
                if addr != address_variants[0]:
                    logger.info(f"Job {job.id}: Fallback-Geocoding mit '{addr[:50]}' erfolgreich")
                break

        if result:
            # PostGIS Point erstellen: ST_SetSRID(ST_MakePoint(lon, lat), 4326)
            point = func.ST_SetSRID(
                func.ST_MakePoint(result.longitude, result.latitude),
                4326,
            )
            job.location_coords = point

            # ── Koordinaten auch auf dem Unternehmen speichern (fuer zukuenftige Jobs) ──
            if job.company_id:
                company = await self.db.get(Company, job.company_id)
                if company and company.location_coords is None:
                    company.location_coords = func.ST_SetSRID(
                        func.ST_MakePoint(result.longitude, result.latitude),
                        4326,
                    )
                    # Auch Stadt speichern falls nicht vorhanden
                    if not company.city and job.city:
                        company.city = job.city
                    logger.info(
                        f"Unternehmen '{company.name}': Koordinaten gespeichert "
                        f"({result.latitude}, {result.longitude}) — zukuenftige Jobs erben diese"
                    )

            return True

        return False

    async def geocode_candidate(self, candidate: Candidate) -> bool:
        """
        Geokodiert einen Kandidaten (mit Fallback bei fehlerhafter Strasse).

        Args:
            candidate: Candidate-Objekt

        Returns:
            True bei Erfolg
        """
        # Versuche: 1) Volle Adresse, 2) PLZ + Stadt, 3) Nur Stadt
        address_variants = []
        full_addr = self._build_address(
            street=candidate.street_address,
            postal_code=candidate.postal_code,
            city=candidate.city,
        )
        if full_addr:
            address_variants.append(full_addr)

        # Fallback ohne Strasse
        if candidate.street_address:
            fallback = self._build_address(
                street=None,
                postal_code=candidate.postal_code,
                city=candidate.city,
            )
            if fallback and fallback != full_addr:
                address_variants.append(fallback)

        # Fallback: Stadt bereinigt (ohne "OT ...", "bei ...", etc.)
        if candidate.city:
            clean_city = re.sub(r'\s*(?:OT|bei|Ortsteil)\s+\S+.*', '', candidate.city).strip()
            if clean_city != candidate.city:
                clean_fallback = self._build_address(
                    street=None, postal_code=candidate.postal_code, city=clean_city,
                )
                if clean_fallback and clean_fallback not in address_variants:
                    address_variants.append(clean_fallback)

        # Fallback: Nur PLZ (praeziser als Stadt — PLZ vor Stadt!)
        if candidate.postal_code:
            plz_only = f"{candidate.postal_code}, Deutschland"
            if plz_only not in address_variants:
                address_variants.append(plz_only)

        # Fallback: Nur Stadt (ungenauer, aber besser als nichts)
        if candidate.city:
            city_only = self._build_address(street=None, postal_code=None, city=candidate.city)
            if city_only and city_only not in address_variants:
                address_variants.append(city_only)

        if not address_variants:
            logger.debug(f"Kandidat {candidate.id}: Keine Adresse vorhanden")
            return False

        result = None
        for i, addr in enumerate(address_variants):
            result = await self.geocode(addr)
            if result:
                if i > 0:
                    logger.info(f"Kandidat {candidate.id}: Fallback-Geocoding [{i+1}/{len(address_variants)}] mit '{addr[:60]}' erfolgreich")
                break
            else:
                logger.debug(f"Kandidat {candidate.id}: Variante [{i+1}/{len(address_variants)}] '{addr[:60]}' — kein Ergebnis")

        if result:
            candidate.address_coords = func.ST_SetSRID(
                func.ST_MakePoint(result.longitude, result.latitude),
                4326,
            )
            return True

        logger.warning(
            f"Kandidat {candidate.id}: Geocoding komplett fehlgeschlagen nach {len(address_variants)} Varianten. "
            f"Stadt={candidate.city}, PLZ={candidate.postal_code}, Strasse={candidate.street_address}"
        )
        return False

    async def inherit_geocodes_from_companies(self) -> dict:
        """
        Phase 0: Erbt Koordinaten von Unternehmen auf Jobs (kein API-Aufruf).

        Fuer alle Jobs ohne Koordinaten, deren Unternehmen bereits Koordinaten hat:
        → Kopiere Koordinaten direkt (spart Nominatim API-Aufrufe).

        Returns:
            Dict mit inherited-Zaehler
        """
        # Jobs ohne Koordinaten die ein Unternehmen MIT Koordinaten haben
        result = await self.db.execute(
            select(Job)
            .join(Company, Job.company_id == Company.id)
            .where(
                Job.location_coords.is_(None),
                Job.deleted_at.is_(None),
                Company.location_coords.isnot(None),
            )
        )
        jobs = result.scalars().all()

        inherited = 0
        for job in jobs:
            company = await self.db.get(Company, job.company_id)
            if company and company.location_coords is not None:
                job.location_coords = company.location_coords
                inherited += 1

        if inherited > 0:
            await self.db.commit()
            logger.info(
                f"Geocode-Vererbung: {inherited} Jobs haben Koordinaten "
                f"vom Unternehmen geerbt (0 API-Aufrufe)"
            )

        return {"inherited": inherited}

    async def process_pending_jobs(self) -> ProcessResult:
        """
        Geokodiert alle Jobs ohne Koordinaten.

        Phase 0: Erst Vererbung von Unternehmen (kostenlos, kein API-Aufruf)
        Phase 1: Dann restliche Jobs per Nominatim geocoden

        Batch-Commits alle 25 Eintraege damit Fortschritt sofort in DB sichtbar.

        Returns:
            ProcessResult mit Statistiken
        """
        BATCH = 25

        # Phase 0: Vererbung
        inherit_result = await self.inherit_geocodes_from_companies()
        inherited = inherit_result["inherited"]

        # Phase 1: Restliche Jobs ohne Koordinaten laden (nur mit Stadt!)
        result = await self.db.execute(
            select(Job).where(
                Job.location_coords.is_(None),
                Job.deleted_at.is_(None),
                Job.city.isnot(None),
                Job.city != "",
            )
        )
        jobs = result.scalars().all()

        total = len(jobs) + inherited
        successful = inherited
        failed = 0
        skipped = 0
        errors: list[dict] = []

        logger.info(
            f"Starte Geokodierung für {len(jobs)} Jobs "
            f"({inherited} bereits von Unternehmen geerbt)"
        )

        for i, job in enumerate(jobs):
            try:
                if await self.geocode_job(job):
                    successful += 1
                else:
                    skipped += 1
            except Exception as e:
                failed += 1
                errors.append({"job_id": str(job.id), "error": str(e)})
                logger.error(f"Fehler bei Job {job.id}: {e}")

            # Batch-Commit alle 25 Items
            if (i + 1) % BATCH == 0:
                await self.db.commit()
                logger.info(
                    f"Geocoding Jobs: {i+1}/{len(jobs)} "
                    f"({successful} OK, {skipped} skip, {failed} fail)"
                )

        await self.db.commit()

        logger.info(
            f"Job-Geokodierung abgeschlossen: "
            f"{successful} erfolgreich ({inherited} geerbt), "
            f"{skipped} übersprungen, {failed} fehlgeschlagen"
        )

        return ProcessResult(
            total=total,
            successful=successful,
            failed=failed,
            skipped=skipped,
            errors=errors[:50],
        )

    async def process_pending_candidates(self) -> ProcessResult:
        """
        Geokodiert alle Kandidaten ohne Koordinaten.

        Batch-Commits alle 25 Eintraege damit Fortschritt sofort in DB sichtbar.

        Returns:
            ProcessResult mit Statistiken
        """
        BATCH = 25

        # Kandidaten ohne Koordinaten laden (nur mit Stadt!)
        result = await self.db.execute(
            select(Candidate).where(
                Candidate.address_coords.is_(None),
                Candidate.hidden.is_(False),
                Candidate.city.isnot(None),
                Candidate.city != "",
            )
        )
        candidates = result.scalars().all()

        total = len(candidates)
        successful = 0
        failed = 0
        skipped = 0
        errors: list[dict] = []

        logger.info(f"Starte Geokodierung für {total} Kandidaten")

        for i, candidate in enumerate(candidates):
            try:
                if await self.geocode_candidate(candidate):
                    successful += 1
                else:
                    skipped += 1
            except Exception as e:
                failed += 1
                errors.append({"candidate_id": str(candidate.id), "error": str(e)})
                logger.error(f"Fehler bei Kandidat {candidate.id}: {e}")

            # Batch-Commit alle 25 Items
            if (i + 1) % BATCH == 0:
                await self.db.commit()
                logger.info(
                    f"Geocoding Kandidaten: {i+1}/{len(candidates)} "
                    f"({successful} OK, {skipped} skip, {failed} fail)"
                )

        await self.db.commit()

        logger.info(
            f"Kandidaten-Geokodierung abgeschlossen: "
            f"{successful} erfolgreich, {skipped} übersprungen, {failed} fehlgeschlagen"
        )

        return ProcessResult(
            total=total,
            successful=successful,
            failed=failed,
            skipped=skipped,
            errors=errors[:50],
        )

    async def process_all_pending(self) -> dict:
        """
        Geokodiert alle Jobs und Kandidaten ohne Koordinaten.

        Returns:
            Dictionary mit Ergebnissen für beide Typen
        """
        jobs_result = await self.process_pending_jobs()
        candidates_result = await self.process_pending_candidates()

        return {
            "jobs": {
                "total": jobs_result.total,
                "successful": jobs_result.successful,
                "failed": jobs_result.failed,
                "skipped": jobs_result.skipped,
            },
            "candidates": {
                "total": candidates_result.total,
                "successful": candidates_result.successful,
                "failed": candidates_result.failed,
                "skipped": candidates_result.skipped,
            },
        }


# ==================== Background-Task Helpers ====================


async def process_job_after_create(job_id: UUID):
    """Background-Task: Geocoding + Kategorisierung + Profiling fuer einen neuen Job.

    Reihenfolge:
      1. Geocoding        (Adresse → Koordinaten)
      2. Kategorisierung  (Job → FINANCE/ENGINEERING/...)
      3. Profiling        (nur wenn FINANCE → GPT-4o-mini Profil)

    Erstellt eigene DB-Session (nicht die Request-Session wiederverwenden!).
    Fehler blockieren NICHT die Job-Erstellung — nur Warning-Log.
    Pattern von _process_candidate_after_create uebernommen.
    """
    from app.database import async_session_maker
    from app.services.categorization_service import CategorizationService
    from app.services.profile_engine_service import ProfileEngineService

    async with async_session_maker() as db:
        try:
            result = await db.execute(
                select(Job).where(Job.id == job_id)
            )
            job = result.scalar_one_or_none()
            if not job:
                logger.error(f"Post-Create Processing: Job {job_id} nicht gefunden")
                return

            # Schritt 1: Geocoding
            logger.info(f"Post-Create Schritt 1/3: Geocoding fuer Job {job_id} ({job.position})")
            try:
                geo_service = GeocodingService(db)
                success = await geo_service.geocode_job(job)
                if success:
                    logger.info(f"Post-Create Geocoding: Job {job_id} erfolgreich geocodiert")
                else:
                    logger.warning(f"Post-Create Geocoding: Job {job_id} konnte nicht geocodiert werden (keine passende Adresse)")
            except Exception as e:
                logger.warning(f"Post-Create Geocoding fehlgeschlagen fuer Job {job_id}: {e}")

            # Schritt 2: Kategorisierung (FINANCE / ENGINEERING / ...)
            logger.info(f"Post-Create Schritt 2/3: Kategorisierung fuer Job {job_id}")
            try:
                cat_service = CategorizationService(db)
                cat_result = cat_service.categorize_job(job)
                cat_service.apply_to_job(job, cat_result)
                logger.info(f"Post-Create Kategorisierung: Job {job_id} → {cat_result.category}")
            except Exception as e:
                logger.warning(f"Post-Create Kategorisierung fehlgeschlagen fuer Job {job_id}: {e}")

            # Schritt 3: Profiling (nur fuer FINANCE-Jobs)
            if job.hotlist_category == "FINANCE":
                logger.info(f"Post-Create Schritt 3/3: Profiling fuer Job {job_id}")
                try:
                    profile_service = ProfileEngineService(db)
                    profile = await profile_service.create_job_profile(job_id)
                    if profile.success:
                        logger.info(
                            f"Post-Create Profiling: Job {job_id} erfolgreich profiliert "
                            f"(Level {profile.seniority_level}, {len(profile.required_skills)} Skills)"
                        )
                    else:
                        logger.warning(f"Post-Create Profiling: Job {job_id} uebersprungen — {profile.error}")
                except Exception as e:
                    logger.warning(f"Post-Create Profiling fehlgeschlagen fuer Job {job_id}: {e}")
            else:
                logger.info(
                    f"Post-Create Schritt 3/3: Profiling uebersprungen "
                    f"(Kategorie: {job.hotlist_category or 'NULL'}, nur FINANCE wird profiliert)"
                )

            await db.commit()
            logger.info(f"Post-Create Processing komplett fuer Job {job_id}")

        except Exception as e:
            logger.error(f"Post-Create Processing komplett fehlgeschlagen fuer Job {job_id}: {e}")
            try:
                await db.rollback()
            except Exception:
                pass
