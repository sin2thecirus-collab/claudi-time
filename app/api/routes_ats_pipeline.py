"""API-Routen fuer ATS-Pipeline (Kanban)."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.database import get_db
from app.models.match import Match
from app.services.ats_pipeline_service import ATSPipelineService

router = APIRouter(prefix="/ats/pipeline", tags=["ATS Pipeline"])


# ── Pydantic Schemas ─────────────────────────────

class AddCandidateRequest(BaseModel):
    candidate_id: UUID
    stage: Optional[str] = "sent"  # Default: Vorgestellt (da MATCHED nicht in Pipeline-Uebersicht sichtbar)
    notes: Optional[str] = None


class MoveStageRequest(BaseModel):
    stage: str


class ReorderRequest(BaseModel):
    sort_order: int


class BulkMoveRequest(BaseModel):
    entry_ids: list[UUID]
    stage: str


# ── Endpoints ────────────────────────────────────

@router.get("/{job_id}")
async def get_pipeline(job_id: UUID, db: AsyncSession = Depends(get_db)):
    """Laedt die Pipeline fuer eine Stelle (Kanban-Daten)."""
    service = ATSPipelineService(db)
    pipeline = await service.get_pipeline(job_id)

    # Drive-Time Lookup: Lade Fahrzeit-Daten aus matches fuer alle Kandidaten dieses Jobs
    drive_time_map: dict[str, dict] = {}
    try:
        dt_result = await db.execute(
            select(
                Match.candidate_id,
                Match.drive_time_car_min,
                Match.drive_time_transit_min,
            ).where(Match.job_id == job_id)
        )
        for row in dt_result.all():
            drive_time_map[str(row.candidate_id)] = {
                "drive_time_car_min": row.drive_time_car_min,
                "drive_time_transit_min": row.drive_time_transit_min,
            }
    except Exception:
        pass  # Fahrzeit ist optional — Pipeline funktioniert auch ohne

    # Entries serialisieren
    result = {}
    for stage_key, stage_data in pipeline.items():
        entries = []
        for entry in stage_data["entries"]:
            candidate = entry.candidate
            cand_id_str = str(entry.candidate_id) if entry.candidate_id else None
            dt = drive_time_map.get(cand_id_str, {})
            entries.append({
                "id": str(entry.id),
                "candidate_id": cand_id_str,
                "candidate_name": f"{candidate.first_name or ''} {candidate.last_name or ''}".strip() if candidate else "Unbekannt",
                "candidate_position": candidate.current_position if candidate else None,
                "candidate_city": candidate.city if candidate else None,
                "drive_time_car_min": dt.get("drive_time_car_min"),
                "drive_time_transit_min": dt.get("drive_time_transit_min"),
                "stage": entry.stage.value,
                "stage_label": entry.stage_label,
                "notes": entry.notes,
                "sort_order": entry.sort_order,
                "stage_changed_at": entry.stage_changed_at.isoformat() if entry.stage_changed_at else None,
                "created_at": entry.created_at.isoformat() if entry.created_at else None,
            })
        result[stage_key] = {
            "label": stage_data["label"],
            "entries": entries,
            "count": len(entries),
        }

    return result


@router.post("/{job_id}/candidates")
async def add_candidate_to_pipeline(
    job_id: UUID, data: AddCandidateRequest, db: AsyncSession = Depends(get_db)
):
    """Fuegt einen Kandidaten zur Pipeline hinzu."""
    service = ATSPipelineService(db)
    try:
        entry = await service.add_candidate(
            ats_job_id=job_id,
            candidate_id=data.candidate_id,
            stage=data.stage,
            notes=data.notes,
        )
    except Exception as e:
        if "uq_ats_pipeline_job_candidate" in str(e):
            raise HTTPException(
                status_code=409,
                detail="Kandidat ist bereits in dieser Pipeline",
            )
        raise
    await db.commit()
    return {
        "id": str(entry.id),
        "stage": entry.stage.value,
        "message": "Kandidat zur Pipeline hinzugefuegt",
    }


@router.patch("/entries/{entry_id}/stage")
async def move_pipeline_stage(
    entry_id: UUID, data: MoveStageRequest, db: AsyncSession = Depends(get_db)
):
    """Verschiebt einen Kandidaten in eine neue Stage (Drag & Drop)."""
    valid_stages = [
        "matched", "sent", "feedback",
        "interview_1", "interview_2", "interview_3",
        "offer", "placed", "rejected",
    ]
    if data.stage not in valid_stages:
        raise HTTPException(status_code=400, detail=f"Ungueltiger Stage: {data.stage}")

    service = ATSPipelineService(db)
    entry = await service.move_stage(entry_id, data.stage)
    if not entry:
        raise HTTPException(status_code=404, detail="Pipeline-Eintrag nicht gefunden")
    await db.commit()
    return {
        "id": str(entry.id),
        "stage": entry.stage.value,
        "stage_label": entry.stage_label,
        "message": f"Stage auf '{entry.stage_label}' geaendert",
    }


@router.patch("/entries/{entry_id}/reorder")
async def reorder_pipeline_entry(
    entry_id: UUID, data: ReorderRequest, db: AsyncSession = Depends(get_db)
):
    """Aendert die Reihenfolge eines Eintrags innerhalb einer Stage."""
    service = ATSPipelineService(db)
    entry = await service.reorder(entry_id, data.sort_order)
    if not entry:
        raise HTTPException(status_code=404, detail="Pipeline-Eintrag nicht gefunden")
    await db.commit()
    return {"id": str(entry.id), "sort_order": entry.sort_order}


@router.delete("/entries/{entry_id}")
async def remove_pipeline_entry(
    entry_id: UUID,
    reason: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Entfernt einen Kandidaten aus der Pipeline."""
    service = ATSPipelineService(db)
    removed = await service.remove_candidate(entry_id, reason=reason)
    if not removed:
        raise HTTPException(status_code=404, detail="Pipeline-Eintrag nicht gefunden")
    await db.commit()
    return {"message": "Kandidat aus Pipeline entfernt"}


@router.post("/entries/bulk-move")
async def bulk_move_pipeline_entries(
    data: BulkMoveRequest, db: AsyncSession = Depends(get_db)
):
    """Verschiebt mehrere Kandidaten in eine neue Stage."""
    service = ATSPipelineService(db)
    entries = await service.bulk_move(data.entry_ids, data.stage)
    await db.commit()
    return {
        "moved": len(entries),
        "stage": data.stage,
        "message": f"{len(entries)} Eintraege verschoben",
    }
