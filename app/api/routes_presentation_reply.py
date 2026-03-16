"""API-Endpoints fuer den Presentation Reply Monitor.

Prefix: /api/presentations/reply

Endpoints:
- POST /process              — Antwort verarbeiten (von n8n aufgerufen)
- GET  /log                  — Letzte 50 verarbeitete Antworten
- POST /blocklist/check      — Pruefen ob Domain/E-Mail blockiert ist
- GET  /blocklist             — Alle blockierten Domains
- POST /blocklist/add        — Domain manuell blocken
- DELETE /blocklist/{domain}  — Domain entsperren
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete, desc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

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

@router.post("/process")
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
# 2. REPLY-LOG (letzte 50)
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
# 3. BLOCKLIST: CHECK
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
# 4. BLOCKLIST: ALLE ANZEIGEN
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
# 5. BLOCKLIST: DOMAIN HINZUFUEGEN
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
# 6. BLOCKLIST: DOMAIN ENTFERNEN
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
