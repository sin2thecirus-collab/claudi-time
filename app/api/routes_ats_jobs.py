"""API-Routen fuer ATS-Stellen."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.ats_job_service import ATSJobService

router = APIRouter(prefix="/ats/jobs", tags=["ATS Jobs"])


# ── Pydantic Schemas ─────────────────────────────

class ATSJobCreate(BaseModel):
    title: str
    company_id: Optional[UUID] = None
    contact_id: Optional[UUID] = None
    description: Optional[str] = None
    requirements: Optional[str] = None
    location_city: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    employment_type: Optional[str] = None
    priority: Optional[str] = "medium"
    source: Optional[str] = None
    notes: Optional[str] = None


class ATSJobUpdate(BaseModel):
    title: Optional[str] = None
    company_id: Optional[UUID] = None
    contact_id: Optional[UUID] = None
    description: Optional[str] = None
    requirements: Optional[str] = None
    location_city: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    employment_type: Optional[str] = None
    priority: Optional[str] = None
    source: Optional[str] = None
    notes: Optional[str] = None
    in_pipeline: Optional[bool] = None


# ── Endpoints ────────────────────────────────────

@router.post("")
async def create_ats_job(data: ATSJobCreate, db: AsyncSession = Depends(get_db)):
    """Erstellt eine neue ATS-Stelle."""
    service = ATSJobService(db)
    job = await service.create_job(**data.model_dump(exclude_unset=True))
    await db.commit()
    return {
        "id": str(job.id),
        "title": job.title,
        "message": f"Stelle '{job.title}' erstellt",
    }


@router.get("")
async def list_ats_jobs(
    company_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort_by: str = Query("created_at"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Listet ATS-Stellen mit Filter."""
    service = ATSJobService(db)
    result = await service.list_jobs(
        company_id=company_id,
        status=status,
        priority=priority,
        search=search,
        sort_by=sort_by,
        page=page,
        per_page=per_page,
    )

    items = []
    for item in result["items"]:
        job = item["job"]
        items.append({
            "id": str(job.id),
            "title": job.title,
            "company_name": job.company.name if job.company else None,
            "company_id": str(job.company_id) if job.company_id else None,
            "location_city": job.location_city,
            "status": job.status.value,
            "priority": job.priority.value,
            "employment_type": job.employment_type,
            "pipeline_count": item["pipeline_count"],
            "in_pipeline": getattr(job, 'in_pipeline', False),
            "created_at": job.created_at.isoformat() if job.created_at else None,
        })

    return {
        "items": items,
        "total": result["total"],
        "page": result["page"],
        "per_page": result["per_page"],
        "pages": result["pages"],
    }


@router.get("/{job_id}")
async def get_ats_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    """Holt Details einer ATS-Stelle."""
    service = ATSJobService(db)
    job = await service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="ATS-Stelle nicht gefunden")

    return {
        "id": str(job.id),
        "title": job.title,
        "company_id": str(job.company_id) if job.company_id else None,
        "company_name": job.company.name if job.company else None,
        "contact_id": str(job.contact_id) if job.contact_id else None,
        "description": job.description,
        "requirements": job.requirements,
        "location_city": job.location_city,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "salary_display": job.salary_display,
        "employment_type": job.employment_type,
        "priority": job.priority.value,
        "status": job.status.value,
        "source": job.source,
        "notes": job.notes,
        "filled_at": job.filled_at.isoformat() if job.filled_at else None,
        "pipeline_count": len(job.pipeline_entries) if job.pipeline_entries else 0,
        "in_pipeline": getattr(job, 'in_pipeline', False),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


@router.patch("/{job_id}")
async def update_ats_job(
    job_id: UUID, data: ATSJobUpdate, db: AsyncSession = Depends(get_db)
):
    """Aktualisiert eine ATS-Stelle."""
    service = ATSJobService(db)
    job = await service.update_job(job_id, data.model_dump(exclude_unset=True))
    if not job:
        raise HTTPException(status_code=404, detail="ATS-Stelle nicht gefunden")
    await db.commit()
    return {"message": f"Stelle '{job.title}' aktualisiert", "id": str(job.id)}


@router.delete("/{job_id}")
async def delete_ats_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    """Loescht eine ATS-Stelle."""
    service = ATSJobService(db)
    deleted = await service.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="ATS-Stelle nicht gefunden")
    await db.commit()
    return {"message": "Stelle geloescht"}


@router.put("/{job_id}/status")
async def set_ats_job_status(
    job_id: UUID,
    status: str = Query(..., description="open/paused/filled/cancelled"),
    db: AsyncSession = Depends(get_db),
):
    """Aendert den Status einer ATS-Stelle."""
    if status not in ("open", "paused", "filled", "cancelled"):
        raise HTTPException(status_code=400, detail="Ungueltiger Status")
    service = ATSJobService(db)
    job = await service.change_status(job_id, status)
    if not job:
        raise HTTPException(status_code=404, detail="ATS-Stelle nicht gefunden")
    await db.commit()
    return {"message": f"Status auf '{status}' gesetzt", "id": str(job.id)}


@router.put("/{job_id}/to-pipeline")
async def move_job_to_pipeline(job_id: UUID, db: AsyncSession = Depends(get_db)):
    """Setzt in_pipeline=True — Job erscheint dann in der Interview-Pipeline-Uebersicht."""
    service = ATSJobService(db)
    job = await service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="ATS-Stelle nicht gefunden")

    job.in_pipeline = True
    await db.commit()
    return {
        "message": f"'{job.title}' ist jetzt in der Interview-Pipeline",
        "id": str(job.id),
        "in_pipeline": True,
    }


@router.put("/{job_id}/from-pipeline")
async def remove_job_from_pipeline(job_id: UUID, db: AsyncSession = Depends(get_db)):
    """Setzt in_pipeline=False — Job wird aus der Interview-Pipeline-Uebersicht entfernt."""
    service = ATSJobService(db)
    job = await service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="ATS-Stelle nicht gefunden")

    job.in_pipeline = False
    await db.commit()
    return {
        "message": f"'{job.title}' wurde aus der Interview-Pipeline entfernt",
        "id": str(job.id),
        "in_pipeline": False,
    }
