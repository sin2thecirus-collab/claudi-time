"""AcquisitionEmailService — GPT-basierte Akquise-E-Mails generieren und versenden.

Generiert personalisierte Kaltakquise-E-Mails mit fiktivem Kandidaten,
verwaltet 3-E-Mail-Sequenz (Initial → Follow-up → Break-up),
sendet via Microsoft Graph (M365) oder IONOS SMTP.
"""

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.acquisition_email import AcquisitionEmail
from app.models.company_contact import CompanyContact
from app.models.job import Job

logger = logging.getLogger(__name__)

# ── GPT-Prompt fuer Akquise-E-Mail: Kandidaten-Pitch ──
AKQUISE_EMAIL_SYSTEM = """Du bist ein Personalberater im Finance-Bereich.
Du schreibst eine sachliche Kaltakquise-E-Mail, in der du einen fiktiven Kandidaten vorstellst.

ZIEL: Der Kunde soll neugierig werden und antworten. NICHT beeindrucken — sachlich informieren.

TONFALL: Nuechterner Geschaeftston. Schreibe wie ein erfahrener Berater, der kurz und praezise kommuniziert. KEINE Superlative, KEIN Schmeicheln, KEIN Uebertreiben. Stell dir vor, du schreibst einem CFO — der hat keine Zeit fuer Blabla.

REGELN:
- PLAIN-TEXT, kein HTML, keine Formatierung, KEINE Links/URLs
- 150-190 Woerter (KUERZER ist besser)
- IMMER SIEZEN ("Sie", "Ihnen", "Ihr") — NIEMALS duzen!
- Anrede: Nutze die uebergebene Anrede (Herr/Frau) exakt. Falls keine Anrede vorhanden: Bestimme das Geschlecht anhand des Vornamens. Bei nicht eindeutigen Vornamen: "Guten Tag [Vorname Nachname]"

BRANCHE DES UNTERNEHMENS — SO ERKENNST DU SIE:
- Lies den STELLENTEXT und die FIRMENBESCHREIBUNG genau
- Die uebergebene Branche aus der Datenbank kann FALSCH oder zu allgemein sein (z.B. "Elektrizitaetsversorgung" obwohl die Firma ein Facility-Manager ist)
- Bestimme die TATSAECHLICHE Branche des Unternehmens aus dem Stellentext (Was macht die Firma? Wer sind ihre Kunden?)
- Nutze DIESE erkannte Branche fuer den Kandidaten, NICHT blind das Branche-Feld

DER FIKTIVE KANDIDAT:
- NIEMALS einen Namen — immer "der Kandidat" oder "die Kandidatin"
- Erfahrungsjahre: ZWISCHEN 5 und 10 (NIEMALS weniger als 5)
- Branchenerfahrung: In der TATSAECHLICHEN Branche des Unternehmens (aus Stellentext erkannt)
- 2 KONKRETE Anforderungen aus der Stelle erwaehnen (Software, Abschlussarten, Aufgaben)
- Wechselgrund: "in einer beruflichen Neuorientierung" oder "sucht den naechsten Schritt" — EIN Satz, nicht mehr

TEXT-AUFBAU:
1. Anrede
2. EIN Satz Einstieg: Du hast einen Kandidaten der zur Stelle passen koennte
3. 2-3 Saetze Kandidaten-Profil: Erfahrungsjahre, Branche, 2 konkrete Staerken aus der Ausschreibung
4. "Unter welchen Voraussetzungen darf ich Ihnen das Profil weiterleiten?"
5. "Fuer Fragen bin ich zwischen 9 und 18 Uhr erreichbar."
6. "Mit freundlichen Gruessen" OHNE Signatur

VERBOTEN (Verstoss = sofort verwerfen):
- Superlative: "perfekt", "ideal", "umfassend", "hervorragend", "exzellent", "erstklassig"
- Schmeichelei: "renommiert", "hochgeschaetzt", "beeindruckend", "erfolgreich"
- Fuellwoerter: "ich hoffe es geht Ihnen gut", "mit Engagement und Expertise"
- Uebertreibungen: "genau der richtige", "passt perfekt", "einzigartige Qualifikation"
- Kandidaten-Name
- Negative Wechselgruende
- Weniger als 5 Jahre Erfahrung
- Annahmen ueber die Firma die NICHT im Stellentext stehen
- "Terminvorschlag"
- Links, URLs, Webseiten-Verweise
- Marketing-Floskeln ("dynamisches Umfeld", "spannende Aufgabe", "Know-how")
"""

AKQUISE_EMAIL_USER = """Erstelle eine Kaltakquise-E-Mail fuer folgende Vakanz:

**Firma:** {company_name}
**Branche laut Datenbank:** {industry} (ACHTUNG: kann falsch/ungenau sein — lies den Stellentext!)
**Position:** {position}
**Ansprechpartner:** {contact_salutation} {contact_name} ({contact_function})

**Stellenausschreibung (lies genau — hieraus erkennst du die ECHTE Branche):**
{job_text_excerpt}

AUFGABE:
1. Lies den Stellentext und erkenne was die Firma WIRKLICH macht (nicht blind "{industry}" uebernehmen)
2. Erfinde einen Kandidaten mit Erfahrung in der ECHTEN Branche der Firma
3. Nenne 2 konkrete Anforderungen aus der Ausschreibung
4. Sachlicher Ton — KEINE Superlative, KEIN Schmeicheln

Erstelle:
1. E-Mail-Betreff (max 60 Zeichen, sachlich, OHNE "Bewerbung")
2. E-Mail-Text (Plain-Text, 150-190 Woerter, nuechterner Geschaeftston)
3. Fiktiver Kandidat als JSON (OHNE Name!): {{"alter": ..., "erfahrung_jahre": ..., "aktuelle_position": "...", "branche": "...", "erp": "...", "besonderheit": "..."}}

Antwort als JSON:
{{"subject": "...", "body": "...", "candidate_fiction": {{...}}}}
"""

# ── GPT-Prompt fuer Kontaktdaten-E-Mail (Selbstpraesentation) ──
KONTAKTDATEN_EMAIL_SYSTEM = """Du schreibst eine professionelle Vorstellungs-E-Mail eines Personalberaters an einen Kunden, der im Telefonat gesagt hat "Schicken Sie mir Ihre Kontaktdaten, ich melde mich."

ZIEL: Der Kunde soll nach dem Lesen denken: "Das ist ein echter Experte, kein gewoehnlicher Recruiter. Wenn ich jemanden suche, rufe ich den an."

REGELN:
- PLAIN-TEXT, kein HTML, keine Formatierung, KEINE Links/URLs
- 200-250 Woerter
- IMMER SIEZEN ("Sie", "Ihnen", "Ihr") — NIEMALS duzen!
- Anrede: Nutze die uebergebene Anrede (Herr/Frau) exakt. Bei nicht eindeutigen Vornamen: "Guten Tag [Vorname Nachname]"

TEXT-AUFBAU:
1. EINSTIEG: Bedanke dich fuer das Telefonat. Beziehe dich darauf, dass der Kunde Kontaktdaten haben wollte.

2. VORSTELLUNG MILAD HAMDARD — folgende Punkte MUESSEN rein:
   - Seit 6 Jahren spezialisiert auf die Vermittlung von Finanzfachkraeften und Fuehrungskraeften (FiBu, BiBu, Lohn, Controlling, StFA)
   - Studium im Finanzbereich — versteht Positionen aus fachlicher Sicht, nicht nur als Recruiter
   - Kann Finanzpositionen tiefgehend verstehen und zwischen den Zeilen lesen
   - Versteht die Beduerfnisse von Unternehmen und kann Positionen praezise einordnen
   - Wenn der ideale Kandidat nicht sofort verfuegbar ist, findet er schnell passende Alternativen
   - Langjaerige Zusammenarbeit mit CFOs, Leitern Rechnungswesen und Personalabteilungen
   - Viele erfolgreiche Vermittlungen in den letzten Jahren

3. WAS DER KUNDE DAVON HAT:
   - Keine Flut an unqualifizierten Profilen — keine Massenvorschlaege auf gut Glueck
   - Jeder Kandidat wird im Vorfeld persoenlich qualifiziert und auf die spezifischen Anforderungen des Kunden abgestimmt
   - Alle relevanten Daten (Gehalt, Kuendigungsfrist, Verfuegbarkeit, Fachkenntnisse) werden VOR der Vorstellung mit dem Kandidaten abgeklaert
   - Der Kunde bekommt eine gezielte Vorauswahl, die auf seine Punkte und sein Unternehmen abgestimmt ist

4. ABSCHLUSS:
   - "Ich freue mich, wenn sich ein Bedarf ergibt — melden Sie sich jederzeit bei mir."
   - "Fuer Fragen bin ich zwischen 9 und 18 Uhr erreichbar."
   - "Mit freundlichen Gruessen" OHNE Signatur — die Signatur wird automatisch angehaengt

WENN DIE BRANCHE DES UNTERNEHMENS BEKANNT IST: Erwaehne, dass du bereits erfolgreich Positionen in dieser oder aehnlichen Branchen besetzt hast.

TONFALL: Selbstbewusst aber nicht arrogant. Professionell. Zeige Kompetenz durch konkrete Aussagen, nicht durch leere Floskeln.

VERBOTEN:
- Links, URLs, Webseiten-Verweise
- Marketing-Sprech, Floskeln
- Uebertreibungen ("der beste Recruiter", "garantiert")
"""

KONTAKTDATEN_EMAIL_USER = """Erstelle eine Vorstellungs-E-Mail fuer folgendes Telefonat:

**Firma:** {company_name}
**Branche:** {industry}
**Ansprechpartner:** {contact_salutation} {contact_name} ({contact_function})

Erstelle:
1. E-Mail-Betreff (max 60 Zeichen, z.B. "Unsere Kontaktdaten — Personalberatung Finance")
2. E-Mail-Text (Plain-Text, 200-250 Woerter)

Antwort als JSON:
{{"subject": "...", "body": "..."}}
"""

# ── Follow-up fuer Kontaktdaten-E-Mail (nach 14 Tagen) ──
KONTAKTDATEN_FOLLOWUP_SYSTEM = """Du schreibst eine kurze Nachfass-E-Mail (Plain-Text, max 80 Woerter).
Vor 2 Wochen wurde eine Vorstellungs-E-Mail mit Kontaktdaten gesendet. Keine Antwort kam.
IMMER SIEZEN ("Sie", "Ihnen", "Ihr") — NIEMALS duzen!
KEINE Links, KEINE URLs im Text.
- Beziehe dich auf das Telefonat und die gesendeten Kontaktdaten
- Nicht vorwurfsvoll, sondern verstaendnisvoll
- Erwaehne, dass du aktuell gute Kandidaten im Finance-Bereich hast
- "Sollte sich in Ihrem Haus ein Bedarf ergeben, stehe ich Ihnen jederzeit zur Verfuegung"
- Kurz, knapp, respektvoll
"""

# ── Follow-up Prompt ──
FOLLOWUP_EMAIL_SYSTEM = """Du schreibst eine kurze Follow-up-E-Mail (Plain-Text, max 80 Woerter).
Die Erst-E-Mail wurde vor 5-7 Tagen gesendet, keine Antwort kam.
IMMER SIEZEN ("Sie", "Ihnen", "Ihr") — NIEMALS duzen!
KEINE Links, KEINE URLs im Text.
Beziehe dich auf die Erst-E-Mail und erhoehe die Dringlichkeit:
- "Der Kandidat ist aktuell noch in Gespraechen..."
- "Wollte Ihnen die Gelegenheit nicht vorenthalten..."
- Kurz, knapp, respektvoll, kein Vorwurf
"""

# ── Break-up Prompt ──
BREAKUP_EMAIL_SYSTEM = """Du schreibst eine letzte, freundliche Abschluss-E-Mail (Plain-Text, max 60 Woerter).
Dies ist die 3. und letzte E-Mail. Tonfall: Verstaendnisvoll, tueroffnend.
IMMER SIEZEN ("Sie", "Ihnen", "Ihr") — NIEMALS duzen!
KEINE Links, KEINE URLs im Text.
- "Ich verstehe, dass es gerade nicht passt..."
- "Sollte sich die Situation aendern, melden Sie sich gerne"
- Keine Vorwuerfe, kein Druck
"""

# Signatur-Template (Plain-Text, konsistent mit bestehenden Signaturen)
EMAIL_SIGNATURE = """
--
Milad Hamdard
Senior Personalberater · Rechnungswesen & Controlling
+49 40 238 345 320 | +49 176 8000 47 41
{from_email}
www.sincirus.com | Ballindamm 3, 20095 Hamburg
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

        # Bei Follow-up/Break-up/Kontaktdaten-Followup: Parent-Email finden
        parent_email = None
        if email_type in ("follow_up", "break_up", "kontaktdaten_followup"):
            parent_email = await self._find_parent_email(job_id, email_type)

        # GPT-Call fuer E-Mail-Generierung
        subject, body, candidate_fiction = await self._generate_email_text(
            job=job,
            contact=contact,
            email_type=email_type,
            parent_email=parent_email,
        )

        # From-Email bestimmen
        # Bei Kontaktdaten-Followup: GLEICHE Mailbox wie Original-Kontaktdaten-E-Mail
        if not from_email and email_type == "kontaktdaten_followup" and parent_email:
            from_email = parent_email.from_email
        if not from_email:
            from app.config import settings
            from_email = settings.microsoft_sender_email

        # Unsubscribe-Token generieren
        unsubscribe_token = secrets.token_urlsafe(48)[:64]

        # Signatur anfuegen (KEIN Abmelde-Link in der E-Mail — Milad-Entscheidung)
        body_with_sig = body + EMAIL_SIGNATURE.format(from_email=from_email)

        # Sequence-Position bestimmen
        seq_map = {"initial": 1, "kontaktdaten": 1, "follow_up": 2, "kontaktdaten_followup": 2, "break_up": 3}
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

    async def _check_daily_limit(self, from_email: str) -> tuple[int, int]:
        """Prueft Tages-Limit fuer eine Mailbox. Returns (sent_today, daily_limit)."""
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        result = await self.db.execute(
            select(func.count(AcquisitionEmail.id)).where(
                AcquisitionEmail.from_email == from_email,
                AcquisitionEmail.sent_at >= today_start,
                AcquisitionEmail.status == "sent",
            )
        )
        sent_today = result.scalar() or 0

        # IONOS-Domains: 20/Tag (Warmup), M365: 100/Tag
        if "sincirus-karriere.de" in from_email or "jobs-sincirus.com" in from_email:
            daily_limit = 20
        else:
            daily_limit = 100

        return sent_today, daily_limit

    async def send_email(
        self,
        email_id: uuid.UUID,
        from_email: str | None = None,
    ) -> dict:
        """Sendet oder plant einen vorbereiteten E-Mail-Draft.

        Im Test-Modus: sofort senden (an Test-Adresse).
        Im Produktionsmodus: mit 2h Delay schedulen (n8n Cron sendet).

        Returns:
            {"success": bool, "message": str, "graph_message_id": str | None, "scheduled": bool}
        """
        email = await self.db.get(AcquisitionEmail, email_id)
        if not email:
            raise ValueError(f"Email {email_id} nicht gefunden")

        if email.status not in ("draft", "scheduled"):
            raise ValueError(f"Email hat Status '{email.status}', kann nur Drafts/Scheduled senden")

        if not email.to_email:
            raise ValueError("Keine Empfaenger-Adresse")

        # From-Email aktualisieren falls uebergeben
        if from_email:
            email.from_email = from_email

        # Tages-Limit pruefen (Backend-Sperre, nicht nur Frontend)
        sent_today, daily_limit = await self._check_daily_limit(email.from_email)
        if sent_today >= daily_limit:
            raise ValueError(
                f"Tages-Limit fuer {email.from_email} erreicht: {sent_today}/{daily_limit}"
            )

        # Test-Modus pruefen
        from app.services.acquisition_test_helpers import (
            is_test_mode,
            get_test_email,
            override_email_if_test,
        )
        test_mode = await is_test_mode(self.db)

        if test_mode:
            # Test-Modus: Sofort senden an Test-Adresse (kein Delay)
            test_email_addr = await get_test_email(self.db)
            email.to_email, email.subject = override_email_if_test(
                email.to_email, email.subject, test_mode, test_email_addr,
            )
            logger.info(f"TEST-MODUS: E-Mail umgeleitet an {email.to_email}")
            return await self._do_send(email)

        # Produktionsmodus: Mit Delay schedulen (wenn noch nicht scheduled)
        if email.status == "draft":
            delay_minutes = await self._get_email_delay_minutes()
            if delay_minutes > 0:
                email.status = "scheduled"
                email.scheduled_send_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
                await self.db.commit()
                logger.info(
                    f"E-Mail {email_id} geplant fuer {email.scheduled_send_at} "
                    f"(Delay: {delay_minutes} Min)"
                )
                return {
                    "success": True,
                    "message": f"E-Mail wird in {delay_minutes} Minuten gesendet",
                    "graph_message_id": None,
                    "scheduled": True,
                    "scheduled_send_at": email.scheduled_send_at.isoformat(),
                }

        # Sofort senden (Delay=0 oder bereits scheduled und jetzt faellig)
        return await self._do_send(email)

    async def _do_send(self, email: AcquisitionEmail) -> dict:
        """Fuehrt den tatsaechlichen E-Mail-Versand durch (SMTP/Graph)."""
        try:
            # Thread-Linking: In-Reply-To Header bei Follow-ups
            in_reply_to = None
            if email.parent_email_id:
                parent = await self.db.get(AcquisitionEmail, email.parent_email_id)
                if parent and parent.graph_message_id:
                    in_reply_to = parent.graph_message_id

            # Routing: IONOS-Domains via SMTP, sincirus.com via Microsoft Graph
            from app.services.ionos_smtp_client import IonosSmtpClient, is_ionos_mailbox

            if is_ionos_mailbox(email.from_email):
                # IONOS SMTP Versand
                from app.config import settings
                result = await IonosSmtpClient.send_email(
                    to_email=email.to_email,
                    subject=email.subject,
                    body_plain=email.body_plain,
                    from_email=email.from_email,
                    password=settings.ionos_smtp_password,
                    in_reply_to=in_reply_to,
                )
            else:
                # Microsoft Graph Versand (sincirus.com)
                from app.services.email_service import MicrosoftGraphClient
                result = await MicrosoftGraphClient.send_email(
                    to_email=email.to_email,
                    subject=email.subject,
                    body_html=f"<pre>{email.body_plain}</pre>",
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
                    "scheduled": False,
                }
            else:
                email.status = "failed"
                await self.db.commit()
                return {
                    "success": False,
                    "message": f"Fehler: {result.get('error', 'Unbekannt')}",
                    "graph_message_id": None,
                    "scheduled": False,
                }

        except Exception as e:
            email.status = "failed"
            await self.db.commit()
            logger.error(f"E-Mail-Versand fehlgeschlagen: {e}")
            return {
                "success": False,
                "message": str(e),
                "graph_message_id": None,
                "scheduled": False,
            }

    async def _get_email_delay_minutes(self) -> int:
        """Liest den E-Mail-Delay aus system_settings (Default: 120 Min = 2h)."""
        from app.models.settings import SystemSetting
        result = await self.db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == "acquisition_email_delay_minutes"
            )
        )
        val = result.scalar_one_or_none()
        try:
            return int(val) if val else 120
        except (ValueError, TypeError):
            return 120

    async def send_scheduled_emails(self) -> dict:
        """Sendet alle faelligen geplanten E-Mails (aufgerufen von n8n Cron).

        Returns:
            {"sent": int, "failed": int, "details": list}
        """
        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            select(AcquisitionEmail).where(
                AcquisitionEmail.status == "scheduled",
                AcquisitionEmail.scheduled_send_at.isnot(None),
                AcquisitionEmail.scheduled_send_at <= now,
            ).order_by(AcquisitionEmail.scheduled_send_at.asc())
            .limit(20)  # Max 20 pro Durchlauf (Tages-Limits beachten)
        )
        emails = result.scalars().all()

        sent = 0
        failed = 0
        details = []

        for email in emails:
            send_result = await self._do_send(email)
            if send_result.get("success"):
                sent += 1
                details.append({"id": str(email.id), "status": "sent", "to": email.to_email})
            else:
                failed += 1
                details.append({"id": str(email.id), "status": "failed", "error": send_result.get("message")})

        return {"sent": sent, "failed": failed, "details": details}

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
        elif email_type == "kontaktdaten_followup":
            # Suche letzte Kontaktdaten-Email
            target_type = "kontaktdaten"
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
        contact_salutation = contact.salutation or ""  # "Herr" / "Frau" aus CSV/DB
        contact_function = contact.position or "Personalverantwortliche/r"

        # System-Prompt je nach Typ
        if email_type == "kontaktdaten":
            system_prompt = KONTAKTDATEN_EMAIL_SYSTEM
        elif email_type == "kontaktdaten_followup":
            system_prompt = KONTAKTDATEN_FOLLOWUP_SYSTEM
        elif email_type == "follow_up":
            system_prompt = FOLLOWUP_EMAIL_SYSTEM
        elif email_type == "break_up":
            system_prompt = BREAKUP_EMAIL_SYSTEM
        else:
            system_prompt = AKQUISE_EMAIL_SYSTEM

        # User-Prompt je nach Typ
        if email_type in ("kontaktdaten", "kontaktdaten_followup"):
            user_prompt = KONTAKTDATEN_EMAIL_USER.format(
                company_name=job.company_name,
                industry=job.industry or "Unbekannt",
                contact_salutation=contact_salutation,
                contact_name=contact_name,
                contact_function=contact_function,
            )
        else:
            user_prompt = AKQUISE_EMAIL_USER.format(
                company_name=job.company_name,
                position=job.position,
                industry=job.industry or "Unbekannt",
                contact_salutation=contact_salutation,
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
