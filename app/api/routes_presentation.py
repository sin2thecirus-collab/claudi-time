"""Presentation Routes — API fuer Kandidaten-Vorstellung an Unternehmen (Kunde Vorstellen).

Endpoints:
- GET  /api/presentations/modal-data/{match_id}    → Modal-Daten laden
- POST /api/presentations/send                      → Vorstellung senden (+ n8n Trigger)
- POST /api/presentations/generate-email/{match_id} → KI-E-Mail generieren
- POST /api/presentations/generate-followup/{id}    → Follow-Up E-Mail generieren
- GET  /api/presentations/match/{match_id}          → Vorstellungen pro Match
- GET  /api/presentations/by-email                  → Vorstellung per E-Mail finden (n8n)
- GET  /api/presentations/{id}                      → Einzelne Vorstellung laden (n8n)
- POST /api/presentations/{id}/stop                 → Sequenz stoppen
- POST /api/presentations/{id}/sent                 → Versand bestaetigen (n8n Callback)
- POST /api/presentations/{id}/response             → Kunden-Antwort (n8n Callback)
- POST /api/presentations/{id}/followup             → Follow-Up Status (n8n Callback)
- POST /api/presentations/{id}/fallback-result      → Fallback-Ergebnis (n8n Callback)
"""

import logging
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services.presentation_service import PresentationService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Client-Presentations"])


# ── n8n Token-Verifizierung ─────────────────────────────────

async def verify_n8n_token(authorization: str = Header(default="")):
    """Prueft den n8n API-Token (fuer Inbound-Callbacks von n8n)."""
    if not settings.n8n_api_token:
        logger.warning(
            "verify_n8n_token: N8N_API_TOKEN nicht konfiguriert — "
            "Callback-Endpoints sind UNGESCHUETZT! Bitte Token in Railway setzen."
        )
        return
    expected = f"Bearer {settings.n8n_api_token}"
    if authorization != expected:
        logger.warning(f"verify_n8n_token: Ungueltiger Token-Versuch")
        raise HTTPException(status_code=401, detail="Ungueltiger n8n-Token")


# ── Pydantic Schemas ─────────────────────────────────────────

class SendPresentationRequest(BaseModel):
    """Request-Body fuer POST /api/presentations/send."""
    match_id: UUID
    contact_id: Optional[UUID] = None
    email_to: str
    email_from: str
    email_subject: str
    email_body_text: Optional[str] = None
    email_signature_html: Optional[str] = None
    mailbox_used: Optional[str] = None
    presentation_mode: str = Field(default="ai_generated")
    pdf_attached: bool = Field(default=True)
    pdf_r2_key: Optional[str] = None


class ClientResponseRequest(BaseModel):
    """Request-Body fuer POST /api/presentations/{id}/response (n8n Callback)."""
    category: str
    response_text: Optional[str] = ""
    raw_email: Optional[str] = ""


class FollowupUpdateRequest(BaseModel):
    """Request-Body fuer POST /api/presentations/{id}/followup (n8n Callback)."""
    step: int = Field(..., ge=2, le=3, description="Follow-Up Step (2 oder 3)")


class SentConfirmRequest(BaseModel):
    """Request-Body fuer POST /api/presentations/{id}/sent (n8n Callback)."""
    n8n_execution_id: Optional[str] = None


class FallbackResultRequest(BaseModel):
    """Request-Body fuer POST /api/presentations/{id}/fallback-result (n8n Callback)."""
    successful_email: Optional[str] = None
    attempts: list = Field(default_factory=list, description="Liste von Versuchen [{email, status, tried_at}]")


# ── n8n Workflow Trigger ─────────────────────────────────────

async def _trigger_n8n_workflow(presentation_data: dict, contact_name: str | None = None) -> bool:
    """Triggert den n8n Workflow fuer Kunde-Vorstellen.

    Sendet alle relevanten Daten als JSON-Payload an den n8n Webhook.
    Wenn pdf_attached=True, wird das Kandidaten-PDF generiert und als
    base64 im Payload mitgesendet (n8n erstellt daraus den Anhang).

    Args:
        presentation_data: Dict mit Presentation-Daten (KEIN ORM-Objekt! Muss vor db.commit() extrahiert werden)
        contact_name: Name des Ansprechpartners (optional)

    Returns:
        True bei Erfolg, False bei Fehler
    """
    if not settings.n8n_webhook_url:
        logger.warning(
            "_trigger_n8n_workflow: n8n_webhook_url nicht konfiguriert — "
            "Workflow wird NICHT getriggert"
        )
        return False

    webhook_url = f"{settings.n8n_webhook_url}/webhook/kunde-vorstellen"
    pres_id = presentation_data["id"]

    # PDF generieren wenn angefordert
    pdf_base64 = None
    pdf_filename = None
    if presentation_data.get("pdf_attached") and presentation_data.get("candidate_id"):
        try:
            import base64
            from app.database import async_session_maker
            from app.services.profile_pdf_service import ProfilePdfService

            async with async_session_maker() as pdf_db:
                pdf_service = ProfilePdfService(pdf_db)
                pdf_bytes = await pdf_service.generate_profile_pdf(
                    presentation_data["candidate_id"]
                )
                pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
                pdf_filename = "Kandidatenprofil.pdf"
                logger.info(
                    f"PDF generiert fuer Presentation {pres_id} "
                    f"({len(pdf_bytes)} bytes)"
                )
        except Exception as e:
            logger.warning(
                f"PDF-Generierung fuer n8n fehlgeschlagen: {e} — "
                f"E-Mail wird ohne Anhang gesendet"
            )

    payload = {
        "presentation_id": presentation_data["id"],
        "match_id": presentation_data.get("match_id"),
        "candidate_id": presentation_data.get("candidate_id"),
        "job_id": presentation_data.get("job_id"),
        "company_id": presentation_data.get("company_id"),
        "contact_id": presentation_data.get("contact_id"),
        "email_to": presentation_data.get("email_to"),
        "email_from": presentation_data.get("email_from"),
        "email_subject": presentation_data.get("email_subject"),
        "email_body_text": presentation_data.get("email_body_text"),
        "email_signature_html": presentation_data.get("email_signature_html"),
        "mailbox_used": presentation_data.get("mailbox_used"),
        "pdf_r2_key": presentation_data.get("pdf_r2_key"),
        "pdf_attached": presentation_data.get("pdf_attached"),
        "presentation_mode": presentation_data.get("presentation_mode"),
        "contact_name": contact_name,
        "pdf_base64": pdf_base64,
        "pdf_filename": pdf_filename,
    }

    headers = {}
    if settings.n8n_api_token:
        headers["Authorization"] = f"Bearer {settings.n8n_api_token}"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                webhook_url,
                json=payload,
                headers=headers,
            )

        if resp.status_code == 200:
            logger.info(
                f"n8n Workflow getriggert fuer Presentation {pres_id} "
                f"(Status={resp.status_code})"
            )
            return True
        else:
            logger.error(
                f"n8n Workflow Trigger fehlgeschlagen: "
                f"Status={resp.status_code}, Body={resp.text[:500]}"
            )
            return False

    except httpx.TimeoutException:
        logger.error(
            f"n8n Workflow Trigger Timeout fuer Presentation {pres_id}"
        )
        return False
    except Exception as e:
        logger.error(
            f"n8n Workflow Trigger Fehler fuer Presentation {pres_id}: {e}"
        )
        return False


# ═══════════════════════════════════════════════════════════════
# API ENDPOINTS
# ═══════════════════════════════════════════════════════════════


@router.post("/api/presentations/generate-email/{match_id}")
async def generate_email(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Generiert eine KI-Vorstellungs-E-Mail via Claude Sonnet.

    Laedt Match-Daten und generiert einen professionellen E-Mail-Entwurf
    mit Betreff, Body-Text und HTML-Signatur.
    """
    from app.services.email_generator_service import EmailGeneratorService

    service = EmailGeneratorService(db)

    try:
        result = await service.generate_presentation_email(match_id)
    except Exception as e:
        logger.error(f"generate_email fehlgeschlagen fuer Match {match_id}: {e}")
        raise HTTPException(status_code=500, detail=f"E-Mail-Generierung fehlgeschlagen: {e}")

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.post("/api/presentations/generate-followup/{presentation_id}")
async def generate_followup(
    presentation_id: UUID,
    step: int = 2,
    db: AsyncSession = Depends(get_db),
):
    """Generiert eine Follow-Up E-Mail via Claude Sonnet.

    Step 2 = 1. Erinnerung (nach 2 Tagen)
    Step 3 = 2. Erinnerung (nach 3 weiteren Tagen)
    """
    from app.services.email_generator_service import EmailGeneratorService

    service = EmailGeneratorService(db)

    try:
        result = await service.generate_followup_email(presentation_id, step)
    except Exception as e:
        logger.error(f"generate_followup fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=f"Follow-Up-Generierung fehlgeschlagen: {e}")

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.get("/api/presentations/modal-data/{match_id}")
async def get_modal_data(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Laedt alle Daten fuer das Vorstellungs-Modal.

    Gibt Match, Job, Kandidat, Unternehmen, Kontakte, Mailboxes
    und den already_presented Status zurueck.
    """
    service = PresentationService(db)

    try:
        data = await service.get_modal_data(match_id)
    except Exception as e:
        logger.error(f"get_modal_data fehlgeschlagen fuer Match {match_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if "error" in data:
        raise HTTPException(status_code=404, detail=data["error"])

    return data


@router.get("/api/presentations/debug-send-check")
async def debug_send_check(
    db: AsyncSession = Depends(get_db),
):
    """Debug-Endpoint: Prueft ob alle Voraussetzungen fuer E-Mail-Versand erfuellt sind.

    Rufe diesen Endpoint auf BEVOR du auf 'Senden' klickst.
    Zeigt genau, was funktioniert und was nicht.
    """
    checks = {}

    # 1. n8n Webhook URL konfiguriert?
    n8n_url = settings.n8n_webhook_url
    checks["n8n_webhook_url_configured"] = {
        "status": "OK" if n8n_url else "FEHLER",
        "value": n8n_url if n8n_url else "NICHT GESETZT",
        "detail": "N8N_WEBHOOK_URL muss in Railway gesetzt sein" if not n8n_url else None,
    }

    # 2. n8n API Token konfiguriert?
    n8n_token = settings.n8n_api_token
    checks["n8n_api_token_configured"] = {
        "status": "OK" if n8n_token else "WARNUNG",
        "value": f"{n8n_token[:8]}..." if n8n_token else "NICHT GESETZT",
        "detail": "Optional aber empfohlen fuer Authentifizierung" if not n8n_token else None,
    }

    # 3. n8n Webhook erreichbar?
    webhook_url = f"{n8n_url}/webhook/kunde-vorstellen" if n8n_url else None
    checks["n8n_webhook_reachable"] = {"status": "NICHT GEPRUEFT", "url": webhook_url}

    if n8n_url:
        try:
            headers = {}
            if n8n_token:
                headers["Authorization"] = f"Bearer {n8n_token}"
            # HEAD/GET Request zum Testen (n8n Webhooks antworten auf GET mit "Webhook is not for GET")
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    webhook_url,
                    headers=headers,
                )
            # n8n gibt bei aktiven POST-only Webhooks auf GET-Anfrage:
            # 200 = Test-Trigger aktiv, 405 = POST-only (normal!)
            # 404 mit "POST request" in Body = Webhook EXISTIERT, akzeptiert nur POST → OK!
            # 404 ohne "POST request" = Webhook nicht registriert → FEHLER
            resp_text = resp.text[:300]
            is_post_only_404 = (
                resp.status_code == 404
                and "POST request" in resp_text
            )
            if resp.status_code in (200, 405) or is_post_only_404:
                checks["n8n_webhook_reachable"] = {
                    "status": "OK",
                    "url": webhook_url,
                    "http_status": resp.status_code,
                    "detail": "Webhook ist erreichbar und aktiv (POST-only)" if is_post_only_404
                              else "Webhook ist erreichbar und aktiv",
                }
            elif resp.status_code == 404:
                checks["n8n_webhook_reachable"] = {
                    "status": "FEHLER",
                    "url": webhook_url,
                    "http_status": 404,
                    "detail": "Webhook '/webhook/kunde-vorstellen' existiert NICHT in n8n! "
                              "Der Workflow muss in n8n aktiviert sein.",
                    "response_body": resp_text,
                }
            else:
                checks["n8n_webhook_reachable"] = {
                    "status": "WARNUNG",
                    "url": webhook_url,
                    "http_status": resp.status_code,
                    "detail": f"Unerwarteter Status-Code {resp.status_code}",
                    "response_body": resp.text[:300],
                }
        except httpx.TimeoutException:
            checks["n8n_webhook_reachable"] = {
                "status": "FEHLER",
                "url": webhook_url,
                "detail": "TIMEOUT — n8n antwortet nicht innerhalb 10s",
            }
        except Exception as e:
            checks["n8n_webhook_reachable"] = {
                "status": "FEHLER",
                "url": webhook_url,
                "detail": f"Verbindungsfehler: {str(e)}",
            }

    # 4. Anthropic Opus Key (fuer E-Mail-Generierung)
    opus_key = settings.anthropic_opus_api_key
    std_key = settings.anthropic_api_key
    checks["anthropic_opus_key"] = {
        "status": "OK" if opus_key else ("WARNUNG" if std_key else "FEHLER"),
        "value": f"{opus_key[:12]}..." if opus_key else ("Fallback auf Standard-Key" if std_key else "NICHT GESETZT"),
    }

    # 5. DB-Verbindung pruefen
    try:
        from sqlalchemy import text
        result = await db.execute(text("SELECT 1"))
        checks["database"] = {"status": "OK", "detail": "PostgreSQL erreichbar"}
    except Exception as e:
        checks["database"] = {"status": "FEHLER", "detail": str(e)}

    # 6. Zusammenfassung
    all_ok = all(
        c.get("status") == "OK"
        for key, c in checks.items()
        if key in ("n8n_webhook_url_configured", "n8n_webhook_reachable", "database")
    )

    return {
        "send_will_work": all_ok,
        "summary": (
            "Alle Checks bestanden — E-Mail-Versand sollte funktionieren"
            if all_ok
            else "PROBLEME ERKANNT — E-Mail wird vermutlich NICHT versendet. Siehe Details."
        ),
        "checks": checks,
        "flow_explanation": {
            "1_klick_senden": "Frontend ruft POST /api/presentations/send auf",
            "2_db_erstellen": "Backend erstellt ClientPresentation in DB (das klappt immer)",
            "3_n8n_trigger": f"Backend ruft {webhook_url or 'NICHT KONFIGURIERT'} auf",
            "4_n8n_sendet": "n8n empfaengt Payload und sendet E-Mail via Outlook/IONOS SMTP",
            "5_callback": "n8n meldet Erfolg zurueck an POST /api/presentations/{{id}}/sent",
        },
    }


@router.post("/api/presentations/send")
async def send_presentation(
    body: SendPresentationRequest,
    db: AsyncSession = Depends(get_db),
):
    """Erstellt eine Vorstellung und triggert den n8n Workflow.

    1. Erstellt ClientPresentation + CompanyCorrespondence
    2. Updated Candidate.presented_at_companies
    3. Updated Match.presentation_status
    4. Triggert n8n Webhook fuer E-Mail-Versand + Follow-Up-Sequenz
    """
    service = PresentationService(db)

    # Daten aus Request zusammenstellen
    data = {
        "match_id": body.match_id,
        "contact_id": body.contact_id,
        "email_to": body.email_to,
        "email_from": body.email_from,
        "email_subject": body.email_subject,
        "email_body_text": body.email_body_text,
        "email_signature_html": body.email_signature_html,
        "mailbox_used": body.mailbox_used,
        "presentation_mode": body.presentation_mode,
        "pdf_attached": body.pdf_attached,
        "pdf_r2_key": body.pdf_r2_key,
    }

    try:
        presentation = await service.create_presentation(data)
    except ValueError as e:
        logger.warning(f"send_presentation Validierungsfehler: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"send_presentation fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=f"Fehler beim Erstellen der Vorstellung: {e}")

    # KRITISCH: create_presentation() hat intern db.commit() aufgerufen.
    # In async SQLAlchemy sind ORM-Attribute nach commit() EXPIRED und
    # koennen NICHT lazy-loaded werden (MissingGreenlet Error!).
    # Deshalb: refresh() ODER Dict direkt aus body + presentation.id bauen.
    # Wir nutzen body-Daten (die wir schon haben) + presentation.id via refresh.
    try:
        await db.refresh(presentation)
    except Exception as e:
        # Fallback: Dict aus body-Daten bauen (presentation.id ist ggf. nicht verfuegbar)
        logger.warning(f"presentation refresh fehlgeschlagen: {e} — nutze body-Daten als Fallback")
        # Wir brauchen zumindest die presentation_id aus der DB
        from sqlalchemy import text
        row = await db.execute(
            text("SELECT id FROM client_presentations WHERE match_id = :mid ORDER BY created_at DESC LIMIT 1"),
            {"mid": str(body.match_id)},
        )
        pres_row = row.first()
        presentation_id = str(pres_row[0]) if pres_row else "unknown"
        presentation_dict = {
            "id": presentation_id,
            "match_id": str(body.match_id),
            "candidate_id": None,
            "job_id": None,
            "company_id": None,
            "contact_id": str(body.contact_id) if body.contact_id else None,
            "email_to": body.email_to,
            "email_from": body.email_from,
            "email_subject": body.email_subject,
            "email_body_text": body.email_body_text,
            "email_signature_html": body.email_signature_html,
            "mailbox_used": body.mailbox_used,
            "pdf_r2_key": body.pdf_r2_key,
            "pdf_attached": body.pdf_attached,
            "presentation_mode": body.presentation_mode,
        }
        contact_name = None
        n8n_success = await _trigger_n8n_workflow(presentation_dict, contact_name)
        return {
            "success": True,
            "presentation_id": presentation_id,
            "n8n_triggered": n8n_success,
            "status": "sent",
            "message": (
                "Vorstellung erstellt und E-Mail-Versand gestartet"
                if n8n_success
                else "Vorstellung erstellt, aber E-Mail-Versand konnte nicht gestartet werden."
            ),
        }

    # Kontakt-Name fuer n8n ermitteln
    contact_name = None
    if body.contact_id:
        from app.models.company_contact import CompanyContact
        from sqlalchemy import select as sa_select
        contact_result = await db.execute(
            sa_select(CompanyContact).where(CompanyContact.id == body.contact_id)
        )
        contact = contact_result.scalar_one_or_none()
        if contact:
            contact_name = contact.full_name

    # Presentation-Daten als Dict extrahieren (nach refresh sind Attribute wieder verfuegbar)
    presentation_id = str(presentation.id)
    presentation_status = presentation.status
    presentation_dict = {
        "id": str(presentation.id),
        "match_id": str(presentation.match_id) if presentation.match_id else None,
        "candidate_id": str(presentation.candidate_id) if presentation.candidate_id else None,
        "job_id": str(presentation.job_id) if presentation.job_id else None,
        "company_id": str(presentation.company_id) if presentation.company_id else None,
        "contact_id": str(presentation.contact_id) if presentation.contact_id else None,
        "email_to": presentation.email_to,
        "email_from": presentation.email_from,
        "email_subject": presentation.email_subject,
        "email_body_text": presentation.email_body_text,
        "email_signature_html": presentation.email_signature_html,
        "mailbox_used": presentation.mailbox_used,
        "pdf_r2_key": presentation.pdf_r2_key,
        "pdf_attached": presentation.pdf_attached,
        "presentation_mode": presentation.presentation_mode,
    }

    # n8n Workflow triggern (HTTP-Call — KEINE DB-Session offen!)
    # Nutzt presentation_dict statt ORM-Objekt (nach commit expired!)
    n8n_success = await _trigger_n8n_workflow(presentation_dict, contact_name)

    if n8n_success:
        logger.info(
            f"Vorstellung {presentation_id} erstellt und n8n Workflow getriggert"
        )
    else:
        logger.warning(
            f"Vorstellung {presentation_id} erstellt, aber n8n Workflow "
            f"konnte NICHT getriggert werden"
        )

    return {
        "success": True,
        "presentation_id": presentation_id,
        "n8n_triggered": n8n_success,
        "status": presentation_status,
        "message": (
            "Vorstellung erstellt und E-Mail-Versand gestartet"
            if n8n_success
            else "Vorstellung erstellt, aber E-Mail-Versand konnte nicht gestartet werden. "
                 "Bitte pruefen Sie die n8n-Konfiguration."
        ),
    }


@router.get("/api/presentations/match/{match_id}")
async def get_presentations_for_match(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Alle Vorstellungen fuer einen bestimmten Match."""
    service = PresentationService(db)

    try:
        presentations = await service.get_presentations_for_match(match_id)
    except Exception as e:
        logger.error(f"get_presentations_for_match fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "match_id": str(match_id),
        "presentations": presentations,
        "count": len(presentations),
    }


@router.post("/api/presentations/{presentation_id}/stop")
async def stop_presentation_sequence(
    presentation_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Stoppt die Follow-Up-Sequenz einer Vorstellung.

    Setzt sequence_active=False und status='cancelled'.
    """
    service = PresentationService(db)

    try:
        success = await service.stop_sequence(presentation_id)
    except Exception as e:
        logger.error(f"stop_sequence fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"Vorstellung {presentation_id} nicht gefunden"
        )

    return {
        "success": True,
        "presentation_id": str(presentation_id),
        "status": "cancelled",
        "message": "Follow-Up-Sequenz gestoppt",
    }


@router.post(
    "/api/presentations/{presentation_id}/response",
    dependencies=[Depends(verify_n8n_token)],
)
async def process_client_response(
    presentation_id: UUID,
    body: ClientResponseRequest,
    db: AsyncSession = Depends(get_db),
):
    """Verarbeitet eine Kunden-Antwort (n8n Webhook Callback).

    Wird von n8n aufgerufen wenn eine Kunden-Antwort auf die
    Vorstellungs-E-Mail eingeht. n8n klassifiziert die Antwort
    per KI und sendet das Ergebnis hierher.

    Erwartet einen gueltigen n8n Bearer-Token im Authorization-Header.
    """
    service = PresentationService(db)

    try:
        success = await service.process_client_response(
            presentation_id=presentation_id,
            category=body.category,
            response_text=body.response_text or "",
            raw_email=body.raw_email or "",
        )
    except Exception as e:
        logger.error(f"process_client_response fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"Vorstellung {presentation_id} nicht gefunden"
        )

    return {
        "success": True,
        "presentation_id": str(presentation_id),
        "category": body.category,
        "status": "responded",
        "message": f"Kunden-Antwort verarbeitet (Kategorie: {body.category})",
    }


@router.post(
    "/api/presentations/{presentation_id}/followup",
    dependencies=[Depends(verify_n8n_token)],
)
async def update_followup_status(
    presentation_id: UUID,
    body: FollowupUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Updated den Follow-Up-Status einer Vorstellung (n8n Callback).

    Wird von n8n aufgerufen nachdem ein Follow-Up gesendet wurde.
    Step 2 = 1. Erinnerung, Step 3 = 2. Erinnerung.

    Erwartet einen gueltigen n8n Bearer-Token im Authorization-Header.
    """
    service = PresentationService(db)

    try:
        success = await service.update_sequence_step(
            presentation_id=presentation_id,
            step=body.step,
        )
    except Exception as e:
        logger.error(f"update_followup_status fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not success:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Vorstellung {presentation_id} nicht gefunden "
                f"oder Sequenz nicht aktiv"
            ),
        )

    status_label = "followup_1" if body.step == 2 else "followup_2"
    return {
        "success": True,
        "presentation_id": str(presentation_id),
        "step": body.step,
        "status": status_label,
        "message": f"Follow-Up {body.step - 1} Status aktualisiert",
    }


# ═══════════════════════════════════════════════════════════════
# n8n-SPEZIFISCHE ENDPOINTS
# ═══════════════════════════════════════════════════════════════


@router.get("/api/presentations/by-email")
async def find_presentation_by_email(
    email: str,
    db: AsyncSession = Depends(get_db),
):
    """Findet die neueste Vorstellung fuer eine E-Mail-Adresse.

    Wird von n8n Workflow 3 (Antwort verarbeiten) genutzt:
    Wenn eine IMAP-Antwort eingeht, sucht n8n ueber die Empfaenger-E-Mail
    die zugehoerige Vorstellung.
    """
    if not email or not email.strip():
        raise HTTPException(status_code=400, detail="Email-Parameter ist erforderlich")

    service = PresentationService(db)

    try:
        presentation = await service.find_by_email(email.strip())
    except Exception as e:
        logger.error(f"find_by_email fehlgeschlagen fuer {email}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not presentation:
        raise HTTPException(
            status_code=404,
            detail=f"Keine Vorstellung fuer E-Mail {email} gefunden"
        )

    return presentation


@router.get("/api/presentations/{presentation_id}")
async def get_presentation(
    presentation_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Laedt eine einzelne Vorstellung per ID.

    Wird von n8n Workflow 2 (Follow-Up Sequenz) genutzt um zu pruefen
    ob die Sequenz noch aktiv ist bevor der naechste Follow-Up gesendet wird.
    """
    service = PresentationService(db)

    try:
        presentation = await service.get_presentation(presentation_id)
    except Exception as e:
        logger.error(f"get_presentation fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not presentation:
        raise HTTPException(
            status_code=404,
            detail=f"Vorstellung {presentation_id} nicht gefunden"
        )

    return presentation


@router.post(
    "/api/presentations/{presentation_id}/sent",
    dependencies=[Depends(verify_n8n_token)],
)
async def confirm_email_sent(
    presentation_id: UUID,
    body: SentConfirmRequest,
    db: AsyncSession = Depends(get_db),
):
    """Bestaetigt den erfolgreichen E-Mail-Versand (n8n Callback).

    Wird von n8n Workflow 1 aufgerufen nachdem die E-Mail erfolgreich
    ueber Outlook/IONOS SMTP gesendet wurde. Setzt sent_at und
    speichert die n8n execution_id.

    Erwartet einen gueltigen n8n Bearer-Token im Authorization-Header.
    """
    service = PresentationService(db)

    try:
        success = await service.confirm_sent(
            presentation_id=presentation_id,
            n8n_execution_id=body.n8n_execution_id,
        )
    except Exception as e:
        logger.error(f"confirm_sent fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"Vorstellung {presentation_id} nicht gefunden"
        )

    return {
        "success": True,
        "presentation_id": str(presentation_id),
        "message": "Versand bestaetigt",
    }


@router.post(
    "/api/presentations/{presentation_id}/fallback-result",
    dependencies=[Depends(verify_n8n_token)],
)
async def update_fallback_result(
    presentation_id: UUID,
    body: FallbackResultRequest,
    db: AsyncSession = Depends(get_db),
):
    """Verarbeitet das Ergebnis der Fallback-E-Mail-Kaskade (n8n Callback).

    Wird von n8n Workflow 4 (Fallback-Kaskade) aufgerufen nachdem
    die E-Mail-Kaskade (bewerber@, karriere@, hr@, jobs@ etc.)
    durchlaufen wurde.

    Wenn eine E-Mail erfolgreich zugestellt wurde, wird diese als
    neue Empfaenger-Adresse gesetzt. Wenn alle gebounced sind,
    wird die Sequenz gestoppt.

    Erwartet einen gueltigen n8n Bearer-Token im Authorization-Header.
    """
    service = PresentationService(db)

    try:
        success = await service.update_fallback_result(
            presentation_id=presentation_id,
            successful_email=body.successful_email,
            attempts=body.attempts,
        )
    except Exception as e:
        logger.error(f"update_fallback_result fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"Vorstellung {presentation_id} nicht gefunden"
        )

    result_msg = (
        f"Fallback erfolgreich: {body.successful_email}"
        if body.successful_email
        else f"Fallback fehlgeschlagen: Alle {len(body.attempts)} E-Mails gebounced"
    )

    return {
        "success": True,
        "presentation_id": str(presentation_id),
        "successful_email": body.successful_email,
        "total_attempts": len(body.attempts),
        "message": result_msg,
    }
