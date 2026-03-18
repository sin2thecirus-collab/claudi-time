"""API-Routen fuer ATS-Pipeline (Kanban)."""

import asyncio
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select, update

from app.database import get_db
from app.models.match import Match
from app.services.ats_pipeline_service import ATSPipelineService

logger = logging.getLogger(__name__)

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

    # Entries serialisieren — Pipeline-Entry Fahrzeit hat Vorrang vor Match-Daten
    result = {}
    for stage_key, stage_data in pipeline.items():
        entries = []
        for entry in stage_data["entries"]:
            candidate = entry.candidate
            cand_id_str = str(entry.candidate_id) if entry.candidate_id else None
            match_dt = drive_time_map.get(cand_id_str, {})
            # Pipeline-Entry Fahrzeit hat Vorrang
            car_min = entry.drive_time_car_min if entry.drive_time_car_min is not None else match_dt.get("drive_time_car_min")
            transit_min = entry.drive_time_transit_min if entry.drive_time_transit_min is not None else match_dt.get("drive_time_transit_min")
            entries.append({
                "id": str(entry.id),
                "candidate_id": cand_id_str,
                "candidate_name": f"{candidate.first_name or ''} {candidate.last_name or ''}".strip() if candidate else "Unbekannt",
                "candidate_position": candidate.current_position if candidate else None,
                "candidate_city": candidate.city if candidate else None,
                "drive_time_car_min": car_min,
                "drive_time_transit_min": transit_min,
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
    job_id: UUID,
    data: AddCandidateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
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

    # Fahrzeit im Background berechnen
    entry_id = entry.id
    background_tasks.add_task(_calculate_drive_time_for_entry, str(entry_id))

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


@router.post("/drive-times/backfill")
async def backfill_pipeline_drive_times(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Berechnet Fahrzeit fuer alle Pipeline-Eintraege ohne Fahrzeit."""
    from app.models.ats_pipeline import ATSPipelineEntry

    # Zaehle Eintraege ohne Fahrzeit
    result = await db.execute(
        select(ATSPipelineEntry.id).where(
            ATSPipelineEntry.drive_time_car_min.is_(None),
            ATSPipelineEntry.candidate_id.isnot(None),
        )
    )
    entry_ids = [str(row[0]) for row in result.all()]

    if not entry_ids:
        return {"message": "Alle Pipeline-Eintraege haben bereits Fahrzeit", "count": 0}

    background_tasks.add_task(_backfill_drive_times, entry_ids)

    return {
        "message": f"Fahrzeit-Berechnung fuer {len(entry_ids)} Eintraege gestartet",
        "count": len(entry_ids),
    }


# ── Background Tasks ────────────────────────────

async def _calculate_drive_time_for_entry(entry_id: str) -> None:
    """Berechnet Fahrzeit fuer einen einzelnen Pipeline-Eintrag."""
    try:
        from sqlalchemy import func as sa_func

        from app.database import async_session_maker
        from app.models.ats_job import ATSJob
        from app.models.ats_pipeline import ATSPipelineEntry
        from app.models.candidate import Candidate
        from app.services.distance_matrix_service import distance_matrix_service

        if not distance_matrix_service.has_api_key:
            logger.info("Kein Google Maps API Key — ueberspringe Fahrzeit")
            return

        # Daten laden (eigene Session — Railway 30s Timeout!)
        async with async_session_maker() as db:
            entry_result = await db.execute(
                select(ATSPipelineEntry).where(ATSPipelineEntry.id == entry_id)
            )
            entry = entry_result.scalar_one_or_none()
            if not entry or not entry.candidate_id:
                return

            # Job-Koordinaten laden
            job_result = await db.execute(
                select(
                    sa_func.ST_Y(sa_func.ST_GeomFromWKB(ATSJob.location_coords)).label("job_lat"),
                    sa_func.ST_X(sa_func.ST_GeomFromWKB(ATSJob.location_coords)).label("job_lng"),
                    ATSJob.location_city,
                ).where(ATSJob.id == entry.ats_job_id)
            )
            job_row = job_result.one_or_none()
            if not job_row or not job_row.job_lat or not job_row.job_lng:
                logger.info(f"Pipeline-Entry {entry_id}: Job hat keine Koordinaten")
                return

            # Kandidaten-Koordinaten laden
            cand_result = await db.execute(
                select(
                    sa_func.ST_Y(sa_func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lat"),
                    sa_func.ST_X(sa_func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lng"),
                    Candidate.postal_code,
                ).where(Candidate.id == entry.candidate_id)
            )
            cand_row = cand_result.one_or_none()
            if not cand_row or not cand_row.cand_lat or not cand_row.cand_lng:
                logger.info(f"Pipeline-Entry {entry_id}: Kandidat hat keine Koordinaten")
                return

        # Session geschlossen — jetzt API-Call
        result = await distance_matrix_service.get_drive_time(
            origin_lat=cand_row.cand_lat,
            origin_lng=cand_row.cand_lng,
            origin_plz=cand_row.postal_code,
            dest_lat=job_row.job_lat,
            dest_lng=job_row.job_lng,
            dest_plz=None,  # ATSJob hat kein PLZ-Feld — Cache basiert auf Kandidaten-PLZ
        )

        if result.status in ("ok", "same_plz"):
            # Ergebnis in DB schreiben (neue Session)
            async with async_session_maker() as db2:
                await db2.execute(
                    update(ATSPipelineEntry)
                    .where(ATSPipelineEntry.id == entry_id)
                    .values(
                        drive_time_car_min=result.car_min,
                        drive_time_transit_min=result.transit_min,
                        drive_time_car_km=result.car_km,
                    )
                )
                await db2.commit()
            logger.info(
                f"Pipeline-Entry {entry_id}: Fahrzeit berechnet — "
                f"Auto {result.car_min}min, OEPNV {result.transit_min}min"
            )
        else:
            logger.warning(f"Pipeline-Entry {entry_id}: Fahrzeit-API Fehler: {result.status}")

    except Exception as e:
        logger.error(f"Fahrzeit-Berechnung fuer Pipeline-Entry {entry_id} fehlgeschlagen: {e}")


async def _backfill_drive_times(entry_ids: list[str]) -> None:
    """Backfill: Berechnet Fahrzeit fuer mehrere Pipeline-Eintraege."""
    logger.info(f"Fahrzeit-Backfill gestartet fuer {len(entry_ids)} Eintraege")
    success = 0
    errors = 0
    for entry_id in entry_ids:
        try:
            await _calculate_drive_time_for_entry(entry_id)
            success += 1
        except Exception as e:
            logger.error(f"Backfill-Fehler fuer {entry_id}: {e}")
            errors += 1
        # Kleine Pause um Rate-Limits zu vermeiden
        await asyncio.sleep(0.2)
    logger.info(f"Fahrzeit-Backfill fertig: {success} OK, {errors} Fehler")
