"""Candidates API Routes - Endpoints für Kandidaten."""

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.exception_handlers import NotFoundException, ConflictException
from app.api.rate_limiter import RateLimitTier, rate_limit
from app.config import Limits
from app.database import get_db
from app.schemas.candidate import CandidateListResponse, CandidateResponse, CandidateUpdate, LanguageEntry

# ── Globaler Klassifizierungs-Fortschritt (In-Memory) ──────────────
_classification_progress: dict = {
    "running": False,
    "started_at": None,
    "total": 0,
    "processed": 0,
    "classified": 0,
    "errors": 0,
    "cost_usd": 0.0,
    "last_update": None,
    "result": None,
}
from app.schemas.filters import CandidateFilterParams, SortOrder
from app.schemas.pagination import PaginationParams
from app.schemas.validators import BatchDeleteRequest, BatchHideRequest
from app.services.candidate_service import CandidateService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/candidates", tags=["Kandidaten"])


# ==================== CRUD ====================

@router.get(
    "",
    response_model=CandidateListResponse,
    summary="Kandidaten auflisten",
)
async def list_candidates(
    # Pagination
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=Limits.PAGE_SIZE_DEFAULT, ge=1, le=Limits.PAGE_SIZE_MAX),
    # Filter
    name: str | None = Query(default=None, min_length=2, max_length=100),
    cities: list[str] | None = Query(default=None),
    skills: list[str] | None = Query(default=None),
    position: str | None = Query(default=None, min_length=2, max_length=100),
    only_active: bool = Query(default=False, description="Nur aktive (≤30 Tage)"),
    include_hidden: bool = Query(default=False),
    # Sortierung (Standard: zuletzt gesynct zuerst)
    sort_by: str = Query(default="created_at"),
    sort_order: SortOrder = Query(default=SortOrder.DESC),
    db: AsyncSession = Depends(get_db),
):
    """
    Listet Kandidaten mit Filteroptionen.

    Standardmäßig werden alle nicht-versteckten Kandidaten angezeigt.
    """
    candidate_service = CandidateService(db)

    # Filter- und Pagination-Objekte erstellen
    filters = CandidateFilterParams(
        name=name,
        cities=cities,
        skills=skills,
        position=position,
        only_active=only_active,
        include_hidden=include_hidden,
        sort_by=sort_by,
        sort_order=sort_order.value,
    )
    pagination = PaginationParams(page=page, per_page=per_page)

    result = await candidate_service.list_candidates(
        filters=filters,
        pagination=pagination,
    )

    return CandidateListResponse(
        items=result.items,
        total=result.total,
        page=result.page,
        per_page=result.per_page,
        pages=result.pages,
    )


# ==================== Kandidaten-Suche (fuer Pipeline-Hinzufuegen) ====================
# WICHTIG: Muss VOR /{candidate_id} stehen, sonst wird "search" als UUID interpretiert!

@router.get(
    "/search",
    summary="Kandidaten schnell suchen (fuer Autocomplete)",
)
async def search_candidates_quick(
    q: str = Query(..., min_length=2, description="Suchbegriff (Name)"),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """
    Schnelle Kandidatensuche fuer Autocomplete/Dropdown.

    Gibt nur ID, Name und Position zurueck (kompakt).
    Wird z.B. beim Hinzufuegen von Kandidaten zur Pipeline verwendet.
    """
    from sqlalchemy import select, or_
    from app.models.candidate import Candidate

    search_term = f"%{q}%"

    result = await db.execute(
        select(Candidate)
        .where(
            Candidate.deleted_at.is_(None),
            Candidate.hidden.is_(False),
            or_(
                Candidate.first_name.ilike(search_term),
                Candidate.last_name.ilike(search_term),
                (Candidate.first_name + " " + Candidate.last_name).ilike(search_term),
            )
        )
        .order_by(Candidate.last_name, Candidate.first_name)
        .limit(limit)
    )
    candidates = result.scalars().all()

    return [
        {
            "id": str(c.id),
            "name": f"{c.first_name or ''} {c.last_name or ''}".strip() or "Unbekannt",
            "position": c.current_position,
            "city": c.city,
        }
        for c in candidates
    ]



# ==================== Duplikat-Erkennung mit Diff ====================

@router.post(
    "/check-duplicate",
    summary="Prueft ob ein Kandidat bereits existiert und vergleicht alle Felder",
)
async def check_duplicate_candidate(
    data: dict,
    db: AsyncSession = Depends(get_db),
):
    """
    Prueft ob ein Kandidat mit gleicher E-Mail, Telefon oder Name bereits existiert.
    Vergleicht das geparste CV detailliert mit dem bestehenden Profil.
    Gibt Diff zurueck: welche Felder sich geaendert haben (alt vs. neu).
    """
    from sqlalchemy import select, or_, and_, func as sql_func
    from app.models.candidate import Candidate

    conditions = []

    # 1. E-Mail-Duplikat (staerkster Indikator)
    email = (data.get("email") or "").strip().lower()
    if email:
        conditions.append(sql_func.lower(Candidate.email) == email)

    # 2. Telefonnummer-Duplikat (normalisiert: nur Ziffern vergleichen)
    phone = (data.get("phone") or "").strip()
    phone_digits = ""
    if phone:
        phone_digits = "".join(c for c in phone if c.isdigit())
        if len(phone_digits) >= 6:
            phone_suffix = phone_digits[-8:]
            conditions.append(
                sql_func.right(
                    sql_func.regexp_replace(Candidate.phone, r'[^0-9]', '', 'g'),
                    8
                ) == phone_suffix
            )

    # 3. Name-Kombination (Vor- + Nachname)
    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()
    if first_name and last_name:
        name_condition = and_(
            sql_func.lower(Candidate.first_name) == first_name.lower(),
            sql_func.lower(Candidate.last_name) == last_name.lower(),
        )
        conditions.append(name_condition)

    if not conditions:
        return {"duplicate_found": False, "changes": [], "candidate": None}

    # Suche: Nicht geloeschte Kandidaten
    stmt = (
        select(Candidate)
        .where(
            Candidate.deleted_at.is_(None),
            or_(*conditions),
        )
        .order_by(Candidate.updated_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if not existing:
        return {"duplicate_found": False, "changes": [], "candidate": None}

    # Match-Gruende bestimmen
    match_reasons = []
    if email and existing.email and existing.email.lower() == email:
        match_reasons.append("E-Mail")
    if phone and existing.phone:
        ex_digits = "".join(ch for ch in (existing.phone or "") if ch.isdigit())
        if len(ex_digits) >= 6 and len(phone_digits) >= 6:
            if ex_digits[-8:] == phone_digits[-8:]:
                match_reasons.append("Telefon")
    if first_name and last_name and existing.first_name and existing.last_name:
        if existing.first_name.lower() == first_name.lower() and existing.last_name.lower() == last_name.lower():
            match_reasons.append("Name")

    # ==================== Detaillierter Feld-Vergleich ====================
    changes = []

    def compare_str(label: str, field_key: str, old_val, new_val):
        """Vergleicht String-Felder (case-insensitive)."""
        old_s = (old_val or "").strip()
        new_s = (new_val or "").strip()
        if not new_s:
            return  # Neuer Wert leer → kein Update
        if old_s.lower() != new_s.lower():
            changes.append({
                "field": field_key,
                "label": label,
                "old": old_s or None,
                "new": new_s,
                "type": "new" if not old_s else "changed",
            })

    def compare_list(label: str, field_key: str, old_list, new_list):
        """Vergleicht Listen (z.B. Skills, IT-Skills)."""
        old_set = set(s.lower().strip() for s in (old_list or []) if s)
        new_set = set(s.lower().strip() for s in (new_list or []) if s)
        if not new_set:
            return
        if old_set != new_set:
            added = new_set - old_set
            removed = old_set - new_set
            if added or removed:
                changes.append({
                    "field": field_key,
                    "label": label,
                    "old": sorted(old_set) if old_set else None,
                    "new": sorted(new_set),
                    "added": sorted(added) if added else [],
                    "removed": sorted(removed) if removed else [],
                    "type": "new" if not old_set else "changed",
                })

    def compare_json_list(label: str, field_key: str, old_items, new_items):
        """Vergleicht JSONB-Listen (Werdegang, Ausbildung, etc.)."""
        old_count = len(old_items or [])
        new_count = len(new_items or [])
        if not new_items:
            return
        if old_count != new_count:
            changes.append({
                "field": field_key,
                "label": label,
                "old_count": old_count,
                "new_count": new_count,
                "type": "new" if old_count == 0 else "changed",
                "summary": f"{old_count} → {new_count} Eintraege",
            })
        elif old_count > 0:
            # Gleiche Anzahl → pruefen ob Inhalt sich geaendert hat
            import json
            old_normalized = json.dumps(old_items, sort_keys=True, ensure_ascii=False)
            new_normalized = json.dumps(new_items, sort_keys=True, ensure_ascii=False)
            if old_normalized != new_normalized:
                changes.append({
                    "field": field_key,
                    "label": label,
                    "old_count": old_count,
                    "new_count": new_count,
                    "type": "changed",
                    "summary": f"{new_count} Eintraege (aktualisiert)",
                })

    # Persoenliche Daten
    compare_str("Vorname", "first_name", existing.first_name, data.get("first_name"))
    compare_str("Nachname", "last_name", existing.last_name, data.get("last_name"))
    compare_str("E-Mail", "email", existing.email, data.get("email"))
    compare_str("Telefon", "phone", existing.phone, data.get("phone"))
    compare_str("Geburtsdatum", "birth_date",
                str(existing.birth_date) if existing.birth_date else "",
                data.get("birth_date"))

    # Berufliche Daten
    compare_str("Position", "current_position", existing.current_position, data.get("current_position"))
    compare_str("Unternehmen", "current_company", existing.current_company, data.get("current_company"))
    compare_str("Gehalt", "salary", existing.salary, data.get("salary"))
    compare_str("Kuendigungsfrist", "notice_period", existing.notice_period, data.get("notice_period"))

    # Adresse
    compare_str("Strasse", "street_address", existing.street_address, data.get("street_address"))
    compare_str("PLZ", "postal_code", existing.postal_code, data.get("postal_code"))
    compare_str("Stadt", "city", existing.city, data.get("city"))

    # Listen
    compare_list("Skills", "skills", existing.skills, data.get("skills"))
    compare_list("IT-Skills", "it_skills", existing.it_skills, data.get("it_skills"))

    # JSONB-Listen (Werdegang, Ausbildung, Sprachen)
    compare_json_list("Werdegang", "work_history", existing.work_history, data.get("work_history"))
    compare_json_list("Ausbildung", "education", existing.education, data.get("education"))
    compare_json_list("Weiterbildung", "further_education", existing.further_education, data.get("further_education"))

    # Sprachen separat vergleichen
    old_langs = existing.languages or []
    new_langs = data.get("languages") or []
    if new_langs:
        old_lang_set = set()
        for lang in old_langs:
            if isinstance(lang, dict):
                old_lang_set.add((lang.get("language", "").lower(), (lang.get("level") or "").lower()))
        new_lang_set = set()
        for lang in new_langs:
            if isinstance(lang, dict):
                new_lang_set.add((lang.get("language", "").lower(), (lang.get("level") or "").lower()))
        if old_lang_set != new_lang_set:
            changes.append({
                "field": "languages",
                "label": "Sprachen",
                "old_count": len(old_langs),
                "new_count": len(new_langs),
                "type": "new" if not old_langs else "changed",
                "summary": f"{len(old_langs)} → {len(new_langs)} Sprachen",
            })

    return {
        "duplicate_found": True,
        "candidate": {
            "id": str(existing.id),
            "name": existing.full_name,
            "email": existing.email,
            "phone": existing.phone,
            "city": existing.city,
            "current_position": existing.current_position,
            "has_cv": bool(existing.cv_stored_path or existing.cv_url),
        },
        "match_reasons": match_reasons,
        "changes": changes,
        "has_changes": len(changes) > 0,
    }


# ==================== Duplikat-Update (Quick-Add Merge) ====================

@router.post(
    "/{candidate_id}/merge-cv",
    summary="CV-Daten in bestehendes Profil mergen (Quick-Add Duplikat)",
)
@rate_limit(RateLimitTier.WRITE)
async def merge_cv_into_existing(
    candidate_id: UUID,
    data: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Merged geparste CV-Daten in ein bestehendes Kandidaten-Profil.
    Wird aufgerufen wenn Quick-Add ein Duplikat erkennt und es Aenderungen gibt.
    Startet anschliessend Geocoding + Kategorisierung + Finance-Klassifizierung neu.
    """
    from datetime import date as date_type, datetime as dt_cls, timezone as tz_cls
    from sqlalchemy import select
    from app.models.candidate import Candidate

    # Kandidat laden
    result = await db.execute(
        select(Candidate).where(
            Candidate.id == candidate_id,
            Candidate.deleted_at.is_(None),
        )
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    # Felder aktualisieren (nur wenn neuer Wert vorhanden)
    str_fields = [
        "first_name", "last_name", "email", "phone",
        "current_position", "current_company",
        "street_address", "postal_code", "city",
        "salary", "notice_period",
    ]
    for field in str_fields:
        new_val = (data.get(field) or "").strip()
        if new_val:
            setattr(candidate, field, new_val)

    # birth_date parsen
    if data.get("birth_date"):
        try:
            bd = data["birth_date"]
            if isinstance(bd, str):
                if "." in bd:
                    parts = bd.split(".")
                    if len(parts) == 3:
                        candidate.birth_date = date_type(int(parts[2]), int(parts[1]), int(parts[0]))
                elif "-" in bd:
                    parts = bd.split("-")
                    if len(parts) == 3:
                        candidate.birth_date = date_type(int(parts[0]), int(parts[1]), int(parts[2]))
        except (ValueError, IndexError):
            pass

    # Listen-Felder (ersetzen wenn neuer Wert vorhanden)
    if data.get("skills"):
        candidate.skills = data["skills"]
    if data.get("it_skills"):
        candidate.it_skills = data["it_skills"]

    # JSONB-Felder
    if data.get("languages"):
        candidate.languages = data["languages"]
    if data.get("work_history"):
        candidate.work_history = data["work_history"]
    if data.get("education"):
        candidate.education = data["education"]
    if data.get("further_education"):
        candidate.further_education = data["further_education"]

    # CV-Daten aktualisieren
    if data.get("cv_stored_path"):
        candidate.cv_stored_path = data["cv_stored_path"]
    if data.get("cv_text"):
        candidate.cv_text = data["cv_text"]
        candidate.cv_parsed_at = dt_cls.now(tz_cls.utc)

    await db.commit()
    await db.refresh(candidate)

    # Background-Processing: Geocoding + Kategorisierung + Finance-Klassifizierung
    background_tasks.add_task(
        _process_candidate_after_create,
        candidate.id,
    )

    return {
        "id": str(candidate.id),
        "name": candidate.full_name,
        "merged": True,
    }


# ==================== CV parsen ohne Kandidat (Quick-Add) ====================

@router.post(
    "/parse-cv",
    summary="CV parsen ohne Kandidat (fuer Quick-Add)",
)
@rate_limit(RateLimitTier.AI)
async def parse_cv_for_quickadd(
    file: UploadFile = File(..., description="PDF-Datei"),
    db: AsyncSession = Depends(get_db),
):
    """
    Parst ein CV-PDF und gibt strukturierte Daten zurueck,
    ohne einen Kandidaten anzulegen. Fuer den Quick-Add Workflow.
    """
    from app.services.cv_parser_service import CVParserService
    from app.services.r2_storage_service import R2StorageService

    # Datei-Validierung
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return {"success": False, "message": "Nur PDF-Dateien erlaubt"}

    pdf_bytes = await file.read()
    if len(pdf_bytes) < 100:
        return {"success": False, "message": "Datei ist zu klein (leer?)"}
    if len(pdf_bytes) > 10 * 1024 * 1024:
        return {"success": False, "message": "Datei zu gross (max. 10 MB)"}

    # Text extrahieren
    async with CVParserService(db) as parser:
        cv_text = parser.extract_text_from_pdf(pdf_bytes)
        if not cv_text:
            return {"success": False, "message": "Konnte keinen Text aus dem PDF extrahieren"}

        # OpenAI Parsing (synchron warten)
        parse_result = await parser.parse_cv_text(cv_text)

    if not parse_result.success or not parse_result.data:
        return {
            "success": False,
            "message": parse_result.error or "CV-Parsing fehlgeschlagen",
        }

    parsed = parse_result.data

    # R2 Upload mit temporaerer ID
    cv_key = None
    try:
        r2 = R2StorageService()
        if r2.is_available:
            cv_key = r2.upload_cv(
                "temp-" + str(uuid4()),
                pdf_bytes,
                first_name=parsed.first_name or "Unbekannt",
                last_name=parsed.last_name or "Unbekannt",
            )
    except Exception as e:
        logger.warning(f"R2 Upload fuer Quick-Add fehlgeschlagen: {e}")

    return {
        "success": True,
        "cv_key": cv_key,
        "cv_text": cv_text,
        "parsed": {
            "first_name": parsed.first_name,
            "last_name": parsed.last_name,
            "email": parsed.email,
            "phone": parsed.phone,
            "birth_date": parsed.birth_date,
            "estimated_age": parsed.estimated_age,
            "current_position": parsed.current_position,
            "current_company": parsed.current_company,
            "street_address": parsed.street_address,
            "postal_code": parsed.postal_code,
            "city": parsed.city,
            "skills": parsed.skills or [],
            "it_skills": parsed.it_skills or [],
            "languages": [
                lang.model_dump() if hasattr(lang, "model_dump") else lang
                for lang in (parsed.languages or [])
            ],
            "work_history": [
                entry.model_dump() if hasattr(entry, "model_dump") else entry
                for entry in (parsed.work_history or [])
            ],
            "education": [
                entry.model_dump() if hasattr(entry, "model_dump") else entry
                for entry in (parsed.education or [])
            ],
            "further_education": [
                entry.model_dump() if hasattr(entry, "model_dump") else entry
                for entry in (parsed.further_education or [])
            ],
        },
    }


# ==================== Neuen Kandidaten erstellen ====================

@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Neuen Kandidaten erstellen",
)
@rate_limit(RateLimitTier.WRITE)
async def create_candidate(
    data: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Erstellt einen neuen Kandidaten aus den uebergebenen Daten.
    Startet Geocoding + Kategorisierung + Finance-Klassifizierung im Hintergrund.
    """
    from datetime import date as date_type
    from app.models.candidate import Candidate

    # birth_date String -> date parsen
    birth_date_val = None
    if data.get("birth_date"):
        try:
            bd = data["birth_date"]
            if isinstance(bd, str):
                # Unterstuetzte Formate: DD.MM.YYYY, YYYY-MM-DD
                if "." in bd:
                    parts = bd.split(".")
                    if len(parts) == 3:
                        birth_date_val = date_type(int(parts[2]), int(parts[1]), int(parts[0]))
                elif "-" in bd:
                    parts = bd.split("-")
                    if len(parts) == 3:
                        birth_date_val = date_type(int(parts[0]), int(parts[1]), int(parts[2]))
            elif isinstance(bd, date_type):
                birth_date_val = bd
        except (ValueError, IndexError) as e:
            logger.warning(f"Konnte birth_date nicht parsen: {data.get('birth_date')} - {e}")

    # Kandidat erstellen
    from datetime import datetime as dt_cls, timezone as tz_cls
    now = dt_cls.now(tz_cls.utc)

    candidate = Candidate(
        first_name=data.get("first_name"),
        last_name=data.get("last_name"),
        email=data.get("email"),
        phone=data.get("phone"),
        birth_date=birth_date_val,
        current_position=data.get("current_position"),
        current_company=data.get("current_company"),
        street_address=data.get("street_address"),
        postal_code=data.get("postal_code"),
        city=data.get("city"),
        salary=data.get("salary"),
        notice_period=data.get("notice_period"),
        skills=data.get("skills"),
        it_skills=data.get("it_skills"),
        languages=data.get("languages"),
        work_history=data.get("work_history"),
        education=data.get("education"),
        further_education=data.get("further_education"),
        cv_stored_path=data.get("cv_stored_path"),
        cv_text=data.get("cv_text"),
        cv_parsed_at=now if data.get("cv_text") else None,
    )

    db.add(candidate)
    await db.commit()
    await db.refresh(candidate)

    # Background-Processing: Geocoding + Kategorisierung + Finance-Klassifizierung
    background_tasks.add_task(
        _process_candidate_after_create,
        candidate.id,
    )

    return {"id": str(candidate.id), "name": candidate.full_name}


async def _process_candidate_after_create(candidate_id: UUID):
    """Background-Task: Geocoding + Kategorisierung + Finance-Klassifizierung fuer neuen Kandidaten."""
    from app.database import async_session_maker
    from app.services.geocoding_service import GeocodingService
    from app.services.categorization_service import CategorizationService
    from app.services.finance_classifier_service import FinanceClassifierService
    from sqlalchemy import select
    from app.models.candidate import Candidate

    async with async_session_maker() as db:
        try:
            # Kandidat laden
            result = await db.execute(
                select(Candidate).where(Candidate.id == candidate_id)
            )
            candidate = result.scalar_one_or_none()
            if not candidate:
                logger.error(f"Post-Create Processing: Kandidat {candidate_id} nicht gefunden")
                return

            # Schritt 1: Geocoding
            logger.info(f"Post-Create Schritt 1/3: Geocoding fuer {candidate_id}")
            try:
                geo_service = GeocodingService(db)
                await geo_service.geocode_candidate(candidate)
            except Exception as e:
                logger.warning(f"Geocoding fehlgeschlagen fuer {candidate_id}: {e}")

            # Schritt 2: Kategorisierung
            logger.info(f"Post-Create Schritt 2/3: Kategorisierung fuer {candidate_id}")
            try:
                cat_service = CategorizationService(db)
                cat_result = cat_service.categorize_candidate(candidate)
                cat_service.apply_to_candidate(candidate, cat_result)
            except Exception as e:
                logger.warning(f"Kategorisierung fehlgeschlagen fuer {candidate_id}: {e}")

            # Schritt 3: Finance-Klassifizierung (nur fuer FINANCE-Kandidaten)
            if candidate.hotlist_category == "FINANCE":
                logger.info(f"Post-Create Schritt 3/3: Finance-Klassifizierung fuer {candidate_id}")
                try:
                    fin_service = FinanceClassifierService(db)
                    fin_result = await fin_service.classify_candidate(candidate)
                    if fin_result.success:
                        fin_service.apply_to_candidate(candidate, fin_result)
                except Exception as e:
                    logger.warning(f"Finance-Klassifizierung fehlgeschlagen fuer {candidate_id}: {e}")
            else:
                logger.info(f"Post-Create Schritt 3/3: uebersprungen (nicht FINANCE) fuer {candidate_id}")

            await db.commit()
            logger.info(f"Post-Create Processing komplett fuer {candidate_id}")

        except Exception as e:
            logger.error(f"Post-Create Processing fehlgeschlagen fuer {candidate_id}: {e}")
            try:
                await db.rollback()
            except Exception:
                pass


# ==================== Einzelner Kandidat ====================

@router.get(
    "/{candidate_id}",
    response_model=CandidateResponse,
    summary="Kandidaten-Details abrufen",
)
async def get_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Gibt die Details eines Kandidaten zurück."""
    candidate_service = CandidateService(db)
    candidate = await candidate_service.get_candidate(candidate_id)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    return _candidate_to_response(candidate)


@router.patch(
    "/{candidate_id}",
    response_model=CandidateResponse,
    summary="Kandidaten aktualisieren",
)
@rate_limit(RateLimitTier.WRITE)
async def update_candidate(
    candidate_id: UUID,
    data: CandidateUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Aktualisiert einen Kandidaten."""
    candidate_service = CandidateService(db)
    candidate = await candidate_service.update_candidate(candidate_id, data)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    return _candidate_to_response(candidate)


# ==================== Hide/Unhide ====================

@router.put(
    "/{candidate_id}/hide",
    response_model=CandidateResponse,
    summary="Kandidaten ausblenden",
)
@rate_limit(RateLimitTier.WRITE)
async def hide_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Blendet einen Kandidaten aus.

    Ausgeblendete Kandidaten erscheinen nicht mehr in Suchergebnissen
    und Matching-Listen.
    """
    candidate_service = CandidateService(db)
    candidate = await candidate_service.hide_candidate(candidate_id)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    return _candidate_to_response(candidate)


@router.put(
    "/{candidate_id}/unhide",
    response_model=CandidateResponse,
    summary="Kandidaten wieder einblenden",
)
@rate_limit(RateLimitTier.WRITE)
async def unhide_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Macht das Ausblenden eines Kandidaten rückgängig."""
    candidate_service = CandidateService(db)
    candidate = await candidate_service.unhide_candidate(candidate_id)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    return _candidate_to_response(candidate)


@router.put(
    "/batch/hide",
    summary="Mehrere Kandidaten ausblenden",
)
@rate_limit(RateLimitTier.WRITE)
async def batch_hide_candidates(
    request: BatchHideRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Blendet mehrere Kandidaten auf einmal aus.

    Maximal 100 Kandidaten pro Anfrage.
    """
    candidate_service = CandidateService(db)
    hidden_count = await candidate_service.batch_hide(request.ids)

    return {"hidden_count": hidden_count}


@router.put(
    "/batch/unhide",
    summary="Mehrere Kandidaten wieder einblenden",
)
@rate_limit(RateLimitTier.WRITE)
async def batch_unhide_candidates(
    request: BatchHideRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Blendet mehrere Kandidaten wieder ein.

    Maximal 100 Kandidaten pro Anfrage.
    """
    candidate_service = CandidateService(db)
    unhidden_count = await candidate_service.batch_unhide(request.ids)

    return {"unhidden_count": unhidden_count}


# ==================== Delete (Soft-Delete) ====================

@router.delete(
    "/batch/delete",
    summary="Mehrere Kandidaten löschen (Soft-Delete)",
)
@rate_limit(RateLimitTier.WRITE)
async def batch_delete_candidates(
    request: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Löscht mehrere Kandidaten auf einmal (Soft-Delete).

    Maximal 100 Kandidaten pro Anfrage.
    Gelöschte Kandidaten werden beim CRM-Sync ignoriert.
    """
    candidate_service = CandidateService(db)
    deleted_count = await candidate_service.batch_delete(request.ids)
    await db.commit()

    return {"deleted_count": deleted_count}


@router.post(
    "/{candidate_id}/reparse-cv",
    summary="CV eines Kandidaten neu parsen (OpenAI)",
)
async def reparse_single_cv(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Parst das CV eines einzelnen Kandidaten erneut mit OpenAI.

    Gibt den alten und neuen Namen zurück, damit das Frontend
    den Fortschritt live anzeigen kann.
    """
    from app.services.cv_parser_service import CVParserService
    from sqlalchemy import select
    from app.models.candidate import Candidate

    # Kandidat laden
    result = await db.execute(
        select(Candidate).where(Candidate.id == candidate_id)
    )
    candidate = result.scalar_one_or_none()

    if not candidate:
        return {"status": "error", "message": "Kandidat nicht gefunden"}

    old_first = candidate.first_name or ""
    old_last = candidate.last_name or ""
    old_name = f"{old_first} {old_last}".strip() or "—"

    if not candidate.cv_url:
        return {
            "status": "skipped",
            "message": "Kein CV vorhanden",
            "candidate_id": str(candidate_id),
            "old_name": old_name,
            "new_name": old_name,
        }

    # CV parsen
    async with CVParserService(db) as parser:
        try:
            candidate, parse_result = await parser.parse_candidate_cv(candidate_id)
        except Exception as e:
            return {
                "status": "error",
                "message": str(e),
                "candidate_id": str(candidate_id),
                "old_name": old_name,
                "new_name": old_name,
            }

    if not parse_result.success:
        return {
            "status": "error",
            "message": parse_result.error or "Parsing fehlgeschlagen",
            "candidate_id": str(candidate_id),
            "old_name": old_name,
            "new_name": old_name,
        }

    new_first = candidate.first_name or ""
    new_last = candidate.last_name or ""
    new_name = f"{new_first} {new_last}".strip() or "—"
    name_changed = old_name != new_name

    return {
        "status": "ok",
        "candidate_id": str(candidate_id),
        "old_name": old_name,
        "new_name": new_name,
        "name_changed": name_changed,
        "position": candidate.current_position or "—",
    }


@router.post(
    "/{candidate_id}/upload-cv",
    summary="CV hochladen und verarbeiten",
)
@rate_limit(RateLimitTier.WRITE)
async def upload_candidate_cv(
    candidate_id: UUID,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="PDF-Datei"),
    db: AsyncSession = Depends(get_db),
):
    """
    Laedt ein CV-PDF hoch, speichert es in R2 und startet
    automatisch CV-Parsing + Geocoding + Kategorisierung + Finance-Klassifizierung.
    """
    from sqlalchemy import select
    from app.models.candidate import Candidate
    from app.services.r2_storage_service import R2StorageService

    # Kandidat laden
    result = await db.execute(
        select(Candidate).where(
            Candidate.id == candidate_id,
            Candidate.deleted_at.is_(None),
        )
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    # Datei-Validierung
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return {"success": False, "message": "Nur PDF-Dateien erlaubt"}

    pdf_bytes = await file.read()
    if len(pdf_bytes) < 100:
        return {"success": False, "message": "Datei ist zu klein (leer?)"}
    if len(pdf_bytes) > 10 * 1024 * 1024:
        return {"success": False, "message": "Datei zu gross (max. 10 MB)"}

    # R2 Upload
    r2 = R2StorageService()
    if not r2.is_available:
        return {"success": False, "message": "R2 Storage nicht verfuegbar"}

    try:
        key = r2.upload_cv(
            str(candidate.id),
            pdf_bytes,
            first_name=candidate.first_name,
            last_name=candidate.last_name,
            hotlist_category=candidate.hotlist_category,
        )
    except Exception as e:
        logger.error(f"R2 Upload fehlgeschlagen fuer {candidate_id}: {e}")
        return {"success": False, "message": f"Upload fehlgeschlagen: {e}"}

    # DB aktualisieren
    candidate.cv_stored_path = key
    candidate.cv_url = candidate.cv_url or f"r2://{key}"
    await db.commit()

    # Background-Processing starten
    background_tasks.add_task(
        _process_cv_after_upload,
        candidate_id,
        pdf_bytes,
    )

    return {
        "success": True,
        "cv_key": key,
        "message": "CV hochgeladen, wird verarbeitet...",
    }


async def _process_cv_after_upload(candidate_id: UUID, pdf_bytes: bytes):
    """Background-Task: CV-Parsing + Geocoding + Kategorisierung + Finance-Klassifizierung."""
    from app.database import async_session_maker
    from app.services.cv_parser_service import CVParserService
    from app.services.geocoding_service import GeocodingService
    from app.services.categorization_service import CategorizationService
    from app.services.finance_classifier_service import FinanceClassifierService
    from sqlalchemy import select
    from app.models.candidate import Candidate

    async with async_session_maker() as db:
        try:
            # Kandidat laden
            result = await db.execute(
                select(Candidate).where(Candidate.id == candidate_id)
            )
            candidate = result.scalar_one_or_none()
            if not candidate:
                logger.error(f"CV-Processing: Kandidat {candidate_id} nicht gefunden")
                return

            # Schritt 1: CV-Parsing (Text-Extraktion + OpenAI)
            logger.info(f"CV-Processing Schritt 1/4: Parsing fuer {candidate_id}")
            async with CVParserService(db) as parser:
                cv_text = parser.extract_text_from_pdf(pdf_bytes)
                if cv_text:
                    parse_result = await parser.parse_cv_text(cv_text)
                    if parse_result.success and parse_result.data:
                        await parser._update_candidate_from_cv(
                            candidate, parse_result.data, cv_text
                        )
                        logger.info(f"CV-Parsing OK fuer {candidate_id}")
                    else:
                        logger.warning(
                            f"CV-Parsing fehlgeschlagen fuer {candidate_id}: "
                            f"{parse_result.error}"
                        )
                        # cv_text trotzdem speichern
                        candidate.cv_text = cv_text

            # Schritt 2: Geocoding
            logger.info(f"CV-Processing Schritt 2/4: Geocoding fuer {candidate_id}")
            try:
                geo_service = GeocodingService(db)
                await geo_service.geocode_candidate(candidate)
            except Exception as e:
                logger.warning(f"Geocoding fehlgeschlagen fuer {candidate_id}: {e}")

            # Schritt 3: Kategorisierung
            logger.info(f"CV-Processing Schritt 3/4: Kategorisierung fuer {candidate_id}")
            try:
                cat_service = CategorizationService(db)
                cat_result = cat_service.categorize_candidate(candidate)
                cat_service.apply_to_candidate(candidate, cat_result)
            except Exception as e:
                logger.warning(f"Kategorisierung fehlgeschlagen fuer {candidate_id}: {e}")

            # Schritt 4: Finance-Klassifizierung (nur fuer FINANCE-Kandidaten)
            if candidate.hotlist_category == "FINANCE":
                logger.info(f"CV-Processing Schritt 4/4: Finance-Klassifizierung fuer {candidate_id}")
                try:
                    fin_service = FinanceClassifierService(db)
                    fin_result = await fin_service.classify_candidate(candidate)
                    if fin_result.success:
                        fin_service.apply_to_candidate(candidate, fin_result)
                except Exception as e:
                    logger.warning(f"Finance-Klassifizierung fehlgeschlagen fuer {candidate_id}: {e}")
            else:
                logger.info(f"CV-Processing Schritt 4/4: uebersprungen (nicht FINANCE) fuer {candidate_id}")

            # Schritt 4.5: Verifizierte Position setzen (nach Klassifizierung)
            manual = candidate.manual_overrides or {}
            if "current_position" not in manual and candidate.hotlist_job_title:
                if candidate.hotlist_category in ("FINANCE", "ENGINEERING"):
                    candidate.current_position = candidate.hotlist_job_title
                    logger.info(
                        f"CV-Processing: Position verifiziert auf "
                        f"'{candidate.hotlist_job_title}' fuer {candidate_id}"
                    )

            await db.commit()
            logger.info(f"CV-Processing komplett fuer {candidate_id}")

        except Exception as e:
            logger.error(f"CV-Processing fehlgeschlagen fuer {candidate_id}: {e}")
            try:
                await db.rollback()
            except Exception:
                pass


@router.delete(
    "/{candidate_id}",
    summary="Kandidaten löschen (Soft-Delete)",
)
@rate_limit(RateLimitTier.WRITE)
async def delete_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Löscht einen Kandidaten (Soft-Delete).

    Der Kandidat wird als gelöscht markiert und beim nächsten
    CRM-Sync komplett ignoriert (kein Update, kein Neu-Erstellen).
    """
    candidate_service = CandidateService(db)
    success = await candidate_service.delete_candidate(candidate_id)

    if not success:
        raise NotFoundException(message="Kandidat nicht gefunden")

    await db.commit()

    return {"success": True, "message": "Kandidat gelöscht"}


# ==================== CV-Parsing ====================

@router.post(
    "/{candidate_id}/parse-cv",
    response_model=CandidateResponse,
    summary="CV neu parsen",
)
@rate_limit(RateLimitTier.AI)
async def parse_candidate_cv(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Parst den CV eines Kandidaten erneut.

    Verwendet OpenAI, um strukturierte Daten aus dem CV zu extrahieren.
    """
    from app.services.cv_parser_service import CVParserService

    candidate_service = CandidateService(db)
    candidate = await candidate_service.get_candidate(candidate_id)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    if not candidate.cv_url:
        raise ConflictException(message="Kandidat hat keine CV-URL")

    cv_parser = CVParserService(db)
    updated_candidate = await cv_parser.parse_candidate_cv(candidate_id)

    if not updated_candidate:
        raise ConflictException(message="CV-Parsing fehlgeschlagen")

    return _candidate_to_response(updated_candidate)


# ==================== Matches für Kandidat ====================

@router.get(
    "/{candidate_id}/jobs",
    summary="Passende Jobs für einen Kandidaten",
)
async def get_jobs_for_candidate(
    candidate_id: UUID,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=Limits.PAGE_SIZE_DEFAULT, ge=1, le=Limits.PAGE_SIZE_MAX),
    sort_by: str = Query(default="distance_km"),
    sort_order: SortOrder = Query(default=SortOrder.ASC),
    db: AsyncSession = Depends(get_db),
):
    """
    Gibt passende Jobs für einen Kandidaten zurück.

    Zeigt alle Jobs im Umkreis von 25km mit Match-Daten.
    """
    candidate_service = CandidateService(db)
    candidate = await candidate_service.get_candidate(candidate_id)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    jobs, total = await candidate_service.get_jobs_for_candidate(
        candidate_id=candidate_id,
        page=page,
        per_page=per_page,
        sort_by=sort_by,
        sort_order=sort_order.value,
    )

    pages = (total + per_page - 1) // per_page if per_page > 0 else 0

    return {
        "items": jobs,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    }


# ==================== CV-Proxy (fuer iframe-Vorschau) ====================

def _is_word_document(content: bytes, url: str | None = None) -> bool:
    """Erkennt ob der Inhalt ein Word-Dokument ist (DOCX oder DOC)."""
    # DOCX = ZIP-Archiv (PK Header)
    if content[:4] == b"PK\x03\x04":
        return True
    # DOC = OLE2 Compound File (D0CF Header)
    if content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return True
    # Fallback: URL-Endung pruefen
    if url and any(url.lower().endswith(ext) for ext in (".docx", ".doc")):
        return True
    return False


async def _convert_word_to_pdf(word_content: bytes) -> bytes:
    """Konvertiert Word-Dokument (DOC/DOCX) zu PDF via LibreOffice (async)."""
    import asyncio
    import tempfile
    import os
    import shutil

    if not shutil.which("soffice"):
        raise RuntimeError("LibreOffice nicht installiert (soffice nicht gefunden)")

    tmpdir = tempfile.mkdtemp()
    try:
        # Word-Datei speichern
        input_path = os.path.join(tmpdir, "document.docx")
        with open(input_path, "wb") as f:
            f.write(word_content)

        # HOME + LibreOffice User-Profil in tmpdir
        env = os.environ.copy()
        env["HOME"] = tmpdir
        env["TMPDIR"] = tmpdir

        # Eigenes User-Profil pro Aufruf (verhindert Lock-Konflikte)
        user_profile = f"file://{tmpdir}/libreoffice_profile"

        # LibreOffice async ausfuehren (blockiert nicht den Event Loop)
        process = await asyncio.create_subprocess_exec(
            "soffice",
            "--headless",
            "--norestore",
            "--nofirststartwizard",
            f"-env:UserInstallation={user_profile}",
            "--convert-to", "pdf",
            "--outdir", tmpdir,
            input_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("Word-zu-PDF Konvertierung Timeout (60s)")

        if process.returncode != 0:
            stderr_text = stderr.decode(errors="replace")
            logger.error(f"LibreOffice Konvertierung fehlgeschlagen (exit {process.returncode}): {stderr_text}")
            raise RuntimeError(f"Word-zu-PDF Konvertierung fehlgeschlagen: {stderr_text[:200]}")

        # PDF lesen
        pdf_path = os.path.join(tmpdir, "document.pdf")
        if not os.path.exists(pdf_path):
            # Manchmal erzeugt LibreOffice Dateien mit anderem Namen
            pdf_files = [f for f in os.listdir(tmpdir) if f.endswith(".pdf")]
            if pdf_files:
                pdf_path = os.path.join(tmpdir, pdf_files[0])
            else:
                stdout_text = stdout.decode(errors="replace") if stdout else ""
                logger.error(f"LibreOffice hat kein PDF erzeugt. stdout: {stdout_text}")
                raise RuntimeError("PDF wurde nicht erstellt — LibreOffice hat keine Ausgabe erzeugt")

        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@router.get(
    "/{candidate_id}/cv-preview",
    summary="CV als PDF-Proxy fuer iframe-Vorschau",
)
async def cv_preview_proxy(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Proxy-Endpoint der das CV als PDF liefert.

    Unterstuetzte Formate: PDF, DOCX, DOC (Word wird automatisch konvertiert).

    Reihenfolge:
    1. Aus R2 Object Storage (wenn cv_stored_path vorhanden)
    2. Fallback: Vom CRM-Server holen (EINMALIG in R2 speichern)
    """
    from app.services.r2_storage_service import R2StorageService

    candidate_service = CandidateService(db)
    candidate = await candidate_service.get_candidate(candidate_id)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    if not candidate.cv_stored_path and not candidate.cv_url:
        raise NotFoundException(message="Kein CV vorhanden")

    r2 = R2StorageService()

    # 1. Aus R2 laden (wenn bereits gespeichert)
    if candidate.cv_stored_path and r2.is_available:
        try:
            content = r2.download_cv(candidate.cv_stored_path)
            if content:
                # R2-Datei pruefen: Word-Dokument konvertieren
                if _is_word_document(content, candidate.cv_stored_path):
                    logger.info(f"Word-Dokument in R2 erkannt fuer {candidate.full_name}, konvertiere zu PDF")
                    try:
                        pdf_content = await _convert_word_to_pdf(content)
                    except RuntimeError as e:
                        logger.error(f"Word-Konvertierung fehlgeschlagen (R2) fuer {candidate.full_name}: {e}")
                        raise NotFoundException(
                            message=f"CV ist ein Word-Dokument und konnte nicht konvertiert werden: {e}"
                        )
                    # Konvertiertes PDF in R2 ueberschreiben (nur 1x konvertieren)
                    try:
                        r2.client.put_object(
                            Bucket=r2.bucket,
                            Key=candidate.cv_stored_path,
                            Body=pdf_content,
                            ContentType="application/pdf",
                        )
                        logger.info(f"Word-CV in R2 durch PDF ersetzt: {candidate.cv_stored_path}")
                    except Exception:
                        pass
                    content = pdf_content
                return StreamingResponse(
                    iter([content]),
                    media_type="application/pdf",
                    headers={
                        "Content-Disposition": "inline",
                        "Cache-Control": "private, max-age=3600",
                    },
                )
        except Exception as e:
            logger.warning(f"R2 Download/Konvertierung fehlgeschlagen, Fallback auf CRM: {e}")

    # 2. Fallback: Vom CRM-Server holen
    if not candidate.cv_url:
        raise NotFoundException(message="Kein CV vorhanden")

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(candidate.cv_url)

    if response.status_code != 200:
        raise NotFoundException(message="CV konnte nicht geladen werden")

    file_content = response.content

    # Word-Dokument? → zu PDF konvertieren
    if _is_word_document(file_content, candidate.cv_url):
        logger.info(f"Word-Dokument vom CRM erkannt fuer {candidate.full_name}, konvertiere zu PDF")
        try:
            pdf_content = await _convert_word_to_pdf(file_content)
        except RuntimeError as e:
            logger.error(f"Word-Konvertierung fehlgeschlagen (CRM) fuer {candidate.full_name}: {e}")
            raise NotFoundException(
                message=f"CV ist ein Word-Dokument und konnte nicht konvertiert werden: {e}"
            )
    else:
        pdf_content = file_content

    # EINMALIG in R2 speichern (nur wenn noch nicht vorhanden)
    if r2.is_available and not candidate.cv_stored_path:
        try:
            key = r2.upload_cv(
                str(candidate.id),
                pdf_content,
                first_name=candidate.first_name,
                last_name=candidate.last_name,
                hotlist_category=candidate.hotlist_category,
            )
            candidate.cv_stored_path = key
            await db.commit()
            logger.info(f"CV fuer {candidate.full_name} einmalig in R2 gespeichert: {key}")
        except Exception as e:
            logger.warning(f"R2 Auto-Upload fehlgeschlagen fuer {candidate.id}: {e}")

    return StreamingResponse(
        iter([pdf_content]),
        media_type="application/pdf",
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "private, max-age=300",
        },
    )


# ==================== Profil-PDF (Sincirus Branded) ====================

@router.get(
    "/{candidate_id}/profile-pdf",
    summary="Sincirus Branded Profil-PDF generieren oder aus R2 laden",
)
@rate_limit(RateLimitTier.AI)
async def generate_profile_pdf(
    candidate_id: UUID,
    regenerate: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """
    Liefert das Kandidaten-Profil-PDF.

    1. Wenn ein gespeichertes PDF in R2 existiert UND regenerate=false → R2-Version laden
    2. Sonst → Neu generieren mit WeasyPrint, in R2 speichern, am Kandidaten verknuepfen
    """
    import re
    from datetime import datetime, timezone
    from fastapi.responses import Response
    from app.models.candidate import Candidate
    from app.services.profile_pdf_service import ProfilePdfService
    from app.services.r2_storage_service import R2StorageService

    # Kandidat laden um R2-Key zu pruefen
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        raise NotFoundException(message=f"Kandidat {candidate_id} nicht gefunden")

    # Versuch 1: Gespeichertes PDF aus R2 laden
    if not regenerate and candidate.profile_pdf_r2_key:
        try:
            r2 = R2StorageService()
            if r2.is_available:
                pdf_bytes = r2.download_cv(candidate.profile_pdf_r2_key)
                if pdf_bytes:
                    logger.info(f"Profil-PDF aus R2 geladen: {candidate.profile_pdf_r2_key}")
                    return Response(
                        content=pdf_bytes,
                        media_type="application/pdf",
                        headers={
                            "Content-Disposition": "inline",
                            "Cache-Control": "private, max-age=300",
                        },
                    )
        except Exception as e:
            logger.warning(f"R2-Download fehlgeschlagen, generiere neu: {e}")

    # Versuch 2: Neu generieren
    pdf_service = ProfilePdfService(db)

    try:
        pdf_bytes = await pdf_service.generate_profile_pdf(candidate_id)
    except ValueError as e:
        raise NotFoundException(message=str(e))
    except Exception as e:
        logger.error(f"PDF-Generierung fehlgeschlagen fuer {candidate_id}: {e}")
        raise ConflictException(message=f"PDF-Generierung fehlgeschlagen: {str(e)[:200]}")

    # In R2 speichern und am Kandidaten verknuepfen
    try:
        r2 = R2StorageService()
        if r2.is_available and pdf_bytes:
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{candidate.first_name}_{candidate.last_name}")
            r2_key = f"profiles/{str(candidate_id)[:8]}_{safe_name}_profil.pdf"
            r2.upload_file(key=r2_key, file_content=pdf_bytes, content_type="application/pdf")
            candidate.profile_pdf_r2_key = r2_key
            candidate.profile_pdf_generated_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info(f"Profil-PDF generiert + R2 + DB: {r2_key}")
    except Exception as e:
        logger.warning(f"R2-Upload nach manueller Generierung fehlgeschlagen: {e}")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "private, max-age=60",
        },
    )


# ==================== R2 Migration ====================

@router.post(
    "/migrate-cvs-to-r2",
    summary="Migriert bestehende CVs von CRM-URLs nach R2",
)
async def migrate_cvs_to_r2(
    batch_size: int = Query(default=20, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Migriert CVs die noch nicht in R2 gespeichert sind.

    Holt PDFs von CRM-URLs und laedt sie nach R2 hoch.
    Laeuft in Batches um Timeouts zu vermeiden.
    """
    from sqlalchemy import select
    from app.models.candidate import Candidate
    from app.services.r2_storage_service import R2StorageService

    r2 = R2StorageService()
    if not r2.is_available:
        return {"error": "R2 Storage nicht konfiguriert", "migrated": 0}

    # Kandidaten mit CV-URL aber ohne R2-Pfad finden
    stmt = (
        select(Candidate)
        .where(Candidate.cv_url.isnot(None))
        .where(Candidate.cv_stored_path.is_(None))
        .where(Candidate.deleted_at.is_(None))
        .limit(batch_size)
    )
    result = await db.execute(stmt)
    candidates = result.scalars().all()

    migrated = 0
    errors = 0

    for candidate in candidates:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                response = await client.get(candidate.cv_url)

            if response.status_code == 200 and len(response.content) > 100:
                key = r2.upload_cv(
                    str(candidate.id),
                    response.content,
                    first_name=candidate.first_name,
                    last_name=candidate.last_name,
                    hotlist_category=candidate.hotlist_category,
                )
                candidate.cv_stored_path = key
                migrated += 1
            else:
                errors += 1
                logger.warning(
                    f"CV-Migration: HTTP {response.status_code} fuer {candidate.id}"
                )
        except Exception as e:
            errors += 1
            logger.warning(f"CV-Migration fehlgeschlagen fuer {candidate.id}: {e}")

    await db.commit()

    # Wie viele sind noch offen?
    count_stmt = (
        select(Candidate)
        .where(Candidate.cv_url.isnot(None))
        .where(Candidate.cv_stored_path.is_(None))
        .where(Candidate.deleted_at.is_(None))
    )
    remaining_result = await db.execute(count_stmt)
    remaining = len(remaining_result.scalars().all())

    return {
        "migrated": migrated,
        "errors": errors,
        "remaining": remaining,
        "message": f"{migrated} CVs nach R2 migriert, {remaining} verbleibend",
    }


# ==================== Hilfsfunktionen ====================

def _candidate_to_response(candidate) -> CandidateResponse:
    """Konvertiert ein Candidate-Model zu einem Response-Schema."""
    return CandidateResponse(
        id=candidate.id,
        crm_id=candidate.crm_id,
        first_name=candidate.first_name,
        last_name=candidate.last_name,
        full_name=candidate.full_name,
        email=candidate.email,
        phone=candidate.phone,
        birth_date=candidate.birth_date,
        age=candidate.age,
        current_position=candidate.current_position,
        current_company=candidate.current_company,
        skills=candidate.skills,
        languages=[
            LanguageEntry(**lang) if isinstance(lang, dict) else lang
            for lang in (candidate.languages or [])
        ] or None,
        it_skills=candidate.it_skills,
        work_history=candidate.work_history,
        education=candidate.education,
        further_education=candidate.further_education,
        street_address=candidate.street_address,
        postal_code=candidate.postal_code,
        city=candidate.city,
        salary=candidate.salary,
        notice_period=candidate.notice_period,
        erp=candidate.erp,
        rating=candidate.rating,
        source=candidate.source,
        last_contact=candidate.last_contact,
        willingness_to_change=candidate.willingness_to_change,
        candidate_notes=candidate.candidate_notes,
        candidate_number=candidate.candidate_number,
        presented_at_companies=candidate.presented_at_companies,
        # Qualifizierungsgespräch-Felder
        desired_positions=candidate.desired_positions,
        key_activities=candidate.key_activities,
        home_office_days=candidate.home_office_days,
        commute_max=candidate.commute_max,
        commute_transport=candidate.commute_transport,
        erp_main=candidate.erp_main,
        employment_type=candidate.employment_type,
        part_time_hours=candidate.part_time_hours,
        preferred_industries=candidate.preferred_industries,
        avoided_industries=candidate.avoided_industries,
        open_office_ok=candidate.open_office_ok,
        whatsapp_ok=candidate.whatsapp_ok,
        other_recruiters=candidate.other_recruiters,
        exclusivity_agreed=candidate.exclusivity_agreed,
        applied_at_companies_text=candidate.applied_at_companies_text,
        call_transcript=candidate.call_transcript,
        call_summary=candidate.call_summary,
        call_date=candidate.call_date,
        call_type=candidate.call_type,
        has_coordinates=candidate.address_coords is not None,
        cv_url=candidate.cv_url,
        cv_parsed_at=candidate.cv_parsed_at,
        hidden=candidate.hidden,
        is_active=candidate.is_active,
        crm_synced_at=candidate.crm_synced_at,
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
    )


# ══════════════════════════════════════════════════════════════
#  Candidate Notes (Notizen-Verlauf)
# ══════════════════════════════════════════════════════════════

from datetime import datetime, timezone
from pydantic import BaseModel, Field
from sqlalchemy import select
from app.models.candidate import Candidate
from app.models.candidate_note import CandidateNote


class CandidateNoteCreate(BaseModel):
    """Schema fuer neue Kandidaten-Notiz."""
    content: str = Field(..., min_length=1)
    title: str | None = None
    note_date: str | None = None  # ISO-String oder "YYYY-MM-DD" — Default: jetzt


@router.get("/{candidate_id}/notes")
async def list_candidate_notes(candidate_id: UUID, db: AsyncSession = Depends(get_db)):
    """Listet alle Notizen eines Kandidaten (neueste zuerst)."""
    result = await db.execute(
        select(CandidateNote)
        .where(CandidateNote.candidate_id == candidate_id)
        .order_by(CandidateNote.note_date.desc())
    )
    notes = result.scalars().all()
    return [
        {
            "id": str(n.id),
            "candidate_id": str(n.candidate_id),
            "title": n.title,
            "content": n.content,
            "source": n.source,
            "note_date": n.note_date.isoformat() if n.note_date else None,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in notes
    ]


@router.post("/{candidate_id}/notes")
async def create_candidate_note(
    candidate_id: UUID,
    data: CandidateNoteCreate,
    db: AsyncSession = Depends(get_db),
):
    """Erstellt eine neue Notiz fuer einen Kandidaten."""
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        raise NotFoundException("Kandidat nicht gefunden")

    # Datum parsen (falls angegeben)
    note_date = None
    if data.note_date:
        try:
            # Versuche ISO Format
            note_date = datetime.fromisoformat(data.note_date.replace("Z", "+00:00"))
        except ValueError:
            try:
                # Versuche deutsches Format DD.MM.YYYY
                note_date = datetime.strptime(data.note_date, "%d.%m.%Y").replace(tzinfo=timezone.utc)
            except ValueError:
                note_date = None

    note = CandidateNote(
        candidate_id=candidate_id,
        title=data.title,
        content=data.content,
        source="manual",
        note_date=note_date or datetime.now(timezone.utc),
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)

    return {
        "id": str(note.id),
        "candidate_id": str(note.candidate_id),
        "title": note.title,
        "content": note.content,
        "source": note.source,
        "note_date": note.note_date.isoformat() if note.note_date else None,
        "created_at": note.created_at.isoformat() if note.created_at else None,
        "message": "Notiz erstellt",
    }


@router.delete("/notes/{note_id}")
async def delete_candidate_note(note_id: UUID, db: AsyncSession = Depends(get_db)):
    """Loescht eine Kandidaten-Notiz."""
    note = await db.get(CandidateNote, note_id)
    if not note:
        raise NotFoundException("Notiz nicht gefunden")

    await db.delete(note)
    await db.commit()
    return {"message": "Notiz geloescht"}


# ══════════════════════════════════════════════════════════════════
# Phase 9: Deep Classification + Automatische Trigger
# ══════════════════════════════════════════════════════════════════


@router.post(
    "/classify/{candidate_id}",
    summary="Kandidat deep-klassifizieren (Werdegang-Analyse)",
    tags=["Classification"],
)
@rate_limit(RateLimitTier.AI)
async def classify_candidate(
    request: Request,
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Deep Classification fuer einen einzelnen Kandidaten.
    Analysiert den gesamten Werdegang und bestimmt die echte Rolle + Level.
    """
    from app.models.candidate import Candidate
    from app.services.finance_classifier_service import FinanceClassifierService

    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        raise NotFoundException("Kandidat nicht gefunden")

    classifier = FinanceClassifierService(db)
    classification = await classifier.classify_candidate(candidate)

    if classification.success:
        classifier.apply_to_candidate(candidate, classification)
        await db.commit()

    return {
        "candidate_id": str(candidate_id),
        "success": classification.success,
        "is_leadership": classification.is_leadership,
        "primary_role": classification.primary_role,
        "roles": classification.roles,
        "reasoning": classification.reasoning,
        "cost_usd": classification.cost_usd,
    }


@router.post(
    "/match/{candidate_id}",
    summary="Kandidat gegen alle Jobs matchen",
    tags=["Matching"],
)
@rate_limit(RateLimitTier.AI)
async def match_candidate_against_jobs(
    request: Request,
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Matcht einen einzelnen Kandidaten gegen alle offene FINANCE-Jobs.
    Nutzt die Matching Engine V2 mit den neuen Gewichtungen.
    """
    from app.models.candidate import Candidate
    from app.services.matching_engine_v2 import MatchingEngineV2

    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        raise NotFoundException("Kandidat nicht gefunden")

    matcher = MatchingEngineV2(db)
    result = await matcher.match_candidate_against_all_jobs(candidate_id)
    await db.commit()

    return {
        "candidate_id": str(candidate_id),
        "matches_created": len(result) if isinstance(result, list) else getattr(result, "total_matches_created", 0),
    }


@router.post(
    "/on-new",
    summary="Trigger: Neuer Kandidat → volle Pipeline",
    tags=["Automation"],
)
@rate_limit(RateLimitTier.AI)
async def on_new_candidate(
    request: Request,
    candidate_id: UUID = Query(..., description="ID des neuen Kandidaten"),
    db: AsyncSession = Depends(get_db),
):
    """
    n8n-Webhook: Neuer Kandidat eingetroffen.
    Loest die gesamte Kandidaten-Pipeline aus:
    1. Deep Classification (Werdegang-Analyse)
    2. GPT-Profiling
    3. Embedding generieren
    4. Matching gegen alle offene Jobs
    """
    from app.models.candidate import Candidate
    from app.services.finance_classifier_service import FinanceClassifierService

    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        raise NotFoundException("Kandidat nicht gefunden")

    steps_completed = []

    # Step 1: Deep Classification
    try:
        classifier = FinanceClassifierService(db)
        classification = await classifier.classify_candidate(candidate)
        if classification.success:
            classifier.apply_to_candidate(candidate, classification)
            await db.commit()
            steps_completed.append("classification")
    except Exception as e:
        logger.error(f"on-new classification failed for {candidate_id}: {e}")

    # Step 2: GPT-Profiling
    try:
        from app.services.profile_engine_service import ProfileEngineService
        profiler = ProfileEngineService(db)
        await profiler.profile_candidate(candidate)
        await db.commit()
        steps_completed.append("profiling")
    except Exception as e:
        logger.error(f"on-new profiling failed for {candidate_id}: {e}")

    # Step 3: Embedding
    try:
        from app.services.embedding_service import EmbeddingService
        emb_service = EmbeddingService(db)
        await emb_service.embed_candidate(candidate)
        await db.commit()
        steps_completed.append("embedding")
    except Exception as e:
        logger.error(f"on-new embedding failed for {candidate_id}: {e}")

    # Step 4: Matching gegen alle Jobs
    matches_created = 0
    try:
        from app.services.matching_engine_v2 import MatchingEngineV2
        matcher = MatchingEngineV2(db)
        result = await matcher.match_candidate_against_all_jobs(candidate_id)
        await db.commit()
        matches_created = len(result) if isinstance(result, list) else getattr(result, "total_matches_created", 0)
        steps_completed.append("matching")
    except Exception as e:
        logger.error(f"on-new matching failed for {candidate_id}: {e}")

    return {
        "candidate_id": str(candidate_id),
        "status": "completed",
        "steps_completed": steps_completed,
        "matches_created": matches_created,
    }


@router.post(
    "/maintenance/reclassify-finance",
    summary="Alle FINANCE-Kandidaten neu klassifizieren (Background)",
    tags=["Maintenance"],
)
@rate_limit(RateLimitTier.ADMIN)
async def reclassify_finance_candidates(
    request: Request,
    force: bool = False,
):
    """
    Startet Bulk Deep Classification als Background-Task.
    Fortschritt abrufbar via GET /candidates/maintenance/classification-status.
    """
    global _classification_progress

    if _classification_progress["running"]:
        return {
            "status": "already_running",
            "message": "Klassifizierung laeuft bereits",
            "progress": _classification_progress,
        }

    # Background-Task starten
    import asyncio
    asyncio.create_task(_run_classification_background(force=force))

    return {
        "status": "started",
        "message": "Klassifizierung als Background-Task gestartet. Status via GET /candidates/maintenance/classification-status",
    }


async def _run_classification_background(force: bool = False) -> None:
    """Background-Task fuer Kandidaten-Klassifizierung.

    Verarbeitet Kandidaten EINZELN mit eigener DB-Session pro Kandidat.
    5 parallel via Semaphore. Jeder Kandidat: Load → Classify → Save.
    Kein idle-in-transaction moeglich.
    """
    global _classification_progress
    import asyncio
    from app.database import async_session_maker
    from app.services.finance_classifier_service import FinanceClassifierService

    log = logging.getLogger(__name__)

    _classification_progress = {
        "running": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total": 0,
        "processed": 0,
        "classified": 0,
        "errors": 0,
        "cost_usd": 0.0,
        "last_update": datetime.now(timezone.utc).isoformat(),
        "result": None,
    }

    try:
        log.info(f"Klassifizierung Background-Task gestartet (force={force})")

        # 1. IDs laden (kurze DB-Session)
        async with async_session_maker() as db:
            from sqlalchemy import and_, select
            from app.models.candidate import Candidate

            query = (
                select(Candidate.id)
                .where(
                    and_(
                        Candidate.hotlist_category == "FINANCE",
                        Candidate.deleted_at.is_(None),
                    )
                )
            )
            if not force:
                query = query.where(Candidate.classification_data.is_(None))

            result = await db.execute(query)
            candidate_ids = [row[0] for row in result.fetchall()]

        total = len(candidate_ids)
        _classification_progress["total"] = total
        log.info(f"Klassifizierung: {total} IDs geladen (force={force})")

        if total == 0:
            _classification_progress["result"] = {"total": 0, "classified": 0, "message": "Keine Kandidaten"}
            return

        start_time = datetime.now(timezone.utc)
        semaphore = asyncio.Semaphore(2)  # 2 parallel (RPM-Schonung bei 429s)

        # Zaehler (thread-safe via asyncio single-thread)
        stats = {
            "classified": 0, "leadership": 0, "no_cv": 0,
            "no_role": 0, "errors": 0, "processed": 0,
            "input_tokens": 0, "output_tokens": 0,
            "roles": {},
            "last_errors": [],  # Letzte 5 Fehlermeldungen fuer Debugging
        }

        async def _classify_single(cid) -> None:
            """Einen einzelnen Kandidaten klassifizieren (eigene DB-Session + eigener Classifier)."""
            async with semaphore:
                try:
                    async with async_session_maker() as db:
                        # Jeder Task bekommt EIGENEN Classifier mit eigenem httpx-Client
                        classifier = FinanceClassifierService(db)
                        from app.models.candidate import Candidate

                        # Kandidat laden
                        result = await db.execute(
                            select(Candidate).where(Candidate.id == cid)
                        )
                        candidate = result.scalar_one_or_none()
                        if not candidate:
                            stats["errors"] += 1
                            return

                        # Klassifizieren
                        classification = await classifier.classify_candidate(candidate)
                        stats["input_tokens"] += classification.input_tokens
                        stats["output_tokens"] += classification.output_tokens

                        if not classification.success:
                            if classification.error == "Kein Werdegang vorhanden":
                                stats["no_cv"] += 1
                            else:
                                stats["errors"] += 1
                                err_msg = f"{cid}: {classification.error}"
                                log.warning(f"Kandidat {err_msg}")
                                if len(stats["last_errors"]) < 5:
                                    stats["last_errors"].append(err_msg)
                        elif classification.is_leadership:
                            stats["leadership"] += 1
                            classifier.apply_to_candidate(candidate, classification)
                            await db.commit()
                        elif not classification.roles:
                            stats["no_role"] += 1
                            classifier.apply_to_candidate(candidate, classification)
                            await db.commit()
                        else:
                            classifier.apply_to_candidate(candidate, classification)
                            await db.commit()
                            stats["classified"] += 1
                            for role in classification.roles:
                                stats["roles"][role] = stats["roles"].get(role, 0) + 1

                        await classifier.close()

                except Exception as e:
                    err_msg = f"{cid}: {str(e)[:200]}"
                    log.error(f"Fehler Kandidat {err_msg}")
                    stats["errors"] += 1
                    if len(stats["last_errors"]) < 5:
                        stats["last_errors"].append(err_msg)

                finally:
                    stats["processed"] += 1
                    # Live-Progress nach JEDEM Kandidaten aktualisieren
                    _classification_progress["processed"] = stats["processed"]
                    _classification_progress["classified"] = stats["classified"]
                    _classification_progress["errors"] = stats["errors"]
                    input_cost = (stats["input_tokens"] / 1_000_000) * 0.15
                    output_cost = (stats["output_tokens"] / 1_000_000) * 0.60
                    _classification_progress["cost_usd"] = round(input_cost + output_cost, 4)
                    _classification_progress["last_update"] = datetime.now(timezone.utc).isoformat()

        # In Chunks von 10 verarbeiten (kleiner = weniger RPM-Spitzen)
        chunk_size = 10
        for chunk_start in range(0, total, chunk_size):
            chunk = candidate_ids[chunk_start:chunk_start + chunk_size]
            tasks = [_classify_single(cid) for cid in chunk]
            await asyncio.gather(*tasks)

            # Logging nach Chunk
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            done = stats["processed"]
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0

            log.info(
                f"Klassifizierung: {done}/{total} "
                f"({stats['classified']} klass., {stats['errors']} err, "
                f"${_classification_progress['cost_usd']:.3f}, "
                f"{rate:.1f}/s, ETA {eta:.0f}s)"
            )

            # 2s Pause zwischen Chunks um RPM-Limit nicht zu triggern
            await asyncio.sleep(2)

        # Ergebnis
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()

        _classification_progress["result"] = {
            "total": total,
            "classified": stats["classified"],
            "leadership": stats["leadership"],
            "no_cv": stats["no_cv"],
            "no_role": stats["no_role"],
            "errors": stats["errors"],
            "cost_usd": _classification_progress["cost_usd"],
            "duration_seconds": round(duration, 1),
            "roles_distribution": stats["roles"],
            "last_errors": stats["last_errors"],
        }
        log.info(f"Klassifizierung fertig: {stats['classified']}/{total} in {duration:.0f}s")

    except Exception as e:
        log.error(f"Klassifizierung fehlgeschlagen: {e}")
        _classification_progress["result"] = {"error": str(e)}

    finally:
        _classification_progress["running"] = False
        _classification_progress["last_update"] = datetime.now(timezone.utc).isoformat()


@router.get(
    "/maintenance/classification-status",
    summary="Status der Kandidaten-Klassifizierung",
    tags=["Maintenance"],
)
@rate_limit(RateLimitTier.STANDARD)
async def candidate_classification_status(
    db: AsyncSession = Depends(get_db),
):
    """
    Zeigt den kombinierten Status: DB-Stand + Live-Fortschritt der laufenden Klassifizierung.
    """
    from sqlalchemy import text

    try:
        # DB-Stand abfragen (separate Queries fuer Robustheit)
        r1 = await db.execute(text(
            "SELECT COUNT(*) FROM candidates WHERE hotlist_category = 'FINANCE' AND deleted_at IS NULL"
        ))
        total_finance = r1.scalar() or 0

        r2 = await db.execute(text(
            "SELECT COUNT(*) FROM candidates WHERE hotlist_category = 'FINANCE' AND deleted_at IS NULL AND classification_data IS NOT NULL"
        ))
        classified = r2.scalar() or 0

        r3 = await db.execute(text(
            "SELECT COUNT(*) FROM candidates WHERE deleted_at IS NULL"
        ))
        total_all = r3.scalar() or 0

        r4 = await db.execute(text(
            "SELECT COUNT(*) FROM candidates WHERE v2_seniority_level IS NOT NULL AND deleted_at IS NULL AND hotlist_category = 'FINANCE'"
        ))
        profiled = r4.scalar() or 0

        unclassified = total_finance - classified

    except Exception as e:
        return {
            "db_status": {"error": str(e)},
            "live_progress": _classification_progress,
        }

    return {
        "db_status": {
            "total_candidates": total_all,
            "total_finance": total_finance,
            "classified": classified,
            "unclassified": unclassified,
            "profiled": profiled,
            "unprofiled": total_finance - profiled,
            "classification_percent": round((classified / total_finance * 100), 1) if total_finance > 0 else 0,
            "profiling_percent": round((profiled / total_finance * 100), 1) if total_finance > 0 else 0,
        },
        "live_progress": _classification_progress,
    }


# ==================== Temporär: n8n Massen-Profiling ====================


@router.get(
    "/maintenance/unprofiled-ids",
    summary="IDs aller unprufilierten Finance-Kandidaten",
    tags=["Maintenance"],
)
@rate_limit(RateLimitTier.STANDARD)
async def get_unprofiled_ids(
    db: AsyncSession = Depends(get_db),
):
    """Gibt IDs aller FINANCE-Kandidaten ohne v2-Profil zurueck (fuer n8n Batch)."""
    from sqlalchemy import text

    result = await db.execute(text(
        "SELECT id::text FROM candidates "
        "WHERE v2_seniority_level IS NULL AND deleted_at IS NULL "
        "AND hotlist_category = 'FINANCE' "
        "ORDER BY created_at DESC"
    ))
    ids = [row[0] for row in result.fetchall()]
    return {"count": len(ids), "ids": ids}


@router.get(
    "/maintenance/profile-data/{candidate_id}",
    summary="Rohdaten fuer GPT-Profiling eines Kandidaten",
    tags=["Maintenance"],
)
@rate_limit(RateLimitTier.STANDARD)
async def get_profile_data(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Gibt die strukturierten Kandidaten-Daten zurueck die der GPT-Prompt braucht."""
    from app.models.candidate import Candidate

    c = await db.get(Candidate, candidate_id)
    if not c:
        raise NotFoundException("Kandidat nicht gefunden")

    # Selbe Logik wie _build_candidate_input
    parts = []
    if c.current_position:
        parts.append(f"Aktuelle Position: {c.current_position}")
    if c.current_company:
        parts.append(f"Aktuelles Unternehmen: {c.current_company}")
    if c.work_history:
        parts.append("\nBerufserfahrung:")
        if isinstance(c.work_history, list):
            for entry in c.work_history[:10]:
                if isinstance(entry, dict):
                    period = entry.get("period", "")
                    title = entry.get("title", entry.get("position", ""))
                    company = entry.get("company", "")
                    tasks = entry.get("tasks", entry.get("description", ""))
                    parts.append(f"  - {period}: {title} bei {company}")
                    if tasks:
                        if isinstance(tasks, list):
                            parts.append(f"    Taetigkeiten: {'; '.join(tasks[:5])}")
                        else:
                            parts.append(f"    Taetigkeiten: {str(tasks)[:300]}")
    if c.education:
        parts.append("\nAusbildung:")
        if isinstance(c.education, list):
            for entry in c.education[:5]:
                if isinstance(entry, dict):
                    parts.append(f"  - {entry.get('degree', '')} - {entry.get('institution', '')} ({entry.get('year', '')})")
    if c.further_education:
        parts.append("\nWeiterbildungen:")
        if isinstance(c.further_education, list):
            for entry in c.further_education[:5]:
                parts.append(f"  - {str(entry)[:200]}")
    if c.skills:
        parts.append(f"\nSkills: {', '.join(c.skills[:20])}")
    if c.it_skills:
        parts.append(f"IT-Skills: {', '.join(c.it_skills[:15])}")
    if c.erp:
        parts.append(f"ERP-Systeme: {', '.join(c.erp[:10])}")
    if c.languages:
        if isinstance(c.languages, list):
            parts.append(f"Sprachen: {', '.join(str(l) for l in c.languages[:5])}")

    prompt_text = "\n".join(parts) if parts else ""

    return {
        "candidate_id": str(c.id),
        "full_name": c.full_name,
        "prompt_text": prompt_text,
        "has_data": len(prompt_text) >= 50,
    }


@router.patch(
    "/maintenance/save-profile/{candidate_id}",
    summary="GPT-Profil-Ergebnis speichern (n8n Callback)",
    tags=["Maintenance"],
)
@rate_limit(RateLimitTier.STANDARD)
async def save_profile_result(
    candidate_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Speichert das GPT-Profil-Ergebnis direkt in die v2-Felder."""
    from app.models.candidate import Candidate
    from datetime import datetime, timezone

    body = await request.json()

    c = await db.get(Candidate, candidate_id)
    if not c:
        raise NotFoundException("Kandidat nicht gefunden")

    c.v2_seniority_level = body.get("seniority_level")
    c.v2_career_trajectory = body.get("career_trajectory")
    c.v2_years_experience = body.get("years_experience")
    c.v2_current_role_summary = body.get("current_role_summary", "")[:500]
    c.v2_structured_skills = body.get("structured_skills", [])
    c.v2_certifications = body.get("certifications", [])
    c.v2_industries = body.get("industries", [])
    c.v2_profile_created_at = datetime.now(timezone.utc)
    await db.commit()

    return {
        "candidate_id": str(c.id),
        "saved": True,
        "seniority_level": c.v2_seniority_level,
    }
