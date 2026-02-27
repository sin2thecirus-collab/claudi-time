"""AcquisitionEmailService — GPT-basierte Akquise-E-Mails generieren und versenden.

Generiert personalisierte Kaltakquise-E-Mails mit fiktivem Kandidaten,
verwaltet 3-E-Mail-Sequenz (Initial → Follow-up → Break-up),
sendet via Microsoft Graph (M365) oder IONOS SMTP.
"""

import logging
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.acquisition_email import AcquisitionEmail
from app.models.company_contact import CompanyContact
from app.models.job import Job

logger = logging.getLogger(__name__)

# ── GPT-Prompt fuer Akquise-E-Mail (Initial) ──
AKQUISE_EMAIL_SYSTEM = """Du bist ein erfahrener Personalberater im Finance-Bereich (FiBu, BiBu, Lohn, StFA).
Du schreibst persoenliche, kurze Kaltakquise-E-Mails an Personaler.

REGELN:
- PLAIN-TEXT, kein HTML, keine Formatierung
- Max 150 Woerter (kurz und knackig)
- Du erfindest einen FIKTIVEN Kandidaten der EXAKT auf die Stelle passt
- Der Kandidat "muss wechseln" (nicht "will wechseln" — Dringlichkeit!)
- Beziehe dich auf KONKRETE Anforderungen aus der Stellenausschreibung
- Nenne spezifische Skills (ERP-System, Branche, Weiterbildung)
- Call-to-Action: "Unter welchen Voraussetzungen waere ein Austausch moeglich?"
- Erreichbarkeit 9-18 Uhr, Terminvorschlag
- Tonfall: Professionell aber nicht steif, direkt, kein Marketing-Sprech
- KEIN "Sehr geehrte Damen und Herren" — immer persoenlich mit Name
"""

AKQUISE_EMAIL_USER = """Erstelle eine Kaltakquise-E-Mail fuer folgende Vakanz:

**Firma:** {company_name}
**Position:** {position}
**Branche:** {industry}
**Ansprechpartner:** {contact_name} ({contact_function})

**Stellenausschreibung:**
{job_text_excerpt}

Erstelle:
1. Einen passenden E-Mail-Betreff (max 60 Zeichen, persoenlich, ohne "Bewerbung")
2. Den E-Mail-Text (Plain-Text, max 150 Woerter)
3. Den fiktiven Kandidaten als JSON: {{"name": "...", "alter": ..., "erfahrung_jahre": ..., "aktuelle_position": "...", "branche": "...", "erp": "...", "besonderheit": "..."}}

Antwort als JSON:
{{"subject": "...", "body": "...", "candidate_fiction": {{...}}}}
"""

# ── Follow-up Prompt ──
FOLLOWUP_EMAIL_SYSTEM = """Du schreibst eine kurze Follow-up-E-Mail (Plain-Text, max 80 Woerter).
Die Erst-E-Mail wurde vor 5-7 Tagen gesendet, keine Antwort kam.
Beziehe dich auf die Erst-E-Mail und erhoehe die Dringlichkeit:
- "Der Kandidat ist aktuell noch in Gespraechen..."
- "Wollte Ihnen die Gelegenheit nicht vorenthalten..."
- Kurz, knapp, persoenlich, kein Vorwurf
"""

# ── Break-up Prompt ──
BREAKUP_EMAIL_SYSTEM = """Du schreibst eine letzte, freundliche Abschluss-E-Mail (Plain-Text, max 60 Woerter).
Dies ist die 3. und letzte E-Mail. Tonfall: Verstaendnisvoll, tueroffnend.
- "Ich verstehe, dass es gerade nicht passt..."
- "Sollte sich die Situation aendern, melden Sie sich gerne"
- Keine Vorwuerfe, kein Druck
- Abmelde-Hinweis am Ende
"""

# Signatur-Template
EMAIL_SIGNATURE = """
--
Milad Hamdard
Personalberater | Finance & Accounting
sincirus GmbH
Tel: +49 (0) 40 XXX XXX XX
E-Mail: {from_email}
Web: www.sincirus.com
"""

# Abmelde-Hinweis
UNSUBSCRIBE_FOOTER = """

---
Sie moechten keine weiteren E-Mails erhalten?
{unsubscribe_url}"""


class AcquisitionEmailService:
    """Generiert und versendet Akquise-E-Mails."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def generate_draft(
        self,
        job_id: uuid.UUID,
        contact_id: uuid.UUID,
        email_type: str = "initial",
        from_email: str | None = None,
    ) -> dict:
        """Generiert einen E-Mail-Entwurf via GPT.

        Args:
            job_id: Lead/Job ID
            contact_id: Empfaenger-Contact
            email_type: "initial" / "follow_up" / "break_up"
            from_email: Absender-Postfach (optional, default aus Config)

        Returns:
            {
                "email_id": UUID,
                "subject": str,
                "body_plain": str,
                "candidate_fiction": dict,
                "from_email": str,
                "to_email": str,
            }
        """
        # Job + Contact laden
        job = await self.db.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} nicht gefunden")

        contact = await self.db.get(CompanyContact, contact_id)
        if not contact:
            raise ValueError(f"Contact {contact_id} nicht gefunden")

        # Bei Follow-up/Break-up: Parent-Email finden
        parent_email = None
        if email_type in ("follow_up", "break_up"):
            parent_email = await self._find_parent_email(job_id, email_type)

        # GPT-Call fuer E-Mail-Generierung
        subject, body, candidate_fiction = await self._generate_email_text(
            job=job,
            contact=contact,
            email_type=email_type,
            parent_email=parent_email,
        )

        # From-Email bestimmen
        if not from_email:
            from app.config import settings
            from_email = settings.microsoft_sender_email

        # Unsubscribe-Token generieren
        unsubscribe_token = secrets.token_urlsafe(48)[:64]

        # Abmelde-Link anfuegen
        from app.config import settings
        base_url = getattr(settings, "app_url", "https://claudi-time-production-46a5.up.railway.app")
        unsubscribe_url = f"{base_url}/api/akquise/unsubscribe/{unsubscribe_token}"
        body_with_footer = body + UNSUBSCRIBE_FOOTER.format(unsubscribe_url=unsubscribe_url)

        # Signatur anfuegen
        body_with_sig = body_with_footer + EMAIL_SIGNATURE.format(from_email=from_email)

        # Sequence-Position bestimmen
        seq_map = {"initial": 1, "follow_up": 2, "break_up": 3}
        sequence_position = seq_map.get(email_type, 1)

        # Draft in DB speichern
        email = AcquisitionEmail(
            job_id=job_id,
            contact_id=contact_id,
            company_id=job.company_id,
            parent_email_id=parent_email.id if parent_email else None,
            from_email=from_email,
            to_email=contact.email,
            subject=subject,
            body_plain=body_with_sig,
            candidate_fiction=candidate_fiction,
            email_type=email_type,
            sequence_position=sequence_position,
            status="draft",
            unsubscribe_token=unsubscribe_token,
        )
        self.db.add(email)
        await self.db.commit()

        return {
            "email_id": str(email.id),
            "subject": subject,
            "body_plain": body_with_sig,
            "candidate_fiction": candidate_fiction,
            "from_email": from_email,
            "to_email": contact.email,
        }

    async def send_email(
        self,
        email_id: uuid.UUID,
        from_email: str | None = None,
    ) -> dict:
        """Sendet einen vorbereiteten E-Mail-Draft.

        Returns:
            {"success": bool, "message": str, "graph_message_id": str | None}
        """
        email = await self.db.get(AcquisitionEmail, email_id)
        if not email:
            raise ValueError(f"Email {email_id} nicht gefunden")

        if email.status != "draft":
            raise ValueError(f"Email hat Status '{email.status}', kann nur Drafts senden")

        if not email.to_email:
            raise ValueError("Keine Empfaenger-Adresse")

        # From-Email aktualisieren falls uebergeben
        if from_email:
            email.from_email = from_email

        try:
            # Thread-Linking: In-Reply-To Header bei Follow-ups
            headers = {}
            if email.parent_email_id:
                parent = await self.db.get(AcquisitionEmail, email.parent_email_id)
                if parent and parent.graph_message_id:
                    headers["In-Reply-To"] = parent.graph_message_id
                    headers["References"] = parent.graph_message_id

            # Via Microsoft Graph senden
            from app.services.email_service import MicrosoftGraphClient

            result = await MicrosoftGraphClient.send_email(
                to_email=email.to_email,
                subject=email.subject,
                body_html=f"<pre>{email.body_plain}</pre>",  # Plain-Text als Pre-formatted
                from_email=email.from_email,
            )

            if result.get("success"):
                email.status = "sent"
                email.sent_at = datetime.now(timezone.utc)
                email.graph_message_id = result.get("message_id")
                await self.db.commit()
                return {
                    "success": True,
                    "message": f"E-Mail an {email.to_email} gesendet",
                    "graph_message_id": result.get("message_id"),
                }
            else:
                email.status = "failed"
                await self.db.commit()
                return {
                    "success": False,
                    "message": f"Fehler: {result.get('error', 'Unbekannt')}",
                    "graph_message_id": None,
                }

        except Exception as e:
            email.status = "failed"
            await self.db.commit()
            logger.error(f"E-Mail-Versand fehlgeschlagen: {e}")
            return {
                "success": False,
                "message": str(e),
                "graph_message_id": None,
            }

    async def update_draft(
        self,
        email_id: uuid.UUID,
        subject: str | None = None,
        body_plain: str | None = None,
    ) -> dict:
        """Aktualisiert einen Draft (Bearbeitung durch Milad)."""
        email = await self.db.get(AcquisitionEmail, email_id)
        if not email:
            raise ValueError(f"Email {email_id} nicht gefunden")
        if email.status != "draft":
            raise ValueError("Nur Drafts koennen bearbeitet werden")

        if subject:
            email.subject = subject
        if body_plain:
            email.body_plain = body_plain

        await self.db.commit()
        return {"email_id": str(email.id), "updated": True}

    async def handle_unsubscribe(self, token: str) -> bool:
        """Verarbeitet Abmelde-Link (oeffentlich, kein Auth).

        Setzt acquisition_status auf blacklist_weich fuer die Company.
        """
        result = await self.db.execute(
            select(AcquisitionEmail)
            .where(AcquisitionEmail.unsubscribe_token == token)
        )
        email = result.scalar_one_or_none()

        if not email:
            return False

        # Company auf blacklist_weich setzen
        if email.company_id:
            from app.models.company import Company
            company = await self.db.get(Company, email.company_id)
            if company:
                company.acquisition_status = "blacklist"

        # Alle offenen Jobs dieser Firma auf blacklist_weich
        if email.company_id:
            from sqlalchemy import update as sql_update
            await self.db.execute(
                sql_update(Job)
                .where(
                    Job.company_id == email.company_id,
                    Job.acquisition_source.isnot(None),
                    Job.akquise_status.notin_(["blacklist_hart", "stelle_erstellt"]),
                )
                .values(
                    akquise_status="blacklist_weich",
                    akquise_status_changed_at=datetime.now(timezone.utc),
                )
            )

        await self.db.commit()
        return True

    async def get_emails_for_job(self, job_id: uuid.UUID) -> list[dict]:
        """Holt alle E-Mails zu einem Lead."""
        result = await self.db.execute(
            select(AcquisitionEmail)
            .where(AcquisitionEmail.job_id == job_id)
            .order_by(AcquisitionEmail.created_at.desc())
        )
        emails = result.scalars().all()

        return [
            {
                "id": str(e.id),
                "email_type": e.email_type,
                "sequence_position": e.sequence_position,
                "subject": e.subject,
                "status": e.status,
                "from_email": e.from_email,
                "to_email": e.to_email,
                "sent_at": e.sent_at.isoformat() if e.sent_at else None,
                "created_at": e.created_at.isoformat(),
            }
            for e in emails
        ]

    async def _find_parent_email(
        self, job_id: uuid.UUID, email_type: str,
    ) -> AcquisitionEmail | None:
        """Findet die Parent-Email fuer Follow-up/Break-up."""
        if email_type == "follow_up":
            # Suche letzte Initial-Email
            target_type = "initial"
        elif email_type == "break_up":
            # Suche letzte Follow-up oder Initial
            target_type = "follow_up"
        else:
            return None

        result = await self.db.execute(
            select(AcquisitionEmail)
            .where(
                AcquisitionEmail.job_id == job_id,
                AcquisitionEmail.email_type == target_type,
                AcquisitionEmail.status == "sent",
            )
            .order_by(AcquisitionEmail.sent_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _generate_email_text(
        self,
        job: Job,
        contact: CompanyContact,
        email_type: str,
        parent_email: AcquisitionEmail | None,
    ) -> tuple[str, str, dict]:
        """Generiert E-Mail-Text via GPT. Returns (subject, body, candidate_fiction)."""
        import json

        # Job-Text kuerzen (max 2000 Zeichen fuer GPT)
        job_text = (job.job_text or "")[:2000]

        contact_name = contact.full_name
        contact_function = contact.position or "Personalverantwortliche/r"

        # System-Prompt je nach Typ
        if email_type == "follow_up":
            system_prompt = FOLLOWUP_EMAIL_SYSTEM
        elif email_type == "break_up":
            system_prompt = BREAKUP_EMAIL_SYSTEM
        else:
            system_prompt = AKQUISE_EMAIL_SYSTEM

        user_prompt = AKQUISE_EMAIL_USER.format(
            company_name=job.company_name,
            position=job.position,
            industry=job.industry or "Unbekannt",
            contact_name=contact_name,
            contact_function=contact_function,
            job_text_excerpt=job_text,
        )

        try:
            from app.config import settings
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.7,
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]

            parsed = json.loads(content)
            subject = parsed.get("subject", f"Zu Ihrer Vakanz: {job.position}")
            body = parsed.get("body", "")
            candidate_fiction = parsed.get("candidate_fiction", {})

            return subject, body, candidate_fiction

        except Exception as e:
            logger.error(f"GPT E-Mail-Generierung fehlgeschlagen: {e}")
            # Fallback: Einfache Template-Email
            subject = f"Passender Kandidat fuer: {job.position}"
            body = (
                f"Guten Tag {contact_name},\n\n"
                f"bezugnehmend auf Ihre ausgeschriebene Stelle als {job.position} "
                f"bei {job.company_name} habe ich einen interessanten Kandidaten "
                f"in meinem Netzwerk, der gut passen koennte.\n\n"
                f"Gerne wuerde ich Ihnen diesen in einem kurzen Telefonat vorstellen.\n\n"
                f"Wann passt es Ihnen am besten?\n\n"
                f"Beste Gruesse"
            )
            return subject, body, {}
