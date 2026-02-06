"""HTML-Seiten-Routen fuer ATS."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.ats_pipeline import PIPELINE_STAGE_LABELS, PIPELINE_STAGE_ORDER, PipelineStage
from app.services.ats_call_note_service import ATSCallNoteService
from app.services.ats_job_service import ATSJobService
from app.services.ats_pipeline_service import ATSPipelineService
from app.services.ats_todo_service import ATSTodoService

router = APIRouter(tags=["ATS Pages"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/ats", response_class=HTMLResponse)
async def ats_main(
    request: Request,
    company_id: UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """ATS Hauptseite — Kanban-Pipeline im JSX-Design."""
    import math
    from datetime import datetime as dt, timezone as tz

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.ats_job import ATSJob
    from app.models.ats_pipeline import ATSPipelineEntry
    from app.models.company import Company

    # Alle Jobs die in_pipeline=True haben und NICHT geloescht sind
    query = (
        select(ATSJob)
        .options(
            selectinload(ATSJob.company),
            selectinload(ATSJob.pipeline_entries).selectinload(ATSPipelineEntry.candidate),
        )
        .where(ATSJob.in_pipeline == True)
        .where(ATSJob.deleted_at.is_(None))
        .order_by(ATSJob.created_at.desc())
    )

    if company_id:
        query = query.where(ATSJob.company_id == company_id)

    result = await db.execute(query)
    jobs_in_pipeline = result.scalars().all()

    # ── Positions (fuer Tabs) ──
    positions = []
    for job in jobs_in_pipeline:
        total_entries = len(job.pipeline_entries)
        positions.append({
            "id": str(job.id),
            "name": job.title,
            "company": job.company.name if job.company else "",
            "candidates": total_entries,
            "active": job.status.value in ("open", "active") if hasattr(job, "status") else True,
        })

    # ── Kandidaten-Karten pro Job aufbauen ──
    now = dt.now(tz.utc)
    pipeline_by_job = {}
    for job in jobs_in_pipeline:
        job_id_str = str(job.id)
        cards = []
        for entry in job.pipeline_entries:
            c = entry.candidate
            if not c:
                continue
            # Tage in aktueller Phase
            if entry.stage_changed_at:
                days_in_stage = (now - entry.stage_changed_at).days
            else:
                days_in_stage = (now - entry.created_at).days if entry.created_at else 0
            # Avatar-Initialen
            fn = (c.first_name or "U")[0].upper()
            ln = (c.last_name or "")[0].upper()
            avatar = f"{fn}{ln}"
            # ERP als komma-separierter String (fuer Tags)
            erp_str = ", ".join(c.erp) if c.erp else ""
            cards.append({
                "id": str(c.id),
                "entry_id": str(entry.id),
                "name": c.full_name,
                "role": c.current_position or c.hotlist_job_title or "-",
                "phase": entry.stage.value,
                "days": days_in_stage,
                "salary": c.salary or "",
                "notice": c.notice_period or "",
                "erp": erp_str,
                "erp_list": c.erp or [],
                "avatar": avatar,
                "rating": c.rating or 0,
                "notes": entry.notes or "",
                "city": c.city or "",
            })
        pipeline_by_job[job_id_str] = cards

    # ── Alle Companies fuer Filter ──
    companies_result = await db.execute(
        select(Company).order_by(Company.name)
    )
    all_companies = companies_result.scalars().all()

    selected_company_name = None
    if company_id:
        for c in all_companies:
            if str(c.id) == str(company_id):
                selected_company_name = c.name
                break

    return templates.TemplateResponse("ats_pipeline_overview.html", {
        "request": request,
        "positions": positions,
        "pipeline_by_job": pipeline_by_job,
        "stages": PIPELINE_STAGE_ORDER,
        "stage_labels": PIPELINE_STAGE_LABELS,
        "all_companies": [{"id": str(c.id), "name": c.name} for c in all_companies],
        "selected_company_id": str(company_id) if company_id else None,
        "selected_company_name": selected_company_name,
        "total_jobs": len(jobs_in_pipeline),
    })


@router.get("/ats/stellen", response_class=HTMLResponse)
async def ats_jobs_list(
    request: Request,
    status: str | None = Query(None),
    priority: str | None = Query(None),
    search: str | None = Query(None),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    """ATS Stellen-Liste."""
    service = ATSJobService(db)
    result = await service.list_jobs(
        status=status,
        priority=priority,
        search=search,
        page=page,
        per_page=25,
    )

    return templates.TemplateResponse("ats_jobs_list.html", {
        "request": request,
        "jobs": result["items"],
        "total": result["total"],
        "page": result["page"],
        "pages": result["pages"],
        "filters": {
            "status": status,
            "priority": priority,
            "search": search,
        },
    })


@router.get("/ats/pipeline", response_class=HTMLResponse)
async def ats_pipeline_overview(
    request: Request,
    company_id: UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Pipeline-Uebersicht — Redirect auf /ats (gleiche Ansicht)."""
    from fastapi.responses import RedirectResponse
    url = "/ats"
    if company_id:
        url += f"?company_id={company_id}"
    return RedirectResponse(url=url, status_code=302)


@router.get("/ats/pipeline-design", response_class=HTMLResponse)
async def pipeline_design_preview(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Pipeline Design Vorschau - 4 Varianten zum Auswählen."""
    pipeline_service = ATSPipelineService(db)

    # Demo-Daten für Vorschau (leere Pipeline falls keine Jobs vorhanden)
    # Verwende UUID.int für einen "Demo-Job"
    from uuid import UUID as UUIDType
    demo_job_id = UUIDType("00000000-0000-0000-0000-000000000001")

    # Leere Demo-Pipeline erstellen
    pipeline = {
        "matched": {"label": "Gematcht", "entries": []},
        "sent": {"label": "Vorgestellt", "entries": []},
        "feedback": {"label": "Feedback", "entries": []},
        "interview_1": {"label": "Interview 1", "entries": []},
        "interview_2": {"label": "Interview 2", "entries": []},
        "interview_3": {"label": "Interview 3", "entries": []},
        "offer": {"label": "Angebot", "entries": []},
        "placed": {"label": "Platziert", "entries": []},
        "rejected": {"label": "Abgelehnt", "entries": []},
    }

    return templates.TemplateResponse("pipeline_design_preview.html", {
        "request": request,
        "job_id": demo_job_id,
        "pipeline": pipeline,
        "stages": PIPELINE_STAGE_ORDER,
        "stage_labels": PIPELINE_STAGE_LABELS,
        "PipelineStage": PipelineStage,
    })


@router.get("/ats/stellen/{job_id}", response_class=HTMLResponse)
async def ats_job_detail(
    request: Request,
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """ATS Stelle Detail mit Kanban-Pipeline."""
    job_service = ATSJobService(db)
    pipeline_service = ATSPipelineService(db)

    job = await job_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Stelle nicht gefunden")

    pipeline = await pipeline_service.get_pipeline(job_id)
    pipeline_stats = await pipeline_service.get_pipeline_stats(job_id)

    return templates.TemplateResponse("ats_job_detail.html", {
        "request": request,
        "job": job,
        "pipeline": pipeline,
        "pipeline_stats": pipeline_stats,
        "stages": PIPELINE_STAGE_ORDER,
        "stage_labels": PIPELINE_STAGE_LABELS,
        "PipelineStage": PipelineStage,
    })


@router.get("/ats/stellen/{job_id}/pipeline", response_class=HTMLResponse)
async def ats_pipeline_partial(
    request: Request,
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Pipeline HTMX-Partial (wird bei Drag & Drop neu geladen)."""
    pipeline_service = ATSPipelineService(db)
    pipeline = await pipeline_service.get_pipeline(job_id)

    return templates.TemplateResponse("partials/ats_pipeline_board.html", {
        "request": request,
        "job_id": job_id,
        "pipeline": pipeline,
        "stages": PIPELINE_STAGE_ORDER,
        "stage_labels": PIPELINE_STAGE_LABELS,
        "PipelineStage": PipelineStage,
    })


@router.get("/ats/todos", response_class=HTMLResponse)
async def ats_todos_page(
    request: Request,
    status: str | None = Query(None),
    priority: str | None = Query(None),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    """ATS Todo-Uebersicht."""
    service = ATSTodoService(db)
    result = await service.list_todos(
        status=status,
        priority=priority,
        page=page,
        per_page=50,
    )
    stats = await service.get_stats()

    return templates.TemplateResponse("ats_todos.html", {
        "request": request,
        "todos": result["items"],
        "total": result["total"],
        "page": result["page"],
        "pages": result["pages"],
        "stats": stats,
        "filters": {
            "status": status,
            "priority": priority,
        },
    })


@router.get("/ats/anrufe", response_class=HTMLResponse)
async def ats_call_notes_page(
    request: Request,
    call_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    """ATS Call-Notes Uebersicht."""
    service = ATSCallNoteService(db)
    result = await service.list_call_notes(
        call_type=call_type,
        page=page,
        per_page=25,
    )

    return templates.TemplateResponse("ats_call_notes.html", {
        "request": request,
        "notes": result["items"],
        "total": result["total"],
        "page": result["page"],
        "pages": result["pages"],
        "filters": {
            "call_type": call_type,
        },
    })
