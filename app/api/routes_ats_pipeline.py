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

async def _geocode_city(city: str) -> tuple[float, float] | None:
    """Geocodiert eine Stadt per Nominatim (OpenStreetMap). Returns (lat, lng) oder None."""
    import httpx

    if not city or not city.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": f"{city.strip()}, Deutschland", "format": "json", "limit": "1"},
                headers={"User-Agent": "PulspointCRM/1.0"},
            )
            resp.raise_for_status()
            data = resp.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        logger.warning(f"Geocoding fuer '{city}' fehlgeschlagen: {e}")
    return None


async def _calculate_drive_time_for_entry(entry_id: str) -> None:
    """Berechnet Fahrzeit fuer einen einzelnen Pipeline-Eintrag.

    Fallback-Kette fuer Job-Koordinaten:
    1. ATSJob.location_coords (PostGIS)
    2. Source-Job (jobs Tabelle) ueber source_job_id -> location_coords
    3. Company.location_coords (ueber ATSJob.company_id ODER Source-Job.company_name)
    4. Nominatim Geocoding der Stadt (ATSJob.location_city)

    Fallback-Kette fuer Kandidaten-Koordinaten:
    1. Candidate.address_coords (PostGIS)
    2. Nominatim Geocoding der Stadt (Candidate.city)
    """
    try:
        from sqlalchemy import func as sa_func, text

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

        # Daten laden (eigene Session — Railway 30s Timeout!)
        job_lat = None
        job_lng = None
        job_city = None
        cand_lat = None
        cand_lng = None
        cand_city = None
        cand_plz = None
        dest_plz = None

        async with async_session_maker() as db:
            # Entry laden
            entry_result = await db.execute(
                select(ATSPipelineEntry).where(ATSPipelineEntry.id == entry_id)
            )
            entry = entry_result.scalar_one_or_none()
            if not entry or not entry.candidate_id:
                return

            ats_job_id = entry.ats_job_id
            candidate_id = entry.candidate_id

            # ── Job-Koordinaten: ATSJob direkt ──
            job_result = await db.execute(
                select(
                    sa_func.ST_Y(sa_func.ST_GeomFromWKB(ATSJob.location_coords)).label("job_lat"),
                    sa_func.ST_X(sa_func.ST_GeomFromWKB(ATSJob.location_coords)).label("job_lng"),
                    ATSJob.location_city,
                    ATSJob.company_id,
                    ATSJob.source_job_id,
                ).where(ATSJob.id == ats_job_id)
            )
            job_row = job_result.one_or_none()
            if not job_row:
                logger.warning(f"Pipeline-Entry {entry_id}: ATSJob {ats_job_id} nicht gefunden")
                return

            job_lat = job_row.job_lat
            job_lng = job_row.job_lng
            job_city = job_row.location_city
            source_job_id = job_row.source_job_id
            company_id = job_row.company_id

            # ── Fallback 1: Source-Job (jobs Tabelle) ──
            if (not job_lat or not job_lng) and source_job_id:
                src_result = await db.execute(
                    select(
                        sa_func.ST_Y(sa_func.ST_GeomFromWKB(Job.location_coords)).label("lat"),
                        sa_func.ST_X(sa_func.ST_GeomFromWKB(Job.location_coords)).label("lng"),
                        Job.postal_code,
                        Job.city,
                    ).where(Job.id == source_job_id)
                )
                src_row = src_result.one_or_none()
                if src_row:
                    if src_row.lat and src_row.lng:
                        job_lat, job_lng = src_row.lat, src_row.lng
                        dest_plz = src_row.postal_code
                        logger.info(f"Pipeline-Entry {entry_id}: Koordinaten vom Source-Job {source_job_id}")
                    elif src_row.city:
                        job_city = job_city or src_row.city

            # ── Fallback 2: Company-Koordinaten ──
            if not job_lat or not job_lng:
                comp_id_to_check = company_id
                # Falls ATSJob keine Company hat, versuche Company ueber Source-Job
                if not comp_id_to_check and source_job_id:
                    src_comp = await db.execute(
                        select(Job.company_name).where(Job.id == source_job_id)
                    )
                    src_comp_row = src_comp.one_or_none()
                    if src_comp_row and src_comp_row.company_name:
                        # Company per Name finden
                        from app.models.company import Company
                        comp_by_name = await db.execute(
                            select(Company.id).where(
                                Company.name.ilike(src_comp_row.company_name)
                            ).limit(1)
                        )
                        comp_match = comp_by_name.scalar_one_or_none()
                        if comp_match:
                            comp_id_to_check = comp_match

                if comp_id_to_check:
                    comp_result = await db.execute(
                        select(
                            sa_func.ST_Y(sa_func.ST_GeomFromWKB(Company.location_coords)).label("lat"),
                            sa_func.ST_X(sa_func.ST_GeomFromWKB(Company.location_coords)).label("lng"),
                            Company.city,
                            Company.postal_code,
                        ).where(Company.id == comp_id_to_check)
                    )
                    comp_row = comp_result.one_or_none()
                    if comp_row:
                        if comp_row.lat and comp_row.lng:
                            job_lat, job_lng = comp_row.lat, comp_row.lng
                            dest_plz = comp_row.postal_code
                            logger.info(f"Pipeline-Entry {entry_id}: Koordinaten von Company {comp_id_to_check}")
                        elif comp_row.city:
                            job_city = job_city or comp_row.city

            # ── Kandidaten-Koordinaten ──
            cand_result = await db.execute(
                select(
                    sa_func.ST_Y(sa_func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lat"),
                    sa_func.ST_X(sa_func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lng"),
                    Candidate.postal_code,
                    Candidate.city.label("cand_city"),
                ).where(Candidate.id == candidate_id)
            )
            cand_row = cand_result.one_or_none()
            if not cand_row:
                return

            cand_lat = cand_row.cand_lat
            cand_lng = cand_row.cand_lng
            cand_city = cand_row.cand_city
            cand_plz = cand_row.postal_code

        # Session geschlossen — jetzt Geocoding falls noetig

        # ── Fallback 3: Job-Stadt geocoden per Nominatim ──
        if (not job_lat or not job_lng) and job_city:
            coords = await _geocode_city(job_city)
            if coords:
                job_lat, job_lng = coords
                # Koordinaten auf ATSJob speichern fuer naechstes Mal
                async with async_session_maker() as db_geo:
                    await db_geo.execute(text(
                        "UPDATE ats_jobs SET location_coords = ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography WHERE id = :jid"
                    ), {"lng": job_lng, "lat": job_lat, "jid": str(ats_job_id)})
                    await db_geo.commit()
                logger.info(f"ATSJob {ats_job_id}: Geocoded '{job_city}' -> {job_lat},{job_lng}")

        if not job_lat or not job_lng:
            logger.warning(f"Pipeline-Entry {entry_id}: Keine Job-Koordinaten gefunden (kein ATSJob, Source-Job, Company oder Stadt)")
            return

        # ── Kandidat geocoden falls noetig ──
        if (not cand_lat or not cand_lng) and cand_city:
            coords = await _geocode_city(cand_city)
            if coords:
                cand_lat, cand_lng = coords
                async with async_session_maker() as db_geo2:
                    await db_geo2.execute(text(
                        "UPDATE candidates SET address_coords = ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography WHERE id = :cid"
                    ), {"lng": cand_lng, "lat": cand_lat, "cid": str(candidate_id)})
                    await db_geo2.commit()
                logger.info(f"Kandidat {candidate_id}: Geocoded '{cand_city}' -> {cand_lat},{cand_lng}")
            # Rate-Limit: 1 Sekunde zwischen Nominatim-Requests
            await asyncio.sleep(1.0)

        if not cand_lat or not cand_lng:
            logger.warning(f"Pipeline-Entry {entry_id}: Keine Kandidaten-Koordinaten gefunden (keine coords, keine Stadt)")
            return

        # ── Google Maps Fahrzeit berechnen ──
        result = await distance_matrix_service.get_drive_time(
            origin_lat=cand_lat,
            origin_lng=cand_lng,
            origin_plz=cand_plz,
            dest_lat=job_lat,
            dest_lng=job_lng,
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
            logger.warning(f"Pipeline-Entry {entry_id}: Fahrzeit-API Fehler: {result.status}")

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
