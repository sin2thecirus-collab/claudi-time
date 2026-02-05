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
async def ats_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    """ATS Dashboard — Uebersicht."""
    job_service = ATSJobService(db)
    todo_service = ATSTodoService(db)

    # Daten laden
    job_stats = await job_service.get_stats()
    todo_stats = await todo_service.get_stats()
    today_todos = await todo_service.get_today_todos()
    overdue_todos = await todo_service.get_overdue_todos()

    # Letzte Stellen
    recent_jobs = await job_service.list_jobs(page=1, per_page=5)

    return templates.TemplateResponse("ats_dashboard.html", {
        "request": request,
        "job_stats": job_stats,
        "todo_stats": todo_stats,
        "today_todos": today_todos,
        "overdue_todos": overdue_todos,
        "recent_jobs": recent_jobs["items"],
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
