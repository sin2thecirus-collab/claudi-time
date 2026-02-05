"""n8n Notify Service — Outbound-Webhooks an n8n senden."""

import logging
from uuid import UUID

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Timeout fuer Webhook-Requests
WEBHOOK_TIMEOUT = 10.0


class N8nNotifyService:
    """Service fuer ausgehende n8n-Webhook-Benachrichtigungen.

    Sendet Events an n8n-Workflows wenn im MT etwas passiert.
    n8n kann daraufhin Aktionen ausfuehren (E-Mails senden, Todos erstellen, etc.)
    """

    @staticmethod
    def _get_webhook_url(event_type: str) -> str | None:
        """Gibt die Webhook-URL fuer einen Event-Typ zurueck."""
        base = settings.n8n_webhook_url
        if not base:
            return None
        return f"{base.rstrip('/')}/webhook/{event_type}"

    @staticmethod
    async def _send_webhook(event_type: str, payload: dict) -> bool:
        """Sendet einen Webhook an n8n. Gibt True bei Erfolg zurueck."""
        url = N8nNotifyService._get_webhook_url(event_type)
        if not url:
            logger.debug(f"n8n Webhook uebersprungen (keine URL konfiguriert): {event_type}")
            return False

        try:
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                if response.status_code < 300:
                    logger.info(f"n8n Webhook gesendet: {event_type} -> {response.status_code}")
                    return True
                else:
                    logger.warning(
                        f"n8n Webhook fehlgeschlagen: {event_type} -> {response.status_code} {response.text[:200]}"
                    )
                    return False
        except httpx.TimeoutException:
            logger.warning(f"n8n Webhook Timeout: {event_type}")
            return False
        except Exception as e:
            logger.error(f"n8n Webhook Fehler: {event_type} -> {e}")
            return False

    # ── Event-spezifische Methoden ───────────────────

    @staticmethod
    async def notify_stage_change(
        entry_id: UUID,
        from_stage: str,
        to_stage: str,
        job_title: str,
        candidate_name: str,
        candidate_email: str | None = None,
        company_name: str | None = None,
    ) -> bool:
        """Benachrichtigt n8n ueber eine Pipeline-Stage-Aenderung."""
        return await N8nNotifyService._send_webhook("stage-change", {
            "entry_id": str(entry_id),
            "from_stage": from_stage,
            "to_stage": to_stage,
            "job_title": job_title,
            "candidate_name": candidate_name,
            "candidate_email": candidate_email,
            "company_name": company_name,
        })

    @staticmethod
    async def notify_new_job(
        ats_job_id: UUID,
        title: str,
        company_name: str | None = None,
        location_city: str | None = None,
        priority: str | None = None,
    ) -> bool:
        """Benachrichtigt n8n ueber eine neue ATS-Stelle."""
        return await N8nNotifyService._send_webhook("new-job", {
            "ats_job_id": str(ats_job_id),
            "title": title,
            "company_name": company_name,
            "location_city": location_city,
            "priority": priority,
        })

    @staticmethod
    async def notify_new_call_note(
        call_note_id: UUID,
        call_type: str,
        summary: str,
        action_items: list | None = None,
        company_name: str | None = None,
    ) -> bool:
        """Benachrichtigt n8n ueber eine neue Call-Note."""
        return await N8nNotifyService._send_webhook("new-call-note", {
            "call_note_id": str(call_note_id),
            "call_type": call_type,
            "summary": summary,
            "action_items": action_items or [],
            "company_name": company_name,
        })

    @staticmethod
    async def notify_todo_overdue(
        todo_ids: list[UUID],
        todo_titles: list[str],
    ) -> bool:
        """Benachrichtigt n8n ueber ueberfaellige Todos."""
        return await N8nNotifyService._send_webhook("todo-overdue", {
            "todo_ids": [str(tid) for tid in todo_ids],
            "todo_titles": todo_titles,
            "count": len(todo_ids),
        })

    @staticmethod
    async def notify_candidate_placed(
        entry_id: UUID,
        candidate_name: str,
        job_title: str,
        company_name: str | None = None,
    ) -> bool:
        """Benachrichtigt n8n wenn ein Kandidat platziert wurde."""
        return await N8nNotifyService._send_webhook("candidate-placed", {
            "entry_id": str(entry_id),
            "candidate_name": candidate_name,
            "job_title": job_title,
            "company_name": company_name,
        })
