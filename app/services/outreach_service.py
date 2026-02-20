"""OutreachService — Kandidaten-Ansprache per E-Mail + Job-Description-PDF.

Phase 11c: Orchestriert den gesamten Outreach-Prozess:
1. Match laden (Job + Kandidat + Fahrzeit)
2. Job-Description-PDF generieren (WeasyPrint)
3. Personalisierte E-Mail generieren (GPT-4o-mini)
4. E-Mail + PDF-Anhang versenden (Microsoft Graph API)
5. Match-Status updaten (outreach_status = "sent")

Nutzt:
- JobDescriptionPdfService (Phase 11a) für PDF
- MicrosoftGraphClient (bestehend) für E-Mail-Versand
- OpenAI GPT-4o-mini für personalisierten E-Mail-Text
"""

import base64
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match

logger = logging.getLogger(__name__)

# ── E-Mail-Signatur (identisch mit email_service.py) ──
_BASE_URL = "https://claudi-time-production-46a5.up.railway.app"

EMAIL_SIGNATURE_OUTREACH = f"""
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

# ── GPT Prompt für personalisierte E-Mail ──
OUTREACH_EMAIL_PROMPT = """Du bist Milad Hamdard, Senior Personalberater bei sincirus (Personalberatung für Rechnungswesen & Controlling).

Schreibe eine kurze, professionelle E-Mail an einen Kandidaten, um ihm/ihr eine passende Stelle vorzustellen.

KANDIDAT:
- Name: {candidate_name}
- Anrede: {salutation}
- Aktuelle Position: {current_position}
- Aktuelles Unternehmen: {current_company}
- Stadt: {candidate_city}

STELLE:
- Position: {job_position}
- Unternehmen: {job_company}
- Stadt: {job_city}
- Branche: {job_industry}
- Fahrzeit mit Auto: {drive_time_car} Minuten
- Fahrzeit mit Bahn: {drive_time_transit} Minuten

REGELN:
1. Sieze den Kandidaten (Sie/Ihnen/Ihre)
2. Maximal 4-5 Sätze im Hauptteil
3. Erwähne die kurze Fahrzeit wenn unter 30 Minuten
4. Erwähne NICHT den Matching-Score
5. Erwähne dass ein PDF mit Details angehängt ist
6. Ton: Professionell aber persönlich, nicht übertrieben enthusiastisch
7. Kein "Sehr geehrte/r" — das steht schon in der Anrede. Starte direkt mit dem Text.
8. Keine Grußformel am Ende — die kommt aus der Signatur.
9. Schreibe NUR den E-Mail-Body (kein Betreff, keine Signatur).

Beispiel-Stil:
"im Rahmen meiner Recherche bin ich auf Ihr Profil aufmerksam geworden. Aktuell betreue ich eine spannende Vakanz als [Position] bei [Unternehmen] in [Stadt], die hervorragend zu Ihrem bisherigen Werdegang passt.

Der Arbeitsweg wäre mit ca. [X] Minuten sehr gut machbar. Im Anhang finden Sie eine ausführliche Stellenbeschreibung mit allen Details.

Haben Sie Interesse an einem kurzen, unverbindlichen Austausch? Ich freue mich auf Ihre Rückmeldung."
"""


class OutreachService:
    """Orchestriert die Kandidaten-Ansprache per E-Mail + Job-PDF."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def send_to_candidate(
        self,
        match_id: UUID,
        custom_message: str | None = None,
    ) -> dict:
        """Sendet Job-Description-PDF + personalisierte E-Mail an Kandidaten.

        Args:
            match_id: UUID des Match-Eintrags
            custom_message: Optionaler benutzerdefinierter E-Mail-Text (statt GPT)

        Returns:
            {"success": True/False, "message": "...", "email_sent_to": "..."}
        """
        # 1. Match laden
        match = await self._load_match(match_id)
        if not match:
            return {"success": False, "message": f"Match {match_id} nicht gefunden"}

        job = match.job
        candidate = match.candidate

        if not job:
            return {"success": False, "message": "Job nicht gefunden"}
        if not candidate:
            return {"success": False, "message": "Kandidat nicht gefunden"}
        if not candidate.email:
            return {"success": False, "message": f"Kandidat {candidate.full_name} hat keine E-Mail-Adresse"}

        # 2. Job-Description-PDF generieren
        try:
            from app.services.job_description_pdf_service import JobDescriptionPdfService
            pdf_service = JobDescriptionPdfService(self.db)
            pdf_bytes = await pdf_service.generate_job_pdf(match_id)
        except Exception as e:
            logger.error(f"PDF-Generierung fehlgeschlagen: {e}")
            return {"success": False, "message": f"PDF-Fehler: {e}"}

        # 3. E-Mail-Text generieren (GPT oder Custom)
        if custom_message:
            email_body_text = custom_message
        else:
            email_body_text = await self._generate_email_text(match, job, candidate)

        # 4. HTML E-Mail zusammenbauen
        salutation = self._build_salutation(candidate)
        email_html = self._build_email_html(salutation, email_body_text)

        # 5. E-Mail mit PDF-Anhang senden
        subject = f"Stellenangebot: {job.position} bei {job.company_name} – sincirus"
        pdf_filename = f"Stellenbeschreibung_{job.position.replace(' ', '_')}_{job.company_name.replace(' ', '_')}.pdf"

        result = await self._send_email_with_attachment(
            to_email=candidate.email,
            subject=subject,
            body_html=email_html,
            attachment_bytes=pdf_bytes,
            attachment_filename=pdf_filename,
        )

        # 6. Match-Status updaten
        if result.get("success"):
            match.outreach_status = "sent"
            match.outreach_sent_at = datetime.now(timezone.utc)
            await self.db.commit()

            logger.info(
                f"Outreach gesendet: {candidate.full_name} ({candidate.email}) "
                f"← Job: {job.position} @ {job.company_name}"
            )

        return {
            "success": result.get("success", False),
            "message": result.get("message", result.get("error", "")),
            "email_sent_to": candidate.email,
            "candidate_name": candidate.full_name,
            "job_position": job.position,
            "job_company": job.company_name,
        }

    async def batch_send(
        self,
        match_ids: list[UUID],
    ) -> dict:
        """Sendet E-Mails an mehrere Kandidaten gleichzeitig.

        Args:
            match_ids: Liste von Match-IDs

        Returns:
            {"total": N, "sent": N, "failed": N, "results": [...]}
        """
        results = []
        sent = 0
        failed = 0

        for match_id in match_ids:
            result = await self.send_to_candidate(match_id)
            results.append({
                "match_id": str(match_id),
                **result,
            })
            if result.get("success"):
                sent += 1
            else:
                failed += 1

        return {
            "total": len(match_ids),
            "sent": sent,
            "failed": failed,
            "results": results,
        }

    # ── Private Methods ──────────────────────────────

    async def _load_match(self, match_id: UUID) -> Match | None:
        """Lädt Match mit Job und Kandidat."""
        query = (
            select(Match)
            .options(
                selectinload(Match.job),
                selectinload(Match.candidate),
            )
            .where(Match.id == match_id)
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def _generate_email_text(
        self, match: Match, job: Job, candidate: Candidate
    ) -> str:
        """Generiert personalisierten E-Mail-Text via GPT-4o-mini."""
        try:
            prompt = OUTREACH_EMAIL_PROMPT.format(
                candidate_name=candidate.full_name or "",
                salutation=self._build_salutation(candidate),
                current_position=candidate.current_position or "nicht angegeben",
                current_company=candidate.current_company or "nicht angegeben",
                candidate_city=candidate.city or "nicht angegeben",
                job_position=job.position or "",
                job_company=job.company_name or "",
                job_city=job.display_city,
                job_industry=job.industry or "nicht angegeben",
                drive_time_car=match.drive_time_car_min or "unbekannt",
                drive_time_transit=match.drive_time_transit_min or "unbekannt",
            )

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o",
                        "messages": [
                            {"role": "system", "content": prompt},
                        ],
                        "max_tokens": 300,
                        "temperature": 0.7,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()

        except Exception as e:
            logger.warning(f"GPT E-Mail-Generierung fehlgeschlagen: {e}, nutze Fallback")
            return self._fallback_email_text(match, job, candidate)

    def _fallback_email_text(
        self, match: Match, job: Job, candidate: Candidate
    ) -> str:
        """Fallback E-Mail-Text wenn GPT nicht verfügbar."""
        drive_info = ""
        if match.drive_time_car_min:
            drive_info = f" Der Arbeitsweg beträgt ca. {match.drive_time_car_min} Minuten mit dem Auto."

        return (
            f"im Rahmen meiner Personalberatung bin ich auf Ihr Profil aufmerksam geworden. "
            f"Aktuell betreue ich eine interessante Vakanz als {job.position} "
            f"bei {job.company_name} in {job.display_city}, die sehr gut zu Ihrem "
            f"beruflichen Werdegang passen könnte.{drive_info}\n\n"
            f"Im Anhang finden Sie eine ausführliche Stellenbeschreibung mit allen Details "
            f"zur Position und zum Unternehmen.\n\n"
            f"Haben Sie Interesse an einem kurzen, unverbindlichen Austausch? "
            f"Ich freue mich auf Ihre Rückmeldung."
        )

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
        """Baut die HTML-E-Mail zusammen (Anrede + Text + Signatur)."""
        # Newlines in <br> umwandeln
        body_html = body_text.replace("\n\n", "</p><p style='font-size:15px; line-height:1.6;'>")
        body_html = body_html.replace("\n", "<br>")

        return f"""
<div style="font-family: 'DM Sans', Arial, sans-serif; color: #2d3748; max-width: 600px; margin: 0 auto; padding: 20px;">
    <p style="font-size: 15px; line-height: 1.6;">{salutation},</p>
    <p style="font-size: 15px; line-height: 1.6;">{body_html}</p>
    <p style="font-size: 15px; line-height: 1.6;">
        Mit freundlichen Grüßen
    </p>
    <br>
    {EMAIL_SIGNATURE_OUTREACH}
</div>
"""

    async def _send_email_with_attachment(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        attachment_bytes: bytes,
        attachment_filename: str,
    ) -> dict:
        """Sendet E-Mail mit PDF-Anhang via Microsoft Graph API.

        Microsoft Graph unterstützt Attachments direkt im sendMail-Payload.
        """
        import time

        sender = settings.microsoft_sender_email
        if not sender:
            return {"success": False, "error": "Kein Absender konfiguriert (MICROSOFT_SENDER_EMAIL)"}

        # Token holen
        try:
            from app.services.email_service import MicrosoftGraphClient
            token = await MicrosoftGraphClient._get_access_token()
        except Exception as e:
            logger.error(f"Graph Token-Fehler: {e}")
            return {"success": False, "error": f"Token-Fehler: {e}"}

        graph_url = f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"

        # PDF als Base64 kodieren
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
                logger.info(f"Outreach-Email gesendet an {to_email}: {subject}")
                return {
                    "success": True,
                    "message": f"E-Mail gesendet an {to_email}",
                    "message_id": resp.headers.get("request-id", ""),
                }
            else:
                error_text = resp.text[:500]
                logger.error(f"Graph Outreach-Email Fehler {resp.status_code}: {error_text}")
                return {"success": False, "error": f"HTTP {resp.status_code}: {error_text}"}

        except Exception as e:
            logger.error(f"Graph Outreach-Email Exception: {e}")
            return {"success": False, "error": str(e)}
