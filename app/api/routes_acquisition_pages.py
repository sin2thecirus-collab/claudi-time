"""Akquise-Seite HTML Routes — Hauptseite + HTMX Partials.

GET /akquise → Hauptseite mit Tabs
GET /akquise/partials/tab/{tab_name} → Tab-Inhalt (HTMX)
GET /akquise/partials/call-screen/{job_id} → Call-Screen Panel (HTMX)
GET /akquise/partials/email-modal/{job_id} → E-Mail Modal (HTMX)
"""

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.acquisition_call import AcquisitionCall
from app.models.acquisition_email import AcquisitionEmail
from app.models.company import Company
from app.models.company_contact import CompanyContact
from app.models.job import Job

router = APIRouter(tags=["Akquise-Pages"])
templates = Jinja2Templates(directory="app/templates")

# Status-Gruppen fuer Tabs
STATUS_GROUPS = {
    "heute": ["neu", "angerufen", "wiedervorlage"],
    "neu": ["neu"],
    "wiedervorlagen": ["wiedervorlage"],
    "nicht_erreicht": ["email_gesendet", "email_followup"],
    "qualifiziert": ["qualifiziert", "stelle_erstellt"],
    "archiv": ["blacklist_hart", "blacklist_weich", "verloren", "followup_abgeschlossen"],
}


@router.get("/akquise", response_class=HTMLResponse)
async def akquise_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Akquise-Hauptseite mit Tab-Navigation."""
    counts = {}
    for tab, statuses in STATUS_GROUPS.items():
        result = await db.execute(
            select(func.count(Job.id)).where(
                Job.acquisition_source.isnot(None),
                Job.deleted_at.is_(None),
                Job.akquise_status.in_(statuses),
            )
        )
        counts[tab] = result.scalar_one()

    # Test-Modus pruefen
    from app.services.acquisition_test_helpers import is_test_mode
    test_mode = await is_test_mode(db)

    # Intelligenter Default-Tab: Wenn "Heute" leer, zum ersten nicht-leeren Tab
    active_tab = "heute"
    if counts.get("heute", 0) == 0:
        for fallback_tab in ["neu", "wiedervorlagen", "nicht_erreicht", "qualifiziert"]:
            if counts.get(fallback_tab, 0) > 0:
                active_tab = fallback_tab
                break

    return templates.TemplateResponse(
        "akquise/akquise_page.html",
        {
            "request": request,
            "tab_counts": counts,
            "active_tab": active_tab,
            "test_mode": test_mode,
        },
    )


@router.get("/akquise/partials/tab/{tab_name}", response_class=HTMLResponse)
async def tab_partial(
    tab_name: str,
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """HTMX-Partial: Tab-Inhalt laden."""
    # Sonderfall: Unzugeordnete Anrufe (eigene Tabelle, nicht Jobs)
    if tab_name == "unzugeordnet":
        return await _tab_unzugeordnet(request, page, per_page, db)

    statuses = STATUS_GROUPS.get(tab_name)
    if not statuses:
        return HTMLResponse('<p style="color:var(--pp-red);">Unbekannter Tab</p>')

    # Jobs laden
    query = (
        select(Job)
        .where(
            Job.acquisition_source.isnot(None),
            Job.deleted_at.is_(None),
            Job.akquise_status.in_(statuses),
        )
        .options(selectinload(Job.company))
        .order_by(
            Job.akquise_priority.desc().nullslast(),
            Job.first_seen_at.asc().nullslast(),
        )
    )

    # Total
    total_result = await db.execute(
        select(func.count()).select_from(query.subquery())
    )
    total = total_result.scalar_one()

    # Pagination
    query = query.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    jobs = result.scalars().all()

    # Gruppierung nach Company
    groups = _group_by_company(jobs)

    # Wiedervorlagen fuer Tab "heute"
    wiedervorlagen = []
    if tab_name == "heute":
        wiedervorlagen = await _get_faellige_wiedervorlagen(db)

    # E-Mail-Infos fuer "nicht_erreicht" Tab
    if tab_name == "nicht_erreicht":
        groups = await _enrich_email_info(db, groups)

    template_map = {
        "heute": "partials/akquise/tab_heute.html",
        "neu": "partials/akquise/tab_neu.html",
        "wiedervorlagen": "partials/akquise/tab_wiedervorlagen.html",
        "nicht_erreicht": "partials/akquise/tab_nicht_erreicht.html",
        "qualifiziert": "partials/akquise/tab_qualifiziert.html",
        "archiv": "partials/akquise/tab_archiv.html",
    }

    return templates.TemplateResponse(
        template_map[tab_name],
        {
            "request": request,
            "groups": groups,
            "total": total,
            "page": page,
            "pages": (total + per_page - 1) // per_page,
            "tab": tab_name,
            "wiedervorlagen": wiedervorlagen,
        },
    )


@router.get("/akquise/partials/call-screen/{job_id}", response_class=HTMLResponse)
async def call_screen_partial(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """HTMX-Partial: Call-Screen fuer einen Lead."""
    # Job laden
    job_result = await db.execute(
        select(Job)
        .where(Job.id == job_id)
        .options(selectinload(Job.company))
    )
    job = job_result.scalar_one_or_none()

    if not job:
        return HTMLResponse(
            '<p style="padding:40px;text-align:center;color:var(--pp-red);">Lead nicht gefunden</p>'
        )

    # Job-Daten als Dict
    job_data = {
        "id": str(job.id),
        "position": job.position,
        "company_name": job.company_name,
        "city": job.city,
        "postal_code": job.postal_code,
        "job_url": job.job_url,
        "job_text": job.job_text,
        "employment_type": job.employment_type,
        "industry": job.industry,
        "company_size": job.company_size,
        "akquise_status": job.akquise_status,
        "akquise_priority": job.akquise_priority,
        "anzeigen_id": job.anzeigen_id,
    }

    # Company-Daten
    company_data = None
    if job.company:
        company_data = {
            "id": str(job.company.id),
            "name": job.company.name,
            "acquisition_status": job.company.acquisition_status,
            "phone": job.company.phone,
            "domain": job.company.domain,
            "city": job.company.city,
        }

    # Contacts laden
    contacts = []
    if job.company_id:
        contacts_result = await db.execute(
            select(CompanyContact)
            .where(CompanyContact.company_id == job.company_id)
            .order_by(CompanyContact.created_at.desc())
        )
        contacts = [
            {
                "id": str(c.id),
                "name": c.full_name,
                "position": c.position,
                "phone": c.phone,
                "mobile": c.mobile,
                "email": c.email,
                "contact_role": c.contact_role,
            }
            for c in contacts_result.scalars().all()
        ]

    # Call-History
    calls_result = await db.execute(
        select(AcquisitionCall)
        .where(AcquisitionCall.job_id == job_id)
        .order_by(AcquisitionCall.created_at.desc())
        .limit(5)
    )
    call_history = [
        {
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "disposition": c.disposition,
            "notes": c.notes,
            "duration_seconds": c.duration_seconds,
        }
        for c in calls_result.scalars().all()
    ]

    # Email-History
    emails_result = await db.execute(
        select(AcquisitionEmail)
        .where(AcquisitionEmail.job_id == job_id)
        .order_by(AcquisitionEmail.created_at.desc())
        .limit(5)
    )
    email_history = [
        {
            "id": str(e.id),
            "email_type": e.email_type,
            "subject": e.subject,
            "status": e.status,
            "sent_at": e.sent_at.isoformat() if e.sent_at else None,
        }
        for e in emails_result.scalars().all()
    ]

    # Weitere Stellen der Firma
    other_jobs = []
    if job.company_id:
        other_result = await db.execute(
            select(Job)
            .where(
                Job.company_id == job.company_id,
                Job.id != job_id,
                Job.acquisition_source.isnot(None),
                Job.deleted_at.is_(None),
            )
            .order_by(Job.akquise_priority.desc())
        )
        other_jobs = [
            {
                "id": str(j.id),
                "position": j.position,
                "akquise_status": j.akquise_status,
            }
            for j in other_result.scalars().all()
        ]

    # Test-Modus pruefen
    from app.services.acquisition_test_helpers import is_test_mode
    test_mode = await is_test_mode(db)

    # Job-Text in Sektionen parsen
    job_sections = _parse_job_sections(job_data.get("job_text"))

    return templates.TemplateResponse(
        "partials/akquise/call_screen.html",
        {
            "request": request,
            "job": job_data,
            "company": company_data,
            "contacts": contacts,
            "call_history": call_history,
            "email_history": email_history,
            "other_jobs": other_jobs,
            "job_sections": job_sections,
            "now_date": datetime.now(timezone.utc).strftime("%d.%m.%Y"),
            "test_mode": test_mode,
        },
    )


@router.get("/akquise/partials/email-modal/{job_id}", response_class=HTMLResponse)
async def email_modal_partial(
    job_id: uuid.UUID,
    request: Request,
    contact_id: uuid.UUID | None = Query(None),
    email_type: str = Query("initial"),
    db: AsyncSession = Depends(get_db),
):
    """HTMX-Partial: E-Mail-Modal mit Mailbox-Dropdown."""
    # Job laden
    job_result = await db.execute(
        select(Job).where(Job.id == job_id)
    )
    job = job_result.scalar_one_or_none()
    if not job:
        return HTMLResponse('<p style="color:var(--pp-red);">Job nicht gefunden</p>')

    job_data = {
        "id": str(job.id),
        "position": job.position,
        "company_name": job.company_name,
    }

    # Contacts laden
    contacts = []
    if job.company_id:
        contacts_result = await db.execute(
            select(CompanyContact)
            .where(CompanyContact.company_id == job.company_id)
            .order_by(CompanyContact.created_at.desc())
        )
        contacts = [
            {
                "id": str(c.id),
                "name": c.full_name,
                "email": c.email,
            }
            for c in contacts_result.scalars().all()
            if c.email
        ]

    # Mailboxes
    from app.config import settings as app_settings

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_counts_result = await db.execute(
        select(
            AcquisitionEmail.from_email,
            func.count(AcquisitionEmail.id),
        )
        .where(
            AcquisitionEmail.sent_at >= today,
            AcquisitionEmail.status == "sent",
        )
        .group_by(AcquisitionEmail.from_email)
    )
    sent_counts = {row[0]: row[1] for row in sent_counts_result.all()}

    mailboxes = [
        {"email": app_settings.microsoft_sender_email, "purpose": "Haupt", "daily_limit": 100},
        {"email": "hamdard@sincirus-karriere.de", "purpose": "Erst-Mail", "daily_limit": 20},
        {"email": "m.hamdard@sincirus-karriere.de", "purpose": "Follow-up", "daily_limit": 20},
        {"email": "m.hamdard@jobs-sincirus.com", "purpose": "Break-up", "daily_limit": 20},
        {"email": "hamdard@jobs-sincirus.com", "purpose": "Reserve", "daily_limit": 20},
    ]
    for mb in mailboxes:
        mb["sent_today"] = sent_counts.get(mb["email"], 0)
        mb["remaining"] = mb["daily_limit"] - mb["sent_today"]

    # email_type validieren
    valid_types = ("initial", "kontaktdaten", "follow_up", "break_up", "kontaktdaten_followup")
    if email_type not in valid_types:
        email_type = "initial"

    return templates.TemplateResponse(
        "partials/akquise/email_modal.html",
        {
            "request": request,
            "job": job_data,
            "contacts": contacts,
            "mailboxes": mailboxes,
            "draft": None,
            "default_email_type": email_type,
        },
    )


# ── SSE Events ──


@router.get("/akquise/events")
async def sse_events(request: Request):
    """SSE-Stream fuer Echtzeit-Events (Rueckruf-Popup, Benachrichtigungen)."""
    from app.services.acquisition_event_bus import subscribe, unsubscribe

    queue = subscribe()

    async def event_generator():
        try:
            while True:
                # Client-Disconnect pruefen
                if await request.is_disconnected():
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    event_type = event.get("event", "message")
                    data = json.dumps(event.get("data", {}), ensure_ascii=False)
                    yield f"event: {event_type}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat alle 30s (haelt Connection offen)
                    yield ": heartbeat\n\n"
        finally:
            unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Hilfsfunktionen ──


async def _tab_unzugeordnet(
    request: Request,
    page: int,
    per_page: int,
    db: AsyncSession,
):
    """Sonderfall-Tab: Unzugeordnete Anrufe aus unassigned_calls Tabelle."""
    from app.models.unassigned_call import UnassignedCall

    # Total
    total_result = await db.execute(
        select(func.count(UnassignedCall.id)).where(UnassignedCall.assigned == False)
    )
    total = total_result.scalar_one()

    # Paginiert laden
    result = await db.execute(
        select(UnassignedCall)
        .where(UnassignedCall.assigned == False)
        .order_by(UnassignedCall.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    calls = result.scalars().all()

    call_dicts = [
        {
            "id": str(c.id),
            "phone_number": c.phone_number or "Unbekannt",
            "direction": c.direction or "inbound",
            "call_date": c.call_date,
            "duration_seconds": c.duration_seconds,
            "call_summary": c.call_summary or "",
            "transcript": c.transcript or "",
            "recording_topic": c.recording_topic or "",
            "created_at": c.created_at,
        }
        for c in calls
    ]

    return templates.TemplateResponse(
        "partials/akquise/tab_unzugeordnet.html",
        {
            "request": request,
            "calls": call_dicts,
            "total": total,
            "page": page,
            "pages": (total + per_page - 1) // per_page,
            "tab": "unzugeordnet",
        },
    )


def _group_by_company(jobs: list) -> list[dict]:
    """Gruppiert Jobs nach Company fuer die Lead-Liste."""
    groups: dict[str, dict] = {}
    for job in jobs:
        key = str(job.company_id) if job.company_id else (job.company_name or "Unbekannt")
        if key not in groups:
            groups[key] = {
                "company_id": str(job.company_id) if job.company_id else None,
                "company_name": job.company_name,
                "city": job.city,
                "company_status": job.company.acquisition_status if job.company else None,
                "jobs": [],
            }
        groups[key]["jobs"].append({
            "id": str(job.id),
            "position": job.position,
            "akquise_status": job.akquise_status,
            "akquise_priority": job.akquise_priority,
            "city": job.city,
            "employment_type": job.employment_type,
            "first_seen_at": job.first_seen_at.isoformat() if job.first_seen_at else None,
        })
    return list(groups.values())


async def _get_faellige_wiedervorlagen(db: AsyncSession) -> list[dict]:
    """Holt heute faellige Wiedervorlagen."""
    now = datetime.now(timezone.utc)
    today_end = now.replace(hour=23, minute=59, second=59)

    result = await db.execute(
        select(AcquisitionCall)
        .where(
            AcquisitionCall.follow_up_date.isnot(None),
            AcquisitionCall.follow_up_date <= today_end,
        )
        .order_by(AcquisitionCall.follow_up_date.asc())
        .limit(50)
    )
    calls = result.scalars().all()

    wiedervorlagen = []
    for call in calls:
        # Zugehoeriges Job laden
        job_result = await db.execute(
            select(Job).where(Job.id == call.job_id)
        )
        job = job_result.scalar_one_or_none()
        if not job or job.akquise_status not in ("wiedervorlage", "angerufen", "email_gesendet"):
            continue

        wiedervorlagen.append({
            "job_id": str(call.job_id),
            "company_name": job.company_name,
            "position": job.position,
            "follow_up_note": call.follow_up_note,
            "follow_up_time": call.follow_up_date.strftime("%H:%M") if call.follow_up_date else None,
        })
    return wiedervorlagen


async def _enrich_email_info(db: AsyncSession, groups: list[dict]) -> list[dict]:
    """Reichert Nicht-erreicht-Tab mit E-Mail-Infos an."""
    now = datetime.now(timezone.utc)
    for group in groups:
        for job in group["jobs"]:
            # Letzte gesendete E-Mail finden
            email_result = await db.execute(
                select(AcquisitionEmail)
                .where(
                    AcquisitionEmail.job_id == uuid.UUID(job["id"]),
                    AcquisitionEmail.status == "sent",
                )
                .order_by(AcquisitionEmail.sent_at.desc())
                .limit(1)
            )
            email = email_result.scalar_one_or_none()
            if email and email.sent_at:
                job["email_sent_at"] = email.sent_at.isoformat()
                job["days_since_email"] = (now - email.sent_at).days
                # Contact-ID fuer Follow-up Button
                job["contact_id"] = str(email.contact_id) if email.contact_id else None
            else:
                job["email_sent_at"] = None
                job["days_since_email"] = None
                job["contact_id"] = None
    return groups


# ── Job-Text Sektionen Parser ──

# Sektions-Definitionen: (type, display_title, regex_keywords)
# Die Keywords werden sowohl zeilenbasiert als auch inline erkannt.
_SECTION_DEFS: list[tuple[str, str, str]] = [
    ("tasks", "Aufgaben",
     r"ihre\s+aufgaben|aufgaben(?:bereich|gebiet|profil)?|das\s+erwartet\s+(?:sie|dich)|"
     r"t[äa]tigkeiten|(?:ihr\s+)?verantwortungsbereich|was\s+(?:sie|dich)\s+erwartet|"
     r"the\s+role|responsibilities|job\s*description"),
    ("requirements", "Anforderungen",
     r"ihr\s+profil|anforderung(?:en|sprofil)?|was\s+(?:sie|du)\s+mitbring(?:st|en)|"
     r"qualifikation(?:en)?|voraussetzung(?:en)?|das\s+bringst?\s+(?:sie|du)\s+mit|"
     r"das\s+sollten\s+(?:sie|du)|(?:sie|du)\s+bringst?\s+mit|"
     r"das\s+w[üu]nschen\s+wir\s+uns|requirements?|your\s+profile|profil"),
    ("company", "Unternehmen",
     r"[üu]ber\s+uns|(?:das\s+)?unternehmen(?:sprofil)?|wer\s+wir\s+sind|"
     r"about\s+us|the\s+company|einleitung"),
    ("benefits", "Wir bieten",
     r"wir\s+bieten|das\s+bieten\s+wir|benefits?|(?:ihre?|deine?)\s+vorteile|"
     r"was\s+wir\s+(?:(?:ihnen|dir)\s+)?bieten|unser(?:e)?\s+(?:angebot|leistungen)|"
     r"darauf\s+(?:k[öo]nnen|d[üu]rfen)\s+(?:sie|du)\s+sich\s+freuen|"
     r"what\s+we\s+offer"),
    ("contact", "Kontakt",
     r"kontakt|(?:ihre?\s+)?bewerbung|so\s+bewerben\s+(?:sie|du)\s+(?:sich|dich)|"
     r"ansprechpartner|haben\s+wir\s+(?:ihr|dein)\s+interesse|"
     r"jetzt\s+bewerben|bewirb\s+dich"),
]

# Zeilenbasiertes Pattern (Header auf eigener Zeile)
_LINE_PATTERNS = [
    (sec_type, re.compile(rf"^[\W]*({kw})[\W]*$", re.IGNORECASE), title)
    for sec_type, title, kw in _SECTION_DEFS
]

# Inline-Pattern (Header mitten im Text, gefolgt von Inhalt)
_INLINE_PATTERN = re.compile(
    r"(?:^|\s)(" + "|".join(kw for _, _, kw in _SECTION_DEFS) + r")(?:\s|$|:)",
    re.IGNORECASE,
)

# Mapping: matched keyword → (type, title)
def _classify_header(header_text: str) -> tuple[str, str] | None:
    """Ordnet einen erkannten Header-Text einem Sektionstyp zu."""
    for sec_type, title, kw in _SECTION_DEFS:
        if re.match(rf"^(?:{kw})$", header_text.strip(), re.IGNORECASE):
            return sec_type, title
    return None


def _parse_job_sections(job_text: str | None) -> list[dict]:
    """Parse job_text into structured sections.

    Strategie:
    1. Zuerst zeilenbasiert (Header auf eigener Zeile) — das ist der saubere Fall.
    2. Wenn nur 1 Sektion (= alles 'Allgemein'), dann Inline-Erkennung:
       Suche Header-Keywords mitten im Text und splitte dort.
    """
    if not job_text or not job_text.strip():
        return []

    # ── Versuch 1: Zeilenbasiert ──
    sections = _parse_line_based(job_text)
    if len(sections) > 1:
        return sections

    # ── Versuch 2: Inline-Erkennung (CSV-Texte ohne Zeilenumbrueche) ──
    return _parse_inline(job_text)


def _parse_line_based(job_text: str) -> list[dict]:
    """Parst nach Zeilenumbruechen — Header muss auf eigener Zeile stehen."""
    lines = job_text.split("\n")
    sections: list[dict] = []
    current_type = "general"
    current_title = "Allgemein"
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()

        if stripped and len(stripped) < 80 and stripped[:1] not in ("-", "*", "–"):
            matched = False
            for sec_type, pattern, sec_title in _LINE_PATTERNS:
                if pattern.match(stripped):
                    content = "\n".join(current_lines).strip()
                    if content:
                        sections.append({"type": current_type, "title": current_title, "content": content})
                    current_type = sec_type
                    current_title = sec_title
                    current_lines = []
                    matched = True
                    break
            if matched:
                continue

        current_lines.append(line)

    content = "\n".join(current_lines).strip()
    if content:
        sections.append({"type": current_type, "title": current_title, "content": content})

    return sections


def _is_mid_sentence(job_text: str, match_start: int, match_end: int) -> bool:
    """Prüft ob ein Keyword mitten im Satz steht (= kein Header).

    Ein echtes Header-Keyword steht typischerweise:
    - Am Textanfang
    - Nach Satzzeichen (. ! ? : ;)
    - Allein oder gefolgt von Aufzaehlungsinhalten

    False Positives erkennen wir an:
    1. Vor dem Keyword steht ein Kleinbuchstabe-Wort (Adjektiv/Artikel)
    2. Nach dem Keyword folgt direkt ein Verb → normaler Satz, kein Header
    """
    # Text vor dem Match
    before = job_text[:match_start].rstrip()
    if not before:
        return False  # Textanfang → Header

    last_char = before[-1]
    # Nach Satzzeichen / Aufzaehlungszeichen → Potentieller Header
    if last_char in ".!?:;–—•·-)\n":
        # Aber: Check ob nach dem Keyword ein Verb folgt (= normaler Satz)
        after = job_text[match_end:match_end + 30].strip().split()
        if after:
            first_word_after = after[0].rstrip(".,;:!?-–—").lower()
            # Typische deutsche Verben die nach einem Subjekt-Keyword stehen
            sentence_verbs = {
                "zeichnet", "ist", "hat", "bietet", "sucht", "liegt", "steht",
                "befindet", "gehört", "verfügt", "wurde", "wird", "kann",
                "soll", "muss", "darf", "möchte", "arbeitet", "beschäftigt",
            }
            if first_word_after in sentence_verbs or first_word_after == "sich":
                return True
        return False

    # Letztes Wort davor extrahieren
    words_before = before.split()
    if not words_before:
        return False

    last_word = words_before[-1].rstrip(".,;:!?-–—")
    if not last_word:
        return False

    # Wenn letztes Wort mit Kleinbuchstabe beginnt → mid-sentence
    # (Adjektive, Artikel, Pronomen: "abwechslungsreiche", "familienfreundlichen", "dein")
    if last_word[0].islower():
        return True

    return False


def _parse_inline(job_text: str) -> list[dict]:
    """Erkennt Sektions-Header auch inline im Text und splittet dort.

    Schutzmechanismen gegen False-Positives:
    1. Mid-sentence-Check: Keyword nach Kleinbuchstabe-Wort wird ignoriert
    2. Deduplizierung: Nur erstes Vorkommen jedes Sektionstyps zaehlt
    """
    # Alle Header-Positionen im Text finden
    seen_types: set[str] = set()
    matches = []
    for m in _INLINE_PATTERN.finditer(job_text):
        header_text = m.group(1).strip()
        classified = _classify_header(header_text)
        if not classified:
            continue

        sec_type, sec_title = classified

        # Filter 1: Mid-sentence → kein Header
        if _is_mid_sentence(job_text, m.start(1), m.end(1)):
            continue

        # Filter 2: Sektionstyp schon gesehen → Duplikat ignorieren
        if sec_type in seen_types:
            continue

        seen_types.add(sec_type)
        matches.append((m.start(1), m.end(1), sec_type, sec_title))

    if not matches:
        # Kein Header gefunden — gesamten Text als eine Sektion zurueckgeben
        return [{"type": "general", "title": "Stellenbeschreibung", "content": job_text.strip()}]

    sections: list[dict] = []

    # Text vor dem ersten Header
    before = job_text[:matches[0][0]].strip()
    if before:
        sections.append({"type": "general", "title": "Allgemein", "content": before})

    # Sektionen zwischen den Headern
    for i, (start, end, sec_type, sec_title) in enumerate(matches):
        # Inhalt: von Ende dieses Headers bis Anfang des naechsten
        next_start = matches[i + 1][0] if i + 1 < len(matches) else len(job_text)
        content = job_text[end:next_start].strip()
        if content:
            sections.append({"type": sec_type, "title": sec_title, "content": content})

    return sections
