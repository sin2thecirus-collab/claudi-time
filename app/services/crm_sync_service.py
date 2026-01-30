"""CRM Sync Service - Synchronisiert Kandidaten aus Recruit CRM.

Dieser Service:
- Führt Initial-Sync (alle Kandidaten) durch
- Führt Incremental-Sync (nur geänderte) durch
- Triggert CV-Parsing für neue/aktualisierte Kandidaten (OpenAI)
- Extrahiert Position, Werdegang und Adresse aus CVs
- Trackt den Sync-Status
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job_run import JobRun
from app.schemas.candidate import CandidateCreate
from app.services.crm_client import (
    CRMError,
    CRMRateLimitError,
    RecruitCRMClient,
)
from app.services.cv_parser_service import CVParserService

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Ergebnis einer Sync-Operation."""

    total_processed: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[dict] = field(default_factory=list)
    duration_seconds: float = 0.0
    is_initial_sync: bool = False
    new_candidate_ids: list[UUID] = field(default_factory=list)
    # CV-Parsing Statistiken
    cvs_parsed: int = 0
    cvs_failed: int = 0

    @property
    def success_rate(self) -> float:
        """Berechnet die Erfolgsrate."""
        if self.total_processed == 0:
            return 100.0
        return ((self.created + self.updated + self.skipped) / self.total_processed) * 100


class CRMSyncService:
    """Service für die Synchronisation mit Recruit CRM.

    Unterstützt:
    - Initial-Sync: Alle Kandidaten importieren
    - Incremental-Sync: Nur Änderungen seit letztem Sync
    - Einzelner Kandidat: Manueller Sync
    - Automatisches CV-Parsing bei Sync (OpenAI)
    """

    def __init__(
        self,
        db: AsyncSession,
        crm_client: RecruitCRMClient | None = None,
        enable_cv_parsing: bool = True,
    ):
        """Initialisiert den Sync-Service.

        Args:
            db: Datenbank-Session
            crm_client: Optional CRM-Client (wird sonst erstellt)
            enable_cv_parsing: CV-Parsing mit OpenAI aktivieren (Standard: True)
        """
        self.db = db
        self._crm_client = crm_client
        self._owns_client = crm_client is None
        self._enable_cv_parsing = enable_cv_parsing
        self._cv_parser: CVParserService | None = None

    async def _get_client(self) -> RecruitCRMClient:
        """Gibt den CRM-Client zurück."""
        if self._crm_client is None:
            self._crm_client = RecruitCRMClient()
        return self._crm_client

    async def _get_cv_parser(self) -> CVParserService:
        """Gibt den CV-Parser zurück."""
        if self._cv_parser is None:
            self._cv_parser = CVParserService(self.db)
        return self._cv_parser

    async def close(self) -> None:
        """Schließt Ressourcen."""
        if self._owns_client and self._crm_client:
            await self._crm_client.close()
        if self._cv_parser:
            await self._cv_parser.close()

    async def get_last_sync_time(self) -> datetime | None:
        """Ermittelt den Zeitpunkt des letzten erfolgreichen Syncs.

        Returns:
            Datetime des letzten Syncs oder None
        """
        result = await self.db.execute(
            select(JobRun)
            .where(JobRun.job_type == "crm_sync")
            .where(JobRun.status == "completed")
            .order_by(JobRun.completed_at.desc())
            .limit(1)
        )
        job_run = result.scalar_one_or_none()

        if job_run and job_run.completed_at:
            return job_run.completed_at

        # Alternativ: Letzter crm_synced_at aus Kandidaten
        result = await self.db.execute(
            select(func.max(Candidate.crm_synced_at))
        )
        return result.scalar_one_or_none()

    async def has_candidates(self) -> bool:
        """Prüft, ob bereits Kandidaten in der DB sind."""
        result = await self.db.execute(
            select(func.count(Candidate.id))
        )
        count = result.scalar_one()
        return count > 0

    async def sync_all(
        self,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> SyncResult:
        """Führt einen vollständigen Sync durch.

        Entscheidet automatisch zwischen Initial- und Incremental-Sync.

        Args:
            progress_callback: Optional callback(processed, total) für Fortschrittsupdates

        Returns:
            SyncResult mit Statistiken
        """
        has_existing = await self.has_candidates()
        last_sync = await self.get_last_sync_time()

        if not has_existing:
            logger.info("Kein Kandidat vorhanden, starte Initial-Sync")
            return await self.initial_sync(progress_callback=progress_callback)
        elif last_sync:
            logger.info(f"Letzter Sync: {last_sync}, starte Incremental-Sync")
            return await self.incremental_sync(since=last_sync, progress_callback=progress_callback)
        else:
            logger.info("Kein Sync-Zeitpunkt bekannt, starte Initial-Sync")
            return await self.initial_sync(progress_callback=progress_callback)

    async def initial_sync(
        self,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> SyncResult:
        """Führt einen Initial-Sync durch (alle Kandidaten).

        Args:
            progress_callback: Optional callback(processed, total) für Fortschrittsupdates

        Returns:
            SyncResult mit Statistiken
        """
        start_time = datetime.now(timezone.utc)
        result = SyncResult(is_initial_sync=True)

        try:
            client = await self._get_client()

            async for page_num, candidates, total in client.get_all_candidates_paginated(
                per_page=100
            ):
                logger.info(f"Verarbeite Seite {page_num}, {len(candidates)} Kandidaten (Gesamt: {total})")

                for crm_data in candidates:
                    try:
                        candidate, created = await self._upsert_candidate(crm_data)
                        result.total_processed += 1

                        if created:
                            result.created += 1
                            result.new_candidate_ids.append(candidate.id)
                        else:
                            result.updated += 1

                    except Exception as e:
                        result.failed += 1
                        result.errors.append({
                            "crm_id": crm_data.get("id"),
                            "error": str(e),
                        })
                        logger.error(f"Fehler bei Kandidat {crm_data.get('id')}: {e}")

                        if len(result.errors) >= 100:
                            logger.warning("Maximale Fehleranzahl erreicht")
                            break

                # Commit nach jeder Seite
                await self.db.commit()

                # Fortschritts-Callback aufrufen
                if progress_callback:
                    try:
                        await progress_callback(result.total_processed, total)
                    except Exception as e:
                        logger.warning(f"Progress callback fehlgeschlagen: {e}")

        except CRMRateLimitError as e:
            logger.error(f"Rate-Limit erreicht: {e}")
            result.errors.append({"error": str(e), "type": "rate_limit"})
        except CRMError as e:
            logger.error(f"CRM-Fehler: {e}")
            result.errors.append({"error": str(e), "type": "crm_error"})
        except Exception as e:
            logger.exception(f"Unerwarteter Fehler beim Sync: {e}")
            result.errors.append({"error": str(e), "type": "unexpected"})

        result.duration_seconds = (datetime.now(timezone.utc) - start_time).total_seconds()

        logger.info(
            f"Initial-Sync abgeschlossen: {result.created} erstellt, "
            f"{result.updated} aktualisiert, {result.failed} fehlgeschlagen "
            f"in {result.duration_seconds:.1f}s"
        )

        return result

    async def incremental_sync(
        self,
        since: datetime | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> SyncResult:
        """Führt einen Incremental-Sync durch (nur Änderungen).

        Args:
            since: Zeitpunkt, ab dem geänderte Kandidaten abgerufen werden
            progress_callback: Optional callback(processed, total) für Fortschrittsupdates

        Returns:
            SyncResult mit Statistiken
        """
        start_time = datetime.now(timezone.utc)
        result = SyncResult(is_initial_sync=False)

        if since is None:
            since = await self.get_last_sync_time()
            if since is None:
                logger.warning("Kein Sync-Zeitpunkt, führe Initial-Sync durch")
                return await self.initial_sync()

        try:
            client = await self._get_client()

            async for page_num, candidates, total in client.get_all_candidates_paginated(
                per_page=100,
                updated_since=since,
            ):
                logger.info(
                    f"Incremental Seite {page_num}, {len(candidates)} geänderte Kandidaten"
                )

                if not candidates:
                    logger.info("Keine geänderten Kandidaten gefunden")
                    break

                for crm_data in candidates:
                    try:
                        candidate, created = await self._upsert_candidate(crm_data)
                        result.total_processed += 1

                        if created:
                            result.created += 1
                            result.new_candidate_ids.append(candidate.id)
                        else:
                            result.updated += 1

                    except Exception as e:
                        result.failed += 1
                        result.errors.append({
                            "crm_id": crm_data.get("id"),
                            "error": str(e),
                        })
                        logger.error(f"Fehler bei Kandidat {crm_data.get('id')}: {e}")

                await self.db.commit()

                # Fortschritts-Callback aufrufen
                if progress_callback:
                    try:
                        await progress_callback(result.total_processed, total)
                    except Exception as e:
                        logger.warning(f"Progress callback fehlgeschlagen: {e}")

        except CRMRateLimitError as e:
            logger.error(f"Rate-Limit erreicht: {e}")
            result.errors.append({"error": str(e), "type": "rate_limit"})
        except CRMError as e:
            logger.error(f"CRM-Fehler: {e}")
            result.errors.append({"error": str(e), "type": "crm_error"})
        except Exception as e:
            logger.exception(f"Unerwarteter Fehler beim Sync: {e}")
            result.errors.append({"error": str(e), "type": "unexpected"})

        result.duration_seconds = (datetime.now(timezone.utc) - start_time).total_seconds()

        logger.info(
            f"Incremental-Sync abgeschlossen: {result.created} neu, "
            f"{result.updated} aktualisiert in {result.duration_seconds:.1f}s"
        )

        return result

    async def sync_single_candidate(self, crm_id: str, parse_cv: bool = True) -> Candidate:
        """Synchronisiert einen einzelnen Kandidaten.

        Args:
            crm_id: CRM-ID des Kandidaten
            parse_cv: CV mit OpenAI parsen wenn Daten fehlen (Standard: True)

        Returns:
            Aktualisierter Kandidat

        Raises:
            CRMNotFoundError: Wenn Kandidat nicht im CRM gefunden
        """
        client = await self._get_client()
        crm_data = await client.get_candidate(crm_id)

        candidate, created = await self._upsert_candidate(crm_data)
        await self.db.commit()

        # CV-Parsing wenn aktiviert und Daten fehlen
        if parse_cv and self._enable_cv_parsing:
            if self._needs_cv_parsing(candidate):
                await self._parse_candidate_cv(candidate)
                await self.db.commit()

        return candidate

    def _needs_cv_parsing(self, candidate: Candidate) -> bool:
        """Prüft ob CV-Parsing nötig ist.

        CV-Parsing wird durchgeführt wenn:
        - Kandidat hat eine CV-URL (Resume im CRM)
        - CV wurde noch nicht geparst (cv_parsed_at ist NULL)

        Aus dem CV werden extrahiert:
        - Aktuelle Position (Beruflicher Titel)
        - VOLLSTÄNDIGER Werdegang (alle beruflichen Stationen)
        - Ausbildung & Qualifikationen
        """
        # Kein CV vorhanden?
        if not candidate.cv_url:
            return False

        # Bereits geparst?
        if candidate.cv_parsed_at:
            return False

        # CV vorhanden und noch nicht geparst -> Parsen!
        return True

    async def _parse_candidate_cv(self, candidate: Candidate) -> bool:
        """Parst das CV eines Kandidaten mit OpenAI.

        Extrahiert aus dem Lebenslauf (Resume PDF):
        - Aktuelle Position (Beruflicher Titel)
        - VOLLSTÄNDIGER beruflicher Werdegang (alle Stationen mit Zeiten und Tätigkeiten)
        - Ausbildung & Qualifikationen (Schule, Ausbildung, Studium, Weiterbildungen)
        - Skills/Kenntnisse

        Args:
            candidate: Kandidat mit cv_url

        Returns:
            True wenn erfolgreich geparst
        """
        if not candidate.cv_url:
            return False

        try:
            cv_parser = await self._get_cv_parser()
            candidate_updated, parse_result = await cv_parser.parse_candidate_cv(candidate.id)

            if parse_result.success:
                logger.info(
                    f"CV erfolgreich geparst für Kandidat {candidate.crm_id}: "
                    f"Position={candidate_updated.current_position}, "
                    f"Work History={len(candidate_updated.work_history or [])} Einträge"
                )
                return True
            else:
                logger.warning(
                    f"CV-Parsing fehlgeschlagen für {candidate.crm_id}: {parse_result.error}"
                )
                return False

        except Exception as e:
            logger.error(f"CV-Parsing Fehler für {candidate.crm_id}: {e}")
            return False

    async def sync_with_cv_parsing(
        self,
        full_sync: bool = True,
        progress_callback: Callable[[int, int], None] | None = None,
        cv_parsing_callback: Callable[[int, int], None] | None = None,
    ) -> SyncResult:
        """Führt Sync durch und parst CVs für alle Kandidaten mit Resume.

        Dies ist ein zwei-Phasen-Prozess:
        1. CRM-Sync: Kandidaten aus CRM importieren (Basisdaten + Adresse)
        2. CV-Parsing: Für alle Kandidaten mit CV (Resume PDF) via OpenAI:
           - Aktuelle Position (Beruflicher Titel)
           - VOLLSTÄNDIGER Werdegang (alle beruflichen Stationen)
           - Ausbildung & Qualifikationen

        Args:
            full_sync: True für ALLE Kandidaten, False für nur Änderungen
            progress_callback: Callback für Sync-Fortschritt
            cv_parsing_callback: Callback für CV-Parsing-Fortschritt

        Returns:
            SyncResult mit Sync- und CV-Parsing-Statistiken
        """
        # Phase 1: CRM-Sync (Basisdaten + Adresse aus CRM)
        if full_sync:
            result = await self.initial_sync(progress_callback=progress_callback)
        else:
            result = await self.sync_all(progress_callback=progress_callback)

        # Phase 2: CV-Parsing für alle neuen Kandidaten mit Resume
        if self._enable_cv_parsing and result.new_candidate_ids:
            logger.info(f"Starte CV-Parsing für {len(result.new_candidate_ids)} neue Kandidaten")

            candidates_to_parse = []
            for candidate_id in result.new_candidate_ids:
                query_result = await self.db.execute(
                    select(Candidate).where(Candidate.id == candidate_id)
                )
                candidate = query_result.scalar_one_or_none()
                if candidate and self._needs_cv_parsing(candidate):
                    candidates_to_parse.append(candidate)

            total_to_parse = len(candidates_to_parse)
            logger.info(f"{total_to_parse} Kandidaten haben CV/Resume zum Parsen")

            for i, candidate in enumerate(candidates_to_parse):
                try:
                    success = await self._parse_candidate_cv(candidate)
                    if success:
                        result.cvs_parsed += 1
                    else:
                        result.cvs_failed += 1
                except Exception as e:
                    result.cvs_failed += 1
                    result.errors.append({
                        "candidate_id": str(candidate.id),
                        "crm_id": candidate.crm_id,
                        "error": f"CV-Parsing: {str(e)}",
                        "type": "cv_parsing"
                    })

                # CV-Parsing Progress Callback
                if cv_parsing_callback:
                    try:
                        await cv_parsing_callback(i + 1, total_to_parse)
                    except Exception as e:
                        logger.warning(f"CV parsing progress callback fehlgeschlagen: {e}")

                # Commit nach jedem CV
                await self.db.commit()

            logger.info(
                f"CV-Parsing abgeschlossen: {result.cvs_parsed} erfolgreich, "
                f"{result.cvs_failed} fehlgeschlagen"
            )

        return result

    async def _upsert_candidate(self, crm_data: dict) -> tuple[Candidate, bool]:
        """Erstellt oder aktualisiert einen Kandidaten.

        Args:
            crm_data: Rohdaten aus der CRM API

        Returns:
            Tuple (Candidate, created: bool)
        """
        client = await self._get_client()
        mapped_data = client.map_to_candidate_data(crm_data)
        crm_id = mapped_data.get("crm_id")

        if not crm_id:
            raise ValueError("Kandidat hat keine CRM-ID")

        # Prüfe ob Kandidat existiert
        result = await self.db.execute(
            select(Candidate).where(Candidate.crm_id == crm_id)
        )
        existing = result.scalar_one_or_none()

        now = datetime.now(timezone.utc)

        if existing:
            # Gelöschter Kandidat → komplett überspringen
            if existing.deleted_at is not None:
                logger.debug(f"Kandidat {crm_id} ist gelöscht, überspringe Sync")
                return existing, False

            # Update - alle gemappten Felder übernehmen
            for key, value in mapped_data.items():
                if key != "crm_id" and value is not None:
                    setattr(existing, key, value)
            existing.crm_synced_at = now
            existing.updated_at = now

            logger.debug(
                f"Kandidat {crm_id} aktualisiert: "
                f"Position={mapped_data.get('current_position')}, "
                f"Work History={len(mapped_data.get('work_history') or [])} Einträge"
            )

            return existing, False
        else:
            # Create - alle Felder inkl. work_history und education
            candidate = Candidate(
                crm_id=crm_id,
                first_name=mapped_data.get("first_name"),
                last_name=mapped_data.get("last_name"),
                email=mapped_data.get("email"),
                phone=mapped_data.get("phone"),
                current_position=mapped_data.get("current_position"),
                current_company=mapped_data.get("current_company"),
                skills=mapped_data.get("skills"),
                work_history=mapped_data.get("work_history"),
                education=mapped_data.get("education"),
                street_address=mapped_data.get("street_address"),
                postal_code=mapped_data.get("postal_code"),
                city=mapped_data.get("city"),
                cv_url=mapped_data.get("cv_url"),
                crm_synced_at=now,
            )
            self.db.add(candidate)
            await self.db.flush()  # Um die ID zu erhalten

            logger.debug(
                f"Kandidat {crm_id} erstellt: "
                f"Position={mapped_data.get('current_position')}, "
                f"Work History={len(mapped_data.get('work_history') or [])} Einträge, "
                f"Education={len(mapped_data.get('education') or [])} Einträge"
            )

            return candidate, True

    async def __aenter__(self) -> "CRMSyncService":
        """Context-Manager Entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context-Manager Exit."""
        await self.close()
