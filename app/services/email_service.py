"""EmailService - Automatischer Email-Versand nach Kandidatengespraechen.

Versendet Emails via Microsoft Graph API (Outlook/M365).
Drei Email-Typen:
1. Kontaktdaten (nach Qualifizierung) → sofort senden
2. Stellenausschreibung (Job besprochen) → Job matchen + sofort senden
3. Individuell (andere Zusagen) → Draft erstellen → Recruiter prueft

Architektur:
- MicrosoftGraphClient: Token-Caching + Email-Versand via Graph API
- EmailService: Orchestriert Templates, Job-Matching, Drafts
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from jinja2 import Template
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.ats_activity import ATSActivity, ActivityType
from app.models.ats_job import ATSJob, ATSJobStatus
from app.models.candidate import Candidate
from app.models.company import Company
from app.models.email_draft import EmailDraft, EmailDraftStatus, EmailType

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  Microsoft Graph API Client
# ═══════════════════════════════════════════════════════════════

class MicrosoftGraphClient:
    """Sendet Emails via Microsoft Graph API (Client Credentials Flow)."""

    _token: Optional[str] = None
    _token_expires_at: float = 0

    @classmethod
    async def _get_access_token(cls) -> str:
        """Holt oder cached einen Access Token (1h TTL)."""
        now = time.time()
        if cls._token and cls._token_expires_at > now + 60:
            return cls._token

        tenant_id = settings.microsoft_tenant_id
        client_id = settings.microsoft_client_id
        client_secret = settings.microsoft_client_secret

        if not all([tenant_id, client_id, client_secret]):
            raise ValueError(
                "Microsoft Graph API nicht konfiguriert. "
                "Bitte MICROSOFT_TENANT_ID, MICROSOFT_CLIENT_ID, "
                "MICROSOFT_CLIENT_SECRET in Railway setzen."
            )

        token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        cls._token = data["access_token"]
        cls._token_expires_at = now + data.get("expires_in", 3600)
        logger.info("Microsoft Graph Token erfolgreich geholt/erneuert.")
        return cls._token

    @classmethod
    async def send_email(
        cls,
        to_email: str,
        subject: str,
        body_html: str,
        from_email: Optional[str] = None,
    ) -> dict:
        """Sendet eine Email via Microsoft Graph API.

        Returns: {"success": True, "message_id": "..."} oder {"success": False, "error": "..."}
        """
        sender = from_email or settings.microsoft_sender_email
        if not sender:
            return {"success": False, "error": "Kein Absender konfiguriert (MICROSOFT_SENDER_EMAIL)"}

        try:
            token = await cls._get_access_token()
        except Exception as e:
            logger.error(f"Graph Token-Fehler: {e}")
            return {"success": False, "error": f"Token-Fehler: {e}"}

        graph_url = f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"

        payload = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "HTML",
                    "content": body_html,
                },
                "toRecipients": [
                    {"emailAddress": {"address": to_email}}
                ],
            },
            "saveToSentItems": True,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    graph_url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )

            if resp.status_code == 202:
                logger.info(f"Email gesendet an {to_email}: {subject}")
                return {"success": True, "message_id": resp.headers.get("request-id", "")}
            else:
                error_text = resp.text[:500]
                logger.error(f"Graph Email-Fehler {resp.status_code}: {error_text}")
                return {"success": False, "error": f"HTTP {resp.status_code}: {error_text}"}

        except Exception as e:
            logger.error(f"Graph Email-Exception: {e}")
            return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
#  Email-Templates (Jinja2 Rendering)
# ═══════════════════════════════════════════════════════════════

TEMPLATE_KONTAKTDATEN_SUBJECT = "Schön, dass wir gesprochen haben – Ihre Kontaktdaten bei sincirus"

TEMPLATE_KONTAKTDATEN_BODY = """
<div style="font-family: 'DM Sans', Arial, sans-serif; color: #2d3748; max-width: 600px; margin: 0 auto; padding: 20px;">
    <p style="font-size: 15px; line-height: 1.6;">{{ salutation }},</p>

    <p style="font-size: 15px; line-height: 1.6;">
        herzlichen Dank für das angenehme Gespräch heute.
    </p>

    <p style="font-size: 15px; line-height: 1.6;">
        Wie besprochen, erhalten Sie anbei meine Kontaktdaten. Falls Sie Ihre Zeugnisse aktuell
        zur Hand haben, würde ich mich freuen, diese zur besseren Einschätzung Ihrer fachlichen
        und beruflichen Expertise ebenfalls zu erhalten.
    </p>

    <div style="background: #f7fafc; border-left: 4px solid #34D399; padding: 16px 20px; margin: 20px 0; border-radius: 8px;">
        <p style="margin: 0 0 4px; font-weight: 600; font-size: 15px;">Milad Hamdard</p>
        <p style="margin: 0 0 4px; font-size: 14px; color: #718096;">Senior Personalberater</p>
        <p style="margin: 0 0 4px; font-size: 14px;">📞 +49 176 81605498</p>
        <p style="margin: 0 0 4px; font-size: 14px;">✉️ hamdard@sincirus.com</p>
        <p style="margin: 0; font-size: 14px;">🌐 www.sincirus.de</p>
    </div>

    <p style="font-size: 15px; line-height: 1.6;">
        Sobald sich bei uns interessante Themen ergeben, melde ich mich umgehend bei Ihnen.
    </p>

    <p style="font-size: 15px; line-height: 1.6;">
        Mit freundlichen Grüßen<br>
        <strong>Milad Hamdard</strong><br>
        <span style="color: #718096; font-size: 13px;">sincirus GmbH – Personalberatung</span>
    </p>
</div>
"""

TEMPLATE_STELLENAUSSCHREIBUNG_SUBJECT = "Stellenangebot: {{ job_title }} bei {{ company_name }} – sincirus"

TEMPLATE_STELLENAUSSCHREIBUNG_BODY = """
<div style="font-family: 'DM Sans', Arial, sans-serif; color: #2d3748; max-width: 600px; margin: 0 auto; padding: 20px;">
    <p style="font-size: 15px; line-height: 1.6;">{{ salutation }},</p>

    <p style="font-size: 15px; line-height: 1.6;">
        wie besprochen, hier die Details zur Stelle, über die wir heute telefoniert haben:
    </p>

    <div style="background: #f7fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 24px; margin: 20px 0;">
        <h2 style="margin: 0 0 8px; font-size: 18px; color: #1a202c;">{{ job_title }}</h2>
        <p style="margin: 0 0 16px; font-size: 14px; color: #718096;">
            {{ company_name }}{% if job_location %} · {{ job_location }}{% endif %}
        </p>

        {% if job_description %}
        <div style="margin-bottom: 16px;">
            <p style="font-weight: 600; font-size: 14px; margin: 0 0 6px; color: #4a5568;">Aufgaben:</p>
            <p style="font-size: 14px; line-height: 1.6; margin: 0; color: #4a5568;">{{ job_description }}</p>
        </div>
        {% endif %}

        {% if job_requirements %}
        <div style="margin-bottom: 16px;">
            <p style="font-weight: 600; font-size: 14px; margin: 0 0 6px; color: #4a5568;">Anforderungen:</p>
            <p style="font-size: 14px; line-height: 1.6; margin: 0; color: #4a5568;">{{ job_requirements }}</p>
        </div>
        {% endif %}

        {% if salary_display %}
        <p style="font-size: 14px; margin: 0 0 4px;"><strong>Gehalt:</strong> {{ salary_display }}</p>
        {% endif %}
        {% if employment_type %}
        <p style="font-size: 14px; margin: 0;"><strong>Arbeitsmodell:</strong> {{ employment_type }}</p>
        {% endif %}
    </div>

    <p style="font-size: 15px; line-height: 1.6;">
        Falls Sie Interesse haben, melden Sie sich gerne bei mir – ich stelle den Kontakt zum Unternehmen her.
    </p>

    <p style="font-size: 15px; line-height: 1.6;">
        Mit freundlichen Grüßen<br>
        <strong>Milad Hamdard</strong><br>
        <span style="color: #718096; font-size: 13px;">sincirus GmbH – Personalberatung</span>
    </p>
</div>
"""


# ═══════════════════════════════════════════════════════════════
#  EmailService
# ═══════════════════════════════════════════════════════════════

class EmailService:
    """Orchestriert Email-Erstellung, Template-Rendering und Versand."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Anrede-Helper ──

    @staticmethod
    def _get_salutation(candidate: Candidate) -> str:
        """Erzeugt Anrede: 'Hallo Herr Schmidt' / 'Hallo Frau Mueller'.
        Fallback: 'Hallo Vorname' wenn kein gender gesetzt."""
        if candidate.gender and candidate.last_name:
            return f"Hallo {candidate.gender} {candidate.last_name}"
        return f"Hallo {candidate.first_name or 'zusammen'}"

    # ── Template Rendering ──

    def _render_kontaktdaten(self, candidate: Candidate) -> tuple[str, str]:
        """Rendert Kontaktdaten-Email. Returns (subject, body_html)."""
        subject = TEMPLATE_KONTAKTDATEN_SUBJECT
        body = Template(TEMPLATE_KONTAKTDATEN_BODY).render(
            salutation=self._get_salutation(candidate),
        )
        return subject, body

    def _render_stellenausschreibung(
        self, candidate: Candidate, job: ATSJob
    ) -> tuple[str, str]:
        """Rendert Stellenausschreibung-Email. Returns (subject, body_html)."""
        company_name = job.company.name if job.company else "Unternehmen"
        ctx = {
            "salutation": self._get_salutation(candidate),
            "job_title": job.title,
            "company_name": company_name,
            "job_location": job.location_city or "",
            "job_description": job.description or "",
            "job_requirements": job.requirements or "",
            "salary_display": job.salary_display if hasattr(job, "salary_display") else "",
            "employment_type": job.employment_type or "",
        }
        subject = Template(TEMPLATE_STELLENAUSSCHREIBUNG_SUBJECT).render(**ctx)
        body = Template(TEMPLATE_STELLENAUSSCHREIBUNG_BODY).render(**ctx)
        return subject, body

    # ── Job-Matching ──

    async def match_job_from_keywords(self, keywords: str) -> Optional[ATSJob]:
        """Findet einen ATS-Job anhand von GPT-Keywords (z.B. 'Bilanzbuchhalter München Allianz').

        Strategie: Zerlege Keywords, suche nach Company+Title, dann Title+City.
        """
        if not keywords or len(keywords.strip()) < 3:
            return None

        words = keywords.strip().split()

        # Tier 1: Versuche Company-Name + Titel zu matchen
        for word in words:
            query = (
                select(ATSJob)
                .join(Company, ATSJob.company_id == Company.id, isouter=True)
                .where(
                    ATSJob.status == ATSJobStatus.OPEN,
                    ATSJob.deleted_at.is_(None),
                    Company.name.ilike(f"%{word}%"),
                )
                .limit(5)
            )
            result = await self.db.execute(query)
            jobs = result.scalars().all()
            if jobs:
                # Unter den gefundenen: bestes Title-Match
                for job in jobs:
                    for w in words:
                        if w.lower() in (job.title or "").lower():
                            logger.info(f"Job matched (Company+Title): {job.title} [{job.id}]")
                            return job
                # Fallback: erster Treffer mit passender Company
                logger.info(f"Job matched (Company only): {jobs[0].title} [{jobs[0].id}]")
                return jobs[0]

        # Tier 2: Title + City Match
        for word in words:
            query = (
                select(ATSJob)
                .where(
                    ATSJob.status == ATSJobStatus.OPEN,
                    ATSJob.deleted_at.is_(None),
                    ATSJob.title.ilike(f"%{word}%"),
                )
                .limit(10)
            )
            result = await self.db.execute(query)
            jobs = result.scalars().all()
            for job in jobs:
                # Pruefe ob ein anderes Keyword im Stadtnamen vorkommt
                for w2 in words:
                    if w2 != word and w2.lower() in (job.location_city or "").lower():
                        logger.info(f"Job matched (Title+City): {job.title} [{job.id}]")
                        return job
            # Nur Title-Match als letzter Fallback
            if jobs:
                logger.info(f"Job matched (Title only): {jobs[0].title} [{jobs[0].id}]")
                return jobs[0]

        logger.warning(f"Kein ATS-Job gefunden fuer Keywords: {keywords}")
        return None

    # ── Email erstellen + senden ──

    async def _create_and_send_email(
        self,
        candidate: Candidate,
        email_type: str,
        subject: str,
        body_html: str,
        auto_send: bool,
        call_note_id=None,
        ats_job_id=None,
        gpt_context=None,
    ) -> EmailDraft:
        """Erstellt einen EmailDraft und sendet ihn ggf. sofort."""

        draft = EmailDraft(
            candidate_id=candidate.id,
            ats_job_id=ats_job_id,
            call_note_id=call_note_id,
            email_type=email_type,
            to_email=candidate.email,
            subject=subject,
            body_html=body_html,
            status=EmailDraftStatus.DRAFT.value,
            auto_send=auto_send,
            gpt_context=gpt_context,
        )
        self.db.add(draft)
        await self.db.flush()

        if auto_send:
            result = await MicrosoftGraphClient.send_email(
                to_email=candidate.email,
                subject=subject,
                body_html=body_html,
            )

            if result["success"]:
                draft.status = EmailDraftStatus.SENT.value
                draft.sent_at = datetime.now(timezone.utc)
                draft.microsoft_message_id = result.get("message_id")
                logger.info(f"Auto-Email gesendet: {email_type} an {candidate.email}")

                # Activity loggen
                activity = ATSActivity(
                    activity_type=ActivityType.EMAIL_SENT,
                    description=f"Auto-Email ({email_type}): {subject[:80]}",
                    candidate_id=candidate.id,
                    metadata_json={
                        "email_type": email_type,
                        "to_email": candidate.email,
                        "subject": subject,
                        "auto_send": True,
                        "draft_id": str(draft.id),
                    },
                )
                self.db.add(activity)
            else:
                draft.status = EmailDraftStatus.FAILED.value
                draft.send_error = result.get("error", "Unbekannter Fehler")
                logger.error(f"Auto-Email fehlgeschlagen: {result.get('error')}")

        return draft

    # ── Haupt-Orchestrierung ──

    async def process_email_actions(
        self,
        candidate_id,
        email_actions: list,
        call_note_id=None,
        call_type: str = None,
    ) -> dict:
        """Verarbeitet alle email_actions aus einem GPT-Extrakt.

        Returns: {"emails_sent": N, "drafts_created": N, "errors": [...]}
        """
        result = {"emails_sent": 0, "drafts_created": 0, "errors": []}

        # Kandidat laden
        candidate = await self.db.get(Candidate, candidate_id)
        if not candidate:
            result["errors"].append(f"Kandidat {candidate_id} nicht gefunden")
            return result

        if not candidate.email:
            result["errors"].append(f"Kandidat {candidate.first_name} {candidate.last_name} hat keine Email-Adresse")
            return result

        # Bei Qualifizierung IMMER Kontaktdaten-Email
        has_kontaktdaten = any(
            a.get("type") == "kontaktdaten"
            for a in email_actions
            if isinstance(a, dict)
        )
        if call_type == "qualifizierung" and not has_kontaktdaten:
            email_actions = list(email_actions) + [
                {"type": "kontaktdaten", "description": "Kontaktdaten nach Qualifizierungsgespräch"}
            ]

        for action in email_actions:
            if not isinstance(action, dict):
                continue

            action_type = action.get("type", "").lower()

            try:
                if action_type == "kontaktdaten":
                    subject, body = self._render_kontaktdaten(candidate)
                    draft = await self._create_and_send_email(
                        candidate=candidate,
                        email_type=EmailType.KONTAKTDATEN.value,
                        subject=subject,
                        body_html=body,
                        auto_send=True,
                        call_note_id=call_note_id,
                        gpt_context=action.get("description"),
                    )
                    if draft.is_sent:
                        result["emails_sent"] += 1
                    else:
                        result["errors"].append(f"Kontaktdaten-Email: {draft.send_error}")

                elif action_type == "stellenausschreibung":
                    # Stellenausschreibungen IMMER als Draft — Milad schaut drüber
                    job_keywords = action.get("job_keywords", "")
                    job = await self.match_job_from_keywords(job_keywords)

                    if job:
                        subject, body = self._render_stellenausschreibung(candidate, job)
                        draft = await self._create_and_send_email(
                            candidate=candidate,
                            email_type=EmailType.STELLENAUSSCHREIBUNG.value,
                            subject=subject,
                            body_html=body,
                            auto_send=False,
                            call_note_id=call_note_id,
                            ats_job_id=job.id,
                            gpt_context=action.get("description"),
                        )
                    else:
                        draft = await self._create_and_send_email(
                            candidate=candidate,
                            email_type=EmailType.STELLENAUSSCHREIBUNG.value,
                            subject=f"Stellenangebot für {candidate.first_name} – sincirus",
                            body_html=f"<p>Job-Keywords von GPT: {job_keywords}</p><p>Bitte manuell die richtige Stelle anhängen.</p>",
                            auto_send=False,
                            call_note_id=call_note_id,
                            gpt_context=f"Job nicht automatisch gefunden. Keywords: {job_keywords}",
                        )
                    result["drafts_created"] += 1

                elif action_type == "individuell":
                    # Individuell → immer Draft
                    description = action.get("description", "Email-Zusage aus Gespräch")
                    draft = await self._create_and_send_email(
                        candidate=candidate,
                        email_type=EmailType.INDIVIDUELL.value,
                        subject=f"Nachricht an {candidate.first_name} {candidate.last_name} – sincirus",
                        body_html=f"""
<div style="font-family: 'DM Sans', Arial, sans-serif; color: #2d3748; max-width: 600px; margin: 0 auto; padding: 20px;">
    <p style="font-size: 15px; line-height: 1.6;">{self._get_salutation(candidate)},</p>
    <p style="font-size: 15px; line-height: 1.6;">[Hier Inhalt ergänzen]</p>
    <p style="font-size: 15px; line-height: 1.6;">
        Mit freundlichen Grüßen<br>
        <strong>Milad Hamdard</strong><br>
        <span style="color: #718096; font-size: 13px;">sincirus GmbH – Personalberatung</span>
    </p>
</div>""",
                        auto_send=False,
                        call_note_id=call_note_id,
                        gpt_context=description,
                    )
                    result["drafts_created"] += 1

            except Exception as e:
                logger.error(f"Email-Action Fehler ({action_type}): {e}")
                result["errors"].append(f"{action_type}: {str(e)}")

        await self.db.flush()
        logger.info(
            f"Email-Actions verarbeitet: {result['emails_sent']} gesendet, "
            f"{result['drafts_created']} Drafts, {len(result['errors'])} Fehler"
        )
        return result

    # ── Draft Management ──

    async def send_draft(self, draft_id) -> dict:
        """Sendet einen Draft (nach Recruiter-Pruefung)."""
        draft = await self.db.get(EmailDraft, draft_id)
        if not draft:
            return {"success": False, "error": "Draft nicht gefunden"}

        if draft.status != EmailDraftStatus.DRAFT.value:
            return {"success": False, "error": f"Draft hat Status '{draft.status}', kann nicht gesendet werden"}

        result = await MicrosoftGraphClient.send_email(
            to_email=draft.to_email,
            subject=draft.subject,
            body_html=draft.body_html,
        )

        if result["success"]:
            draft.status = EmailDraftStatus.SENT.value
            draft.sent_at = datetime.now(timezone.utc)
            draft.microsoft_message_id = result.get("message_id")

            # Activity loggen
            activity = ATSActivity(
                activity_type=ActivityType.EMAIL_SENT,
                description=f"Email gesendet: {draft.subject[:80]}",
                candidate_id=draft.candidate_id,
                metadata_json={
                    "email_type": draft.email_type,
                    "to_email": draft.to_email,
                    "subject": draft.subject,
                    "auto_send": False,
                    "draft_id": str(draft.id),
                },
            )
            self.db.add(activity)
            await self.db.flush()

            return {"success": True, "message_id": result.get("message_id")}
        else:
            draft.send_error = result.get("error")
            draft.status = EmailDraftStatus.FAILED.value
            await self.db.flush()
            return {"success": False, "error": result.get("error")}

    async def list_drafts(
        self, candidate_id=None, status: str = None, limit: int = 50
    ) -> list[EmailDraft]:
        """Listet Email-Drafts, optional gefiltert."""
        query = select(EmailDraft).order_by(EmailDraft.created_at.desc()).limit(limit)
        if candidate_id:
            query = query.where(EmailDraft.candidate_id == candidate_id)
        if status:
            query = query.where(EmailDraft.status == status)
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def count_pending_drafts(self) -> int:
        """Zaehlt offene Drafts (fuer Dashboard-Badge)."""
        result = await self.db.execute(
            select(func.count(EmailDraft.id)).where(
                EmailDraft.status == EmailDraftStatus.DRAFT.value
            )
        )
        return result.scalar() or 0

    async def get_email_stats(self) -> dict:
        """Aggregierte Statistiken fuer das Email-Dashboard."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        seven_days_ago = now - timedelta(days=7)

        # Count by status
        status_counts = {}
        for status_val in [
            EmailDraftStatus.DRAFT.value,
            EmailDraftStatus.SENT.value,
            EmailDraftStatus.FAILED.value,
            EmailDraftStatus.CANCELLED.value,
        ]:
            result = await self.db.execute(
                select(func.count(EmailDraft.id)).where(
                    EmailDraft.status == status_val
                )
            )
            status_counts[status_val] = result.scalar() or 0

        # Sent today
        sent_today_result = await self.db.execute(
            select(func.count(EmailDraft.id)).where(
                EmailDraft.status == EmailDraftStatus.SENT.value,
                EmailDraft.sent_at >= today_start,
            )
        )
        sent_today = sent_today_result.scalar() or 0

        # Sent last 7 days
        sent_7d_result = await self.db.execute(
            select(func.count(EmailDraft.id)).where(
                EmailDraft.status == EmailDraftStatus.SENT.value,
                EmailDraft.sent_at >= seven_days_ago,
            )
        )
        sent_7d = sent_7d_result.scalar() or 0

        return {
            "pending": status_counts.get(EmailDraftStatus.DRAFT.value, 0),
            "failed": status_counts.get(EmailDraftStatus.FAILED.value, 0),
            "sent_today": sent_today,
            "sent_7d": sent_7d,
            "total_sent": status_counts.get(EmailDraftStatus.SENT.value, 0),
        }

    async def list_drafts_by_date(
        self,
        status: str = None,
        since: datetime = None,
        until: datetime = None,
        limit: int = 50,
    ) -> list[EmailDraft]:
        """Listet Email-Drafts mit Zeitraum-Filter."""
        query = (
            select(EmailDraft)
            .order_by(EmailDraft.created_at.desc())
            .limit(limit)
        )
        if status:
            query = query.where(EmailDraft.status == status)
        if since:
            if status == EmailDraftStatus.SENT.value:
                query = query.where(EmailDraft.sent_at >= since)
            else:
                query = query.where(EmailDraft.created_at >= since)
        if until:
            if status == EmailDraftStatus.SENT.value:
                query = query.where(EmailDraft.sent_at < until)
            else:
                query = query.where(EmailDraft.created_at < until)
        result = await self.db.execute(query)
        return list(result.scalars().all())
