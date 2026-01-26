"""Page Routes - HTML-Seiten fuer das Frontend."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.job_service import JobService
from app.services.candidate_service import CandidateService
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
    filters = {}
    if search:
        filters["search"] = search
    if cities:
        filters["cities"] = cities.split(",")
    if industry:
        filters["industry"] = industry
    if has_active_candidates:
        filters["has_active_candidates"] = True

    # Jobs laden
    result = await job_service.list_jobs(
        filters=filters,
        page=page,
        per_page=per_page,
        sort_by=sort_by
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


@router.get("/partials/job-pagination", response_class=HTMLResponse)
async def job_pagination_partial(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db)
):
    """Partial: Job-Pagination fuer HTMX."""
    job_service = JobService(db)
    result = await job_service.list_jobs(page=page, per_page=per_page)

    return templates.TemplateResponse(
        "components/pagination.html",
        {
            "request": request,
            "page": result.page,
            "total_pages": result.total_pages,
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


@router.get("/partials/import-dialog", response_class=HTMLResponse)
async def import_dialog_partial(request: Request):
    """Partial: Import-Dialog fuer Modal."""
    return templates.TemplateResponse(
        "components/import_dialog.html",
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
