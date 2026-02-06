"""Page Routes - HTML-Seiten fuer das Frontend."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job_run import JobSource, JobType
from app.services.job_runner_service import JobRunnerService
from app.schemas.filters import JobFilterParams
from app.services.job_service import JobService
from app.services.candidate_service import CandidateService
from app.services.company_service import CompanyService
from app.services.filter_service import FilterService
from app.services.statistics_service import StatisticsService
from app.services.alert_service import AlertService

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

    return templates.TemplateResponse(
        "candidate_detail.html",
        {
            "request": request,
            "candidate": candidate
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

    return templates.TemplateResponse(
        "company_detail.html",
        {
            "request": request,
            "company": company,
            "job_count": job_count,
            "ats_job_count": ats_job_count,
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

    # Filter aufbauen
    filters = JobFilterParams(
        search=safe_search,
        cities=cities.split(",") if cities else None,
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


@router.get("/api/admin/crm-sync/status", response_class=HTMLResponse)
async def crm_sync_status_partial(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Partial: CRM-Sync-Status fuer Admin-Seite."""
    from app.models.job_run import JobType
    from app.services.job_runner_service import JobRunnerService

    job_runner = JobRunnerService(db)
    status_data = await job_runner.get_status(JobType.CRM_SYNC)

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
            "job_type": "crm-sync",
            "label": "CRM-Sync",
            "description": "Kandidaten aus Recruit CRM synchronisieren",
            "status": status,
            "trigger_url": "/api/admin/crm-sync/trigger",
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


@router.post("/api/admin/crm-sync/trigger", response_class=HTMLResponse)
async def trigger_crm_sync_html(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """Startet CRM-Sync und gibt HTML-Status zurueck."""
    from app.api.routes_admin import _run_crm_sync

    job_runner = JobRunnerService(db)

    if await job_runner.is_running(JobType.CRM_SYNC):
        return await crm_sync_status_partial(request, db)

    job_run = await job_runner.start_job(JobType.CRM_SYNC, JobSource.MANUAL)
    # full_sync=True um ALLE Kandidaten zu holen (nicht nur Änderungen)
    # parse_cvs=False - CV-Parsing erstmal deaktiviert
    background_tasks.add_task(_run_crm_sync, db, job_run.id, True, False)

    return await crm_sync_status_partial(request, db)


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
