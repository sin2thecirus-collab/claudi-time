"""Job -> Stelle (ATSJob) Dynamic Sync Service.

Synchronisiert Daten vom importierten Job-Datensatz zum verknuepften ATSJob (Stelle).
Respektiert manuelle Ueberschreibungen: Felder die manuell oder via Call-Quali
geaendert wurden, werden NICHT ueberschrieben.

Layer-Prioritaet:
  Manual Override > Call Qualification > Job Import
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ats_job import ATSJob
from app.models.job import Job

logger = logging.getLogger(__name__)

# Felder die vom Job auf den ATSJob synchronisiert werden koennen
SYNCABLE_FIELDS = {
    "position": "title",              # Job.position -> ATSJob.title
    "job_text": "description",        # Job.job_text -> ATSJob.description
    "work_location_city": "location_city",
    "employment_type": "employment_type",
}


class JobStelleSyncService:
    """Synchronisiert Job (Import) Daten zu verknuepftem ATSJob (Stelle)."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def sync_job_to_stelle(
        self, job_id: UUID, force: bool = False
    ) -> dict:
        """Synchronisiert einen Job mit seinem verknuepften ATSJob.

        Nur Felder die auf dem ATSJob noch NULL sind oder nicht manuell
        ueberschrieben wurden, werden aktualisiert.

        Args:
            job_id: UUID des Quell-Jobs
            force: Wenn True, werden auch manuell ueberschriebene Felder aktualisiert

        Returns:
            {"synced_fields": [...], "skipped_fields": [...], "ats_job_id": str | None}
        """
        # Job laden
        job = await self.db.get(Job, job_id)
        if not job:
            return {"synced_fields": [], "skipped_fields": [], "error": "Job nicht gefunden"}

        # Verknuepften ATSJob suchen
        stmt = (
            select(ATSJob)
            .where(
                ATSJob.source_job_id == job_id,
                ATSJob.deleted_at.is_(None),
            )
            .order_by(ATSJob.created_at.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        ats_job = result.scalar_one_or_none()

        if not ats_job:
            return {"synced_fields": [], "skipped_fields": [], "error": "Kein verknuepfter ATSJob"}

        manual_overrides = ats_job.manual_overrides or {} if hasattr(ats_job, "manual_overrides") else {}
        synced = []
        skipped = []

        for job_field, ats_field in SYNCABLE_FIELDS.items():
            job_value = getattr(job, job_field, None)
            if job_value is None:
                continue

            # Manuell ueberschrieben?
            if not force and manual_overrides.get(ats_field):
                skipped.append(ats_field)
                continue

            # Nur updaten wenn ATSJob-Feld NULL ist oder force=True
            current = getattr(ats_job, ats_field, None)
            if current is None or force:
                setattr(ats_job, ats_field, job_value)
                synced.append(ats_field)
            else:
                skipped.append(ats_field)

        if synced:
            await self.db.flush()
            logger.info(
                f"Job {job_id} -> ATSJob {ats_job.id} synchronisiert: "
                f"synced={synced}, skipped={skipped}"
            )

        return {
            "synced_fields": synced,
            "skipped_fields": skipped,
            "ats_job_id": str(ats_job.id),
        }

    async def get_sync_status(self, ats_job_id: UUID) -> dict:
        """Zeigt den Sync-Status eines ATSJobs: Welche Felder stammen woher.

        Returns:
            {
                "field_sources": {
                    "title": "import" | "manual" | "call_quali" | "empty",
                    ...
                }
            }
        """
        ats_job = await self.db.get(ATSJob, ats_job_id)
        if not ats_job:
            return {"error": "ATSJob nicht gefunden"}

        manual_overrides = ats_job.manual_overrides or {} if hasattr(ats_job, "manual_overrides") else {}

        field_sources = {}
        all_fields = list(SYNCABLE_FIELDS.values()) + [
            "team_size", "erp_system", "home_office_days", "flextime", "core_hours",
            "vacation_days", "overtime_handling", "salary_min", "salary_max",
        ]

        for field in all_fields:
            value = getattr(ats_job, field, None)
            if value is None:
                field_sources[field] = "empty"
            elif manual_overrides.get(field):
                field_sources[field] = "manual"
            elif field in SYNCABLE_FIELDS.values():
                field_sources[field] = "import"
            else:
                field_sources[field] = "call_quali"

        return {
            "ats_job_id": str(ats_job_id),
            "ats_job_title": ats_job.title,
            "source_job_id": str(ats_job.source_job_id) if ats_job.source_job_id else None,
            "field_sources": field_sources,
        }
