"""API-Endpoints fuer direkte Kandidaten-Vorstellung.

Prefix: /api/presentations/direct

Endpoints:
- POST /extract-job          — Stellentext analysieren (GPT-4o)
- POST /skills-match         — Skills-Vergleich berechnen
- POST /generate-email       — E-Mail generieren
- POST /send                 — Presentation erstellen + n8n triggern
- POST /calculate-drive-time — Fahrzeit berechnen
- GET  /candidate/{id}       — Alle Vorstellungen eines Kandidaten
- POST /bulk/upload          — CSV hochladen + Vorschau
- POST /bulk/start           — Bulk-Versand starten (Background-Task)
- GET  /bulk/{batch_id}/status — Batch-Fortschritt
"""

import asyncio
import logging
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, async_session_maker
from app.config import get_settings
from app.services.candidate_presentation_service import CandidatePresentationService
from app.services.presentation_service import MAILBOXES

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/presentations/direct", tags=["Direkte Vorstellung"])


def _split_contact_name(full_name: str) -> tuple[str, str]:
    """Teilt einen vollen Namen in Vorname + Nachname auf.

    Beruecksichtigt Anrede-Praefixe (Herr, Frau, Dr., Prof.) die
    NICHT als Vorname gezaehlt werden sollen.

    Beispiele:
        "Max Müller" → ("Max", "Müller")
        "Herr Müller" → ("", "Müller")
        "Frau Dr. Anna Schmidt" → ("Anna", "Schmidt")
        "Max" → ("Max", "")
        "" → ("", "")
    """
    if not full_name or not full_name.strip():
        return ("", "")

    # Anrede-Praefixe entfernen
    prefixes = {"herr", "frau", "hr.", "fr.", "dr.", "prof.", "prof", "dr"}
    parts = full_name.strip().split()
    clean_parts = [p for p in parts if p.lower().rstrip(".") not in prefixes and p.lower() not in prefixes]

    if not clean_parts:
        # Nur Praefixe, kein echter Name
        return ("", full_name.strip())
    elif len(clean_parts) == 1:
        # Nur ein Wort → als Nachname behandeln
        return ("", clean_parts[0])
    else:
        # Erstes Wort = Vorname, Rest = Nachname
        return (clean_parts[0], " ".join(clean_parts[1:]))


# ── Request/Response Models ──

class ExtractJobRequest(BaseModel):
    job_posting_text: str
    check_spam: bool = True
    city: str = ""

class SkillsMatchRequest(BaseModel):
    candidate_id: str
    extracted_job_data: dict

class GenerateEmailRequest(BaseModel):
    candidate_id: str
    extracted_job_data: dict
    skills_comparison: dict
    drive_time: Optional[dict] = None
    step: int = 1

class SendPresentationRequest(BaseModel):
    candidate_id: str
    company_name: str
    city: str = ""
    contact_name: str = ""
    contact_salutation: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    email_to: str
    email_from: str
    email_subject: str
    email_body_text: str
    email_body_html: str = ""
    mailbox_used: str
    job_posting_text: Optional[str] = None
    extracted_job_data: Optional[dict] = None
    skills_comparison: Optional[dict] = None

class DriveTimeRequest(BaseModel):
    candidate_id: str
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None
    dest_plz: str = ""
    company_name: str = ""  # Firma in DB suchen → deren Koordinaten nehmen
    city: str = ""          # Zusaetzlich zur Firma-Suche

class BulkStartRequest(BaseModel):
    candidate_id: str
    rows: list[dict]


# ═══════════════════════════════════════════════════════════════
# 1. STELLENTEXT ANALYSIEREN
# ═══════════════════════════════════════════════════════════════

@router.post("/extract-job")
async def extract_job(req: ExtractJobRequest, db: AsyncSession = Depends(get_db)):
    """Stellentext → GPT-4o → Strukturierte Daten + Spam-Check."""
    extracted = await CandidatePresentationService.extract_job_data(req.job_posting_text)
    result = extracted.model_dump()

    # Spam-Check wenn gewuenscht
    if req.check_spam and extracted.company_name:
        spam = await CandidatePresentationService.check_spam_block(
            db,
            company_name=extracted.company_name,
            city=req.city or extracted.city,
        )
        result["spam_check"] = spam

    return result


# ═══════════════════════════════════════════════════════════════
# 2. SKILLS-MATCH BERECHNEN
# ═══════════════════════════════════════════════════════════════

@router.post("/skills-match")
async def skills_match(req: SkillsMatchRequest, db: AsyncSession = Depends(get_db)):
    """Kandidat vs. Job → Qualitativer Vergleich (GPT-4o)."""
    candidate_id = uuid.UUID(req.candidate_id)

    # Kandidaten-Daten laden (Privacy-konform)
    candidate_data = await CandidatePresentationService.extract_candidate_data(db, candidate_id)
    if not candidate_data:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    # DB-Session schliessen bevor GPT-Call
    comparison = await CandidatePresentationService.calculate_skills_match(
        candidate_data, req.extracted_job_data
    )
    return comparison.model_dump()


# ═══════════════════════════════════════════════════════════════
# 3. E-MAIL GENERIEREN
# ═══════════════════════════════════════════════════════════════

@router.post("/generate-email")
async def generate_email(req: GenerateEmailRequest, db: AsyncSession = Depends(get_db)):
    """Plain-Text E-Mail generieren (GPT-4o, ICH-Form)."""
    candidate_id = uuid.UUID(req.candidate_id)
    candidate_data = await CandidatePresentationService.extract_candidate_data(db, candidate_id)
    if not candidate_data:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    email = await CandidatePresentationService.generate_presentation_email(
        candidate_data=candidate_data,
        extracted_job_data=req.extracted_job_data,
        skills_comparison=req.skills_comparison,
        drive_time=req.drive_time,
        step=req.step,
    )
    return email


# ═══════════════════════════════════════════════════════════════
# 4. PRESENTATION ERSTELLEN + N8N TRIGGERN
# ═══════════════════════════════════════════════════════════════

@router.post("/send")
async def send_presentation(req: SendPresentationRequest, db: AsyncSession = Depends(get_db)):
    """Erstellt Presentation (draft) + triggert n8n Workflow."""
    import traceback as _tb
    _step = "init"
    try:
        _step = "parse_candidate_id"
        candidate_id = uuid.UUID(req.candidate_id)

        # Blocklist-Check: Empfaenger-Domain pruefen
        _step = "blocklist_check"
        from app.api.routes_presentation_reply import is_domain_blocked
        recipient_blocked = await is_domain_blocked(db, req.email_to)
        if recipient_blocked:
            raise HTTPException(
                status_code=409,
                detail="Domain ist auf der Blockliste",
            )

        # Domain-Kapazitaet pruefen
        _step = "import_domain_protection"
        from app.services.domain_protection_service import check_domain_capacity, get_domain_for_company

        _step = "check_domain_capacity"
        domain_check = await check_domain_capacity(db, req.email_from)
        if not domain_check["allowed"]:
            raise HTTPException(
                status_code=429,
                detail=f"Domain-Limit erreicht: {domain_check['domain']} — {domain_check['sent_today']}/{domain_check['limit']} heute. Bitte morgen erneut versuchen.",
            )

        # Domain-Konsistenz: Warnung wenn Firma zuvor von anderer Domain kontaktiert
        _step = "get_domain_for_company"
        previous_domain = await get_domain_for_company(db, req.company_name)
        selected_domain = req.email_from.split("@", 1)[1].lower() if "@" in req.email_from else ""
        domain_warning = None
        if previous_domain and previous_domain != selected_domain:
            domain_warning = f"Achtung: Firma wurde zuvor von {previous_domain} kontaktiert, jetzt {selected_domain}"
            logger.warning(f"Domain-Konsistenz: {domain_warning}")

        # Company finden/erstellen
        _step = "import_company_service"
        from app.services.company_service import CompanyService

        _step = "get_or_create_company"
        company_svc = CompanyService(db)

        # Adresse aus extracted_job_data zusammenbauen
        ejd = req.extracted_job_data or {}
        company_address = ejd.get("address", "")
        company_plz = ejd.get("plz", "")
        company_domain = ejd.get("domain", "")
        # PLZ + Ort + Strasse → vollstaendige Adresse
        address_parts = [p for p in [company_address, company_plz, req.city] if p and p.strip()]
        full_address = ", ".join(address_parts) if address_parts else ""

        company = await company_svc.get_or_create_by_name(
            req.company_name,
            city=req.city,
            domain=company_domain,
            address=full_address,
        )
        if not company:
            raise HTTPException(status_code=409, detail="Firma ist auf der Blacklist")

        # Contact erstellen/finden (mit Duplikat-Erkennung + Auto-Anrede)
        _step = "create_contact"
        contact_id = None
        if req.contact_email or req.contact_name:
            # Name korrekt aufteilen (Vorname / Nachname)
            first_name, last_name = _split_contact_name(req.contact_name)

            contact = await company_svc.get_or_create_contact(
                company_id=company.id,
                first_name=first_name,
                last_name=last_name,
                email=req.contact_email or None,
                phone=req.contact_phone or None,
                salutation=req.contact_salutation or None,
                source="vorstellung",
            )
            contact_id = contact.id

        # Presentation erstellen (draft)
        _step = "create_presentation"
        presentation = await CandidatePresentationService.create_direct_presentation(
            db=db,
            candidate_id=candidate_id,
            company_id=company.id,
            contact_id=contact_id,
            email_to=req.email_to,
            email_from=req.email_from,
            email_subject=req.email_subject,
            email_body_text=req.email_body_text,
            email_body_html=req.email_body_html,
            mailbox_used=req.mailbox_used,
            source="candidate_direct",
            job_posting_text=req.job_posting_text,
            extracted_job_data=req.extracted_job_data,
            skills_comparison=req.skills_comparison,
        )

        # KRITISCH: Alle Daten als Dict extrahieren BEVOR db.commit()!
        _step = "extract_dict"
        presentation_dict = {
            "id": str(presentation.id),
            "candidate_id": str(presentation.candidate_id) if presentation.candidate_id else None,
            "company_id": str(presentation.company_id) if presentation.company_id else None,
            "contact_id": str(presentation.contact_id) if presentation.contact_id else None,
            "email_to": presentation.email_to,
            "email_from": presentation.email_from,
            "email_subject": presentation.email_subject,
            "email_body_text": presentation.email_body_text,
            "email_body_html": getattr(presentation, "email_body_html", "") or "",
            "mailbox_used": presentation.mailbox_used,
            "reply_to_email": getattr(presentation, "reply_to_email", None) or presentation.email_from or "hamdard@sincirus.com",
            "source": getattr(presentation, "source", "candidate_direct"),
            "status": presentation.status,
        }
        company_id_str = str(company.id)

        _step = "db_commit"
        await db.commit()

        # n8n triggern (DB-Session ist bereits geschlossen — Railway 30s Timeout!)
        _step = "trigger_n8n"
        n8n_success = await _trigger_direct_n8n(presentation_dict, req.contact_name)

        # Status auf "sending" setzen — "sent" kommt erst durch n8n Callback
        # (POST /api/presentations/{id}/sent wird von n8n NACH erfolgreichem
        #  SMTP/Outlook-Versand aufgerufen und setzt dann status="sent" + sent_at)
        final_status = presentation_dict["status"]
        if n8n_success:
            _step = "update_status_sending"
            try:
                from app.models.client_presentation import ClientPresentation
                from sqlalchemy import update

                async with async_session_maker() as db_update:
                    await db_update.execute(
                        update(ClientPresentation)
                        .where(ClientPresentation.id == uuid.UUID(presentation_dict["id"]))
                        .values(status="sending")
                    )
                    await db_update.commit()
                final_status = "sending"
                logger.info(f"Presentation {presentation_dict['id']} Status → sending (n8n Callback setzt 'sent')")
            except Exception as e:
                logger.error(f"Status-Update auf 'sending' fehlgeschlagen: {e}")
                # n8n hat trotzdem gesendet — nicht den ganzen Request failen lassen

        return {
            "presentation_id": presentation_dict["id"],
            "company_id": company_id_str,
            "status": final_status,
            "n8n_triggered": n8n_success,
            "domain_warning": domain_warning,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"send_presentation FAILED at step={_step}: {type(e).__name__}: {e}\n{_tb.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "send_failed",
                "step": _step,
                "exception": type(e).__name__,
                "message": str(e)[:500],
            },
        )


# ═══════════════════════════════════════════════════════════════
# 5. FAHRZEIT BERECHNEN
# ═══════════════════════════════════════════════════════════════

@router.post("/calculate-drive-time")
async def calculate_drive_time(req: DriveTimeRequest, db: AsyncSession = Depends(get_db)):
    """Fahrzeit Kandidat → Firma berechnen.

    Drei Wege die Firma-Koordinaten zu finden:
    1. dest_lat/dest_lng direkt angegeben → nutzen
    2. company_name angegeben → Firma in DB suchen → deren Koordinaten
    3. address + city angegeben → Geocoden via Google Maps
    """
    from app.services.distance_matrix_service import DistanceMatrixService
    from app.models.candidate import Candidate
    from app.models.company import Company
    from sqlalchemy import select, func

    candidate_id = uuid.UUID(req.candidate_id)

    # Kandidat-Koordinaten laden
    result = await db.execute(
        select(
            func.ST_Y(func.ST_GeomFromWKB(Candidate.address_coords)).label("lat"),
            func.ST_X(func.ST_GeomFromWKB(Candidate.address_coords)).label("lng"),
            Candidate.plz,
        ).where(Candidate.id == candidate_id)
    )
    cand = result.first()
    if not cand or not cand.lat or not cand.lng:
        return {"error": "Kandidat hat keine Koordinaten", "car_min": None, "transit_min": None}

    # Firma-Koordinaten bestimmen (3 Wege)
    dest_lat = req.dest_lat
    dest_lng = req.dest_lng
    dest_plz = req.dest_plz

    # Weg 2: Firma in DB suchen
    if (not dest_lat or not dest_lng) and req.company_name:
        company_query = select(
            func.ST_Y(func.ST_GeomFromWKB(Company.location_coords)).label("lat"),
            func.ST_X(func.ST_GeomFromWKB(Company.location_coords)).label("lng"),
            Company.plz,
        ).where(func.lower(Company.name) == req.company_name.strip().lower())
        if req.city:
            company_query = company_query.where(func.lower(Company.city) == req.city.strip().lower())
        company_query = company_query.limit(1)
        comp_result = await db.execute(company_query)
        comp = comp_result.first()
        if comp and comp.lat and comp.lng:
            dest_lat = comp.lat
            dest_lng = comp.lng
            dest_plz = comp.plz or dest_plz
            logger.info(f"Firma '{req.company_name}' in DB gefunden: {dest_lat}, {dest_lng}")

    if not dest_lat or not dest_lng:
        return {"error": "Firma-Koordinaten nicht ermittelbar", "car_min": None, "transit_min": None}

    # Drive-Time berechnen (OHNE DB-Session!)
    dm_service = DistanceMatrixService.get_instance()
    dt_result = await dm_service.get_drive_time(
        origin_lat=cand.lat,
        origin_lng=cand.lng,
        origin_plz=cand.plz or "",
        dest_lat=dest_lat,
        dest_lng=dest_lng,
        dest_plz=dest_plz,
    )

    return {
        "car_min": dt_result.car_min,
        "transit_min": dt_result.transit_min,
        "car_km": dt_result.car_km,
        "status": dt_result.status,
    }


# ═══════════════════════════════════════════════════════════════
# 6. VORSTELLUNGEN FUER KANDIDAT (Tab-Query)
# ═══════════════════════════════════════════════════════════════

@router.get("/candidate/{candidate_id}")
async def get_candidate_presentations(candidate_id: str, db: AsyncSession = Depends(get_db)):
    """Alle Vorstellungen eines Kandidaten (chronologisch)."""
    cid = uuid.UUID(candidate_id)
    presentations = await CandidatePresentationService.get_presentations_for_candidate(db, cid)
    return {"presentations": presentations, "total": len(presentations)}


# ═══════════════════════════════════════════════════════════════
# 7. CSV-BULK: UPLOAD + VORSCHAU
# ═══════════════════════════════════════════════════════════════

@router.post("/bulk/upload")
async def bulk_upload(
    candidate_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """CSV hochladen → Parsen → Vorschau mit Spam-Check pro Zeile."""
    from app.services.bulk_presentation_service import parse_csv, preview_bulk

    content = await file.read()
    rows = parse_csv(content)

    if not rows:
        raise HTTPException(status_code=400, detail="Keine gueltigen Zeilen in CSV gefunden")

    if len(rows) > 100:
        raise HTTPException(status_code=400, detail="Maximal 100 Zeilen pro CSV-Upload")

    cid = uuid.UUID(candidate_id)
    annotated, estimated_cost = await preview_bulk(async_session_maker, cid, rows)

    sendable = [r for r in annotated if r.get("can_send")]
    skipped = [r for r in annotated if not r.get("can_send")]

    return {
        "total_rows": len(rows),
        "sendable": len(sendable),
        "skipped": len(skipped),
        "rows": annotated,
        "mailboxes": MAILBOXES,
        "estimated_cost": estimated_cost,
    }


# ═══════════════════════════════════════════════════════════════
# 8. CSV-BULK: START (Background-Task)
# ═══════════════════════════════════════════════════════════════

@router.post("/bulk/start")
async def bulk_start(
    req: BulkStartRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Startet den Bulk-Versand als Background-Task."""
    from app.models.presentation_batch import PresentationBatch
    from app.services.bulk_presentation_service import process_bulk
    from sqlalchemy import select

    cid = uuid.UUID(req.candidate_id)

    # Parallel-Schutz: Pruefen ob bereits ein Bulk-Versand laeuft
    existing = await db.execute(
        select(PresentationBatch).where(
            PresentationBatch.candidate_id == cid,
            PresentationBatch.status == "processing",
        ).limit(1)
    )
    if existing.first():
        raise HTTPException(
            status_code=409,
            detail="Es läuft bereits ein Bulk-Versand für diesen Kandidaten",
        )

    batch_id = uuid.uuid4()

    # Batch-Record erstellen
    batch = PresentationBatch(
        id=batch_id,
        candidate_id=cid,
        csv_filename="bulk_upload.csv",
        total_rows=len(req.rows),
        status="processing",
    )
    db.add(batch)
    await db.commit()

    # Background-Task starten
    background_tasks.add_task(process_bulk, cid, req.rows, batch_id)

    return {
        "batch_id": str(batch_id),
        "total_rows": len(req.rows),
        "status": "processing",
    }


# ═══════════════════════════════════════════════════════════════
# 9. CSV-BULK: STATUS
# ═══════════════════════════════════════════════════════════════

@router.get("/bulk/{batch_id}/status")
async def bulk_status(batch_id: str, db: AsyncSession = Depends(get_db)):
    """Batch-Fortschritt abfragen."""
    from app.models.presentation_batch import PresentationBatch

    bid = uuid.UUID(batch_id)
    batch = await db.get(PresentationBatch, bid)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch nicht gefunden")

    return {
        "batch_id": str(batch.id),
        "status": batch.status,
        "total_rows": batch.total_rows,
        "processed": batch.processed,
        "skipped": batch.skipped,
        "errors": batch.errors,
        "mailbox_distribution": batch.mailbox_distribution,
        "error_details": batch.error_details,
    }


# ═══════════════════════════════════════════════════════════════
# HELPER: n8n Trigger fuer direkte Vorstellung
# ═══════════════════════════════════════════════════════════════

async def _trigger_direct_n8n(presentation_data: dict, contact_name: str = "") -> bool:
    """Triggert n8n Workflow fuer direkte Vorstellung (Plain-Text, kein PDF).

    Args:
        presentation_data: Dict mit Presentation-Daten (KEIN ORM-Objekt!)
        contact_name: Name des Ansprechpartners
    """
    if not settings.n8n_webhook_url:
        logger.warning("_trigger_direct_n8n: n8n_webhook_url nicht konfiguriert")
        return False

    webhook_url = f"{settings.n8n_webhook_url}/webhook/kunde-vorstellen"
    pres_id = presentation_data["id"]
    email_body_html = presentation_data.get("email_body_html", "") or ""

    payload = {
        "presentation_id": pres_id,
        "match_id": None,
        "candidate_id": presentation_data.get("candidate_id"),
        "company_id": presentation_data.get("company_id"),
        "contact_id": presentation_data.get("contact_id"),
        "email_to": presentation_data.get("email_to"),
        "email_from": presentation_data.get("email_from"),
        "email_subject": presentation_data.get("email_subject"),
        "email_body_text": presentation_data.get("email_body_text"),
        "email_body_html": email_body_html,
        "email_signature_html": None,
        "mailbox_used": presentation_data.get("mailbox_used"),
        "pdf_attached": False,
        "pdf_base64": None,
        "pdf_filename": None,
        "presentation_mode": "ai_generated",
        "contact_name": contact_name,
        "reply_to": presentation_data.get("reply_to_email") or presentation_data.get("email_from") or "hamdard@sincirus.com",
        "email_format": "html" if email_body_html else "plain_text",
        "source": presentation_data.get("source", "candidate_direct"),
        "followup_schedule": {"step2_days": 3, "step3_days": 7},
        # Token fuer n8n Callback (damit n8n sich beim Rueckruf authentifizieren kann)
        "callback_auth_token": f"Bearer {settings.n8n_api_token}" if settings.n8n_api_token else "",
    }

    headers = {}
    if settings.n8n_api_token:
        headers["Authorization"] = f"Bearer {settings.n8n_api_token}"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(webhook_url, json=payload, headers=headers)

        if resp.status_code == 200:
            logger.info(f"n8n getriggert fuer Presentation {pres_id}")
            return True
        else:
            logger.error(f"n8n Fehler: Status={resp.status_code}, Body={resp.text[:300]}")
            return False
    except Exception as e:
        logger.error(f"n8n Trigger fehlgeschlagen fuer {pres_id}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# 10. DOMAIN-SCHUTZ: Statistiken + Limits
# ═══════════════════════════════════════════════════════════════

@router.get("/domain-stats")
async def domain_stats(db: AsyncSession = Depends(get_db)):
    """Domain-Statistiken: Sent heute, Limits, Kapazitaet pro Domain."""
    from app.services.domain_protection_service import get_all_domain_stats, is_in_sending_window
    stats = await get_all_domain_stats(db)
    return {
        "domains": stats,
        "in_sending_window": is_in_sending_window(),
        "total_sent": sum(d["sent_today"] for d in stats),
        "total_remaining": sum(d["remaining"] for d in stats),
    }
