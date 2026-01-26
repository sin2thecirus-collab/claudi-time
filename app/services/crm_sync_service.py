"""CRM Sync Service - Synchronisiert Kandidaten aus Recruit CRM.

Dieser Service:
- Führt Initial-Sync (alle Kandidaten) durch
- Führt Incremental-Sync (nur geänderte) durch
- Triggert CV-Parsing für neue Kandidaten
- Trackt den Sync-Status
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    """

    def __init__(self, db: AsyncSession, crm_client: RecruitCRMClient | None = None):
        """Initialisiert den Sync-Service.

        Args:
            db: Datenbank-Session
            crm_client: Optional CRM-Client (wird sonst erstellt)
        """
        self.db = db
        self._crm_client = crm_client
        self._owns_client = crm_client is None

    async def _get_client(self) -> RecruitCRMClient:
        """Gibt den CRM-Client zurück."""
        if self._crm_client is None:
            self._crm_client = RecruitCRMClient()
        return self._crm_client

    async def close(self) -> None:
        """Schließt Ressourcen."""
        if self._owns_client and self._crm_client:
            await self._crm_client.close()

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

    async def sync_all(self) -> SyncResult:
        """Führt einen vollständigen Sync durch.

        Entscheidet automatisch zwischen Initial- und Incremental-Sync.

        Returns:
            SyncResult mit Statistiken
        """
        has_existing = await self.has_candidates()
        last_sync = await self.get_last_sync_time()

        if not has_existing:
            logger.info("Kein Kandidat vorhanden, starte Initial-Sync")
            return await self.initial_sync()
        elif last_sync:
            logger.info(f"Letzter Sync: {last_sync}, starte Incremental-Sync")
            return await self.incremental_sync(since=last_sync)
        else:
            logger.info("Kein Sync-Zeitpunkt bekannt, starte Initial-Sync")
            return await self.initial_sync()

    async def initial_sync(self) -> SyncResult:
        """Führt einen Initial-Sync durch (alle Kandidaten).

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

    async def incremental_sync(self, since: datetime | None = None) -> SyncResult:
        """Führt einen Incremental-Sync durch (nur Änderungen).

        Args:
            since: Zeitpunkt, ab dem geänderte Kandidaten abgerufen werden

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

    async def sync_single_candidate(self, crm_id: str) -> Candidate:
        """Synchronisiert einen einzelnen Kandidaten.

        Args:
            crm_id: CRM-ID des Kandidaten

        Returns:
            Aktualisierter Kandidat

        Raises:
            CRMNotFoundError: Wenn Kandidat nicht im CRM gefunden
        """
        client = await self._get_client()
        crm_data = await client.get_candidate(crm_id)

        candidate, _ = await self._upsert_candidate(crm_data)
        await self.db.commit()

        return candidate

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
            # Update
            for key, value in mapped_data.items():
                if key != "crm_id" and value is not None:
                    setattr(existing, key, value)
            existing.crm_synced_at = now
            existing.updated_at = now

            return existing, False
        else:
            # Create
            candidate = Candidate(
                crm_id=crm_id,
                first_name=mapped_data.get("first_name"),
                last_name=mapped_data.get("last_name"),
                email=mapped_data.get("email"),
                phone=mapped_data.get("phone"),
                current_position=mapped_data.get("current_position"),
                current_company=mapped_data.get("current_company"),
                skills=mapped_data.get("skills"),
                street_address=mapped_data.get("street_address"),
                postal_code=mapped_data.get("postal_code"),
                city=mapped_data.get("city"),
                cv_url=mapped_data.get("cv_url"),
                crm_synced_at=now,
            )
            self.db.add(candidate)
            await self.db.flush()  # Um die ID zu erhalten

            return candidate, True

    async def __aenter__(self) -> "CRMSyncService":
        """Context-Manager Entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context-Manager Exit."""
        await self.close()
