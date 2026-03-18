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
    """Berechnet Fahrzeit fuer einen einzelnen Pipeline-Eintrag.

    Nutzt Google Maps direkt mit ADRESSEN — kein Geocoding noetig!

    Adress-Kette fuer Job/Unternehmen:
    1. Company-Adresse (Strasse + PLZ + Stadt) — Unternehmen hat IMMER eine Adresse
    2. ATSJob.location_city als Fallback
    3. Source-Job Stadt als letzter Fallback

    Adress-Kette fuer Kandidat:
    1. Kandidat-Adresse (Strasse + PLZ + Stadt)
    2. Kandidat Stadt als Fallback
    """
    try:
        from app.database import async_session_maker
        from app.models.ats_job import ATSJob
        from app.models.ats_pipeline import ATSPipelineEntry
        from app.models.candidate import Candidate
        from app.models.company import Company
        from app.models.job import Job
        from app.services.distance_matrix_service import distance_matrix_service

        if not distance_matrix_service.has_api_key:
            logger.info("Kein Google Maps API Key — ueberspringe Fahrzeit")
            return

        dest_address = None
        dest_plz = None
        origin_address = None
        origin_plz = None

        # ── Daten laden (eigene Session — Railway 30s Timeout!) ──
        async with async_session_maker() as db:
            # Entry laden
            entry_result = await db.execute(
                select(ATSPipelineEntry).where(ATSPipelineEntry.id == entry_id)
            )
            entry = entry_result.scalar_one_or_none()
            if not entry or not entry.candidate_id:
                logger.info(f"Pipeline-Entry {entry_id}: Kein Kandidat verknuepft")
                return

            ats_job_id = entry.ats_job_id
            candidate_id = entry.candidate_id

            # ── ATSJob laden (fuer company_id + source_job_id + city) ──
            job_result = await db.execute(
                select(
                    ATSJob.location_city,
                    ATSJob.company_id,
                    ATSJob.source_job_id,
                ).where(ATSJob.id == ats_job_id)
            )
            job_row = job_result.one_or_none()
            if not job_row:
                logger.warning(f"Pipeline-Entry {entry_id}: ATSJob {ats_job_id} nicht gefunden")
                return

            # ── ZIEL-ADRESSE: Company hat IMMER eine vollstaendige Adresse ──
            comp_id = job_row.company_id

            # Falls ATSJob keine company_id hat, ueber Source-Job suchen
            if not comp_id and job_row.source_job_id:
                src_result = await db.execute(
                    select(Job.company_name, Job.city, Job.postal_code).where(Job.id == job_row.source_job_id)
                )
                src_row = src_result.one_or_none()
                if src_row and src_row.company_name:
                    # Company per Name finden
                    comp_by_name = await db.execute(
                        select(Company.id).where(
                            Company.name.ilike(src_row.company_name)
                        ).limit(1)
                    )
                    comp_id = comp_by_name.scalar_one_or_none()

                    # Notfalls Source-Job Stadt als Fallback
                    if not comp_id and src_row.city:
                        dest_address = f"{src_row.postal_code + ' ' if src_row.postal_code else ''}{src_row.city}, Deutschland"
                        dest_plz = src_row.postal_code

            if comp_id:
                comp_result = await db.execute(
                    select(
                        Company.name, Company.address, Company.postal_code, Company.city,
                    ).where(Company.id == comp_id)
                )
                comp = comp_result.one_or_none()
                if comp:
                    # Adresse zusammenbauen: Adresse + PLZ + Stadt
                    parts = []
                    if comp.address:
                        parts.append(comp.address)
                    if comp.postal_code:
                        parts.append(comp.postal_code)
                    if comp.city:
                        parts.append(comp.city)
                    if parts:
                        dest_address = ", ".join(parts) + ", Deutschland"
                        dest_plz = comp.postal_code
                        logger.info(f"Pipeline-Entry {entry_id}: Ziel-Adresse von Company '{comp.name}': {dest_address}")

            # Letzter Fallback: ATSJob location_city
            if not dest_address and job_row.location_city:
                dest_address = f"{job_row.location_city}, Deutschland"
                logger.info(f"Pipeline-Entry {entry_id}: Ziel-Adresse Fallback ATSJob Stadt: {dest_address}")

            # ── KANDIDATEN-ADRESSE ──
            cand_result = await db.execute(
                select(
                    Candidate.street_address, Candidate.postal_code, Candidate.city,
                    Candidate.first_name, Candidate.last_name,
                ).where(Candidate.id == candidate_id)
            )
            cand = cand_result.one_or_none()
            if not cand:
                logger.warning(f"Pipeline-Entry {entry_id}: Kandidat {candidate_id} nicht gefunden")
                return

            # Adresse zusammenbauen
            cand_parts = []
            if cand.street_address:
                cand_parts.append(cand.street_address)
            if cand.postal_code:
                cand_parts.append(cand.postal_code)
            if cand.city:
                cand_parts.append(cand.city)
            if cand_parts:
                origin_address = ", ".join(cand_parts) + ", Deutschland"
                origin_plz = cand.postal_code
            else:
                logger.warning(f"Pipeline-Entry {entry_id}: Kandidat {cand.first_name} {cand.last_name} hat keine Adresse")
                return

        # ── Session geschlossen — Google Maps API Call ──

        if not dest_address:
            logger.warning(f"Pipeline-Entry {entry_id}: Keine Ziel-Adresse gefunden (kein Company, kein Stadt)")
            return

        logger.info(f"Pipeline-Entry {entry_id}: Google Maps: '{origin_address}' -> '{dest_address}'")

        result = await distance_matrix_service.get_drive_time_by_address(
            origin_address=origin_address,
            dest_address=dest_address,
            origin_plz=origin_plz,
            dest_plz=dest_plz,
        )

        if result.status in ("ok", "same_plz"):
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
                f"Auto {result.car_min}min, OEPNV {result.transit_min}min, {result.car_km}km"
            )
        else:
            logger.warning(f"Pipeline-Entry {entry_id}: Google Maps Fehler: {result.status}")

    except Exception as e:
        logger.error(f"Fahrzeit-Berechnung fuer Pipeline-Entry {entry_id} fehlgeschlagen: {e}", exc_info=True)


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
