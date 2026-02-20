"""Telegram Webhook API-Routen.

Empfaengt Updates von Telegram via Webhook.
Verifiziert den Secret-Token Header.
Verarbeitet Updates im Background-Task (200 sofort zurueck).
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["Telegram Bot"])


@router.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """Empfaengt Telegram Updates via Webhook.

    Telegram sendet den Secret-Token im Header X-Telegram-Bot-Api-Secret-Token.
    """
    # Secret-Token verifizieren (wenn konfiguriert)
    if settings.telegram_webhook_secret:
        token = request.headers.get("x-telegram-bot-api-secret-token", "")
        if token != settings.telegram_webhook_secret:
            logger.warning("Telegram Webhook: Ungueltiger Secret-Token")
            return JSONResponse(status_code=403, content={"error": "forbidden"})

    try:
        update = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    # Update im Hintergrund verarbeiten (200 sofort zurueck an Telegram)
    background_tasks.add_task(_process_update, update)

    return {"ok": True}


@router.get("/status")
async def telegram_status():
    """Prueft den Telegram Bot Status und ob der Webhook registriert ist."""
    if not settings.telegram_bot_token:
        return {
            "status": "not_configured",
            "message": "TELEGRAM_BOT_TOKEN nicht gesetzt",
        }

    import httpx

    try:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getWebhookInfo"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        webhook_info = data.get("result", {})
        return {
            "status": "ok",
            "webhook_url": webhook_info.get("url", ""),
            "has_custom_certificate": webhook_info.get("has_custom_certificate", False),
            "pending_update_count": webhook_info.get("pending_update_count", 0),
            "last_error_date": webhook_info.get("last_error_date"),
            "last_error_message": webhook_info.get("last_error_message"),
            "max_connections": webhook_info.get("max_connections"),
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }


@router.post("/calendar/send-reminders")
async def send_calendar_reminders():
    """Sendet Erinnerungs-Emails fuer morgige Kalender-Termine.

    Wird taeglich um 18:00 via n8n aufgerufen.
    Sucht alle Termine von morgen im Outlook-Kalender,
    generiert GPT-Erinnerungs-Emails und sendet sie an Teilnehmer.
    """
    try:
        from app.services.telegram_calendar_handler import send_tomorrow_reminders
        result = await send_tomorrow_reminders()
        return result
    except Exception as e:
        logger.error(f"Calendar Reminders fehlgeschlagen: {e}", exc_info=True)
        return {"reminders_sent": 0, "events_total": 0, "errors": [str(e)]}


async def _process_update(update: dict) -> None:
    """Verarbeitet ein Telegram Update (laeuft als Background-Task)."""
    try:
        from app.services.telegram_bot_service import handle_update
        await handle_update(update)
    except Exception as e:
        logger.error(f"Telegram Update Verarbeitung fehlgeschlagen: {e}", exc_info=True)


async def register_webhook() -> bool:
    """Registriert den Telegram Webhook bei Bot-Start.

    Wird in main.py lifespan() aufgerufen.
    """
    if not settings.telegram_bot_token:
        logger.info("Telegram Bot Token nicht konfiguriert — Webhook-Registrierung uebersprungen")
        return False

    # Webhook-URL: Railway App URL + Webhook-Pfad
    import os
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    if not railway_domain:
        logger.info("RAILWAY_PUBLIC_DOMAIN nicht gesetzt — Telegram Webhook nur lokal")
        return False

    webhook_url = f"https://{railway_domain}/api/telegram/webhook"

    import httpx

    try:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook"
        payload = {
            "url": webhook_url,
            "allowed_updates": ["message", "callback_query"],
        }
        if settings.telegram_webhook_secret:
            payload["secret_token"] = settings.telegram_webhook_secret

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        if data.get("ok"):
            logger.info(f"Telegram Webhook registriert: {webhook_url}")
            return True
        else:
            logger.error(f"Telegram Webhook Registrierung fehlgeschlagen: {data}")
            return False

    except Exception as e:
        logger.error(f"Telegram Webhook Registrierung fehlgeschlagen: {e}")
        return False
