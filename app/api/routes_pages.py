"""Page Routes - HTML-Seiten fuer das Frontend."""

import logging
import re
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.database import get_db
from app.models.job_run import JobSource, JobType
from app.models.company_contact import CompanyContact
from app.services.job_runner_service import JobRunnerService
from app.schemas.filters import JobFilterParams
from app.services.job_service import JobService
from app.services.candidate_service import CandidateService
from app.services.company_service import CompanyService
from app.services.filter_service import FilterService
from app.services.statistics_service import StatisticsService
from app.services.alert_service import AlertService
from app.services.ats_call_note_service import ATSCallNoteService
from app.services.ats_todo_service import ATSTodoService
from app.services.email_service import EmailService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Pages"])
templates = Jinja2Templates(directory="app/templates")


# Jinja2 Filter hinzufuegen
def format_datetime(value, format_string="%d.%m.%Y %H:%M"):
    """Formatiert ein datetime-Objekt."""
    if value is None:
        return ""
    return value.strftime(format_string)


def format_date(value, format_string="%d.%m.%Y"):
    """Formatiert ein Datum."""
    if value is None:
        return ""
    return value.strftime(format_string)


templates.env.filters["datetime"] = format_datetime
templates.env.filters["date"] = format_date

# Jinja2-Filter: UTC → deutsche Zeit (Europe/Berlin)
from zoneinfo import ZoneInfo


def to_berlin(dt_value):
    """Konvertiert UTC datetime nach Europe/Berlin (MEZ/MESZ)."""
    if dt_value is None:
        return dt_value
    berlin = ZoneInfo("Europe/Berlin")
    if dt_value.tzinfo is None:
        from datetime import timezone
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value.astimezone(berlin)


templates.env.filters["to_berlin"] = to_berlin

# now() Funktion fuer Templates
templates.env.globals["now"] = datetime.now


# ============================================================================
# Hauptseiten
# ============================================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Dashboard / Startseite mit Job-Liste."""
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request}
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(
    request: Request,
    job_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    """Job-Detailseite mit Kandidaten."""
    job_service = JobService(db)
    job = await job_service.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")

    return templates.TemplateResponse(
        "job_detail.html",
        {
            "request": request,
            "job": job,
            "now": datetime.now()
        }
    )


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    """Dedizierte Jobs-Uebersichtsseite mit Kachel-/Listenansicht."""
    return templates.TemplateResponse(
        "jobs.html",
        {"request": request}
    )


@router.get("/kandidaten", response_class=HTMLResponse)
async def candidates_list_page(request: Request):
    """Kandidaten-Uebersichtsseite."""
    return templates.TemplateResponse(
        "candidates.html",
        {"request": request}
    )


@router.get("/kandidaten/{candidate_id}", response_class=HTMLResponse)
async def candidate_detail(
    request: Request,
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    """Kandidaten-Detailseite."""
    candidate_service = CandidateService(db)
    candidate = await candidate_service.get_candidate(candidate_id)

    if not candidate:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    # CallNotes fuer Anrufprotokoll laden
    call_note_service = ATSCallNoteService(db)
    call_notes_result = await call_note_service.list_call_notes(
        candidate_id=candidate_id, per_page=100
    )
    call_notes = call_notes_result["items"]

    # Todos fuer Aufgaben-Tab laden
    todo_service = ATSTodoService(db)
    todos_result = await todo_service.list_todos(
        candidate_id=candidate_id, per_page=100
    )
    todos = todos_result["items"]

    # Todos serialisieren fuer Alpine.js tojson im Template
    todos_serialized = []
    for t in todos:
        todos_serialized.append({
            "id": str(t.id),
            "title": t.title,
            "description": t.description,
            "status": t.status.value if hasattr(t.status, 'value') else t.status,
            "priority": t.priority.value if hasattr(t.priority, 'value') else t.priority,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "due_time": t.due_time if hasattr(t, 'due_time') else None,
            "is_overdue": t.is_overdue if hasattr(t, 'is_overdue') else False,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })

    # Email-Drafts fuer diesen Kandidaten laden
    email_service = EmailService(db)
    email_drafts = await email_service.list_drafts(candidate_id=candidate_id)
    drafts_serialized = []
    for d in email_drafts:
        drafts_serialized.append({
            "id": str(d.id),
            "email_type": d.email_type,
            "to_email": d.to_email,
            "subject": d.subject,
            "body_html": d.body_html,
            "status": d.status,
            "auto_send": d.auto_send,
            "sent_at": d.sent_at.isoformat() if d.sent_at else None,
            "send_error": d.send_error,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        })

    return templates.TemplateResponse(
        "candidate_detail.html",
        {
            "request": request,
            "candidate": candidate,
            "call_notes": call_notes,
            "todos": todos,
            "todos_json": todos_serialized,
            "email_drafts_json": drafts_serialized,
        }
    )


@router.get("/unternehmen", response_class=HTMLResponse)
async def companies_page(request: Request):
    """Unternehmen-Uebersichtsseite."""
    return templates.TemplateResponse(
        "companies.html",
        {"request": request}
    )


@router.get("/unternehmen/{company_id}", response_class=HTMLResponse)
async def company_detail(
    request: Request,
    company_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Unternehmen-Detailseite."""
    company_service = CompanyService(db)
    company = await company_service.get_company(company_id)

    if not company:
        raise HTTPException(status_code=404, detail="Unternehmen nicht gefunden")

    # Job-Count laden (normale Jobs)
    from app.models.job import Job
    from app.models.ats_job import ATSJob
    from sqlalchemy import func, select
    job_count_result = await db.execute(
        select(func.count(Job.id)).where(
            Job.company_id == company_id,
            Job.deleted_at.is_(None),
        )
    )
    job_count = job_count_result.scalar() or 0

    # ATS-Jobs Count laden (nur nicht-geloeschte)
    ats_job_count_result = await db.execute(
        select(func.count(ATSJob.id)).where(
            ATSJob.company_id == company_id,
            ATSJob.deleted_at.is_(None),
        )
    )
    ats_job_count = ats_job_count_result.scalar() or 0

    # Call Stats laden
    from app.models.ats_call_note import ATSCallNote
    call_count_result = await db.execute(
        select(func.count(ATSCallNote.id)).where(
            ATSCallNote.company_id == company_id,
        )
    )
    call_count = call_count_result.scalar() or 0

    call_duration_result = await db.execute(
        select(func.coalesce(func.sum(ATSCallNote.duration_minutes), 0)).where(
            ATSCallNote.company_id == company_id,
        )
    )
    call_total_duration = call_duration_result.scalar() or 0

    # Contact Count laden
    contact_count_result = await db.execute(
        select(func.count(CompanyContact.id)).where(
            CompanyContact.company_id == company_id,
        )
    )
    contact_count = contact_count_result.scalar() or 0

    # Kontakte fuer Call-Modal laden
    from sqlalchemy.orm import selectinload
    contacts_result = await db.execute(
        select(CompanyContact)
        .where(CompanyContact.company_id == company_id)
        .order_by(CompanyContact.last_name.asc())
    )
    contacts = contacts_result.scalars().all()

    return templates.TemplateResponse(
        "company_detail.html",
        {
            "request": request,
            "company": company,
            "job_count": job_count,
            "ats_job_count": ats_job_count,
            "call_count": call_count,
            "call_total_duration": call_total_duration,
            "contact_count": contact_count,
            "contacts": contacts,
        }
    )


@router.get("/statistiken", response_class=HTMLResponse)
async def statistics_page(request: Request):
    """Statistiken-Seite."""
    return templates.TemplateResponse(
        "statistics.html",
        {"request": request}
    )


@router.get("/einstellungen", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Einstellungen-Seite."""
    return templates.TemplateResponse(
        "settings.html",
        {"request": request}
    )


# ============================================================================
# Partials (HTMX)
# ============================================================================


@router.get("/partials/job-list", response_class=HTMLResponse)
async def job_list_partial(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    search: Optional[str] = None,
    cities: Optional[str] = None,
    industry: Optional[str] = None,
    sort_by: str = "created_at",
    has_active_candidates: bool = False,
    db: AsyncSession = Depends(get_db)
):
    """Partial: Job-Liste fuer HTMX."""
    job_service = JobService(db)
    filter_service = FilterService(db)

    # Filter aufbauen
    filters = JobFilterParams(
        search=search if search else None,
        cities=cities.split(",") if cities else None,
        industries=[industry] if industry else None,
        has_active_candidates=has_active_candidates if has_active_candidates else False,
        sort_by=sort_by if sort_by else "created_at",
    )

    # Jobs laden
    result = await job_service.list_jobs(
        filters=filters,
        page=page,
        per_page=per_page,
    )

    # Prio-Staedte laden
    priority_cities = await filter_service.get_priority_cities()

    return templates.TemplateResponse(
        "partials/job_list.html",
        {
            "request": request,
            "jobs": result.items,
            "priority_cities": priority_cities,
            "now": datetime.now
        }
    )


@router.get("/partials/jobs-list", response_class=HTMLResponse)
async def jobs_list_partial(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    search: Optional[str] = None,
    cities: Optional[str] = None,
    industry: Optional[str] = None,
    company: Optional[str] = None,
    sort_by: str = "imported_at",
    sort_order: str = "desc",
    imported_days: Optional[int] = None,
    updated_days: Optional[int] = None,
    view: str = "cards",
    db: AsyncSession = Depends(get_db),
):
    """Partial: Jobs-Liste fuer neue /jobs Seite (HTMX)."""
    from app.schemas.filters import JobSortBy, SortOrder as SortOrderEnum

    job_service = JobService(db)
    filter_service = FilterService(db)

    # Sort-Enum aufloesen
    try:
        sort_by_enum = JobSortBy(sort_by)
    except ValueError:
        sort_by_enum = JobSortBy.IMPORTED_AT

    try:
        sort_order_enum = SortOrderEnum(sort_order)
    except ValueError:
        sort_order_enum = SortOrderEnum.DESC

    # Zu kurze Suchbegriffe ignorieren (SearchTerm Validator erfordert min. 2 Zeichen)
    safe_search = search.strip() if search else None
    if safe_search and len(safe_search) < 2:
        safe_search = None
    safe_company = company.strip() if company else None
    if safe_company and len(safe_company) < 2:
        safe_company = None
    # Stadt-Filter: Zu kurze Eingaben ignorieren
    safe_cities = None
    if cities:
        city_parts = [c.strip() for c in cities.split(",") if c.strip() and len(c.strip()) >= 2]
        safe_cities = city_parts if city_parts else None

    # Filter aufbauen
    filters = JobFilterParams(
        search=safe_search,
        cities=safe_cities,
        industries=[industry] if industry else None,
        company=safe_company,
        sort_by=sort_by_enum,
        sort_order=sort_order_enum,
        imported_days=imported_days,
        updated_days=updated_days,
    )

    # Jobs laden
    result = await job_service.list_jobs(
        filters=filters,
        page=page,
        per_page=per_page,
    )

    # Prio-Staedte laden
    priority_cities = await filter_service.get_priority_cities()

    return templates.TemplateResponse(
        "partials/jobs_list.html",
        {
            "request": request,
            "jobs": result.items,
            "total": result.total,
            "page": result.page,
            "per_page": result.per_page,
            "total_pages": result.pages,
            "priority_cities": priority_cities,
            "view": view,
            "search": search,
            "now": datetime.now,
        }
    )


@router.get("/partials/job-pagination", response_class=HTMLResponse)
async def job_pagination_partial(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db)
):
    """Partial: Job-Pagination fuer HTMX."""
    job_service = JobService(db)
    filters = JobFilterParams()
    result = await job_service.list_jobs(filters=filters, page=page, per_page=per_page)

    return templates.TemplateResponse(
        "components/pagination.html",
        {
            "request": request,
            "page": result.page,
            "total_pages": result.pages,
            "total_items": result.total,
            "per_page": result.per_page,
            "base_url": "/partials/job-list",
            "hx_target": "#job-list"
        }
    )


@router.get("/partials/filter-panel", response_class=HTMLResponse)
async def filter_panel_partial(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Partial: Filter-Panel fuer HTMX."""
    filter_service = FilterService(db)

    # Filter-Optionen laden
    cities = await filter_service.get_available_cities()
    skills = await filter_service.get_available_skills()
    industries = await filter_service.get_available_industries()

    return templates.TemplateResponse(
        "components/filter_panel.html",
        {
            "request": request,
            "filters": {},
            "options": {
                "cities": cities,
                "skills": skills,
                "industries": industries
            },
            "filter_url": "/partials/job-list",
            "target_id": "#job-list"
        }
    )


@router.get("/partials/statistics", response_class=HTMLResponse)
async def statistics_partial(
    request: Request,
    days: int = 30,
    db: AsyncSession = Depends(get_db)
):
    """Partial: Statistiken-Inhalt fuer HTMX."""
    statistics_service = StatisticsService(db)
    dashboard_stats = await statistics_service.get_dashboard_stats(days=days)

    # Konvertiere zu Dict fuer Template
    stats = {
        "jobs_active": dashboard_stats.jobs_active,
        "candidates_active": dashboard_stats.candidates_active,
        "candidates_total": dashboard_stats.candidates_total,
        "ai_checks_count": dashboard_stats.ai_checks_count,
        "ai_checks_cost_usd": dashboard_stats.ai_checks_cost_usd,
        "avg_ai_score": dashboard_stats.avg_ai_score,
        "matches_presented": dashboard_stats.matches_presented,
        "matches_placed": dashboard_stats.matches_placed,
        "top_filters": [
            {
                "filter_type": f.filter_type,
                "filter_value": f.filter_value,
                "usage_count": f.usage_count,
            }
            for f in dashboard_stats.top_filters
        ],
        "jobs_without_matches": dashboard_stats.jobs_without_matches,
        "candidates_without_address": dashboard_stats.candidates_without_address,
    }

    return templates.TemplateResponse(
        "partials/statistics_content.html",
        {
            "request": request,
            "stats": stats,
            "period": days
        }
    )


@router.get("/partials/priority-cities", response_class=HTMLResponse)
async def priority_cities_partial(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Partial: Prioritaets-Staedte fuer HTMX."""
    filter_service = FilterService(db)
    cities = await filter_service.get_priority_cities()

    return templates.TemplateResponse(
        "partials/priority_cities.html",
        {
            "request": request,
            "cities": cities
        }
    )


@router.get("/partials/filter-presets", response_class=HTMLResponse)
async def filter_presets_partial(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Partial: Filter-Presets fuer HTMX."""
    filter_service = FilterService(db)
    presets = await filter_service.get_filter_presets()

    return templates.TemplateResponse(
        "partials/filter_presets.html",
        {
            "request": request,
            "presets": presets
        }
    )


@router.get("/partials/candidates-list", response_class=HTMLResponse)
async def candidates_list_partial(
    request: Request,
    page: int = 1,
    per_page: int = 25,
    search: Optional[str] = None,
    position: Optional[str] = None,
    skills: Optional[str] = None,
    city: Optional[str] = None,
    category: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """Partial: Kandidaten-Liste fuer HTMX."""
    from app.schemas.filters import CandidateFilterParams
    from app.schemas.pagination import PaginationParams

    candidate_service = CandidateService(db)

    # Skills-String in Liste splitten (kommagetrennt)
    skills_list = None
    if skills:
        skills_list = [s.strip() for s in skills.split(',') if s.strip()]

    # Filter aufbauen - include_hidden=True damit ALLE Kandidaten findbar sind
    filters = CandidateFilterParams(
        name=search if search else None,
        position=position if position else None,
        skills=skills_list,
        city_search=city if city else None,
        hotlist_category=category if category else None,
        include_hidden=True,
        only_active=False,
    )

    pagination = PaginationParams(page=page, per_page=per_page)

    # Kandidaten laden
    result = await candidate_service.list_candidates(
        filters=filters,
        pagination=pagination,
    )

    return templates.TemplateResponse(
        "partials/candidates_list_page.html",
        {
            "request": request,
            "candidates": result.items,
            "total": result.total,
            "page": result.page,
            "per_page": result.per_page,
            "total_pages": result.pages,
            "search": search,
            "position": position,
            "skills": skills,
            "city": city,
            "category": category,
        }
    )


@router.get("/partials/companies-list", response_class=HTMLResponse)
async def companies_list_partial(
    request: Request,
    page: int = 1,
    per_page: int = 25,
    search: Optional[str] = None,
    city: Optional[str] = None,
    status: Optional[str] = None,
    sort_by: str = "created_at",
    db: AsyncSession = Depends(get_db),
):
    """Partial: Unternehmen-Liste fuer HTMX."""
    company_service = CompanyService(db)
    result = await company_service.list_companies(
        search=search,
        city=city,
        status=status,
        sort_by=sort_by,
        page=page,
        per_page=per_page,
    )

    return templates.TemplateResponse(
        "partials/companies_list.html",
        {
            "request": request,
            "companies": result["items"],
            "total": result["total"],
            "page": result["page"],
            "per_page": result["per_page"],
            "total_pages": result["pages"],
            "search": search,
        }
    )


@router.get("/partials/company-contacts/{company_id}", response_class=HTMLResponse)
async def company_contacts_partial(
    request: Request,
    company_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Kontakte eines Unternehmens fuer HTMX."""
    company_service = CompanyService(db)
    contacts = await company_service.list_contacts(company_id)

    return templates.TemplateResponse(
        "partials/company_contacts.html",
        {
            "request": request,
            "contacts": contacts,
            "company_id": str(company_id),
        }
    )


@router.get("/partials/company-correspondence/{company_id}", response_class=HTMLResponse)
async def company_correspondence_partial(
    request: Request,
    company_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Korrespondenz eines Unternehmens fuer HTMX."""
    company_service = CompanyService(db)
    correspondence = await company_service.list_correspondence(company_id)

    return templates.TemplateResponse(
        "partials/company_correspondence.html",
        {
            "request": request,
            "correspondence": correspondence,
            "company_id": str(company_id),
        }
    )


@router.get("/partials/company-jobs/{company_id}", response_class=HTMLResponse)
async def company_jobs_partial(
    request: Request,
    company_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Jobs eines Unternehmens fuer HTMX."""
    from app.models.job import Job
    from sqlalchemy import select

    result = await db.execute(
        select(Job)
        .where(Job.company_id == company_id, Job.deleted_at.is_(None))
        .order_by(Job.created_at.desc())
        .limit(50)
    )
    jobs = result.scalars().all()

    return templates.TemplateResponse(
        "partials/company_jobs.html",
        {
            "request": request,
            "jobs": jobs,
            "company_id": str(company_id),
        }
    )


@router.get("/partials/company-ats-jobs/{company_id}", response_class=HTMLResponse)
async def company_ats_jobs_partial(
    request: Request,
    company_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Partial: ATS-Stellen eines Unternehmens fuer HTMX."""
    from app.models.ats_job import ATSJob
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(ATSJob)
        .options(selectinload(ATSJob.pipeline_entries))
        .where(
            ATSJob.company_id == company_id,
            ATSJob.deleted_at.is_(None),  # Soft-deleted ausschliessen
        )
        .order_by(ATSJob.created_at.desc())
        .limit(50)
    )
    ats_jobs = result.scalars().all()

    return templates.TemplateResponse(
        "partials/company_ats_jobs.html",
        {
            "request": request,
            "ats_jobs": ats_jobs,
            "company_id": str(company_id),
        }
    )


@router.get("/partials/candidate-jobs/{candidate_id}", response_class=HTMLResponse)
async def candidate_matching_jobs_partial(
    request: Request,
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Passende Jobs fuer einen Kandidaten (HTMX)."""
    candidate_service = CandidateService(db)

    try:
        jobs, total = await candidate_service.get_jobs_for_candidate(
            candidate_id=candidate_id,
            page=1,
            per_page=20,
            sort_by="distance_km",
            sort_order="asc",
        )
    except Exception:
        jobs = []
        total = 0

    return templates.TemplateResponse(
        "partials/candidate_matching_jobs.html",
        {
            "request": request,
            "jobs": jobs,
            "total": total,
            "candidate_id": str(candidate_id),
        }
    )


@router.get("/partials/candidate-pipeline/{candidate_id}", response_class=HTMLResponse)
async def candidate_pipeline_partial(
    request: Request,
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Pipeline-Status eines Kandidaten bei allen Jobs (HTMX)."""
    from app.models.ats_pipeline import ATSPipelineEntry, PIPELINE_STAGE_LABELS
    from app.models.ats_job import ATSJob
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    # Hole alle Pipeline-Entries fuer diesen Kandidaten
    result = await db.execute(
        select(ATSPipelineEntry)
        .options(
            selectinload(ATSPipelineEntry.ats_job).selectinload(ATSJob.company)
        )
        .where(ATSPipelineEntry.candidate_id == candidate_id)
        .order_by(ATSPipelineEntry.updated_at.desc())
    )
    pipeline_entries = result.scalars().all()

    return templates.TemplateResponse(
        "partials/candidate_pipeline.html",
        {
            "request": request,
            "pipeline_entries": pipeline_entries,
            "stage_labels": PIPELINE_STAGE_LABELS,
            "candidate_id": str(candidate_id),
        }
    )


@router.get("/partials/quickadd-kandidat", response_class=HTMLResponse)
async def quickadd_kandidat_partial(request: Request):
    """Partial: Quick-Add Kandidat (CV Upload + Formular)."""
    return templates.TemplateResponse(
        "components/quickadd_kandidat.html",
        {"request": request}
    )


@router.get("/partials/quickadd-unternehmen", response_class=HTMLResponse)
async def quickadd_unternehmen_partial(request: Request):
    """Partial: Quick-Add Unternehmen."""
    return templates.TemplateResponse(
        "components/quickadd_unternehmen.html",
        {"request": request}
    )


@router.get("/partials/quickadd-job", response_class=HTMLResponse)
async def quickadd_job_partial(request: Request):
    """Partial: Quick-Add Job."""
    return templates.TemplateResponse(
        "components/quickadd_job.html",
        {"request": request}
    )


@router.get("/partials/quickadd-stelle", response_class=HTMLResponse)
async def quickadd_stelle_partial(request: Request):
    """Partial: Quick-Add Stelle."""
    return templates.TemplateResponse(
        "components/quickadd_stelle.html",
        {"request": request}
    )


@router.get("/partials/quickadd-aufgabe", response_class=HTMLResponse)
async def quickadd_aufgabe_partial(
    request: Request,
    company_id: Optional[UUID] = None,
    contact_id: Optional[UUID] = None,
    candidate_id: Optional[UUID] = None,
    ats_job_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Quick-Add Aufgabe (Slide-Over Formular)."""
    from app.models.company import Company
    from app.models.candidate import Candidate
    from app.models.ats_job import ATSJob

    prefill_company = await db.get(Company, company_id) if company_id else None
    prefill_contact = await db.get(CompanyContact, contact_id) if contact_id else None
    prefill_candidate = await db.get(Candidate, candidate_id) if candidate_id else None
    prefill_job = await db.get(ATSJob, ats_job_id) if ats_job_id else None

    return templates.TemplateResponse(
        "components/quickadd_aufgabe.html",
        {
            "request": request,
            "prefill_company": prefill_company,
            "prefill_contact": prefill_contact,
            "prefill_candidate": prefill_candidate,
            "prefill_job": prefill_job,
        }
    )


@router.get("/partials/import-dialog", response_class=HTMLResponse)
async def import_dialog_partial(request: Request):
    """Partial: Import-Dialog fuer Modal."""
    return templates.TemplateResponse(
        "components/import_dialog.html",
        {"request": request}
    )


@router.get("/partials/job-add-dialog", response_class=HTMLResponse)
async def job_add_dialog_partial(request: Request):
    """Partial: Job-Hinzufuegen-Dialog fuer Modal."""
    return templates.TemplateResponse(
        "components/job_add_dialog.html",
        {"request": request}
    )


@router.get("/api/health", response_class=HTMLResponse)
async def health_indicator_partial(request: Request):
    """Partial: Health-Indikator fuer Navigation."""
    return templates.TemplateResponse(
        "components/health_indicator.html",
        {
            "request": request,
            "status": "healthy"
        }
    )


@router.get("/api/alerts/active", response_class=HTMLResponse)
async def active_alerts_partial(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Partial: Aktive Alerts fuer Banner."""
    alert_service = AlertService(db)
    alerts = await alert_service.get_active_alerts(limit=5)

    return templates.TemplateResponse(
        "components/alert_banner.html",
        {
            "request": request,
            "alerts": alerts
        }
    )


# ============================================================================
# Admin Job Status Partials (HTMX)
# ============================================================================

@router.get("/api/admin/geocoding/status", response_class=HTMLResponse)
async def geocoding_status_partial(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Partial: Geocoding-Status fuer Admin-Seite."""
    from app.models.job_run import JobType
    from app.services.job_runner_service import JobRunnerService

    job_runner = JobRunnerService(db)
    status_data = await job_runner.get_status(JobType.GEOCODING)

    # Hole aktuellen oder letzten Job
    current_job = status_data.get("current_job")
    last_job = status_data.get("last_job")

    # Konvertiere zu Objekt-artigem Dict fuer Template
    status = None
    if current_job:
        status = type("JobStatus", (), current_job)()
    elif last_job:
        status = type("JobStatus", (), last_job)()

    return templates.TemplateResponse(
        "components/admin_job_row.html",
        {
            "request": request,
            "job_type": "geocoding",
            "label": "Geocoding",
            "description": "Koordinaten fuer Jobs und Kandidaten ermitteln",
            "status": status,
            "trigger_url": "/api/admin/geocoding/trigger",
        }
    )


@router.get("/api/admin/matching/status", response_class=HTMLResponse)
async def matching_status_partial(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Partial: Matching-Status fuer Admin-Seite."""
    from app.models.job_run import JobType
    from app.services.job_runner_service import JobRunnerService

    job_runner = JobRunnerService(db)
    status_data = await job_runner.get_status(JobType.MATCHING)

    current_job = status_data.get("current_job")
    last_job = status_data.get("last_job")

    status = None
    if current_job:
        status = type("JobStatus", (), current_job)()
    elif last_job:
        status = type("JobStatus", (), last_job)()

    return templates.TemplateResponse(
        "components/admin_job_row.html",
        {
            "request": request,
            "job_type": "matching",
            "label": "Matching",
            "description": "Kandidaten-Job-Matches berechnen",
            "status": status,
            "trigger_url": "/api/admin/matching/trigger",
        }
    )


@router.get("/api/admin/cleanup/status", response_class=HTMLResponse)
async def cleanup_status_partial(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Partial: Cleanup-Status fuer Admin-Seite."""
    from app.models.job_run import JobType
    from app.services.job_runner_service import JobRunnerService

    job_runner = JobRunnerService(db)
    status_data = await job_runner.get_status(JobType.CLEANUP)

    current_job = status_data.get("current_job")
    last_job = status_data.get("last_job")

    status = None
    if current_job:
        status = type("JobStatus", (), current_job)()
    elif last_job:
        status = type("JobStatus", (), last_job)()

    return templates.TemplateResponse(
        "components/admin_job_row.html",
        {
            "request": request,
            "job_type": "cleanup",
            "label": "Cleanup",
            "description": "Abgelaufene Jobs und verwaiste Matches bereinigen",
            "status": status,
            "trigger_url": "/api/admin/cleanup/trigger",
        }
    )


@router.get("/api/admin/r2-migration/status-html", response_class=HTMLResponse)
async def r2_migration_status_html(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Partial: R2-Migration-Status fuer Admin-Seite."""
    from sqlalchemy import select, func
    from app.models.candidate import Candidate
    from app.api.routes_admin import _r2_migration_status

    # DB-Zahlen abfragen
    in_r2_result = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.cv_stored_path.isnot(None),
            Candidate.deleted_at.is_(None),
        )
    )
    in_r2 = in_r2_result.scalar() or 0

    remaining_result = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.cv_url.isnot(None),
            Candidate.cv_url != "",
            Candidate.cv_stored_path.is_(None),
            Candidate.deleted_at.is_(None),
        )
    )
    remaining = remaining_result.scalar() or 0

    total_with_cv_result = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.cv_url.isnot(None),
            Candidate.deleted_at.is_(None),
        )
    )
    total_with_cv = total_with_cv_result.scalar() or 0

    percent = round((in_r2 / total_with_cv * 100), 1) if total_with_cv > 0 else 0
    is_running = _r2_migration_status.get("running", False)
    migrated = _r2_migration_status.get("migrated", 0)
    failed = _r2_migration_status.get("failed", 0)
    current = _r2_migration_status.get("current_candidate", "")

    # Status-Farbe
    if is_running:
        badge_color = "bg-blue-100 text-blue-700"
        badge_text = "Laeuft..."
    elif remaining == 0 and total_with_cv > 0:
        badge_color = "bg-green-100 text-green-700"
        badge_text = "Vollstaendig"
    else:
        badge_color = "bg-yellow-100 text-yellow-700"
        badge_text = f"{remaining} offen"

    # Button HTML
    if is_running:
        btn_html = (
            '<div class="flex flex-col gap-1">'
            '<button id="r2-stop-btn" class="inline-flex items-center rounded-md '
            'bg-red-50 px-3 py-2 text-xs font-medium text-red-700 ring-1 ring-inset '
            'ring-red-600/20 hover:bg-red-100 cursor-pointer">Stoppen</button>'
            '<button id="r2-reset-btn" class="inline-flex items-center rounded-md '
            'bg-gray-50 px-3 py-1.5 text-xs font-medium text-gray-500 ring-1 ring-inset '
            'ring-gray-300 hover:bg-gray-100 cursor-pointer">Reset</button>'
            '</div>'
        )
    elif remaining == 0:
        btn_html = (
            '<span class="inline-flex items-center rounded-md bg-green-50 px-3 py-2 '
            'text-xs font-medium text-green-700 ring-1 ring-inset ring-green-600/20">'
            '&#x2705; Fertig</span>'
        )
    else:
        btn_html = (
            '<button id="r2-start-btn" class="inline-flex items-center rounded-md '
            'bg-violet-50 px-3 py-2 text-xs font-medium text-violet-700 ring-1 ring-inset '
            'ring-violet-600/20 hover:bg-violet-100 cursor-pointer">Migration starten</button>'
        )

    # Running info
    running_info = ""
    if is_running:
        running_info = (
            f'<div class="mt-1 text-xs text-blue-600">'
            f'Aktuell: {current} | Migriert: {migrated} | Fehler: {failed}'
            f'</div>'
        )

    html = f"""
    <div class="flex items-center justify-between py-3 px-4 bg-gray-50 rounded-lg border border-gray-200">
        <div class="flex-1 min-w-0">
            <div class="flex items-center gap-3">
                <div class="flex-shrink-0">
                    <span class="text-xl">&#x2601;</span>
                </div>
                <div>
                    <h4 class="text-sm font-semibold text-gray-900">R2 CV-Migration</h4>
                    <p class="text-xs text-gray-500">CVs von externen URLs nach R2 Storage sichern</p>
                </div>
            </div>
            <div class="mt-2 flex items-center gap-4 text-xs text-gray-600">
                <span>&#x2705; In R2: <strong>{in_r2}</strong></span>
                <span>&#x26A0;&#xFE0F; Nur extern: <strong>{remaining}</strong></span>
                <span>Gesamt: <strong>{total_with_cv}</strong></span>
                <span>Fortschritt: <strong>{percent}%</strong></span>
            </div>
            {running_info}
            <div class="mt-2 w-full bg-gray-200 rounded-full h-2">
                <div class="bg-violet-500 h-2 rounded-full transition-all" style="width: {percent}%"></div>
            </div>
        </div>
        <div class="ml-4 flex-shrink-0">
            {btn_html}
        </div>
    </div>
    <script>
    (function() {{
        var wrapper = document.getElementById('admin-r2-migration');
        var startBtn = document.getElementById('r2-start-btn');
        var stopBtn = document.getElementById('r2-stop-btn');
        var resetBtn = document.getElementById('r2-reset-btn');
        var isRunning = {'true' if is_running else 'false'};

        function refreshStatus() {{
            if (wrapper) {{
                wrapper.setAttribute('hx-get', '/api/admin/r2-migration/status-html');
                wrapper.setAttribute('hx-trigger', 'load');
                wrapper.setAttribute('hx-swap', 'innerHTML');
                htmx.process(wrapper);
                htmx.trigger(wrapper, 'load');
            }}
        }}

        if (startBtn) {{
            startBtn.addEventListener('click', function() {{
                startBtn.textContent = 'Wird gestartet...';
                startBtn.disabled = true;
                fetch('/api/admin/migrate-all-cvs-to-r2?batch_size=20&max_candidates=5000', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}}
                }}).then(function() {{
                    setTimeout(refreshStatus, 1500);
                }});
            }});
        }}

        if (stopBtn) {{
            stopBtn.addEventListener('click', function() {{
                stopBtn.textContent = 'Wird gestoppt...';
                stopBtn.disabled = true;
                fetch('/api/admin/stop-r2-migration', {{
                    method: 'POST'
                }}).then(function() {{
                    setTimeout(refreshStatus, 1000);
                }});
            }});
        }}

        if (resetBtn) {{
            resetBtn.addEventListener('click', function() {{
                resetBtn.textContent = 'Wird zurueckgesetzt...';
                resetBtn.disabled = true;
                fetch('/api/admin/reset-r2-migration-status', {{
                    method: 'POST'
                }}).then(function() {{
                    setTimeout(refreshStatus, 500);
                }});
            }});
        }}

        if (isRunning) {{
            setInterval(refreshStatus, 5000);
        }}
    }})();
    </script>
    """

    return HTMLResponse(content=html)


# ============================================================================
# Admin Job Trigger Endpoints (HTML Response fuer HTMX)
# ============================================================================

@router.post("/api/admin/geocoding/trigger", response_class=HTMLResponse)
async def trigger_geocoding_html(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """Startet Geocoding und gibt HTML-Status zurueck."""
    from app.api.routes_admin import _run_geocoding

    job_runner = JobRunnerService(db)

    if await job_runner.is_running(JobType.GEOCODING):
        # Bereits laufend - zeige aktuellen Status
        return await geocoding_status_partial(request, db)

    job_run = await job_runner.start_job(JobType.GEOCODING, JobSource.MANUAL)
    background_tasks.add_task(_run_geocoding, db, job_run.id)

    # Gebe sofort den neuen Status zurueck
    return await geocoding_status_partial(request, db)


@router.post("/api/admin/matching/trigger", response_class=HTMLResponse)
async def trigger_matching_html(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """Startet Matching und gibt HTML-Status zurueck."""
    from app.api.routes_admin import _run_matching

    job_runner = JobRunnerService(db)

    if await job_runner.is_running(JobType.MATCHING):
        return await matching_status_partial(request, db)

    job_run = await job_runner.start_job(JobType.MATCHING, JobSource.MANUAL)
    background_tasks.add_task(_run_matching, db, job_run.id)

    return await matching_status_partial(request, db)


@router.post("/api/admin/cleanup/trigger", response_class=HTMLResponse)
async def trigger_cleanup_html(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """Startet Cleanup und gibt HTML-Status zurueck."""
    from app.api.routes_admin import _run_cleanup

    job_runner = JobRunnerService(db)

    if await job_runner.is_running(JobType.CLEANUP):
        return await cleanup_status_partial(request, db)

    job_run = await job_runner.start_job(JobType.CLEANUP, JobSource.MANUAL)
    background_tasks.add_task(_run_cleanup, db, job_run.id)

    return await cleanup_status_partial(request, db)


# ============================================================================
# Kontakt-Detailseite
# ============================================================================


@router.get("/kontakte/{contact_id}", response_class=HTMLResponse)
async def contact_detail_page(
    contact_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Kontakt-Detailseite."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(CompanyContact)
        .options(selectinload(CompanyContact.company))
        .where(CompanyContact.id == contact_id)
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Kontakt nicht gefunden")
    return templates.TemplateResponse("contact_detail.html", {
        "request": request,
        "contact": contact,
        "company": contact.company,
    })


# ============================================================================
# Kontakt & Unternehmen Partials (HTMX)
# ============================================================================


@router.get("/partials/contact/{contact_id}/call-notes", response_class=HTMLResponse)
async def contact_call_notes_partial(
    contact_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Anrufprotokolle eines Kontakts."""
    from app.models.ats_call_note import ATSCallNote

    result = await db.execute(
        select(ATSCallNote)
        .where(ATSCallNote.contact_id == contact_id)
        .order_by(ATSCallNote.called_at.desc())
    )
    call_notes = result.scalars().all()
    return templates.TemplateResponse("partials/contact_call_notes.html", {
        "request": request,
        "call_notes": call_notes,
        "contact_id": str(contact_id),
    })


@router.get("/partials/contact/{contact_id}/todos", response_class=HTMLResponse)
async def contact_todos_partial(
    contact_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Aufgaben eines Kontakts."""
    from app.models.ats_todo import ATSTodo
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(ATSTodo)
        .options(
            selectinload(ATSTodo.company),
            selectinload(ATSTodo.candidate),
            selectinload(ATSTodo.ats_job),
        )
        .where(ATSTodo.contact_id == contact_id)
        .order_by(ATSTodo.status.asc(), ATSTodo.priority.desc(), ATSTodo.due_date.asc().nullslast(), ATSTodo.created_at.desc())
    )
    todos = result.scalars().all()
    return templates.TemplateResponse("partials/contact_todos.html", {
        "request": request,
        "todos": todos,
        "contact_id": str(contact_id),
    })


@router.get("/partials/contact/{contact_id}/correspondence", response_class=HTMLResponse)
async def contact_correspondence_partial(
    contact_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Korrespondenz eines Kontakts."""
    from app.models.company_correspondence import CompanyCorrespondence

    result = await db.execute(
        select(CompanyCorrespondence)
        .where(CompanyCorrespondence.contact_id == contact_id)
        .order_by(CompanyCorrespondence.sent_at.desc())
    )
    correspondence = result.scalars().all()
    return templates.TemplateResponse("partials/contact_correspondence.html", {
        "request": request,
        "correspondence": correspondence,
        "contact_id": str(contact_id),
    })


@router.get("/partials/company/{company_id}/activities", response_class=HTMLResponse)
async def company_activities_partial(
    company_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: ALLE Aktivitaeten eines Unternehmens + aller Kontakte, chronologisch."""
    from app.models.ats_call_note import ATSCallNote
    from app.models.company_correspondence import CompanyCorrespondence
    from app.models.ats_todo import ATSTodo
    from app.models.company_contact import CompanyContact as CC
    from sqlalchemy.orm import selectinload

    # 1. Alle Contact-IDs dieses Unternehmens laden
    contact_result = await db.execute(
        select(CC.id).where(CC.company_id == company_id)
    )
    contact_ids = [row[0] for row in contact_result.all()]

    # 2. Anrufe laden (Firma + Kontakte)
    from sqlalchemy import or_
    call_conditions = [ATSCallNote.company_id == company_id]
    if contact_ids:
        call_conditions.append(ATSCallNote.contact_id.in_(contact_ids))
    call_result = await db.execute(
        select(ATSCallNote)
        .options(selectinload(ATSCallNote.contact))
        .where(or_(*call_conditions))
        .order_by(ATSCallNote.called_at.desc())
        .limit(100)
    )
    call_notes = call_result.scalars().all()

    # 3. E-Mails laden (Firma + Kontakte)
    corr_conditions = [CompanyCorrespondence.company_id == company_id]
    if contact_ids:
        corr_conditions.append(CompanyCorrespondence.contact_id.in_(contact_ids))
    corr_result = await db.execute(
        select(CompanyCorrespondence)
        .options(selectinload(CompanyCorrespondence.contact))
        .where(or_(*corr_conditions))
        .order_by(CompanyCorrespondence.sent_at.desc())
        .limit(100)
    )
    correspondence = corr_result.scalars().all()

    # 4. Aufgaben laden (Firma + Kontakte)
    todo_conditions = [ATSTodo.company_id == company_id]
    if contact_ids:
        todo_conditions.append(ATSTodo.contact_id.in_(contact_ids))
    todo_result = await db.execute(
        select(ATSTodo)
        .options(selectinload(ATSTodo.contact))
        .where(or_(*todo_conditions))
        .order_by(ATSTodo.created_at.desc())
        .limit(100)
    )
    todos = todo_result.scalars().all()

    # 5. Alles zusammenfuehren + sortieren
    activities = []

    # Call-Type Labels
    _call_type_labels = {
        "acquisition": "Akquise",
        "qualification": "Qualifizierung",
        "followup": "Nachfassen",
        "candidate_call": "Kandidatengespraech",
    }
    _todo_status_labels = {
        "open": "Offen",
        "in_progress": "In Bearbeitung",
        "done": "Erledigt",
        "cancelled": "Abgebrochen",
    }
    _todo_priority_labels = {
        "low": "Niedrig",
        "normal": "Normal",
        "high": "Hoch",
        "urgent": "Dringend",
    }

    for note in call_notes:
        contact_name = note.contact.full_name if note.contact else None
        activities.append({
            "type": "call",
            "title": note.summary or "Anruf",
            "body": note.raw_notes,
            "contact_name": contact_name,
            "date": note.called_at or note.created_at,
            "duration": note.duration_minutes,
            "call_type": _call_type_labels.get(note.call_type.value, note.call_type.value) if note.call_type else None,
            "status": None,
            "priority": None,
        })

    for corr in correspondence:
        contact_name = corr.contact.full_name if hasattr(corr, "contact") and corr.contact else None
        direction = "email_out" if corr.direction.value == "outbound" else "email_in"
        activities.append({
            "type": direction,
            "title": corr.subject or "E-Mail",
            "body": corr.body,
            "contact_name": contact_name,
            "date": corr.sent_at or corr.created_at,
            "duration": None,
            "call_type": None,
            "status": None,
            "priority": None,
        })

    for todo in todos:
        contact_name = todo.contact.full_name if hasattr(todo, "contact") and todo.contact else None
        activities.append({
            "type": "todo",
            "title": todo.title or "Aufgabe",
            "body": todo.description,
            "contact_name": contact_name,
            "date": todo.created_at,
            "duration": None,
            "call_type": None,
            "status": _todo_status_labels.get(todo.status.value, todo.status.value) if todo.status else None,
            "priority": _todo_priority_labels.get(todo.priority.value, todo.priority.value) if todo.priority else None,
        })

    # Sortieren: neueste zuerst
    activities.sort(key=lambda a: a["date"] or datetime.min, reverse=True)

    return templates.TemplateResponse("partials/company_activities.html", {
        "request": request,
        "activities": activities,
        "company_id": str(company_id),
    })


@router.get("/partials/company/{company_id}/call-notes", response_class=HTMLResponse)
async def company_call_notes_partial(
    company_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Anrufprotokolle eines Unternehmens."""
    from app.models.ats_call_note import ATSCallNote

    result = await db.execute(
        select(ATSCallNote)
        .where(ATSCallNote.company_id == company_id)
        .order_by(ATSCallNote.called_at.desc())
    )
    call_notes = result.scalars().all()
    return templates.TemplateResponse("partials/company_call_notes.html", {
        "request": request,
        "call_notes": call_notes,
        "company_id": str(company_id),
    })


@router.get("/partials/company/{company_id}/call-log", response_class=HTMLResponse)
async def company_call_log_partial(
    company_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Anruf-Protokoll (Call-Log) eines Unternehmens fuer Uebersicht."""
    from app.models.ats_call_note import ATSCallNote
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(ATSCallNote)
        .options(selectinload(ATSCallNote.contact))
        .where(ATSCallNote.company_id == company_id)
        .order_by(ATSCallNote.called_at.desc())
        .limit(20)
    )
    call_notes = result.scalars().all()
    return templates.TemplateResponse("partials/company_call_log.html", {
        "request": request,
        "call_notes": call_notes,
        "company_id": str(company_id),
    })


@router.get("/partials/company/{company_id}/todos", response_class=HTMLResponse)
async def company_todos_partial(
    company_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Aufgaben eines Unternehmens."""
    from app.models.ats_todo import ATSTodo
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(ATSTodo)
        .options(
            selectinload(ATSTodo.contact),
            selectinload(ATSTodo.candidate),
            selectinload(ATSTodo.ats_job),
        )
        .where(ATSTodo.company_id == company_id)
        .order_by(ATSTodo.status.asc(), ATSTodo.priority.desc(), ATSTodo.due_date.asc().nullslast(), ATSTodo.created_at.desc())
    )
    todos = result.scalars().all()
    return templates.TemplateResponse("partials/company_todos.html", {
        "request": request,
        "todos": todos,
        "company_id": str(company_id),
    })


@router.get("/partials/company/{company_id}/documents", response_class=HTMLResponse)
async def company_documents_partial(
    company_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Dokumente eines Unternehmens."""
    from app.models.company_document import CompanyDocument

    result = await db.execute(
        select(CompanyDocument)
        .where(CompanyDocument.company_id == company_id)
        .order_by(CompanyDocument.created_at.desc())
    )
    documents = result.scalars().all()
    return templates.TemplateResponse("partials/company_documents.html", {
        "request": request,
        "documents": documents,
        "company_id": str(company_id),
    })


@router.get("/partials/company/{company_id}/notes", response_class=HTMLResponse)
async def company_notes_partial(
    company_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Notizen-Verlauf eines Unternehmens (neueste zuerst)."""
    from app.models.company_note import CompanyNote
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(CompanyNote)
        .options(selectinload(CompanyNote.contact))
        .where(CompanyNote.company_id == company_id)
        .order_by(CompanyNote.created_at.desc())
    )
    notes = result.scalars().all()
    return templates.TemplateResponse("partials/company_notes.html", {
        "request": request,
        "notes": notes,
        "company_id": str(company_id),
    })


# ============================================================================
# Gespräche (Zwischenspeicher für unzugeordnete Anrufe)
# ============================================================================


@router.get("/gespraeche", response_class=HTMLResponse)
async def gespraeche_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Gespräche-Seite: Unzugeordnete transkribierte Anrufe."""
    from sqlalchemy import func as sqla_func
    from app.models.unassigned_call import UnassignedCall

    count = await db.scalar(
        select(sqla_func.count(UnassignedCall.id)).where(
            UnassignedCall.assigned == False  # noqa: E712
        )
    )

    return templates.TemplateResponse("gespraeche.html", {
        "request": request,
        "unassigned_count": count or 0,
    })


@router.get("/partials/gespraeche-list", response_class=HTMLResponse)
async def gespraeche_list_partial(
    request: Request,
    db: AsyncSession = Depends(get_db),
    search: str = "",
):
    """Partial: Liste der unzugeordneten Gespräche."""
    from app.models.unassigned_call import UnassignedCall

    query = select(UnassignedCall).where(
        UnassignedCall.assigned == False  # noqa: E712
    )

    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            UnassignedCall.phone_number.ilike(search_pattern)
            | UnassignedCall.call_summary.ilike(search_pattern)
            | UnassignedCall.recording_topic.ilike(search_pattern)
        )

    query = query.order_by(UnassignedCall.created_at.desc()).limit(100)
    result = await db.execute(query)
    calls = result.scalars().all()

    return templates.TemplateResponse("partials/gespraeche_list.html", {
        "request": request,
        "calls": calls,
        "search": search,
    })


@router.get("/partials/gespraech-assign/{call_id}", response_class=HTMLResponse)
async def gespraech_assign_modal(
    call_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Modal fuer Gespräch-Zuordnung."""
    from app.models.unassigned_call import UnassignedCall

    result = await db.execute(
        select(UnassignedCall).where(UnassignedCall.id == call_id)
    )
    call = result.scalar_one_or_none()
    if not call:
        raise HTTPException(status_code=404, detail="Anruf nicht gefunden")

    return templates.TemplateResponse("partials/gespraech_assign_modal.html", {
        "request": request,
        "call": call,
    })


@router.delete("/api/gespraeche/{call_id}")
async def gespraeche_delete(
    call_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Frontend: Löscht einen unzugeordneten Anruf (Hard Delete)."""
    from app.models.unassigned_call import UnassignedCall

    result = await db.execute(
        select(UnassignedCall).where(UnassignedCall.id == call_id)
    )
    call = result.scalar_one_or_none()
    if not call:
        raise HTTPException(status_code=404, detail="Anruf nicht gefunden")

    await db.delete(call)
    await db.commit()

    # Return empty HTML (Card wird via hx-swap entfernt)
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content="", status_code=200)


@router.post("/api/gespraeche/{call_id}/assign")
async def gespraeche_assign(
    call_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Frontend: Ordnet einen unzugeordneten Anruf manuell zu."""
    from datetime import datetime, timezone
    from app.models.unassigned_call import UnassignedCall
    from app.models.ats_activity import ATSActivity, ActivityType
    from app.models.ats_call_note import ATSCallNote, CallDirection, CallType
    from app.models.candidate import Candidate
    from fastapi.responses import JSONResponse

    body = await request.json()
    entity_type = body.get("entity_type", "candidate")
    entity_id = body.get("entity_id")

    if not entity_id:
        raise HTTPException(status_code=400, detail="entity_id fehlt")

    # Staged Call laden
    result = await db.execute(
        select(UnassignedCall).where(UnassignedCall.id == call_id)
    )
    call = result.scalar_one_or_none()
    if not call:
        raise HTTPException(status_code=404, detail="Anruf nicht gefunden")
    if call.assigned:
        raise HTTPException(status_code=400, detail="Anruf wurde bereits zugeordnet")

    now = datetime.now(timezone.utc)
    actions = []

    if entity_type == "candidate":
        # Kandidat laden + Felder updaten
        cand_result = await db.execute(
            select(Candidate).where(Candidate.id == entity_id)
        )
        candidate = cand_result.scalar_one_or_none()
        if not candidate:
            raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

        # Call-Daten auf Kandidaten schreiben
        if call.transcript:
            candidate.call_transcript = call.transcript
        if call.call_summary:
            candidate.call_summary = call.call_summary
        candidate.call_date = call.call_date or now
        # call_type aus extracted_data uebernehmen (von GPT klassifiziert)
        ext_call_type = (call.extracted_data or {}).get("call_type", "kurzer_call")
        candidate.call_type = ext_call_type
        candidate.last_contact = now

        # Qualifizierungsfelder aus extracted_data
        ext = call.extracted_data or {}
        field_mappings = {
            "desired_positions": "desired_positions",
            "key_activities": "key_activities",
            "home_office_days": "home_office_days",
            "commute_max": "commute_max",
            "commute_transport": "commute_transport",
            "erp_main": "erp_main",
            "employment_type": "employment_type",
            "part_time_hours": "part_time_hours",
            "preferred_industries": "preferred_industries",
            "avoided_industries": "avoided_industries",
            "salary": "salary",
            "notice_period": "notice_period",
        }
        fields_updated = []
        for src_key, dest_key in field_mappings.items():
            val = ext.get(src_key)
            if val is not None and val != "" and val != []:
                if isinstance(val, list):
                    val = ", ".join(str(v) for v in val)
                # Alle Werte zu String konvertieren (GPT liefert z.B. Integer 3 statt "3")
                val = str(val)
                if hasattr(candidate, dest_key):
                    setattr(candidate, dest_key, val)
                    fields_updated.append(dest_key)

        willingness = ext.get("willingness_to_change")
        if willingness in ("ja", "nein", "unbekannt", "unklar"):
            candidate.willingness_to_change = "unbekannt" if willingness == "unklar" else willingness
            fields_updated.append("willingness_to_change")

        actions.append(f"Kandidat aktualisiert: {candidate.first_name} {candidate.last_name}")
        actions.append(f"Felder: {fields_updated}")

        # CallNote erstellen
        direction_val = CallDirection.INBOUND if call.direction == "inbound" else CallDirection.OUTBOUND
        call_note = ATSCallNote(
            candidate_id=candidate.id,
            call_type=CallType.CANDIDATE_CALL,
            direction=direction_val,
            summary=call.call_summary or "KI-transkribiertes Gespräch",
            raw_notes=call.transcript[:5000] if call.transcript else None,
            duration_minutes=(call.duration_seconds // 60) if call.duration_seconds else None,
            called_at=call.call_date or now,
        )
        db.add(call_note)

        # Activity loggen
        activity = ATSActivity(
            activity_type=ActivityType.CALL_LOGGED,
            description=f"Anruf manuell zugeordnet: {call.call_summary[:100] if call.call_summary else 'Transkribiertes Gespräch'}",
            candidate_id=candidate.id,
            metadata_json={"source": "manual_assign", "fields_updated": fields_updated},
        )
        db.add(activity)

    # Staging-Record als zugeordnet markieren
    call.assigned = True
    call.assigned_to_type = entity_type
    call.assigned_to_id = entity_id
    call.assigned_at = now

    await db.commit()

    # Profil-PDF NUR bei Qualifizierungsgespraech generieren
    pdf_status = None
    if entity_type == "candidate" and (call.extracted_data or {}).get("call_type", "").lower() == "qualifizierung":
        try:
            from datetime import timezone as tz
            from app.services.profile_pdf_service import ProfilePdfService
            from app.services.r2_storage_service import R2StorageService

            pdf_service = ProfilePdfService(db)
            pdf_bytes = await pdf_service.generate_profile_pdf(UUID(entity_id))

            if pdf_bytes:
                r2 = R2StorageService()
                if r2.is_available:
                    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{candidate.first_name}_{candidate.last_name}")
                    r2_key = f"profiles/{str(entity_id)[:8]}_{safe_name}_profil.pdf"
                    r2.upload_file(key=r2_key, file_content=pdf_bytes, content_type="application/pdf")

                    # R2-Key am Kandidaten speichern
                    candidate.profile_pdf_r2_key = r2_key
                    candidate.profile_pdf_generated_at = datetime.now(tz.utc)
                    await db.commit()

                    pdf_status = "generated_and_uploaded"
                    actions.append(f"pdf: {pdf_status} ({len(pdf_bytes)} Bytes)")
                    logger.info(f"Profil-PDF generiert + R2 + DB: {r2_key} fuer {candidate.first_name} {candidate.last_name}")
                else:
                    pdf_status = "generated_no_r2"
                    actions.append(f"pdf: {pdf_status}")
        except Exception as e:
            pdf_status = f"error"
            actions.append(f"pdf: error ({str(e)[:100]})")
            logger.error(f"Profil-PDF Generierung fehlgeschlagen bei manueller Zuordnung: {e}")

    return JSONResponse(content={
        "success": True,
        "message": f"Anruf erfolgreich zugeordnet",
        "actions": actions,
        "pdf_status": pdf_status,
    })


@router.post("/api/gespraeche/{call_id}/create-job")
async def gespraeche_create_job(
    call_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Frontend: Erstellt einen neuen ATSJob aus Job-Quali Staging-Daten."""
    from app.models.unassigned_call import UnassignedCall
    from app.models.ats_job import ATSJob
    from fastapi.responses import JSONResponse

    result = await db.execute(
        select(UnassignedCall).where(UnassignedCall.id == call_id)
    )
    call = result.scalar_one_or_none()
    if not call:
        raise HTTPException(status_code=404, detail="Anruf nicht gefunden")
    if call.call_subtype != "job_quali" or not call.extracted_job_data:
        raise HTTPException(status_code=400, detail="Kein Job-Quali-Staging vorhanden")

    jd = call.extracted_job_data

    job = ATSJob(
        title=jd.get("title", "Neue Stelle (aus Gespräch)"),
        company_id=call.company_id,
        contact_id=call.contact_id,
        description=jd.get("description"),
        requirements=jd.get("requirements"),
        location_city=jd.get("location"),
        salary_min=jd.get("salary_min"),
        salary_max=jd.get("salary_max"),
        employment_type=jd.get("employment_type"),
        source="job_quali_call",
        team_size=jd.get("team_size"),
        erp_system=jd.get("erp_system"),
        home_office_days=jd.get("home_office_days"),
        flextime=jd.get("flextime"),
        core_hours=jd.get("core_hours"),
        vacation_days=jd.get("vacation_days"),
        overtime_handling=jd.get("overtime_handling"),
        open_office=jd.get("open_office"),
        english_requirements=jd.get("english_requirements"),
        hiring_process_steps=jd.get("hiring_process_steps"),
        feedback_timeline=jd.get("feedback_timeline"),
        digitalization_level=jd.get("digitalization_level"),
        older_candidates_ok=jd.get("older_candidates_ok"),
        desired_start_date=jd.get("desired_start_date"),
        interviews_started=jd.get("interviews_started"),
        ideal_candidate_description=jd.get("ideal_candidate_description"),
        candidate_tasks=jd.get("candidate_tasks"),
        multiple_entities=jd.get("multiple_entities"),
        task_distribution=jd.get("task_distribution"),
        source_call_note_id=call.call_note_id,
    )
    db.add(job)

    # Staging als erledigt markieren
    call.assigned = True
    call.assigned_to_type = "ats_job"
    from datetime import datetime, timezone
    call.assigned_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(job)

    return JSONResponse(content={
        "success": True,
        "job_id": str(job.id),
        "job_title": job.title,
        "message": f"Stelle '{job.title}' erfolgreich angelegt",
    })


@router.post("/api/gespraeche/{call_id}/assign-to-job/{job_id}")
async def gespraeche_assign_to_job(
    call_id: UUID,
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Frontend: Merged Job-Quali-Daten in einen bestehenden ATSJob."""
    from app.models.unassigned_call import UnassignedCall
    from app.models.ats_job import ATSJob
    from fastapi.responses import JSONResponse

    # Staging laden
    result = await db.execute(
        select(UnassignedCall).where(UnassignedCall.id == call_id)
    )
    call = result.scalar_one_or_none()
    if not call:
        raise HTTPException(status_code=404, detail="Anruf nicht gefunden")
    if call.call_subtype != "job_quali" or not call.extracted_job_data:
        raise HTTPException(status_code=400, detail="Kein Job-Quali-Staging vorhanden")

    # ATSJob laden
    result = await db.execute(
        select(ATSJob).where(ATSJob.id == job_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Stelle nicht gefunden")

    jd = call.extracted_job_data
    fields_updated = []

    # Nur leere Felder befuellen (bestehende Werte nicht ueberschreiben)
    merge_fields = [
        ("description", "description"), ("requirements", "requirements"),
        ("location_city", "location"), ("salary_min", "salary_min"),
        ("salary_max", "salary_max"), ("employment_type", "employment_type"),
        ("team_size", "team_size"), ("erp_system", "erp_system"),
        ("home_office_days", "home_office_days"), ("flextime", "flextime"),
        ("core_hours", "core_hours"), ("vacation_days", "vacation_days"),
        ("overtime_handling", "overtime_handling"), ("open_office", "open_office"),
        ("english_requirements", "english_requirements"),
        ("hiring_process_steps", "hiring_process_steps"),
        ("feedback_timeline", "feedback_timeline"),
        ("digitalization_level", "digitalization_level"),
        ("older_candidates_ok", "older_candidates_ok"),
        ("desired_start_date", "desired_start_date"),
        ("interviews_started", "interviews_started"),
        ("ideal_candidate_description", "ideal_candidate_description"),
        ("candidate_tasks", "candidate_tasks"),
        ("multiple_entities", "multiple_entities"),
        ("task_distribution", "task_distribution"),
    ]

    for job_field, jd_key in merge_fields:
        new_val = jd.get(jd_key)
        if new_val is not None and getattr(job, job_field) is None:
            setattr(job, job_field, new_val)
            fields_updated.append(job_field)

    # source_call_note_id immer setzen
    if call.call_note_id and not job.source_call_note_id:
        job.source_call_note_id = call.call_note_id
        fields_updated.append("source_call_note_id")

    # Staging als erledigt markieren
    call.assigned = True
    call.assigned_to_type = "ats_job"
    call.assigned_to_id = job_id
    from datetime import datetime, timezone
    call.assigned_at = datetime.now(timezone.utc)

    await db.commit()

    return JSONResponse(content={
        "success": True,
        "job_id": str(job.id),
        "job_title": job.title,
        "fields_updated": fields_updated,
        "message": f"Daten in '{job.title}' übernommen ({len(fields_updated)} Felder)",
    })


# ============================================================================
# Kandidaten-Notizen Partials
# ============================================================================

@router.get("/partials/candidate/{candidate_id}/notes", response_class=HTMLResponse)
async def candidate_notes_partial(
    candidate_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Notizen-Verlauf eines Kandidaten (neueste zuerst)."""
    from app.models.candidate_note import CandidateNote

    result = await db.execute(
        select(CandidateNote)
        .where(CandidateNote.candidate_id == candidate_id)
        .order_by(CandidateNote.note_date.desc())
    )
    notes = result.scalars().all()
    return templates.TemplateResponse("partials/candidate_notes.html", {
        "request": request,
        "notes": notes,
        "candidate_id": str(candidate_id),
    })


# ============================================================================
# Email-Dashboard
# ============================================================================

@router.get("/emails", response_class=HTMLResponse)
async def emails_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Email-Dashboard: Alle Emails auf einen Blick."""
    from app.services.email_service import EmailService

    email_service = EmailService(db)
    stats = await email_service.get_email_stats()

    return templates.TemplateResponse("emails.html", {
        "request": request,
        "stats": stats,
    })


@router.get("/partials/emails-action-required", response_class=HTMLResponse)
async def emails_action_required_partial(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Fehlgeschlagene Emails (Sofort erledigen)."""
    from app.services.email_service import EmailService

    email_service = EmailService(db)
    failed = await email_service.list_drafts(status="failed", limit=50)

    return templates.TemplateResponse("partials/emails_action_required.html", {
        "request": request,
        "emails": failed,
    })


@router.get("/partials/emails-pending", response_class=HTMLResponse)
async def emails_pending_partial(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Offene Entwuerfe."""
    from app.services.email_service import EmailService

    email_service = EmailService(db)
    drafts = await email_service.list_drafts(status="draft", limit=50)

    return templates.TemplateResponse("partials/emails_pending.html", {
        "request": request,
        "emails": drafts,
    })


@router.get("/partials/emails-sent-today", response_class=HTMLResponse)
async def emails_sent_today_partial(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Heute gesendete Emails."""
    from datetime import datetime as dt, timezone as tz
    from app.services.email_service import EmailService

    email_service = EmailService(db)
    today_start = dt.now(tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    sent = await email_service.list_drafts_by_date(
        status="sent", since=today_start, limit=50
    )

    return templates.TemplateResponse("partials/emails_sent_today.html", {
        "request": request,
        "emails": sent,
    })


@router.get("/partials/emails-history", response_class=HTMLResponse)
async def emails_history_partial(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Partial: Letzte 7 Tage Email-Verlauf."""
    from datetime import datetime as dt, timezone as tz, timedelta
    from app.services.email_service import EmailService

    email_service = EmailService(db)
    seven_days_ago = dt.now(tz.utc) - timedelta(days=7)
    today_start = dt.now(tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    history = await email_service.list_drafts_by_date(
        status="sent", since=seven_days_ago, until=today_start, limit=100
    )

    return templates.TemplateResponse("partials/emails_history.html", {
        "request": request,
        "emails": history,
    })
