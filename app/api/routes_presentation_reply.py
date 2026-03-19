"""API-Endpoints fuer den Presentation Reply Monitor.

Prefix: /api/presentations/reply

Endpoints:
- POST /process              — Antwort verarbeiten (von n8n aufgerufen)
- POST /check-inbox          — Postfach lesen + Replies erkennen (von n8n Cron alle 15 Min)
- POST /send-pending         — Verzoegerte Auto-Replies senden (von n8n Cron taeglich 8:00)
- GET  /log                  — Letzte 50 verarbeitete Antworten
- POST /blocklist/check      — Pruefen ob Domain/E-Mail blockiert ist
- GET  /blocklist             — Alle blockierten Domains
- POST /blocklist/add        — Domain manuell blocken
- DELETE /blocklist/{domain}  — Domain entsperren
"""

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, delete, desc, text, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.routes_presentation import verify_n8n_token

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/presentations/reply",
    tags=["Presentation Reply Monitor"],
)


# ── Request/Response Models ──

class ProcessReplyRequest(BaseModel):
    email_from: str
    email_subject: str
    email_body: str
    message_id: Optional[str] = None


class BlocklistCheckRequest(BaseModel):
    email: Optional[str] = None
    domain: Optional[str] = None


class BlocklistAddRequest(BaseModel):
    domain: str
    reason: str = ""


# ═══════════════════════════════════════════════════════════════
# 1. ANTWORT VERARBEITEN (von n8n aufgerufen)
# ═══════════════════════════════════════════════════════════════

@router.post("/process", dependencies=[Depends(verify_n8n_token)])
async def process_reply(req: ProcessReplyRequest):
    """Verarbeitet eine erkannte Antwort auf eine Vorstellungs-E-Mail.

    Delegiert komplett an PresentationReplyService.process_reply()
    (eigene DB-Sessions pro Schritt, Railway 30s Timeout beachtet).
    """
    try:
        from app.services.presentation_reply_service import PresentationReplyService

        result = await PresentationReplyService.process_reply(
            email_from=req.email_from,
            email_subject=req.email_subject,
            email_body=req.email_body,
        )

        # Reply-Log schreiben (eigene Session)
        from app.database import async_session_maker
        async with async_session_maker() as db:
            await _log_reply(
                db,
                email_from=req.email_from,
                email_subject=req.email_subject,
                message_id=req.message_id,
                classification=result.get("category", "unknown"),
                action_taken=result.get("action_taken", "none"),
                presentation_id=result.get("presentation_id"),
            )
            await db.commit()

        return result

    except Exception as e:
        logger.error(f"process_reply Endpoint fehlgeschlagen: {e}", exc_info=True)
        return {
            "matched": False,
            "category": "error",
            "action_taken": "endpoint_error",
            "details": {"error": str(e)[:500]},
        }


# ═══════════════════════════════════════════════════════════════
# 2. CHECK-INBOX — Postfach lesen + Replies erkennen (n8n Cron)
# ═══════════════════════════════════════════════════════════════

@router.post("/check-inbox", dependencies=[Depends(verify_n8n_token)])
async def check_inbox(minutes: int = Query(default=15, ge=1, le=60)):
    """Liest hamdard@sincirus.com Postfach via Microsoft Graph und erkennt Replies.

    Wird von n8n alle 15 Minuten aufgerufen.

    Architektur (Railway-safe):
    1. Access Token holen (kein DB)
    2. Alle E-Mails seit {minutes} Minuten laden via Graph (kein DB)
    3. Fuer jede E-Mail: an /process weiterleiten

    Returns:
        {checked: bool, emails_found: int, processed: int, results: list}
    """
    try:
        import httpx
        from app.config import get_settings
        settings = get_settings()

        if not settings.microsoft_tenant_id or not settings.microsoft_client_id:
            return {
                "checked": False,
                "error": "Microsoft Graph nicht konfiguriert (fehlende Credentials)",
                "emails_found": 0,
                "processed": 0,
            }

        # ── Phase 1: Access Token holen ──
        from app.services.email_service import MicrosoftGraphClient
        token = await MicrosoftGraphClient._get_access_token()

        if not token:
            return {
                "checked": False,
                "error": "Konnte keinen Microsoft Graph Access Token erhalten",
                "emails_found": 0,
                "processed": 0,
            }

        # ── Phase 2: E-Mails lesen (KEIN DB!) ──
        # Nur hamdard@sincirus.com — alle Kunden-Antworten landen hier via Reply-To Header
        mailbox = settings.microsoft_sender_email or "hamdard@sincirus.com"
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # Microsoft Graph: Eingehende E-Mails der letzten X Minuten
        # Filter: receivedDateTime >= since, isRead eq false (nur ungelesene)
        graph_url = (
            f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages"
            f"?$filter=receivedDateTime ge {since_str}"
            f"&$select=from,subject,body,internetMessageHeaders,receivedDateTime,isRead"
            f"&$top=50"
            f"&$orderby=receivedDateTime desc"
        )

        raw_emails = []
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(graph_url, headers=headers)
            if resp.status_code != 200:
                logger.error(
                    f"Graph API Fehler beim Lesen von {mailbox}: "
                    f"Status {resp.status_code}, Body: {resp.text[:500]}"
                )
                return {
                    "checked": False,
                    "error": f"Graph API HTTP {resp.status_code}",
                    "emails_found": 0,
                    "processed": 0,
                }

            data = resp.json()
            raw_emails = data.get("value", [])

        logger.info(f"check-inbox: {len(raw_emails)} E-Mails gefunden seit {since_str}")

        # Eigene Absender-Domains filtern (keine eigenen E-Mails verarbeiten)
        OWN_DOMAINS = {"sincirus.com", "sincirus-karriere.de", "jobs-sincirus.com"}

        # Bounce-Signalwoerter
        BOUNCE_SIGNALS = {
            "MAILER-DAEMON", "postmaster", "Undeliverable:", "Unzustellbar:",
            "Delivery Status Notification", "Mail Delivery",
            "delivery has failed", "Delivery Failure",
        }

        results = []
        processed = 0

        for email in raw_emails:
            try:
                from_data = email.get("from", {}).get("emailAddress", {})
                from_email = from_data.get("address", "").strip().lower()
                from_name = from_data.get("name", "")
                subject = email.get("subject", "")
                body_content = email.get("body", {}).get("content", "")

                if not from_email:
                    continue

                # Eigene E-Mails ignorieren
                from_domain = from_email.split("@", 1)[1] if "@" in from_email else ""
                if from_domain in OWN_DOMAINS:
                    continue

                # Bounces erkennen und Sequenz stoppen
                is_bounce = any(
                    sig.lower() in from_email.lower() or sig in subject
                    for sig in BOUNCE_SIGNALS
                )
                if is_bounce:
                    logger.info(f"check-inbox: Bounce erkannt von {from_email}, Subject: {subject[:100]}")
                    bounce_result = await _handle_bounce(from_email, subject, body_content)
                    processed += 1
                    results.append({
                        "from": from_email,
                        "subject": subject[:100],
                        "matched": bounce_result.get("matched", False),
                        "category": "bounce",
                        "action_taken": bounce_result.get("action_taken", "none"),
                    })
                    continue

                # ── Phase 3: Reply verarbeiten ──
                from app.services.presentation_reply_service import PresentationReplyService

                # Body bereinigen (HTML → Plain Text, nur erste 3000 Zeichen)
                plain_body = _strip_html(body_content)[:3000]

                result = await PresentationReplyService.process_reply(
                    email_from=from_email,
                    email_subject=subject,
                    email_body=plain_body,
                )

                processed += 1
                results.append({
                    "from": from_email,
                    "subject": subject[:100],
                    "matched": result.get("matched", False),
                    "category": result.get("category"),
                    "action_taken": result.get("action_taken"),
                })

                # Reply-Log schreiben (eigene Session)
                from app.database import async_session_maker
                async with async_session_maker() as db:
                    await _log_reply(
                        db,
                        email_from=from_email,
                        email_subject=subject,
                        message_id=None,
                        classification=result.get("category", "unknown"),
                        action_taken=result.get("action_taken", "none"),
                        presentation_id=result.get("presentation_id"),
                    )
                    await db.commit()

            except Exception as email_err:
                logger.error(f"check-inbox: Fehler bei E-Mail von {from_email}: {email_err}")
                results.append({
                    "from": from_email if 'from_email' in dir() else "unknown",
                    "error": str(email_err)[:200],
                })

        return {
            "checked": True,
            "emails_found": len(raw_emails),
            "processed": processed,
            "results": results,
        }

    except Exception as e:
        logger.error(f"check-inbox komplett fehlgeschlagen: {e}", exc_info=True)
        return {
            "checked": False,
            "error": str(e)[:500],
            "emails_found": 0,
            "processed": 0,
        }


# ═══════════════════════════════════════════════════════════════
# 3. SEND-PENDING — Verzoegerte Auto-Replies senden (n8n Cron)
# ═══════════════════════════════════════════════════════════════

@router.post("/send-pending", dependencies=[Depends(verify_n8n_token)])
async def send_pending_replies():
    """Sendet alle verzoegerten Auto-Replies deren scheduled_at erreicht ist.

    Wird von n8n taeglich um 8:00 (Europe/Berlin) aufgerufen.
    Liest Presentations mit auto_reply_pending=True in client_response_text.

    Ablauf pro Pending-Reply:
    1. Presentation laden (eigene Session)
    2. Pending-Daten aus client_response_text parsen
    3. Auto-Reply senden (KEINE DB-Session!)
    4. Presentation als gesendet markieren (eigene Session)

    Returns:
        {sent: int, failed: int, skipped: int, details: list}
    """
    sent = 0
    failed = 0
    skipped = 0
    details = []

    try:
        from app.database import async_session_maker
        from app.models.client_presentation import ClientPresentation
        from app.services.presentation_reply_service import PresentationReplyService

        # ── Schritt 1: Alle Presentations mit Pending-Replies finden (eigene Session) ──
        pending_items = []
        async with async_session_maker() as db:
            # client_response_text enthaelt JSON mit "auto_reply_pending": true
            result = await db.execute(
                select(
                    ClientPresentation.id,
                    ClientPresentation.client_response_text,
                ).where(
                    ClientPresentation.client_response_text.isnot(None),
                    ClientPresentation.client_response_text != "",
                )
            )
            rows = result.all()

            for row in rows:
                try:
                    data = json.loads(row[1]) if isinstance(row[1], str) else row[1]
                    if isinstance(data, dict) and data.get("auto_reply_pending"):
                        pending_items.append({
                            "presentation_id": str(row[0]),
                            "pending_data": data,
                        })
                except (json.JSONDecodeError, TypeError):
                    continue
        # Session geschlossen!

        if not pending_items:
            return {"sent": 0, "failed": 0, "skipped": 0, "details": [], "message": "Keine Pending-Replies gefunden"}

        logger.info(f"send-pending: {len(pending_items)} Pending-Replies gefunden")

        now_utc = datetime.now(timezone.utc)

        for item in pending_items:
            pres_id = item["presentation_id"]
            pending = item["pending_data"]

            try:
                # Pruefen ob scheduled_at erreicht ist
                scheduled_at_str = pending.get("auto_reply_scheduled_at", "")
                if scheduled_at_str:
                    scheduled_at = datetime.fromisoformat(scheduled_at_str.replace("Z", "+00:00"))
                    if scheduled_at > now_utc:
                        skipped += 1
                        details.append({
                            "presentation_id": pres_id,
                            "status": "skipped",
                            "reason": f"Noch nicht faellig (scheduled_at={scheduled_at_str})",
                        })
                        continue

                reply_type = pending.get("auto_reply_type", "negative")
                to_email = pending.get("auto_reply_to", "")
                subject = pending.get("auto_reply_subject", "")
                anrede = pending.get("auto_reply_anrede", "Hallo")
                body_context = pending.get("auto_reply_body_context", "")
                mailbox_used = pending.get("mailbox_used", "hamdard@sincirus.com")

                if not to_email:
                    skipped += 1
                    details.append({
                        "presentation_id": pres_id,
                        "status": "skipped",
                        "reason": "Kein to_email in Pending-Daten",
                    })
                    continue

                # ── Schritt 2: Auto-Reply senden (KEINE DB-Session!) ──
                if reply_type == "deletion_confirmation":
                    send_result = await PresentationReplyService.send_deletion_confirmation(
                        to_email=to_email,
                        original_subject=subject,
                        anrede=anrede,
                        reply_body=body_context,
                        mailbox_used=mailbox_used,
                    )
                else:
                    send_result = await PresentationReplyService.send_negative_auto_reply(
                        to_email=to_email,
                        original_subject=subject,
                        mailbox_used=mailbox_used,
                        anrede=anrede,
                        reply_body=body_context,
                    )

                if send_result.get("success"):
                    # ── Schritt 3: Presentation aktualisieren (eigene Session) ──
                    async with async_session_maker() as db:
                        pres_result = await db.execute(
                            select(ClientPresentation).where(
                                ClientPresentation.id == UUID(pres_id)
                            )
                        )
                        pres = pres_result.scalar_one_or_none()
                        if pres:
                            # Pending-Flag entfernen, gesendeten Text speichern
                            pres.auto_reply_sent = True
                            pres.auto_reply_text = send_result.get("reply_text", "")[:2000]
                            # client_response_text bereinigen (pending-Flag entfernen)
                            try:
                                existing_data = json.loads(pres.client_response_text) if isinstance(pres.client_response_text, str) else (pres.client_response_text or {})
                                if isinstance(existing_data, dict):
                                    existing_data["auto_reply_pending"] = False
                                    existing_data["auto_reply_sent_at"] = now_utc.isoformat()
                                    pres.client_response_text = json.dumps(existing_data, ensure_ascii=False)
                            except (json.JSONDecodeError, TypeError):
                                pass
                            await db.commit()
                    # Session geschlossen!

                    sent += 1
                    details.append({
                        "presentation_id": pres_id,
                        "status": "sent",
                        "to": to_email,
                        "type": reply_type,
                    })
                    logger.info(f"send-pending: {reply_type} gesendet an {to_email} (Presentation {pres_id})")
                else:
                    failed += 1
                    error = send_result.get("error", "Unbekannt")
                    details.append({
                        "presentation_id": pres_id,
                        "status": "failed",
                        "to": to_email,
                        "error": error[:200],
                    })
                    logger.error(f"send-pending: Fehler bei {to_email}: {error}")

            except Exception as item_err:
                failed += 1
                details.append({
                    "presentation_id": pres_id,
                    "status": "error",
                    "error": str(item_err)[:200],
                })
                logger.error(f"send-pending: Exception bei Presentation {pres_id}: {item_err}")

    except Exception as e:
        logger.error(f"send-pending komplett fehlgeschlagen: {e}", exc_info=True)
        return {
            "sent": sent,
            "failed": failed + 1,
            "skipped": skipped,
            "error": str(e)[:500],
            "details": details,
        }

    return {
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "details": details,
    }


# ═══════════════════════════════════════════════════════════════
# 4. REPLY-LOG (letzte 50)
# ═══════════════════════════════════════════════════════════════

@router.get("/log")
async def get_reply_log(db: AsyncSession = Depends(get_db)):
    """Gibt die letzten 50 verarbeiteten Antworten zurueck."""
    result = await db.execute(
        text("SELECT value FROM system_settings WHERE key = 'presentation_reply_log'")
    )
    row = result.first()

    if not row or not row[0]:
        return {"replies": [], "total": 0}

    try:
        log_entries = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except (json.JSONDecodeError, TypeError):
        log_entries = []

    log_entries = sorted(log_entries, key=lambda x: x.get("processed_at", ""), reverse=True)[:50]
    return {"replies": log_entries, "total": len(log_entries)}


# ═══════════════════════════════════════════════════════════════
# 5. BLOCKLIST: CHECK
# ═══════════════════════════════════════════════════════════════

@router.post("/blocklist/check")
async def check_blocklist(req: BlocklistCheckRequest, db: AsyncSession = Depends(get_db)):
    """Prueft ob eine E-Mail-Adresse oder Domain blockiert ist."""
    from app.models.email_blocklist import EmailBlocklist

    if not req.email and not req.domain:
        raise HTTPException(status_code=400, detail="email oder domain muss angegeben werden")

    domain = req.domain
    if not domain and req.email:
        domain = req.email.split("@", 1)[1].lower() if "@" in req.email else req.email.lower()
    domain = domain.lower().strip()

    result = await db.execute(
        select(EmailBlocklist).where(EmailBlocklist.domain == domain)
    )
    entry = result.scalar_one_or_none()

    if entry:
        return {
            "blocked": True,
            "reason": entry.reason,
            "blocked_at": entry.blocked_at.isoformat() if entry.blocked_at else None,
            "company_name": entry.company_name_before_deletion,
        }
    return {"blocked": False, "reason": None, "blocked_at": None}


# ═══════════════════════════════════════════════════════════════
# 6. BLOCKLIST: ALLE ANZEIGEN
# ═══════════════════════════════════════════════════════════════

@router.get("/blocklist")
async def get_blocklist(db: AsyncSession = Depends(get_db)):
    """Gibt alle blockierten Domains zurueck."""
    from app.models.email_blocklist import EmailBlocklist

    result = await db.execute(
        select(EmailBlocklist).order_by(desc(EmailBlocklist.blocked_at))
    )
    entries = result.scalars().all()

    domains = [
        {
            "domain": e.domain,
            "reason": e.reason,
            "company_name": e.company_name_before_deletion,
            "contact_email": e.contact_email,
            "blocked_at": e.blocked_at.isoformat() if e.blocked_at else None,
            "blocked_by": e.blocked_by,
        }
        for e in entries
    ]
    return {"domains": domains, "total": len(domains)}


# ═══════════════════════════════════════════════════════════════
# 7. BLOCKLIST: DOMAIN HINZUFUEGEN
# ═══════════════════════════════════════════════════════════════

@router.post("/blocklist/add")
async def add_to_blocklist(req: BlocklistAddRequest, db: AsyncSession = Depends(get_db)):
    """Domain manuell zur Blockliste hinzufuegen."""
    from app.models.email_blocklist import EmailBlocklist

    domain = req.domain.lower().strip()
    if not domain:
        raise HTTPException(status_code=400, detail="Domain darf nicht leer sein")

    existing = await db.execute(
        select(EmailBlocklist.id).where(EmailBlocklist.domain == domain)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Domain '{domain}' ist bereits blockiert")

    entry = EmailBlocklist(
        domain=domain,
        reason=req.reason or "Manuell hinzugefuegt",
        blocked_by="manual",
    )
    db.add(entry)
    await db.commit()

    return {"status": "added", "domain": domain}


# ═══════════════════════════════════════════════════════════════
# 8. BLOCKLIST: DOMAIN ENTFERNEN
# ═══════════════════════════════════════════════════════════════

@router.delete("/blocklist/{domain}")
async def remove_from_blocklist(domain: str, db: AsyncSession = Depends(get_db)):
    """Domain von der Blockliste entfernen."""
    from app.models.email_blocklist import EmailBlocklist

    domain = domain.lower().strip()

    result = await db.execute(
        delete(EmailBlocklist).where(EmailBlocklist.domain == domain)
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"Domain '{domain}' nicht auf der Blockliste")

    await db.commit()
    return {"status": "removed", "domain": domain}


# ═══════════════════════════════════════════════════════════════
# 9. CLEANUP-STALE — Verwaiste Presentations bereinigen (n8n Cron)
# ═══════════════════════════════════════════════════════════════

@router.post("/cleanup-stale", dependencies=[Depends(verify_n8n_token)])
async def cleanup_stale_presentations():
    """Bereinigt verwaiste Presentations:

    1. followup_2 + sequence_active + >14 Tage → no_response
    2. sent + sequence_active + >21 Tage (kein Follow-Up) → no_response
    3. sending + >30 Minuten → sent (Callback-Verlust)

    Wird von n8n taeglich aufgerufen.

    Returns:
        {cleaned_followup2: int, cleaned_sent_stale: int, cleaned_sending: int}
    """
    try:
        from app.database import async_session_maker
        from app.models.client_presentation import ClientPresentation

        now = datetime.now(timezone.utc)
        cleaned_followup2 = 0
        cleaned_sent_stale = 0
        cleaned_sending = 0

        # ── Block 1: followup_2 + sequence_active + >14 Tage → no_response ──
        async with async_session_maker() as db:
            cutoff_14d = now - timedelta(days=14)
            result = await db.execute(
                select(ClientPresentation).where(
                    ClientPresentation.sequence_active == True,
                    ClientPresentation.status == "followup_2",
                    ClientPresentation.followup2_sent_at.isnot(None),
                    ClientPresentation.followup2_sent_at < cutoff_14d,
                )
            )
            stale_fu2 = result.scalars().all()
            for pres in stale_fu2:
                pres.status = "no_response"
                pres.sequence_active = False
            if stale_fu2:
                await db.commit()
            cleaned_followup2 = len(stale_fu2)
        # Session geschlossen!

        # ── Block 2: sent + sequence_active + >21 Tage (Erstversand ohne Follow-Up) → no_response ──
        async with async_session_maker() as db:
            cutoff_21d = now - timedelta(days=21)
            result = await db.execute(
                select(ClientPresentation).where(
                    ClientPresentation.sequence_active == True,
                    ClientPresentation.status == "sent",
                    ClientPresentation.created_at < cutoff_21d,
                )
            )
            stale_sent = result.scalars().all()
            for pres in stale_sent:
                pres.status = "no_response"
                pres.sequence_active = False
            if stale_sent:
                await db.commit()
            cleaned_sent_stale = len(stale_sent)
        # Session geschlossen!

        # ── Block 3: sending + >30 Minuten → sent (Callback-Verlust) ──
        async with async_session_maker() as db:
            cutoff_30m = now - timedelta(minutes=30)
            result = await db.execute(
                select(ClientPresentation).where(
                    ClientPresentation.status == "sending",
                    ClientPresentation.created_at < cutoff_30m,
                )
            )
            stale_sending = result.scalars().all()
            for pres in stale_sending:
                pres.status = "sent"
            if stale_sending:
                await db.commit()
            cleaned_sending = len(stale_sending)
        # Session geschlossen!

        total = cleaned_followup2 + cleaned_sent_stale + cleaned_sending
        logger.info(
            f"cleanup-stale: {total} bereinigt "
            f"(followup2={cleaned_followup2}, sent_stale={cleaned_sent_stale}, sending={cleaned_sending})"
        )

        return {
            "cleaned_followup2": cleaned_followup2,
            "cleaned_sent_stale": cleaned_sent_stale,
            "cleaned_sending": cleaned_sending,
            "total": total,
        }

    except Exception as e:
        logger.error(f"cleanup-stale fehlgeschlagen: {e}", exc_info=True)
        return {
            "error": str(e)[:500],
            "cleaned_followup2": 0,
            "cleaned_sent_stale": 0,
            "cleaned_sending": 0,
            "total": 0,
        }


# ═══════════════════════════════════════════════════════════════
# HELPER: Domain-Check (exportiert fuer andere Module)
# ═══════════════════════════════════════════════════════════════

async def is_domain_blocked(db: AsyncSession, email: str) -> bool:
    """Prueft ob die Domain einer E-Mail blockiert ist."""
    from app.models.email_blocklist import EmailBlocklist

    if not email or "@" not in email:
        return False
    domain = email.split("@", 1)[1].lower().strip()

    result = await db.execute(
        select(EmailBlocklist.id).where(EmailBlocklist.domain == domain).limit(1)
    )
    return result.scalar_one_or_none() is not None


# ═══════════════════════════════════════════════════════════════
# HELPER: Reply-Log (system_settings JSONB)
# ═══════════════════════════════════════════════════════════════

async def _log_reply(
    db: AsyncSession,
    email_from: str,
    email_subject: str,
    message_id: str | None,
    classification: str,
    action_taken: str,
    presentation_id: str | None,
):
    """Schreibt einen Reply-Log-Eintrag in system_settings."""
    new_entry = {
        "email_from": email_from,
        "email_subject": email_subject[:200] if email_subject else "",
        "message_id": message_id,
        "classification": classification,
        "action_taken": action_taken,
        "presentation_id": presentation_id,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    result = await db.execute(
        text("SELECT value FROM system_settings WHERE key = 'presentation_reply_log'")
    )
    row = result.first()

    if row and row[0]:
        try:
            log_entries = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except (json.JSONDecodeError, TypeError):
            log_entries = []
    else:
        log_entries = []

    log_entries.insert(0, new_entry)
    log_entries = log_entries[:200]
    log_json = json.dumps(log_entries, ensure_ascii=False)

    if row:
        await db.execute(
            text("UPDATE system_settings SET value = :val, updated_at = NOW() WHERE key = 'presentation_reply_log'"),
            {"val": log_json},
        )
    else:
        await db.execute(
            text("INSERT INTO system_settings (key, value, updated_at) VALUES ('presentation_reply_log', :val, NOW())"),
            {"val": log_json},
        )


# ═══════════════════════════════════════════════════════════════
# HELPER: HTML → Plain Text (fuer E-Mail-Body aus Graph API)
# ═══════════════════════════════════════════════════════════════

def _strip_html(html: str) -> str:
    """Entfernt HTML-Tags und gibt Plain Text zurueck.

    Einfache Loesung ohne externe Dependencies (BeautifulSoup etc.).
    Microsoft Graph liefert HTML-Body, die GPT-Klassifikation braucht Plain Text.
    """
    if not html:
        return ""

    # <br>, <p>, <div> → Zeilenumbrueche
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)

    # Style/Script Bloecke komplett entfernen
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.IGNORECASE | re.DOTALL)

    # Alle restlichen Tags entfernen
    text = re.sub(r'<[^>]+>', '', text)

    # HTML-Entities decodieren (haeufigste)
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    text = text.replace("&nbsp;", " ")

    # Mehrfache Leerzeilen reduzieren
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()
