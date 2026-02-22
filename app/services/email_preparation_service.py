"""EmailPreparationService — E-Mail-Vorbereitung + Versand fuer Action Board.

Phase 4: 4 E-Mail-Varianten (Erst/Folgekontakt x Kandidat/Kunde),
GPT-4o fachliche Einschaetzung, Microsoft Graph Versand.

Ablauf:
1. prepare_email() — Bestimmt Variante, generiert Text via GPT + Template
2. send_email() — Generiert PDF, baut HTML, sendet via Microsoft Graph
"""

import base64
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus

logger = logging.getLogger(__name__)

# ── Basis-URL fuer Signatur-Bild ──
_BASE_URL = "https://claudi-time-production-46a5.up.railway.app"

# ── E-Mail-Signatur (identisch mit outreach_service.py) ──
EMAIL_SIGNATURE = f"""
<table border="0" cellpadding="0" cellspacing="0" style="font-family:Arial,Helvetica,sans-serif; border-collapse:collapse; max-width:472px; width:100%;">
  <tr><td colspan="2" style="border-top:2px solid #3E577E; padding-bottom:16px;"></td></tr>
  <tr>
    <td width="82" style="vertical-align:top; padding-right:16px;">
      <img src="{_BASE_URL}/static/images/milad_foto.jpg" width="82" height="82" alt="Milad Hamdard" style="border-radius:10px; display:block;">
    </td>
    <td style="vertical-align:top;">
      <table border="0" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
        <tr><td style="font-family:Arial,Helvetica,sans-serif; font-size:17px; font-weight:bold; color:#1e2d45; line-height:20px; padding-bottom:1px;">Milad Hamdard</td></tr>
        <tr><td style="font-family:Arial,Helvetica,sans-serif; font-size:11px; color:#8f8c8d; line-height:14px; padding-bottom:10px;">Senior Personalberater &middot; Rechnungswesen &amp; Controlling</td></tr>
        <tr><td style="font-family:Arial,Helvetica,sans-serif; font-size:12px; line-height:19px; color:#3E577E;">
          <a href="tel:+494087406088" style="text-decoration:none; color:#3E577E;">+49 40 874 060 88</a><span style="color:#c8c8c8;">&ensp;|&ensp;</span><a href="tel:+4917680004741" style="text-decoration:none; color:#3E577E;">+49 176 8000 47 41</a>
        </td></tr>
        <tr><td style="font-family:Arial,Helvetica,sans-serif; font-size:12px; line-height:19px; color:#3E577E;">
          <a href="mailto:hamdard@sincirus.com" style="text-decoration:none; color:#3E577E;">hamdard@sincirus.com</a>
        </td></tr>
        <tr><td style="font-family:Arial,Helvetica,sans-serif; font-size:12px; line-height:19px; color:#3E577E;">
          <a href="https://sincirus.com/" style="text-decoration:none; color:#3E577E;">www.sincirus.com</a><span style="color:#c8c8c8;">&ensp;|&ensp;</span><span style="color:#3E577E;">Ballindamm 3, 20095 Hamburg</span>
        </td></tr>
      </table>
    </td>
  </tr>
</table>
"""

# ── GPT Prompt fuer fachliche Einschaetzung ──
FACHLICHE_EINSCHAETZUNG_PROMPT = """Du bist Milad Hamdard, Senior Personalberater bei sincirus (Personalberatung fuer Rechnungswesen & Controlling).

Schreibe eine kurze fachliche Einschaetzung (2-3 Saetze), warum der Kandidat und die Stelle zusammenpassen.

KANDIDAT:
- Aktuelle Position: {candidate_position}
- Aktuelles Unternehmen: {candidate_company}
- Stadt: {candidate_city}
- Skills: {candidate_skills}

STELLE:
- Position: {job_position}
- Unternehmen: {job_company}
- Stadt: {job_city}
- Branche: {job_industry}

MATCH-DATEN:
- Staerken: {strengths}
- Schwaechen: {weaknesses}

RICHTUNG: {direction_label}

REGELN:
1. Maximal 2-3 Saetze
2. Fachlich und konkret, keine Floskeln
3. Erwaehne konkrete Uebereinstimmungen (z.B. "DATEV-Erfahrung", "BiBu-Qualifikation")
4. Wenn Richtung "An Kandidat": Sieze den Kandidaten, beziehe dich auf "Ihr Profil", "Ihre Erfahrung"
5. Wenn Richtung "An Kunden": Neutral formulieren, beziehe dich auf "der Kandidat", "seine/ihre Erfahrung"
6. Schreibe NUR die Einschaetzung, keine Anrede, kein Betreff, keine Signatur.
"""


class EmailPreparationService:
    """Bereitet E-Mails vor und versendet sie fuer das Action Board."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ══════════════════════════════════════════════════════════
    # PREPARE — E-Mail vorbereiten (Text generieren, NICHT senden)
    # ══════════════════════════════════════════════════════════

    async def prepare_email(
        self,
        match_id: UUID,
        direction: str,
        contact_email: str | None = None,
    ) -> dict:
        """Bereitet eine E-Mail vor (Text + Metadaten), sendet NICHT.

        Args:
            match_id: UUID des Matches
            direction: "job_an_kandidat" oder "profil_an_kunden"
            contact_email: E-Mail des Empfaengers (nur bei profil_an_kunden)

        Returns:
            Dict mit subject, body_text, variant, recipient_email, etc.
        """
        match = await self._load_match(match_id)
        if not match:
            return {"error": f"Match {match_id} nicht gefunden"}

        job = match.job
        candidate = match.candidate
        if not job or not candidate:
            return {"error": "Job oder Kandidat nicht gefunden"}

        # Empfaenger bestimmen
        if direction == "job_an_kandidat":
            recipient_email = candidate.email or ""
            recipient_name = candidate.full_name or ""
        else:
            recipient_email = contact_email or ""
            recipient_name = contact_email or ""

        if not recipient_email:
            return {"error": "Kein Empfaenger (keine E-Mail-Adresse)"}

        # Erst-/Folgekontakt erkennen
        variant = await self._detect_variant(match, direction)

        # Fachliche Einschaetzung via GPT
        einschaetzung = await self._generate_fachliche_einschaetzung(
            match, job, candidate, direction
        )

        # E-Mail-Text aus Template bauen
        subject, body_text = self._build_email_from_template(
            direction, variant, match, job, candidate, einschaetzung, contact_email
        )

        return {
            "subject": subject,
            "body_text": body_text,
            "variant": variant,
            "direction": direction,
            "recipient_email": recipient_email,
            "recipient_name": recipient_name,
            "candidate_name": candidate.full_name or "",
            "candidate_id": str(candidate.id),
            "job_position": job.position or "",
            "job_company": job.company_name or "",
            "job_id": str(job.id),
        }

    # ══════════════════════════════════════════════════════════
    # SEND — E-Mail mit PDF-Anhang senden
    # ══════════════════════════════════════════════════════════

    async def send_email(
        self,
        match_id: UUID,
        direction: str,
        recipient_email: str,
        subject: str,
        body_text: str,
    ) -> dict:
        """Sendet E-Mail mit PDF-Anhang via Microsoft Graph.

        Args:
            match_id: UUID des Matches
            direction: "job_an_kandidat" oder "profil_an_kunden"
            recipient_email: Empfaenger-E-Mail
            subject: Betreff (ggf. vom User editiert)
            body_text: E-Mail-Text (ggf. vom User editiert)

        Returns:
            {"success": True/False, "message": "..."}
        """
        match = await self._load_match(match_id)
        if not match:
            return {"success": False, "message": f"Match {match_id} nicht gefunden"}

        job = match.job
        candidate = match.candidate
        if not job or not candidate:
            return {"success": False, "message": "Job oder Kandidat nicht gefunden"}

        # PDF generieren
        try:
            pdf_bytes, pdf_filename = await self._generate_pdf(
                direction, match, job, candidate
            )
        except Exception as e:
            logger.error(f"PDF-Generierung fehlgeschlagen: {e}")
            return {"success": False, "message": f"PDF-Fehler: {e}"}

        # HTML-E-Mail bauen
        salutation = self._build_salutation(candidate) if direction == "job_an_kandidat" else ""
        email_html = self._build_email_html(salutation, body_text)

        # Via Microsoft Graph senden
        result = await self._send_via_graph(
            to_email=recipient_email,
            subject=subject,
            body_html=email_html,
            attachment_bytes=pdf_bytes,
            attachment_filename=pdf_filename,
        )

        # Match-Status aktualisieren
        if result.get("success"):
            match.user_feedback = direction
            match.status = MatchStatus.PRESENTED
            match.presentation_status = "prepared"
            match.outreach_status = "sent"
            match.outreach_sent_at = datetime.now(timezone.utc)
            if direction == "profil_an_kunden":
                match.feedback_note = f"Empfaenger: {recipient_email}"
            await self.db.commit()

            logger.info(
                f"E-Mail gesendet: {direction} — "
                f"Match {match_id}, an {recipient_email}, "
                f"Kandidat: {candidate.full_name}, Job: {job.position}"
            )

        return {
            "success": result.get("success", False),
            "message": result.get("message", result.get("error", "")),
            "email_sent_to": recipient_email,
        }

    # ══════════════════════════════════════════════════════════
    # PRIVATE METHODS
    # ══════════════════════════════════════════════════════════

    async def _load_match(self, match_id: UUID) -> Match | None:
        query = (
            select(Match)
            .options(selectinload(Match.job), selectinload(Match.candidate))
            .where(Match.id == match_id)
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def _detect_variant(self, match: Match, direction: str) -> str:
        """Erkennt ob Erst- oder Folgekontakt."""
        if direction == "job_an_kandidat":
            # Hat der Kandidat schon eine E-Mail bekommen?
            result = await self.db.execute(
                select(Match.id).where(
                    Match.candidate_id == match.candidate_id,
                    Match.outreach_status == "sent",
                    Match.id != match.id,
                ).limit(1)
            )
            has_previous = result.scalar_one_or_none() is not None
        else:
            # Haben wir diesem Kunden fuer diesen Job schon jemanden vorgestellt?
            result = await self.db.execute(
                select(Match.id).where(
                    Match.job_id == match.job_id,
                    Match.user_feedback == "profil_an_kunden",
                    Match.outreach_status == "sent",
                    Match.id != match.id,
                ).limit(1)
            )
            has_previous = result.scalar_one_or_none() is not None

        return "folgekontakt" if has_previous else "erstkontakt"

    async def _generate_fachliche_einschaetzung(
        self, match: Match, job: Job, candidate: Candidate, direction: str
    ) -> str:
        """Generiert 2-3 Saetze fachliche Einschaetzung via GPT-4o."""
        # Match-Daten fuer den Prompt
        strengths = ""
        weaknesses = ""
        if match.ai_assessment:
            strengths = ", ".join(match.ai_assessment.get("strengths", [])[:5])
            weaknesses = ", ".join(match.ai_assessment.get("weaknesses", [])[:3])

        # Kandidaten-Skills
        candidate_skills = ""
        if candidate.skills and isinstance(candidate.skills, list):
            candidate_skills = ", ".join(candidate.skills[:10])

        direction_label = "An Kandidat" if direction == "job_an_kandidat" else "An Kunden"

        prompt = FACHLICHE_EINSCHAETZUNG_PROMPT.format(
            candidate_position=candidate.current_position or "nicht angegeben",
            candidate_company=candidate.current_company or "nicht angegeben",
            candidate_city=candidate.city or "nicht angegeben",
            candidate_skills=candidate_skills or "nicht angegeben",
            job_position=job.position or "",
            job_company=job.company_name or "",
            job_city=getattr(job, "display_city", "") or job.city or "",
            job_industry=job.industry or "nicht angegeben",
            strengths=strengths or "keine Daten",
            weaknesses=weaknesses or "keine Daten",
            direction_label=direction_label,
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o",
                        "messages": [{"role": "system", "content": prompt}],
                        "max_tokens": 200,
                        "temperature": 0.7,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"GPT fachliche Einschaetzung fehlgeschlagen: {e}")
            return self._fallback_einschaetzung(job, candidate, direction)

    def _fallback_einschaetzung(
        self, job: Job, candidate: Candidate, direction: str
    ) -> str:
        """Fallback wenn GPT nicht verfuegbar."""
        if direction == "job_an_kandidat":
            return (
                f"Die ausgeschriebene Position als {job.position} bei "
                f"{job.company_name} passt gut zu Ihrem bisherigen Werdegang."
            )
        else:
            return (
                f"Der Kandidat bringt relevante Erfahrung fuer die Position "
                f"als {job.position} mit."
            )

    def _build_email_from_template(
        self,
        direction: str,
        variant: str,
        match: Match,
        job: Job,
        candidate: Candidate,
        einschaetzung: str,
        contact_email: str | None,
    ) -> tuple[str, str]:
        """Baut Betreff + Body aus den 4 Template-Varianten."""

        job_city = getattr(job, "display_city", "") or job.city or ""
        salutation_candidate = self._build_salutation(candidate)
        drive_car = match.drive_time_car_min
        drive_transit = match.drive_time_transit_min

        # Eckdaten-Block (fuer Job an Kandidat)
        eckdaten = (
            f"Position:    {job.position}\n"
            f"Unternehmen: {job.company_name}, {job_city}\n"
        )
        if job.employment_type:
            eckdaten += f"Arbeitszeit: {job.employment_type}\n"
        if drive_car:
            eckdaten += f"Fahrzeit:    ca. {drive_car} Minuten von {candidate.city or 'Ihrem Standort'}\n"

        # ── Variante 1: Job → Kandidat (Erstkontakt) ──
        if direction == "job_an_kandidat" and variant == "erstkontakt":
            subject = f"Passende Stelle als {job.position} bei {job.company_name} in {job_city}"
            body = (
                f"{salutation_candidate},\n\n"
                f"mein Name ist Milad Hamdard von Sincirus und ich bin auf Ihr Profil "
                f"aufmerksam geworden. Ich habe eine Stelle, die gut zu Ihrem Hintergrund "
                f"als {candidate.current_position or 'Fachkraft im Rechnungswesen'} passen koennte.\n\n"
                f"{einschaetzung}\n\n"
                f"Hier die Eckdaten auf einen Blick:\n\n"
                f"{eckdaten}\n"
                f"Anbei finden Sie die ausfuehrliche Stellenbeschreibung als PDF.\n\n"
                f"Haetten Sie Interesse an einem kurzen Telefonat dazu?\n\n"
                f"Beste Gruesse\n"
                f"Milad Hamdard\n"
                f"Sincirus | Finance & Accounting Recruiting"
            )

        # ── Variante 2: Job → Kandidat (Folgekontakt) ──
        elif direction == "job_an_kandidat" and variant == "folgekontakt":
            subject = f"Neuer Vorschlag: {job.position} bei {job.company_name}"
            body = (
                f"{salutation_candidate},\n\n"
                f"ich habe eine weitere Stelle gefunden, die zu Ihnen passen koennte.\n\n"
                f"{einschaetzung}\n\n"
                f"Die Details im Ueberblick:\n\n"
                f"{eckdaten}\n"
                f"Anbei die ausfuehrliche Beschreibung. Lassen Sie mich gerne wissen, "
                f"ob diese Stelle fuer Sie interessant ist.\n\n"
                f"Beste Gruesse\n"
                f"Milad Hamdard"
            )

        # ── Variante 3: Profil → Kunde (Erstkontakt) ──
        elif direction == "profil_an_kunden" and variant == "erstkontakt":
            subject = f"Qualifiziertes Profil fuer Ihre Stelle als {job.position}"
            # Gegenuberstellung aus Match-Daten
            gegen = self._build_gegenuberstellung(match, job, candidate)
            body = (
                f"Guten Tag,\n\n"
                f"mein Name ist Milad Hamdard von Sincirus, wir sind spezialisiert auf "
                f"Finance & Accounting Recruiting. Fuer Ihre ausgeschriebene Stelle als "
                f"{job.position} habe ich einen passenden Kandidaten fuer Sie.\n\n"
                f"Kurze Gegenuberstellung:\n\n"
                f"{gegen}\n\n"
                f"{einschaetzung}\n\n"
                f"Anbei das ausfuehrliche Kandidatenprofil als PDF.\n"
                f"Ich freue mich auf Ihre Rueckmeldung.\n\n"
                f"Beste Gruesse\n"
                f"Milad Hamdard\n"
                f"Sincirus | Finance & Accounting Recruiting"
            )

        # ── Variante 4: Profil → Kunde (Folgekontakt) ──
        else:
            subject = f"Weiteres Profil fuer {job.position}"
            gegen = self._build_gegenuberstellung(match, job, candidate)
            body = (
                f"Guten Tag,\n\n"
                f"anbei erhalten Sie ein weiteres Profil fuer Ihre Stelle als {job.position}.\n\n"
                f"Kurzuebersicht:\n\n"
                f"{gegen}\n\n"
                f"{einschaetzung}\n\n"
                f"Das ausfuehrliche Profil finden Sie im Anhang.\n\n"
                f"Beste Gruesse\n"
                f"Milad Hamdard"
            )

        return subject, body

    def _build_gegenuberstellung(
        self, match: Match, job: Job, candidate: Candidate
    ) -> str:
        """Baut eine kurze Gegenuberstellung fuer Kunden-E-Mails."""
        lines = []
        strengths = []
        if match.ai_assessment:
            strengths = match.ai_assessment.get("strengths", [])[:4]

        if candidate.current_position:
            lines.append(f"Position:   {candidate.current_position}")
        if candidate.city:
            lines.append(f"Standort:   {candidate.city}")

        for i, s in enumerate(strengths):
            if i == 0:
                lines.append(f"Staerke:    {s}")
            else:
                lines.append(f"            {s}")

        if match.drive_time_car_min:
            lines.append(f"Fahrzeit:   ca. {match.drive_time_car_min} Min.")

        return "\n".join(lines) if lines else "Siehe angehaengtes Profil fuer Details."

    async def _generate_pdf(
        self, direction: str, match: Match, job: Job, candidate: Candidate
    ) -> tuple[bytes, str]:
        """Generiert das passende PDF (Job oder Profil)."""
        if direction == "job_an_kandidat":
            from app.services.job_vorstellung_pdf_service import JobVorstellungPdfService
            pdf_service = JobVorstellungPdfService(self.db)
            pdf_bytes = await pdf_service.generate_job_vorstellung_pdf(
                job_id=job.id, candidate_id=candidate.id, match_id=match.id
            )
            filename = f"Stellenbeschreibung_{(job.position or 'Stelle').replace(' ', '_')}.pdf"
        else:
            from app.services.profile_pdf_service import ProfilePdfService
            pdf_service = ProfilePdfService(self.db)
            pdf_bytes = await pdf_service.generate_profile_pdf(candidate.id)
            name = (candidate.full_name or "Kandidat").replace(" ", "_")
            filename = f"Kandidatenprofil_{name}.pdf"

        return pdf_bytes, filename

    def _build_salutation(self, candidate: Candidate) -> str:
        """Erstellt die Anrede."""
        gender = (candidate.gender or "").strip().lower()
        last_name = candidate.last_name or ""

        if gender in ("frau", "female", "f", "w"):
            return f"Sehr geehrte Frau {last_name}"
        elif gender in ("herr", "male", "m"):
            return f"Sehr geehrter Herr {last_name}"
        else:
            name = f"{candidate.first_name or ''} {last_name}".strip()
            return f"Guten Tag {name}" if name else "Guten Tag"

    def _build_email_html(self, salutation: str, body_text: str) -> str:
        """Baut die HTML-E-Mail (Text + Signatur)."""
        # Newlines in HTML umwandeln
        body_html = body_text.replace("\n\n", "</p><p style='font-size:15px; line-height:1.6;'>")
        body_html = body_html.replace("\n", "<br>")

        sal_html = f"<p style='font-size:15px; line-height:1.6;'>{salutation},</p>" if salutation else ""

        return f"""
<div style="font-family: 'DM Sans', Arial, sans-serif; color: #2d3748; max-width: 600px; margin: 0 auto; padding: 20px;">
    {sal_html}
    <p style="font-size: 15px; line-height: 1.6;">{body_html}</p>
    <br>
    {EMAIL_SIGNATURE}
</div>
"""

    async def _send_via_graph(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        attachment_bytes: bytes,
        attachment_filename: str,
    ) -> dict:
        """Sendet E-Mail mit PDF-Anhang via Microsoft Graph API."""
        sender = settings.microsoft_sender_email
        if not sender:
            return {"success": False, "error": "Kein Absender konfiguriert (MICROSOFT_SENDER_EMAIL)"}

        try:
            from app.services.email_service import MicrosoftGraphClient
            token = await MicrosoftGraphClient._get_access_token()
        except Exception as e:
            logger.error(f"Graph Token-Fehler: {e}")
            return {"success": False, "error": f"Token-Fehler: {e}"}

        graph_url = f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
        pdf_base64 = base64.b64encode(attachment_bytes).decode("utf-8")

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
                "attachments": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": attachment_filename,
                        "contentType": "application/pdf",
                        "contentBytes": pdf_base64,
                    }
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
                logger.info(f"E-Mail gesendet an {to_email}: {subject}")
                return {"success": True, "message": f"E-Mail gesendet an {to_email}"}
            else:
                error_text = resp.text[:500]
                logger.error(f"Graph E-Mail Fehler {resp.status_code}: {error_text}")
                return {"success": False, "error": f"HTTP {resp.status_code}: {error_text}"}

        except Exception as e:
            logger.error(f"Graph E-Mail Exception: {e}")
            return {"success": False, "error": str(e)}
