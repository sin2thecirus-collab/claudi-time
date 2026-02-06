"""ATS Pipeline Service fuer das Matching-Tool."""

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ats_activity import ATSActivity, ActivityType
from app.models.ats_pipeline import (
    ATSPipelineEntry,
    PIPELINE_STAGE_LABELS,
    PIPELINE_STAGE_ORDER,
    PipelineStage,
)

logger = logging.getLogger(__name__)


class ATSPipelineService:
    """Service fuer die ATS-Kanban-Pipeline."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Kandidat Management ──────────────────────────

    async def add_candidate(
        self, ats_job_id: UUID, candidate_id: UUID,
        stage: str | None = None, notes: str | None = None
    ) -> ATSPipelineEntry:
        """Fuegt einen Kandidaten zur Pipeline hinzu."""
        # Stage bestimmen (Default: SENT weil MATCHED nicht in Uebersicht sichtbar)
        stage_enum = PipelineStage(stage) if stage else PipelineStage.SENT

        # Naechste sort_order ermitteln
        max_order_result = await self.db.execute(
            select(func.max(ATSPipelineEntry.sort_order)).where(
                ATSPipelineEntry.ats_job_id == ats_job_id,
                ATSPipelineEntry.stage == stage_enum,
            )
        )
        max_order = max_order_result.scalar() or 0

        entry = ATSPipelineEntry(
            ats_job_id=ats_job_id,
            candidate_id=candidate_id,
            stage=stage_enum,
            sort_order=max_order + 1,
            notes=notes,
        )
        self.db.add(entry)
        await self.db.flush()

        # Activity loggen
        activity = ATSActivity(
            activity_type=ActivityType.CANDIDATE_ADDED,
            description="Kandidat zur Pipeline hinzugefuegt",
            ats_job_id=ats_job_id,
            pipeline_entry_id=entry.id,
            candidate_id=candidate_id,
        )
        self.db.add(activity)
        await self.db.flush()

        logger.info(f"Pipeline: Kandidat {candidate_id} zu Job {ats_job_id} hinzugefuegt")
        return entry

    async def remove_candidate(self, entry_id: UUID, reason: str | None = None) -> bool:
        """Entfernt einen Kandidaten aus der Pipeline."""
        entry = await self.db.get(ATSPipelineEntry, entry_id)
        if not entry:
            return False

        # Activity loggen vor Loeschung
        activity = ATSActivity(
            activity_type=ActivityType.CANDIDATE_REMOVED,
            description=f"Kandidat entfernt" + (f": {reason}" if reason else ""),
            ats_job_id=entry.ats_job_id,
            candidate_id=entry.candidate_id,
            metadata_json={"reason": reason, "from_stage": entry.stage.value},
        )
        self.db.add(activity)

        await self.db.delete(entry)
        await self.db.flush()
        logger.info(f"Pipeline: Entry {entry_id} entfernt")
        return True

    # ── Stage Management ─────────────────────────────

    async def move_stage(self, entry_id: UUID, new_stage: str) -> ATSPipelineEntry | None:
        """Verschiebt einen Kandidaten in eine neue Stage."""
        entry = await self.db.get(ATSPipelineEntry, entry_id)
        if not entry:
            return None

        old_stage = entry.stage.value
        new_stage_enum = PipelineStage(new_stage)

        entry.stage = new_stage_enum
        entry.stage_changed_at = datetime.now(timezone.utc)

        # Bei Rejection: kein neuer sort_order noetig
        if new_stage_enum == PipelineStage.REJECTED:
            pass
        else:
            # Naechste sort_order in neuer Stage
            max_order_result = await self.db.execute(
                select(func.max(ATSPipelineEntry.sort_order)).where(
                    ATSPipelineEntry.ats_job_id == entry.ats_job_id,
                    ATSPipelineEntry.stage == new_stage_enum,
                    ATSPipelineEntry.id != entry.id,
                )
            )
            max_order = max_order_result.scalar() or 0
            entry.sort_order = max_order + 1

        await self.db.flush()

        # Activity loggen
        from_label = PIPELINE_STAGE_LABELS.get(PipelineStage(old_stage), old_stage)
        to_label = PIPELINE_STAGE_LABELS.get(new_stage_enum, new_stage)
        activity = ATSActivity(
            activity_type=ActivityType.STAGE_CHANGED,
            description=f"Stage von '{from_label}' auf '{to_label}' geaendert",
            ats_job_id=entry.ats_job_id,
            pipeline_entry_id=entry.id,
            candidate_id=entry.candidate_id,
            metadata_json={"from_stage": old_stage, "to_stage": new_stage},
        )
        self.db.add(activity)
        await self.db.flush()

        logger.info(f"Pipeline: Entry {entry_id} von {old_stage} -> {new_stage}")
        return entry

    async def reorder(self, entry_id: UUID, new_sort_order: int) -> ATSPipelineEntry | None:
        """Aendert die Reihenfolge eines Eintrags innerhalb einer Stage."""
        entry = await self.db.get(ATSPipelineEntry, entry_id)
        if not entry:
            return None
        entry.sort_order = new_sort_order
        await self.db.flush()
        return entry

    async def bulk_move(self, entry_ids: list[UUID], new_stage: str) -> list[ATSPipelineEntry]:
        """Verschiebt mehrere Kandidaten in eine neue Stage."""
        entries = []
        for entry_id in entry_ids:
            entry = await self.move_stage(entry_id, new_stage)
            if entry:
                entries.append(entry)
        return entries

    # ── Pipeline laden ───────────────────────────────

    async def get_pipeline(self, ats_job_id: UUID) -> dict:
        """Laedt die komplette Pipeline fuer eine Stelle (Kanban-Daten)."""
        result = await self.db.execute(
            select(ATSPipelineEntry)
            .options(selectinload(ATSPipelineEntry.candidate))
            .where(ATSPipelineEntry.ats_job_id == ats_job_id)
            .order_by(ATSPipelineEntry.sort_order.asc())
        )
        entries = result.scalars().all()

        # Nach Stages gruppieren
        pipeline = {}
        for stage in PIPELINE_STAGE_ORDER:
            pipeline[stage.value] = {
                "label": PIPELINE_STAGE_LABELS[stage],
                "entries": [],
            }

        # Rejected separat
        pipeline["rejected"] = {
            "label": PIPELINE_STAGE_LABELS[PipelineStage.REJECTED],
            "entries": [],
        }

        for entry in entries:
            stage_key = entry.stage.value
            if stage_key in pipeline:
                pipeline[stage_key]["entries"].append(entry)

        return pipeline

    async def get_entry(self, entry_id: UUID) -> ATSPipelineEntry | None:
        """Holt einen Pipeline-Eintrag nach ID."""
        result = await self.db.execute(
            select(ATSPipelineEntry)
            .options(
                selectinload(ATSPipelineEntry.candidate),
                selectinload(ATSPipelineEntry.ats_job),
            )
            .where(ATSPipelineEntry.id == entry_id)
        )
        return result.scalar_one_or_none()

    async def get_entries_for_candidate(self, candidate_id: UUID) -> list[ATSPipelineEntry]:
        """Holt alle Pipeline-Eintraege fuer einen Kandidaten."""
        result = await self.db.execute(
            select(ATSPipelineEntry)
            .options(selectinload(ATSPipelineEntry.ats_job))
            .where(ATSPipelineEntry.candidate_id == candidate_id)
            .order_by(ATSPipelineEntry.created_at.desc())
        )
        return list(result.scalars().all())

    # ── Statistics ───────────────────────────────────

    async def get_pipeline_stats(self, ats_job_id: UUID) -> dict:
        """Gibt Pipeline-Statistiken fuer eine Stelle zurueck."""
        result = await self.db.execute(
            select(
                ATSPipelineEntry.stage,
                func.count(ATSPipelineEntry.id),
            )
            .where(ATSPipelineEntry.ats_job_id == ats_job_id)
            .group_by(ATSPipelineEntry.stage)
        )
        stats = {row[0].value: row[1] for row in result.all()}
        total = sum(stats.values())
        return {"stages": stats, "total": total}
