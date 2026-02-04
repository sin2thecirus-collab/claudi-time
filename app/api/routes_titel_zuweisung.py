"""Titel-Zuweisung Routes — Manuelle Jobtitel-Zuweisung fuer Kandidaten.

Hauptseite: /titel-zuweisung
- Tabelle aller Kandidaten mit Filtern (Stadt, Status)
- Modal zum CV-Lesen und Titel-Zuweisen
- Lern-Integration: Jede Zuweisung → mt_training_data
- KETTENREAKTION: Aenderungen propagieren zu category, matches, embedding
"""

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select, case, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.candidate import Candidate
from app.models.match import Match
from app.services.categorization_service import CategorizationService
from app.services.mt_learning_service import COMMON_JOB_TITLES, MTLearningService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Titel-Zuweisung"])
templates = Jinja2Templates(directory="app/templates")


# ════════════════════════════════════════════════════════════════
# HAUPTSEITE: /titel-zuweisung
# ════════════════════════════════════════════════════════════════

@router.get("/titel-zuweisung", response_class=HTMLResponse)
async def titel_zuweisung_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Titel-Zuweisung Hauptseite."""
    # Statistiken laden
    total_q = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.deleted_at.is_(None)
        )
    )
    total_candidates = total_q.scalar() or 0

    # Bereits zugewiesen
    assigned_q = await db.execute(
        select(func.count(Candidate.id)).where(
            and_(
                Candidate.deleted_at.is_(None),
                Candidate.manual_job_titles.isnot(None),
            )
        )
    )
    assigned_count = assigned_q.scalar() or 0

    # Noch offen
    open_count = total_candidates - assigned_count

    # Staedte mit Kandidaten-Counts
    cities_q = await db.execute(
        select(
            func.coalesce(Candidate.hotlist_city, Candidate.city, "Unbekannt").label("city_name"),
            func.count(Candidate.id).label("cnt"),
        )
        .where(Candidate.deleted_at.is_(None))
        .group_by("city_name")
        .order_by(func.count(Candidate.id).desc())
        .limit(50)
    )
    cities = [{"name": row.city_name, "count": row.cnt} for row in cities_q.all()]

    # Training-Stats
    learning_service = MTLearningService(db)
    training_stats = await learning_service.get_training_stats()

    return templates.TemplateResponse(
        "titel_zuweisung.html",
        {
            "request": request,
            "total_candidates": total_candidates,
            "assigned_count": assigned_count,
            "open_count": open_count,
            "cities": cities,
            "common_job_titles": COMMON_JOB_TITLES,
            "training_stats": training_stats,
        },
    )


# ════════════════════════════════════════════════════════════════
# KANDIDATEN-LISTE (HTMX Partial)
# ════════════════════════════════════════════════════════════════

@router.get("/partials/titel-zuweisung/kandidaten", response_class=HTMLResponse)
async def titel_kandidaten_liste(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    city: str = Query(default=""),
    status: str = Query(default="alle"),  # "alle", "offen", "zugewiesen"
    search: str = Query(default=""),
    has_cv: str = Query(default=""),  # "ja", "nein", "" (alle)
    titel: str = Query(default=""),  # Filter nach zugewiesenem Titel
    min_rating: str = Query(default=""),  # Minimum-Bewertung (1-5)
    db: AsyncSession = Depends(get_db),
):
    """HTMX-Partial: Kandidaten-Tabelle fuer Titel-Zuweisung."""
    query = select(Candidate).where(Candidate.deleted_at.is_(None))

    # Filter: Stadt
    if city:
        query = query.where(
            or_(
                Candidate.hotlist_city == city,
                Candidate.city == city,
            )
        )

    # Filter: Status (offen/zugewiesen)
    if status == "offen":
        query = query.where(
            or_(
                Candidate.manual_job_titles.is_(None),
                func.array_length(Candidate.manual_job_titles, 1).is_(None),
            )
        )
    elif status == "zugewiesen":
        query = query.where(
            and_(
                Candidate.manual_job_titles.isnot(None),
                func.array_length(Candidate.manual_job_titles, 1) > 0,
            )
        )

    # Filter: CV vorhanden (cv_url, cv_stored_path ODER cv_text)
    if has_cv == "ja":
        query = query.where(
            or_(
                Candidate.cv_url.isnot(None),
                Candidate.cv_stored_path.isnot(None),
                and_(
                    Candidate.cv_text.isnot(None),
                    Candidate.cv_text != "",
                ),
            )
        )
    elif has_cv == "nein":
        query = query.where(
            and_(
                Candidate.cv_url.is_(None),
                Candidate.cv_stored_path.is_(None),
                or_(
                    Candidate.cv_text.is_(None),
                    Candidate.cv_text == "",
                ),
            )
        )

    # Filter: Nach Titel (manuelle Titel, hotlist_job_title, current_position)
    if titel:
        titel_search = f"%{titel}%"
        query = query.where(
            or_(
                Candidate.manual_job_titles.any(titel),
                Candidate.hotlist_job_title.ilike(titel_search),
                Candidate.current_position.ilike(titel_search),
            )
        )

    # Filter: Mindest-Bewertung
    if min_rating:
        query = query.where(
            Candidate.rating >= int(min_rating)
        )

    # Filter: Suche (Name oder Position)
    if search:
        search_term = f"%{search}%"
        query = query.where(
            or_(
                Candidate.first_name.ilike(search_term),
                Candidate.last_name.ilike(search_term),
                Candidate.current_position.ilike(search_term),
                Candidate.current_company.ilike(search_term),
            )
        )

    # Sortierung: Offene zuerst, dann nach Name
    query = query.order_by(
        case(
            (Candidate.manual_job_titles.is_(None), 0),
            else_=1,
        ),
        Candidate.last_name.asc(),
        Candidate.first_name.asc(),
    )

    # Gesamt-Count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total_count = total_result.scalar() or 0

    # Pagination
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    result = await db.execute(query)
    candidates = result.scalars().all()

    return templates.TemplateResponse(
        "partials/titel_kandidaten_liste.html",
        {
            "request": request,
            "candidates": candidates,
            "page": page,
            "per_page": per_page,
            "total_count": total_count,
            "total_pages": total_pages,
            "city": city,
            "status": status,
            "search": search,
            "has_cv": has_cv,
            "titel": titel,
            "min_rating": min_rating,
        },
    )


# ════════════════════════════════════════════════════════════════
# KANDIDAT-MODAL (HTMX Partial)
# ════════════════════════════════════════════════════════════════

@router.get("/partials/titel-zuweisung/kandidat/{candidate_id}", response_class=HTMLResponse)
async def titel_kandidat_modal(
    request: Request,
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """HTMX-Partial: Modal mit CV-Details und Titel-Zuweisung."""
    result = await db.execute(
        select(Candidate).where(Candidate.id == candidate_id)
    )
    candidate = result.scalar_one_or_none()

    if not candidate:
        raise HTTPException(404, "Kandidat nicht gefunden")

    # MT-Vorschlag generieren
    learning_service = MTLearningService(db)
    suggestion = await learning_service.get_suggestion_for_candidate(candidate)

    # CV-Daten vorbereiten (sicher als Listen)
    def _ensure_list(val):
        if isinstance(val, list):
            return val
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []
        return []

    work_history = _ensure_list(candidate.work_history)
    education = _ensure_list(candidate.education)
    further_education = _ensure_list(candidate.further_education)
    languages = _ensure_list(candidate.languages)
    it_skills = list(candidate.it_skills) if candidate.it_skills else []
    skills = list(candidate.skills) if candidate.skills else []

    return templates.TemplateResponse(
        "partials/titel_kandidat_modal.html",
        {
            "request": request,
            "candidate": candidate,
            "suggestion": suggestion,
            "common_job_titles": COMMON_JOB_TITLES,
            "work_history": work_history,
            "education": education,
            "further_education": further_education,
            "languages": languages,
            "it_skills": it_skills,
            "skills": skills,
        },
    )


# ════════════════════════════════════════════════════════════════
# TITEL SPEICHERN (API)
# ════════════════════════════════════════════════════════════════

@router.post("/api/titel-zuweisung/save/{candidate_id}")
async def save_titel_zuweisung(
    request: Request,
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Speichert die manuelle Titel-Zuweisung fuer einen Kandidaten.

    Body: { "titles": ["Finanzbuchhalter/in", "Kreditorenbuchhalter/in"], "rating": 4 }
    """
    body = await request.json()
    titles = body.get("titles", [])
    rating = body.get("rating")
    first_name = body.get("first_name")
    last_name = body.get("last_name")

    if not titles:
        raise HTTPException(400, "Mindestens ein Titel erforderlich")

    # Rating validieren (1-5 oder null)
    if rating is not None:
        rating = int(rating)
        if rating < 1 or rating > 10:
            raise HTTPException(400, "Rating muss zwischen 1 und 10 liegen")

    # Kandidat laden
    result = await db.execute(
        select(Candidate).where(Candidate.id == candidate_id)
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(404, "Kandidat nicht gefunden")

    # Vorherige Vorschlaege merken (fuer Training)
    learning_service = MTLearningService(db)
    suggestion = await learning_service.get_suggestion_for_candidate(candidate)
    predicted_titles = suggestion.get("suggested_titles") if suggestion.get("source") != "none" else None

    # ── Vorherige Werte merken (fuer Kettenreaktion) ──
    old_position = candidate.current_position
    old_category = candidate.hotlist_category
    name_changed = False

    # 0. Namen aktualisieren (wenn geaendert)
    if first_name and first_name.strip():
        if candidate.first_name != first_name.strip():
            candidate.first_name = first_name.strip()
            name_changed = True
    if last_name and last_name.strip():
        if candidate.last_name != last_name.strip():
            candidate.last_name = last_name.strip()
            name_changed = True

    # 1. manual_job_titles setzen
    candidate.manual_job_titles = titles
    candidate.manual_job_titles_set_at = datetime.now(timezone.utc)

    # 2. Rating speichern (wenn vorhanden)
    if rating is not None:
        candidate.rating = rating
        candidate.rating_set_at = datetime.now(timezone.utc)

    # 3. hotlist-Felder synchronisieren
    candidate.hotlist_job_title = titles[0]
    candidate.hotlist_job_titles = list(titles)
    candidate.categorized_at = datetime.now(timezone.utc)

    # ═══════════════════════════════════════════════════════
    # KETTENREAKTION: Aenderungen überall propagieren
    # ═══════════════════════════════════════════════════════

    # KR-1: current_position mit erstem manuellen Titel synchronisieren
    #        → wird überall angezeigt (Kandidaten-Liste, Match-Cards, Detail)
    candidate.current_position = titles[0]
    logger.info(f"KR-1: current_position '{old_position}' → '{titles[0]}'")

    # KR-2: hotlist_category aus den manuellen Titeln ableiten
    #        → Kandidat erscheint in der richtigen Hotlist-Kategorie
    cat_service = CategorizationService(db)
    titles_text = " ".join(titles)
    new_category, matched_kw = cat_service.detect_category(titles_text)
    candidate.hotlist_category = new_category
    if old_category != new_category:
        logger.info(f"KR-2: hotlist_category '{old_category}' → '{new_category}' (keywords: {matched_kw})")

    # KR-3: Embedding invalidieren (veraltet nach Titelaenderung)
    #        → naechster Matching-Lauf berechnet neues Embedding
    if candidate.embedding is not None:
        candidate.embedding = None
        logger.info(f"KR-3: Embedding invalidiert fuer {candidate.full_name}")

    # KR-4: Bestehende Matches als stale markieren
    #        → Scores basieren auf altem Profil, muessen neu bewertet werden
    now = datetime.now(timezone.utc)
    stale_result = await db.execute(
        update(Match)
        .where(
            and_(
                Match.candidate_id == candidate_id,
                or_(Match.stale.is_(False), Match.stale.is_(None)),
            )
        )
        .values(
            stale=True,
            stale_reason=f"Titel geaendert: {old_position} → {titles[0]}",
            stale_since=now,
        )
    )
    stale_count = stale_result.rowcount or 0
    if stale_count > 0:
        logger.info(f"KR-4: {stale_count} Matches als stale markiert")

    # ═══════════════════════════════════════════════════════

    # 4. Training-Daten speichern
    await learning_service.save_title_assignment(
        candidate=candidate,
        assigned_titles=titles,
        predicted_titles=predicted_titles,
    )

    await db.commit()

    logger.info(
        f"Titel zugewiesen: {candidate.full_name} → {titles}" + (f" (Rating: {rating})" if rating else "")
    )

    return {
        "success": True,
        "candidate_id": str(candidate_id),
        "assigned_titles": titles,
        "rating": candidate.rating,
        "candidate_name": candidate.full_name,
        "category": new_category,
        "position_updated": titles[0],
        "stale_matches": stale_count,
        "name_changed": name_changed,
    }


# ════════════════════════════════════════════════════════════════
# NAECHSTER KANDIDAT (fuer "Speichern & Naechster")
# ════════════════════════════════════════════════════════════════

@router.get("/api/titel-zuweisung/next/{candidate_id}")
async def next_candidate(
    candidate_id: UUID,
    city: str = Query(default=""),
    status: str = Query(default="offen"),
    has_cv: str = Query(default=""),
    titel: str = Query(default=""),
    min_rating: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Gibt die ID des naechsten Kandidaten zurueck (fuer 'Speichern & Naechster').

    WICHTIG: Muss dieselben Filter wie die Kandidaten-Liste beruecksichtigen,
    damit 'Naechster' nur innerhalb der gefilterten Menge springt.
    """
    # Aktuellen Kandidaten laden
    current = await db.execute(
        select(Candidate).where(Candidate.id == candidate_id)
    )
    current_candidate = current.scalar_one_or_none()

    if not current_candidate:
        return {"next_id": None}

    # Naechsten Kandidaten finden (gleiche Filter wie Hauptliste!)
    query = (
        select(Candidate.id)
        .where(
            and_(
                Candidate.deleted_at.is_(None),
                Candidate.id != candidate_id,
            )
        )
    )

    # Filter: Stadt
    if city:
        query = query.where(
            or_(
                Candidate.hotlist_city == city,
                Candidate.city == city,
            )
        )

    # Filter: Nur offene
    if status == "offen":
        query = query.where(
            or_(
                Candidate.manual_job_titles.is_(None),
                func.array_length(Candidate.manual_job_titles, 1).is_(None),
            )
        )
    elif status == "zugewiesen":
        query = query.where(
            and_(
                Candidate.manual_job_titles.isnot(None),
                func.array_length(Candidate.manual_job_titles, 1) > 0,
            )
        )

    # Filter: CV vorhanden
    if has_cv == "ja":
        query = query.where(
            or_(
                Candidate.cv_url.isnot(None),
                Candidate.cv_stored_path.isnot(None),
                and_(
                    Candidate.cv_text.isnot(None),
                    Candidate.cv_text != "",
                ),
            )
        )
    elif has_cv == "nein":
        query = query.where(
            and_(
                Candidate.cv_url.is_(None),
                Candidate.cv_stored_path.is_(None),
                or_(
                    Candidate.cv_text.is_(None),
                    Candidate.cv_text == "",
                ),
            )
        )

    # Filter: Nach Titel (gleiche Logik wie Hauptliste!)
    if titel:
        titel_search = f"%{titel}%"
        query = query.where(
            or_(
                Candidate.manual_job_titles.any(titel),
                Candidate.hotlist_job_title.ilike(titel_search),
                Candidate.current_position.ilike(titel_search),
            )
        )

    # Filter: Mindest-Bewertung
    if min_rating:
        query = query.where(
            Candidate.rating >= int(min_rating)
        )

    # Sortierung: Nach Name, naechster nach aktuellem
    query = query.order_by(
        Candidate.last_name.asc(),
        Candidate.first_name.asc(),
    ).limit(1)

    result = await db.execute(query)
    next_id = result.scalar_one_or_none()

    return {"next_id": str(next_id) if next_id else None}


# ════════════════════════════════════════════════════════════════
# TRAINING-STATS (API)
# ════════════════════════════════════════════════════════════════

@router.get("/api/titel-zuweisung/stats")
async def training_stats(
    db: AsyncSession = Depends(get_db),
):
    """Gibt Training-Statistiken zurueck."""
    learning_service = MTLearningService(db)
    stats = await learning_service.get_training_stats()
    return stats


# ════════════════════════════════════════════════════════════════
# KANDIDAT LOESCHEN (mit Erkenntnisse-Sicherung)
# ════════════════════════════════════════════════════════════════

@router.delete("/api/titel-zuweisung/kandidat/{candidate_id}")
async def delete_kandidat_with_learning(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Loescht einen Kandidaten (Soft-Delete) und sichert Erkenntnisse.

    Vor dem Loeschen:
    1. CV-Zusammenfassung + manuelle Titel als Training-Eintrag speichern
    2. Dann Soft-Delete (deleted_at = now())

    Ergebnis: Kandidat verschwindet aus allen Listen,
    aber Lern-Daten bleiben in mt_training_data erhalten.
    """
    # Kandidat laden
    result = await db.execute(
        select(Candidate).where(Candidate.id == candidate_id)
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(404, "Kandidat nicht gefunden")

    candidate_name = candidate.full_name

    # Schritt 1: Erkenntnisse sichern (bevor wir loeschen)
    learning_service = MTLearningService(db)
    input_text = learning_service._build_cv_summary(candidate)

    # Finalen Training-Eintrag erstellen
    from app.models.mt_training import MTTrainingData

    training_entry = MTTrainingData(
        entity_type="deleted_candidate",
        entity_id=candidate.id,
        input_text=input_text,
        predicted_titles=None,
        assigned_titles=list(candidate.manual_job_titles) if candidate.manual_job_titles else None,
        was_correct=None,
        reasoning=f"Kandidat '{candidate_name}' wurde geloescht. Erkenntnisse gesichert.",
        embedding=candidate.embedding if hasattr(candidate, "embedding") else None,
    )
    db.add(training_entry)

    # Schritt 2: Soft-Delete
    candidate.deleted_at = datetime.now(timezone.utc)

    await db.commit()

    logger.info(
        f"Kandidat geloescht (soft-delete): {candidate_name} ({candidate_id}), "
        f"Erkenntnisse in mt_training_data gesichert"
    )

    return {
        "success": True,
        "candidate_id": str(candidate_id),
        "candidate_name": candidate_name,
        "message": f"Kandidat '{candidate_name}' geloescht. Lern-Daten bleiben erhalten.",
    }
