"""EmailGeneratorService — KI-generierte Vorstellungs-E-Mails + Fallback-Kaskade.

Generiert professionelle Vorstellungs-E-Mails fuer Kandidaten an Unternehmen
mittels Claude Sonnet. Verwaltet die Follow-Up-Sequenz (2 Tage + 3 Tage)
und die Fallback-E-Mail-Kaskade (bewerber@, karriere@, hr@ etc.).

Ablauf:
1. generate_presentation_email() — Claude generiert initiale Vorstellungs-E-Mail
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
    #  METHODE 1: Initiale Vorstellungs-E-Mail (Claude Sonnet)
    # ══════════════════════════════════════════════════════════════

    async def generate_presentation_email(self, match_id: UUID) -> dict:
        """Generiert eine professionelle Vorstellungs-E-Mail mit Claude Sonnet.

        Laedt Match mit Job + Candidate + Company + Contact Daten,
        erstellt einen strukturierten Prompt und laesst Claude die E-Mail generieren.

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

        # 2. Prompt-Daten zusammenstellen
        company_name = company.name if company else job.company_name or "Unternehmen"
        company_city = (company.city if company else None) or job.city or ""

        contact_salutation = ""
        contact_name = ""
        if contact:
            contact_salutation = contact.salutation or ""
            contact_name = contact.full_name or ""

        job_title = job.position or "Fachkraft"
        job_city = getattr(job, "display_city", "") or job.city or ""

        # Kandidaten-Daten (ANONYMISIERT — kein Name!)
        candidate_ref = self._build_candidate_reference(candidate)
        candidate_position = candidate.current_position or "Fachkraft im Rechnungswesen"
        candidate_city = candidate.city or "nicht angegeben"

        # Fahrzeit
        drive_time_car = match.drive_time_car_min or "nicht berechnet"
        drive_time_transit = match.drive_time_transit_min or "nicht berechnet"

        # Match-Staerken aus v2_score_breakdown oder ai_strengths
        match_strengths = self._extract_strengths(match)
        match_explanation = match.ai_explanation or ""

        # ERP-Systeme
        candidate_erp = ", ".join(candidate.erp) if candidate.erp else "nicht angegeben"

        # Berufserfahrung
        years_experience = candidate.v2_years_experience or "nicht angegeben"

        # 3. Claude Prompt erstellen
        prompt = self._build_presentation_prompt(
            company_name=company_name,
            company_city=company_city,
            contact_salutation=contact_salutation,
            contact_name=contact_name,
            job_title=job_title,
            job_city=job_city,
            candidate_ref=candidate_ref,
            candidate_position=candidate_position,
            candidate_city=candidate_city,
            drive_time_car=drive_time_car,
            drive_time_transit=drive_time_transit,
            match_strengths=match_strengths,
            match_explanation=match_explanation,
            candidate_erp=candidate_erp,
            years_experience=years_experience,
        )

        # 4. Claude API Call
        try:
            result = await self._call_claude(prompt)
        except Exception as e:
            logger.error(f"Claude API Fehler bei Match {match_id}: {e}")
            # Fallback: Manuell generierte E-Mail
            result = self._fallback_presentation_email(
                company_name=company_name,
                company_city=company_city,
                contact_salutation=contact_salutation,
                contact_name=contact_name,
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
        """Ruft Claude Sonnet auf und parst die JSON-Antwort.

        Args:
            prompt: Der vollstaendige Prompt fuer Claude

        Returns:
            {"subject": "...", "body_text": "..."}

        Raises:
            ValueError: Wenn die Antwort kein gueltiges JSON enthaelt
            Exception: Bei API-Fehlern
        """
        if not settings.anthropic_api_key:
            raise ValueError("Anthropic API Key nicht konfiguriert (ANTHROPIC_API_KEY)")

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)

        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
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
        candidate_ref: str,
        candidate_position: str,
        candidate_city: str,
        drive_time_car: int | str,
        drive_time_transit: int | str,
        match_strengths: str,
        match_explanation: str,
        candidate_erp: str,
        years_experience: int | str,
    ) -> str:
        """Erstellt den Claude-Prompt fuer die Vorstellungs-E-Mail."""

        anrede_hinweis = ""
        if contact_salutation and contact_name and contact_name != "Unbekannt":
            anrede_hinweis = f"Ansprechpartner ist bekannt: {contact_salutation} {contact_name}. Beginne mit persoenlicher Anrede."
        else:
            anrede_hinweis = "Kein Ansprechpartner bekannt. Beginne mit 'Guten Tag'."

        return f"""Du bist ein erfahrener Personalberater bei Sincirus in Hamburg.
Schreibe eine professionelle Vorstellungs-E-Mail fuer einen Kandidaten.

Kontext:
- Unternehmen: {company_name} ({company_city})
- {anrede_hinweis}
- Position: {job_title}
- Kandidat-Referenz: {candidate_ref} (KEIN Name!)
- Aktuelle Position: {candidate_position}
- Stadt: {candidate_city}
- Fahrzeit: {drive_time_car} Min Auto / {drive_time_transit} Min Bahn
- Staerken: {match_strengths}
- KI-Begruendung: {match_explanation}
- ERP-Systeme: {candidate_erp}
- Berufserfahrung: {years_experience} Jahre

Regeln:
1. Schreibe in der Sie-Form
2. Klartext, KEIN HTML im Body
3. Verwende KEINEN Kandidaten-Namen (anonymisiert!)
4. Nutze die Referenznummer {candidate_ref}
5. Strukturiere den Vergleich als tabellenartige Gegenuberstellung:
   Position:     {job_title}
   Kandidat:     {candidate_position}
   Standort:     {candidate_city} -> {job_city} ({drive_time_car} Min)
   ERP:          {candidate_erp}
   Erfahrung:    {years_experience} Jahre
6. Halte die E-Mail kurz (max 15 Zeilen Body)
7. Ende mit "Ich freue mich auf Ihre Rueckmeldung"
8. KEIN Gruss am Ende (kommt in der Signatur)

Gib zurueck als JSON:
{{"subject": "...", "body_text": "..."}}"""

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
