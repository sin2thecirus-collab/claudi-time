"""EmailGeneratorService — KI-generierte Vorstellungs-E-Mails + Fallback-Kaskade.

Generiert professionelle Vorstellungs-E-Mails fuer Kandidaten an Unternehmen
mittels Claude Opus 4.6 (Taetigkeitsabgleich-Prompt). Verwaltet die Follow-Up-
Sequenz (2 Tage + 3 Tage) und die Fallback-E-Mail-Kaskade (bewerber@, karriere@, hr@ etc.).

Ablauf:
1. generate_presentation_email() — Claude Opus generiert initiale Vorstellungs-E-Mail
2. generate_followup_email() — Claude generiert Follow-Up-Erinnerungen
3. generate_fallback_emails() — Fallback-Adressen fuer Domains ohne Ansprechpartner
4. get_html_signature() — Sincirus HTML-Signatur fuer E-Mail-Versand
"""

import json
import logging
import re
from uuid import UUID

from anthropic import AsyncAnthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.candidate import Candidate
from app.models.client_presentation import ClientPresentation
from app.models.company import Company
from app.models.company_contact import CompanyContact
from app.models.job import Job
from app.models.match import Match

logger = logging.getLogger(__name__)

# ── Basis-URL fuer Signatur-Bilder (identisch mit email_service.py) ──
_BASE_URL = "https://claudi-time-production-46a5.up.railway.app"

# ── Fallback-E-Mail-Prefixe (Reihenfolge ist relevant) ──
FALLBACK_PREFIXES = [
    "bewerber",
    "karriere",
    "hr",
    "jobs",
    "humanresources",
    "personal",
    "career",
]


class EmailGeneratorService:
    """Generiert KI-basierte Vorstellungs-E-Mails und verwaltet die Fallback-Kaskade."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ══════════════════════════════════════════════════════════════
    #  METHODE 1: Initiale Vorstellungs-E-Mail (Claude Opus 4.6)
    # ══════════════════════════════════════════════════════════════

    async def generate_presentation_email(self, match_id: UUID) -> dict:
        """Generiert eine professionelle Vorstellungs-E-Mail mit Claude Opus 4.6.

        Laedt Match mit ALLEN Kandidaten- und Job-Daten, erstellt einen
        Taetigkeitsabgleich-Prompt und laesst Opus die E-Mail generieren.

        Args:
            match_id: UUID des Matches

        Returns:
            {"subject": "...", "body_text": "...", "signature_html": "..."}
            oder {"error": "..."} bei Fehlern
        """
        # 1. Match mit allen Relations laden
        match = await self._load_match_with_relations(match_id)
        if not match:
            logger.error(f"Match {match_id} nicht gefunden")
            return {"error": f"Match {match_id} nicht gefunden"}

        job = match.job
        candidate = match.candidate
        if not job or not candidate:
            logger.error(f"Match {match_id}: Job oder Kandidat fehlt")
            return {"error": "Job oder Kandidat nicht gefunden"}

        # Company laden (ueber Job)
        company = None
        if job.company_id:
            company = await self.db.get(Company, job.company_id)

        # Contact laden (erster Ansprechpartner der Company, falls vorhanden)
        contact = None
        if company:
            contact_result = await self.db.execute(
                select(CompanyContact)
                .where(CompanyContact.company_id == company.id)
                .limit(1)
            )
            contact = contact_result.scalar_one_or_none()

        # 2. ALLE Daten als Dicts extrahieren (DB-Session schliessen vor API-Call!)
        company_name = company.name if company else job.company_name or "Unternehmen"
        company_city = (company.city if company else None) or job.city or ""

        contact_salutation = ""
        contact_name = ""
        contact_last_name = ""
        if contact:
            contact_salutation = contact.salutation or ""
            contact_name = contact.full_name or ""
            contact_last_name = contact.last_name or ""

        job_title = job.position or "Fachkraft"
        job_city = getattr(job, "display_city", "") or job.city or ""
        job_text = job.job_text or ""
        job_tasks = job.job_tasks or ""
        job_industry = job.industry or ""
        job_classification = job.classification_data or {}

        # Kandidaten-Daten (ANONYMISIERT — KEIN Name, Email, Telefon, Adresse!)
        candidate_ref = self._build_candidate_reference(candidate)
        candidate_position = candidate.current_position or "Fachkraft im Rechnungswesen"
        candidate_city = candidate.city or "nicht angegeben"
        candidate_erp = ", ".join(candidate.erp) if candidate.erp else ""
        candidate_it_skills = ", ".join(candidate.it_skills) if candidate.it_skills else ""
        candidate_skills = ", ".join(candidate.skills) if candidate.skills else ""
        years_experience = candidate.v2_years_experience or ""

        # Werdegang (work_history als strukturierter Text)
        work_history_text = self._format_work_history(candidate.work_history)

        # Ausbildung
        education_text = self._format_education(candidate.education)

        # Qualifizierungsgespraech-Daten
        call_summary = candidate.call_summary or ""
        call_transcript = candidate.call_transcript or ""
        key_activities = candidate.key_activities or ""
        change_motivation = candidate.change_motivation or ""

        # Sprachen
        languages_text = self._format_languages(candidate.languages)

        # Zertifizierungen
        certifications = candidate.v2_certifications or []
        certifications_text = ", ".join(certifications) if isinstance(certifications, list) else str(certifications)

        # Rahmendaten
        salary = candidate.salary or ""
        notice_period = candidate.notice_period or ""
        home_office_days = candidate.home_office_days or ""
        employment_type = candidate.employment_type or ""

        # Pendelzeit-Praeferenz (Fallback wenn keine Google Maps Fahrzeit)
        commute_max = candidate.commute_max or ""
        commute_transport = candidate.commute_transport or ""

        # Fahrzeit
        drive_time_car = match.drive_time_car_min
        drive_time_transit = match.drive_time_transit_min

        # Match-KI-Daten
        match_strengths = self._extract_strengths(match)
        match_explanation = match.ai_explanation or ""

        # Klassifizierung
        candidate_classification = candidate.classification_data or {}
        primary_role = candidate_classification.get("primary_role", "")

        # 3. Claude Prompt erstellen (Anrede: NUR Nachname, z.B. "Hallo Frau Timm")
        prompt = self._build_presentation_prompt(
            company_name=company_name,
            company_city=company_city,
            contact_salutation=contact_salutation,
            contact_name=contact_last_name or contact_name,
            job_title=job_title,
            job_city=job_city,
            job_text=job_text,
            job_tasks=job_tasks,
            job_industry=job_industry,
            candidate_ref=candidate_ref,
            candidate_position=candidate_position,
            candidate_city=candidate_city,
            candidate_erp=candidate_erp,
            candidate_it_skills=candidate_it_skills,
            candidate_skills=candidate_skills,
            years_experience=years_experience,
            work_history_text=work_history_text,
            education_text=education_text,
            call_summary=call_summary,
            key_activities=key_activities,
            change_motivation=change_motivation,
            languages_text=languages_text,
            certifications_text=certifications_text,
            salary=salary,
            notice_period=notice_period,
            home_office_days=home_office_days,
            employment_type=employment_type,
            drive_time_car=drive_time_car,
            drive_time_transit=drive_time_transit,
            commute_max=commute_max,
            commute_transport=commute_transport,
            match_strengths=match_strengths,
            match_explanation=match_explanation,
            primary_role=primary_role,
        )

        # 4. Claude Opus API Call
        try:
            result = await self._call_claude(prompt)
        except Exception as e:
            logger.error(f"Claude Opus API Fehler bei Match {match_id}: {e}")
            # Fallback: Manuell generierte E-Mail
            result = self._fallback_presentation_email(
                company_name=company_name,
                company_city=company_city,
                contact_salutation=contact_salutation,
                contact_name=contact_last_name or contact_name,
                job_title=job_title,
                job_city=job_city,
                candidate_ref=candidate_ref,
                candidate_position=candidate_position,
                candidate_city=candidate_city,
                drive_time_car=drive_time_car,
                drive_time_transit=drive_time_transit,
                candidate_erp=candidate_erp,
                years_experience=years_experience,
            )

        # Signatur hinzufuegen
        result["signature_html"] = self.get_html_signature()

        logger.info(
            f"Vorstellungs-E-Mail generiert fuer Match {match_id}: "
            f"Kandidat {candidate_ref} -> {company_name} ({job_title})"
        )
        return result

    # ══════════════════════════════════════════════════════════════
    #  METHODE 2: Follow-Up E-Mails
    # ══════════════════════════════════════════════════════════════

    async def generate_followup_email(self, presentation_id: UUID, step: int) -> dict:
        """Generiert Follow-Up E-Mails fuer eine bestehende Vorstellung.

        Args:
            presentation_id: UUID der ClientPresentation
            step: Follow-Up-Schritt (2 = erste Erinnerung nach 2 Tagen,
                                      3 = letzte Erinnerung nach 3 weiteren Tagen)

        Returns:
            {"subject": "...", "body_text": "...", "signature_html": "..."}
            oder {"error": "..."} bei Fehlern
        """
        # Presentation laden
        presentation = await self.db.get(ClientPresentation, presentation_id)
        if not presentation:
            logger.error(f"Presentation {presentation_id} nicht gefunden")
            return {"error": f"Presentation {presentation_id} nicht gefunden"}

        # Original-Daten fuer Kontext
        original_subject = presentation.email_subject or ""
        original_body = presentation.email_body_text or ""

        # Job und Company laden fuer Kontext
        job = None
        company_name = "Unternehmen"
        job_title = "die Position"
        if presentation.job_id:
            job = await self.db.get(Job, presentation.job_id)
            if job:
                job_title = job.position or "die Position"
                company_name = job.company_name or "Unternehmen"
        if presentation.company_id:
            company = await self.db.get(Company, presentation.company_id)
            if company:
                company_name = company.name

        # Contact laden
        contact_anrede = "Guten Tag"
        if presentation.contact_id:
            contact = await self.db.get(CompanyContact, presentation.contact_id)
            if contact:
                if contact.salutation and contact.last_name:
                    contact_anrede = f"Sehr geehrte{self._salutation_suffix(contact.salutation)} {contact.salutation} {contact.last_name}"
                elif contact.full_name and contact.full_name != "Unbekannt":
                    contact_anrede = f"Guten Tag {contact.full_name}"

        # Prompt fuer Follow-Up
        if step == 2:
            prompt = self._build_followup_prompt_step2(
                contact_anrede=contact_anrede,
                company_name=company_name,
                job_title=job_title,
                original_subject=original_subject,
            )
        elif step == 3:
            prompt = self._build_followup_prompt_step3(
                contact_anrede=contact_anrede,
                company_name=company_name,
                job_title=job_title,
                original_subject=original_subject,
            )
        else:
            return {"error": f"Ungueltiger Follow-Up Step: {step} (erlaubt: 2, 3)"}

        # Claude API Call
        try:
            result = await self._call_claude(prompt)
        except Exception as e:
            logger.error(f"Claude API Fehler bei Follow-Up (Presentation {presentation_id}, Step {step}): {e}")
            result = self._fallback_followup_email(
                step=step,
                contact_anrede=contact_anrede,
                company_name=company_name,
                job_title=job_title,
                original_subject=original_subject,
            )

        result["signature_html"] = self.get_html_signature()

        logger.info(
            f"Follow-Up E-Mail (Step {step}) generiert fuer Presentation {presentation_id}"
        )
        return result

    # ══════════════════════════════════════════════════════════════
    #  METHODE 3: Fallback-E-Mail-Kaskade
    # ══════════════════════════════════════════════════════════════

    def generate_fallback_emails(self, company_domain: str) -> list[str]:
        """Generiert Fallback-E-Mail-Adressen fuer eine Unternehmens-Domain.

        Wenn kein direkter Ansprechpartner bekannt ist, werden generische
        HR-Adressen in absteigender Wahrscheinlichkeit generiert.

        Args:
            company_domain: Domain des Unternehmens (z.B. "allianz.de")

        Returns:
            Liste von E-Mail-Adressen, z.B.:
            ["bewerber@allianz.de", "karriere@allianz.de", "hr@allianz.de", ...]
        """
        if not company_domain:
            logger.warning("generate_fallback_emails: Leere Domain uebergeben")
            return []

        # Domain bereinigen (kein http://, kein www.)
        domain = company_domain.strip().lower()
        domain = re.sub(r"^https?://", "", domain)
        domain = re.sub(r"^www\.", "", domain)
        domain = domain.split("/")[0]  # Pfade entfernen

        if not domain or "." not in domain:
            logger.warning(f"generate_fallback_emails: Ungueltige Domain '{company_domain}'")
            return []

        fallback_emails = [f"{prefix}@{domain}" for prefix in FALLBACK_PREFIXES]

        logger.debug(
            f"Fallback-Kaskade fuer {domain}: {len(fallback_emails)} Adressen generiert"
        )
        return fallback_emails

    # ══════════════════════════════════════════════════════════════
    #  METHODE 4: HTML-Signatur
    # ══════════════════════════════════════════════════════════════

    def get_html_signature(self) -> str:
        """Gibt die Sincirus HTML-E-Mail-Signatur zurueck.

        Identisch mit der Signatur in email_service.py / outreach_service.py /
        email_preparation_service.py — zentrale Quelle fuer alle E-Mail-Services.

        Returns:
            HTML-String der Signatur inkl. Foto, Kontaktdaten, Social Links,
            Vertraulichkeitshinweis.
        """
        return f"""
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
  <tr><td colspan="2" style="padding-bottom:14px;"></td></tr>
  <tr>
    <td colspan="2" style="border-top:1px solid #eee; padding-top:12px;">
      <table border="0" cellpadding="0" cellspacing="0" style="border-collapse:collapse; width:100%;">
        <tr>
          <td style="vertical-align:middle;">
            <table border="0" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr>
                <td style="vertical-align:middle; padding-right:10px;">
                  <a href="https://sincirus.com/" style="text-decoration:none;">
                    <img src="{_BASE_URL}/static/images/sincirus_icon.png" alt="Sincirus" height="32" style="display:block;">
                  </a>
                </td>
                <td style="vertical-align:middle;">
                  <a href="https://sincirus.com/" style="text-decoration:none; font-family:Arial,Helvetica,sans-serif; font-size:18px; font-weight:bold; color:#6b7280; letter-spacing:0.5px;">Sincirus</a>
                </td>
              </tr>
            </table>
          </td>
          <td style="vertical-align:middle; text-align:right;">
            <a href="https://www.linkedin.com/in/milad-hamdard" style="text-decoration:none; display:inline-block; vertical-align:middle; margin-right:6px;">
              <img src="{_BASE_URL}/static/images/linkedin_icon.png" alt="LinkedIn" width="20" height="20" style="display:inline-block; vertical-align:middle;">
            </a>
            <a href="https://www.xing.com/profile/Milad_Hamdard2" style="text-decoration:none; display:inline-block; vertical-align:middle;">
              <img src="{_BASE_URL}/static/images/xing_icon.png" alt="XING" width="20" height="20" style="display:inline-block; vertical-align:middle;">
            </a>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
<table border="0" cellpadding="0" cellspacing="0" style="border-collapse:collapse; max-width:472px; width:100%; margin-top:12px; border-top:1px solid #eee;">
  <tr>
    <td style="padding-top:8px; font-family:Arial,Helvetica,sans-serif; font-size:7.5px; line-height:10px; color:#9b9a98; text-align:justify;">
      Der Inhalt dieser E-Mail ist vertraulich und ausschlie&szlig;lich f&uuml;r die Empf&auml;nger innerhalb des Unternehmens und der Unternehmensgruppe bestimmt. Ohne die ausdr&uuml;ckliche schriftliche Zustimmung des Absenders ist es strengstens untersagt, den Inhalt dieser Nachricht ganz oder teilweise an Personen oder Organisationen au&szlig;erhalb des Unternehmens oder der Unternehmensgruppe weiterzugeben oder zug&auml;nglich zu machen. Unternehmen innerhalb der Unternehmensgruppe, wie beispielsweise Tochtergesellschaften oder Holdinggesellschaften, gelten nicht als Dritte im Sinne dieser Bestimmung. Innerhalb des Unternehmens und der Unternehmensgruppe ist die Weiterleitung dieser E-Mail an verschiedene Abteilungen gestattet; eine Weitergabe an externe Unternehmen ist jedoch unter keinen Umst&auml;nden zul&auml;ssig.
    </td>
  </tr>
  <tr>
    <td style="padding-top:4px; font-family:Arial,Helvetica,sans-serif; font-size:7.5px; line-height:10px; color:#9b9a98; text-align:justify;">
      Sollten Sie diese Nachricht irrt&uuml;mlich erhalten haben, benachrichtigen Sie bitte unverz&uuml;glich den Absender, indem Sie auf diese E-Mail antworten, und l&ouml;schen Sie die Nachricht anschlie&szlig;end, um sicherzustellen, dass ein solcher Fehler in Zukunft vermieden wird.
    </td>
  </tr>
</table>
"""

    # ══════════════════════════════════════════════════════════════
    #  PRIVATE: Claude API Aufrufe
    # ══════════════════════════════════════════════════════════════

    async def _call_claude(self, prompt: str) -> dict:
        """Ruft Claude Opus 4.6 auf und parst die JSON-Antwort.

        Nutzt den dedizierten Opus API Key (ANTHROPIC_OPUS_API_KEY),
        Fallback auf den Standard-Key (ANTHROPIC_API_KEY).

        Args:
            prompt: Der vollstaendige Prompt fuer Claude

        Returns:
            {"subject": "...", "body_text": "..."}

        Raises:
            ValueError: Wenn die Antwort kein gueltiges JSON enthaelt
            Exception: Bei API-Fehlern
        """
        api_key = settings.anthropic_opus_api_key or settings.anthropic_api_key
        if not api_key:
            raise ValueError("Anthropic API Key nicht konfiguriert (ANTHROPIC_OPUS_API_KEY oder ANTHROPIC_API_KEY)")

        client = AsyncAnthropic(api_key=api_key)

        response = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text.strip()

        # JSON aus der Antwort extrahieren (Claude gibt manchmal Markdown-Codeblocks zurueck)
        parsed = self._parse_json_response(raw_text)
        if not parsed:
            raise ValueError(f"Claude-Antwort enthaelt kein gueltiges JSON: {raw_text[:200]}")

        # Validierung der Pflichtfelder
        if "subject" not in parsed or "body_text" not in parsed:
            raise ValueError(
                f"Claude-Antwort fehlen Pflichtfelder (subject/body_text): {list(parsed.keys())}"
            )

        return {
            "subject": parsed["subject"].strip(),
            "body_text": parsed["body_text"].strip(),
        }

    def _parse_json_response(self, raw_text: str) -> dict | None:
        """Parst JSON aus Claude-Antwort, auch wenn es in Markdown-Codeblocks steht.

        Args:
            raw_text: Rohe Antwort von Claude

        Returns:
            Geparstes dict oder None bei Fehler
        """
        # Versuch 1: Direkt als JSON parsen
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            pass

        # Versuch 2: JSON aus Markdown-Codeblock extrahieren (```json ... ```)
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw_text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # Versuch 3: Erstes {...} finden
        brace_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        logger.error(f"Konnte JSON nicht aus Claude-Antwort extrahieren: {raw_text[:300]}")
        return None

    # ══════════════════════════════════════════════════════════════
    #  PRIVATE: Prompt-Builder
    # ══════════════════════════════════════════════════════════════

    def _build_presentation_prompt(
        self,
        company_name: str,
        company_city: str,
        contact_salutation: str,
        contact_name: str,
        job_title: str,
        job_city: str,
        job_text: str,
        job_tasks: str,
        job_industry: str,
        candidate_ref: str,
        candidate_position: str,
        candidate_city: str,
        candidate_erp: str,
        candidate_it_skills: str,
        candidate_skills: str,
        years_experience: int | str,
        work_history_text: str,
        education_text: str,
        call_summary: str,
        key_activities: str,
        change_motivation: str,
        languages_text: str,
        certifications_text: str,
        salary: str,
        notice_period: str,
        home_office_days: str,
        employment_type: str,
        drive_time_car: int | None,
        drive_time_transit: int | None,
        commute_max: str = "",
        commute_transport: str = "",
        match_strengths: str = "",
        match_explanation: str = "",
        primary_role: str = "",
    ) -> str:
        """Erstellt den Opus-optimierten Taetigkeitsabgleich-Prompt."""

        # Anrede bestimmen
        if contact_salutation and contact_name and contact_name != "Unbekannt":
            anrede = f"Sehr geehrte{'r' if contact_salutation.strip().lower() in ('herr', 'mr', 'm') else ''} {contact_salutation} {contact_name}"
        else:
            anrede = "Guten Tag"

        # Fahrzeit-Info (Google Maps exakt, Fallback: Pendelzeit-Praeferenz)
        fahrzeit_parts = []
        if isinstance(drive_time_car, int):
            fahrzeit_parts.append(f"ca. {drive_time_car} Min. mit dem Auto")
        if isinstance(drive_time_transit, int):
            fahrzeit_parts.append(f"ca. {drive_time_transit} Min. mit oeffentlichen Verkehrsmitteln")
        if fahrzeit_parts:
            fahrzeit_text = ", ".join(fahrzeit_parts)
        elif commute_max or commute_transport:
            # Fallback auf Kandidaten-Pendelzeit
            pendel_parts = []
            if commute_max:
                pendel_parts.append(f"max. {commute_max}")
            if commute_transport:
                pendel_parts.append(f"mit {commute_transport}")
            fahrzeit_text = f"Pendelbereitschaft: {' '.join(pendel_parts)}"
        else:
            fahrzeit_text = "nicht verfuegbar"

        # Job-Anforderungen aus job_text extrahieren (die ersten 3000 Zeichen)
        job_desc = job_text[:3000] if job_text else ""
        if job_tasks:
            job_desc = f"AUFGABEN:\n{job_tasks}\n\nVOLLSTAENDIGE STELLENANZEIGE:\n{job_desc}"

        # Kandidaten-Daten zusammenbauen
        kandidat_daten = f"AKTUELLE POSITION: {candidate_position}"
        if work_history_text:
            kandidat_daten += f"\n\nWERDEGANG:\n{work_history_text}"
        if key_activities:
            kandidat_daten += f"\n\nKERNTAETIGKEITEN (vom Kandidaten bestaetigt):\n{key_activities}"
        if call_summary:
            kandidat_daten += f"\n\nGESPRAECHSZUSAMMENFASSUNG:\n{call_summary}"
        if candidate_erp:
            kandidat_daten += f"\n\nERP-SYSTEME: {candidate_erp}"
        if candidate_it_skills:
            kandidat_daten += f"\nIT-KENNTNISSE: {candidate_it_skills}"
        if candidate_skills:
            kandidat_daten += f"\nWEITERE SKILLS: {candidate_skills}"
        if education_text:
            kandidat_daten += f"\n\nAUSBILDUNG:\n{education_text}"
        if certifications_text:
            kandidat_daten += f"\nZERTIFIZIERUNGEN: {certifications_text}"
        if languages_text:
            kandidat_daten += f"\nSPRACHEN: {languages_text}"
        if years_experience:
            kandidat_daten += f"\nBERUFSERFAHRUNG: {years_experience} Jahre"
        if primary_role:
            kandidat_daten += f"\nPRIMAERE ROLLE: {primary_role}"

        # Rahmendaten als Aufzaehlungsliste
        rahmendaten_parts = []
        rahmendaten_parts.append(f"Fahrweg: {candidate_city} → {job_city} — {fahrzeit_text}")
        if notice_period:
            rahmendaten_parts.append(f"Verfuegbarkeit: {notice_period}")
        if salary:
            rahmendaten_parts.append(f"Gehalt: {salary}")
        if home_office_days:
            rahmendaten_parts.append(f"Home-Office: {home_office_days}")
        if employment_type:
            rahmendaten_parts.append(f"Arbeitszeit: {employment_type}")
        rahmendaten_text = "\n".join(f"• {r}" for r in rahmendaten_parts)

        prompt = f"""Du bist Milad Hamdard, Senior Personalberater bei Sincirus in Hamburg. Du schreibst eine Vorstellungs-E-Mail an ein Unternehmen, um einen Kandidaten fuer eine offene Stelle vorzuschlagen.

═══ STELLENANZEIGE ═══
Unternehmen: {company_name} ({company_city})
Position: {job_title}
Branche: {job_industry}

{job_desc}

═══ KANDIDAT (Referenz: {candidate_ref}) ═══
{kandidat_daten}

═══ RAHMENDATEN ═══
{rahmendaten_text}

═══ KI-MATCHING-ERGEBNIS ═══
Staerken: {match_strengths}
Begruendung: {match_explanation}

═══ DEINE AUFGABE ═══

Schreibe eine vertrieblich starke Vorstellungs-E-Mail. Du MUSST einen TAETIGKEITSABGLEICH machen:

Gehe JEDE wichtige Anforderung aus der Stellenanzeige einzeln durch und zeige, was der Kandidat KONKRET dafuer mitbringt. Belege es mit Fakten aus dem Werdegang, den Kerntaetigkeiten oder der Gespraechszusammenfassung. Schreibe KEINE leeren Worthuelsen.

═══ AUFBAU DER E-MAIL ═══

1. ANREDE: "{anrede}"
2. EINLEITUNG: "Ich hoffe, es geht Ihnen gut." Dann 1-2 Saetze: Wer du bist, welche Position, dass du einen passenden Kandidaten hast.
3. TAETIGKEITSABGLEICH: Bullet-Points (•). Pro Anforderung aus der Stelle:
   • [Anforderung]: [Was der Kandidat konkret mitbringt, mit Beleg aus Werdegang/Gespraech]
   Beispiel: "• Monats- und Jahresabschluesse: Ihr Kandidat erstellt seit 4 Jahren eigenstaendig Monats- und Jahresabschluesse nach HGB bei [Firma aus Werdegang]"
4. IT/ERP-ABGLEICH (wenn relevant): Welche Systeme der Kandidat beherrscht vs. was gefordert ist.
5. RAHMENDATEN als Aufzaehlungspunkte:
{rahmendaten_text}
6. ABSCHLUSS: "Bei Interesse lasse ich Ihnen gerne die vollstaendigen Unterlagen zukommen. Ich freue mich auf Ihre Rueckmeldung."
7. KEIN Gruss am Ende (kommt automatisch in der Signatur).

═══ STIL-REGELN ═══
- IMMER Ich-Form ("Ich betreue...", "Ich erkenne..."), NIEMALS Wir-Form
- Sie-Form gegenueber dem Empfaenger
- Klartext (KEIN HTML, keine Markdown-Formatierung, nur • fuer Aufzaehlungen)
- KEIN Kandidatenname — nur "Ihr Kandidat" oder "mein Kandidat" oder Referenz {candidate_ref}
- KEINE Floskeln wie "bringt mit", "theoretische Fundierung", "systematische Herangehensweise"
- KEINE Schulnoten-Sprache ("gut", "sehr gut", "befriedigend")
- Schreibe wie ein erfahrener Recruiter der den Kandidaten persoenlich kennt
- Nenne KONKRETE Fakten: Firmennamen aus dem Werdegang, Jahre, Systeme, Taetigkeiten
- Die E-Mail soll den Empfaenger UEBERZEUGEN, nicht informieren

═══ AUSGABEFORMAT ═══
Antworte NUR mit diesem JSON (keine Erklaerung, kein Markdown-Codeblock):
{{"subject": "Kandidatenvorstellung: {candidate_ref} — {job_title} ({company_city})", "body_text": "..."}}"""

        return prompt

    def _build_followup_prompt_step2(
        self,
        contact_anrede: str,
        company_name: str,
        job_title: str,
        original_subject: str,
    ) -> str:
        """Prompt fuer die erste Erinnerung (Step 2, nach 2 Tagen)."""
        return f"""Du bist ein erfahrener Personalberater bei Sincirus in Hamburg.
Schreibe eine hoefliche Erinnerungs-E-Mail zu einer Kandidaten-Vorstellung.

Kontext:
- Die originale Vorstellungs-E-Mail wurde vor 2 Tagen gesendet
- Betreff der Original-E-Mail: {original_subject}
- Empfaenger-Anrede: {contact_anrede}
- Unternehmen: {company_name}
- Position: {job_title}

Regeln:
1. Schreibe in der Sie-Form
2. Klartext, KEIN HTML
3. Beziehe dich auf die bereits gesendete E-Mail
4. Hoeflich nachfragen ob die Vorstellung angekommen ist
5. Kurz erwaehnen dass der Kandidat weiterhin verfuegbar ist
6. Maximal 6-8 Zeilen Body
7. KEIN Gruss am Ende (kommt in der Signatur)
8. Ende mit "Ich freue mich auf Ihre Rueckmeldung"

Gib zurueck als JSON:
{{"subject": "...", "body_text": "..."}}"""

    def _build_followup_prompt_step3(
        self,
        contact_anrede: str,
        company_name: str,
        job_title: str,
        original_subject: str,
    ) -> str:
        """Prompt fuer die letzte Erinnerung (Step 3, nach 3 weiteren Tagen)."""
        return f"""Du bist ein erfahrener Personalberater bei Sincirus in Hamburg.
Schreibe eine letzte, kurze Erinnerungs-E-Mail zu einer Kandidaten-Vorstellung.

Kontext:
- Die originale Vorstellungs-E-Mail wurde vor 5 Tagen gesendet
- Eine erste Erinnerung wurde bereits vor 3 Tagen gesendet
- Betreff der Original-E-Mail: {original_subject}
- Empfaenger-Anrede: {contact_anrede}
- Unternehmen: {company_name}
- Position: {job_title}

Regeln:
1. Schreibe in der Sie-Form
2. Klartext, KEIN HTML
3. Kurz und respektvoll — dies ist die letzte Nachfrage
4. Erwaehne dass der Kandidat auch anderen Unternehmen vorgestellt werden koennte
5. Maximal 4-5 Zeilen Body
6. KEIN Gruss am Ende (kommt in der Signatur)
7. Ende mit einer freundlichen Schlussformel

Gib zurueck als JSON:
{{"subject": "...", "body_text": "..."}}"""

    # ══════════════════════════════════════════════════════════════
    #  PRIVATE: Fallback-E-Mail-Generierung (ohne KI)
    # ══════════════════════════════════════════════════════════════

    def _fallback_presentation_email(
        self,
        company_name: str,
        company_city: str,
        contact_salutation: str,
        contact_name: str,
        job_title: str,
        job_city: str,
        candidate_ref: str,
        candidate_position: str,
        candidate_city: str,
        drive_time_car: int | str,
        drive_time_transit: int | str,
        candidate_erp: str,
        years_experience: int | str,
    ) -> dict:
        """Generiert eine Vorstellungs-E-Mail ohne Claude (Fallback).

        Wird verwendet wenn die Anthropic API nicht erreichbar ist
        oder der API Key nicht konfiguriert ist.
        """
        # Anrede
        if contact_salutation and contact_name and contact_name != "Unbekannt":
            suffix = self._salutation_suffix(contact_salutation)
            anrede = f"Sehr geehrte{suffix} {contact_salutation} {contact_name}"
        else:
            anrede = "Guten Tag"

        # Fahrzeitinfo
        drive_info = ""
        if isinstance(drive_time_car, int):
            drive_info = f"   Standort:     {candidate_city} -> {job_city} ({drive_time_car} Min Auto)"
        else:
            drive_info = f"   Standort:     {candidate_city} -> {job_city}"

        subject = f"Kandidatenvorstellung: {candidate_ref} fuer {job_title} - Sincirus"

        body_text = (
            f"{anrede},\n\n"
            f"im Rahmen unserer Personalberatung moechte ich Ihnen einen qualifizierten "
            f"Kandidaten fuer Ihre ausgeschriebene Position vorstellen.\n\n"
            f"Gegenuberstellung Ihrer Anforderungen und dem Kandidatenprofil:\n\n"
            f"   Position:     {job_title}\n"
            f"   Kandidat:     {candidate_position}\n"
            f"{drive_info}\n"
            f"   ERP:          {candidate_erp}\n"
            f"   Erfahrung:    {years_experience} Jahre\n"
            f"   Referenz:     {candidate_ref}\n\n"
            f"Anbei finden Sie das anonymisierte Kandidatenprofil als PDF.\n\n"
            f"Ich freue mich auf Ihre Rueckmeldung."
        )

        return {"subject": subject, "body_text": body_text}

    def _fallback_followup_email(
        self,
        step: int,
        contact_anrede: str,
        company_name: str,
        job_title: str,
        original_subject: str,
    ) -> dict:
        """Generiert eine Follow-Up-E-Mail ohne Claude (Fallback)."""

        if step == 2:
            subject = f"Nachfrage: {original_subject}"
            body_text = (
                f"{contact_anrede},\n\n"
                f"ich wollte hoeflich nachfragen, ob Sie meine Kandidatenvorstellung "
                f"fuer die Position als {job_title} erhalten haben.\n\n"
                f"Der Kandidat ist weiterhin verfuegbar und an der Position bei "
                f"{company_name} interessiert. Gerne stehe ich fuer Rueckfragen "
                f"oder ein kurzes Telefonat zur Verfuegung.\n\n"
                f"Ich freue mich auf Ihre Rueckmeldung."
            )
        else:
            subject = f"Letzte Nachfrage: {original_subject}"
            body_text = (
                f"{contact_anrede},\n\n"
                f"ich moechte mich ein letztes Mal bezueglich meiner Kandidatenvorstellung "
                f"fuer die Position als {job_title} bei Ihnen melden.\n\n"
                f"Sollte ich bis Ende der Woche keine Rueckmeldung erhalten, gehe ich "
                f"davon aus, dass aktuell kein Interesse besteht, und werde den Kandidaten "
                f"anderen Unternehmen vorstellen.\n\n"
                f"Fuer Rueckfragen stehe ich Ihnen jederzeit zur Verfuegung."
            )

        return {"subject": subject, "body_text": body_text}

    # ══════════════════════════════════════════════════════════════
    #  PRIVATE: Hilfsfunktionen
    # ══════════════════════════════════════════════════════════════

    def _format_work_history(self, work_history: dict | list | None) -> str:
        """Formatiert work_history als lesbaren Text (KEINE persoenlichen Daten!)."""
        if not work_history:
            return ""
        entries = work_history if isinstance(work_history, list) else [work_history]
        parts = []
        for entry in entries[:8]:  # Max 8 Eintraege
            if isinstance(entry, dict):
                company = entry.get("company", entry.get("firma", ""))
                position = entry.get("position", entry.get("titel", ""))
                period = entry.get("period", entry.get("zeitraum", ""))
                tasks = entry.get("tasks", entry.get("aufgaben", entry.get("beschreibung", "")))
                line = f"- {position}"
                if company:
                    line += f" bei {company}"
                if period:
                    line += f" ({period})"
                if tasks:
                    if isinstance(tasks, list):
                        tasks = "; ".join(tasks[:5])
                    line += f"\n  Taetigkeiten: {tasks[:300]}"
                parts.append(line)
            elif isinstance(entry, str):
                parts.append(f"- {entry[:200]}")
        return "\n".join(parts)

    def _format_education(self, education: dict | list | None) -> str:
        """Formatiert Ausbildung als lesbaren Text."""
        if not education:
            return ""
        entries = education if isinstance(education, list) else [education]
        parts = []
        for entry in entries[:5]:
            if isinstance(entry, dict):
                degree = entry.get("degree", entry.get("abschluss", ""))
                school = entry.get("school", entry.get("institution", entry.get("schule", "")))
                field = entry.get("field", entry.get("fachrichtung", ""))
                line = f"- {degree}"
                if field:
                    line += f" in {field}"
                if school:
                    line += f" ({school})"
                parts.append(line)
            elif isinstance(entry, str):
                parts.append(f"- {entry[:200]}")
        return "\n".join(parts)

    def _format_languages(self, languages: dict | list | None) -> str:
        """Formatiert Sprachen als Text."""
        if not languages:
            return ""
        if isinstance(languages, list):
            parts = []
            for lang in languages:
                if isinstance(lang, dict):
                    name = lang.get("language", lang.get("sprache", ""))
                    level = lang.get("level", lang.get("niveau", ""))
                    parts.append(f"{name} ({level})" if level else name)
                elif isinstance(lang, str):
                    parts.append(lang)
            return ", ".join(parts)
        if isinstance(languages, dict):
            return ", ".join(f"{k}: {v}" for k, v in languages.items())
        return str(languages)

    async def _load_match_with_relations(self, match_id: UUID) -> Match | None:
        """Laedt einen Match mit Job und Kandidat (eager loading)."""
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

    def _build_candidate_reference(self, candidate: Candidate) -> str:
        """Baut die anonymisierte Kandidaten-Referenznummer.

        Format: SP-2026-XXXX wobei XXXX die candidate_number ist.
        Fallback: SP-2026-0000 wenn keine Nummer vorhanden.
        """
        number = candidate.candidate_number if candidate.candidate_number else 0
        return f"SP-2026-{number:04d}"

    def _extract_strengths(self, match: Match) -> str:
        """Extrahiert die Match-Staerken als kommagetrennten String.

        Prueft ai_strengths (Array) und v2_score_breakdown (JSONB).
        """
        # Primaer: ai_strengths (Claude Matching v4)
        if match.ai_strengths:
            return ", ".join(match.ai_strengths[:5])

        # Sekundaer: v2_score_breakdown
        if match.v2_score_breakdown and isinstance(match.v2_score_breakdown, dict):
            parts = []
            breakdown = match.v2_score_breakdown
            if breakdown.get("staerken"):
                if isinstance(breakdown["staerken"], list):
                    return ", ".join(breakdown["staerken"][:5])
            # Fallback: Score-Komponenten als Staerken beschreiben
            for key, value in breakdown.items():
                if isinstance(value, (int, float)) and value > 70:
                    parts.append(f"{key}: {value:.0f}%")
            if parts:
                return ", ".join(parts[:5])

        return "keine Staerken-Daten verfuegbar"

    @staticmethod
    def _salutation_suffix(salutation: str) -> str:
        """Gibt 'r' zurueck fuer 'Herr', sonst '' (fuer 'Frau').

        Fuer: 'Sehr geehrte[r] Herr/Frau ...'
        """
        if salutation and salutation.strip().lower() in ("herr", "mr", "m"):
            return "r"
        return ""
