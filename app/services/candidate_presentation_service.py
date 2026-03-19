"""CandidatePresentationService — Direkte Kandidaten-Vorstellung bei Unternehmen.

Automatisiert den Prozess der proaktiven Kandidaten-Vorstellung:
1. Job-Text analysieren (GPT-4o) → Strukturierte Daten extrahieren
2. Skills-Match berechnen (GPT-4o) → Qualitativer Vergleich (✓/○/✗)
3. E-Mail generieren (GPT-4o) → Plain-Text, ICH-Form, 3 Sequenz-Steps
4. Presentation erstellen → Company auto-create, Draft-Status
5. Spam-Check → Cooldown pro Firma+Rolle, Response-Check
6. Sequenzen stornieren → Bei Kandidat-Statusaenderung
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.candidate import Candidate
from app.models.client_presentation import ClientPresentation
from app.models.company import Company
from app.models.company_contact import CompanyContact
from app.models.company_correspondence import CompanyCorrespondence, CorrespondenceDirection

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Signatur (Plain-Text) ──
PLAIN_TEXT_SIGNATURE = """
Milad Hamdard
Senior Personalberater | Rechnungswesen & Controlling
040 238 345 320   |   +49 176 8000 47 41
hamdard@sincirus.com
www.sincirus.com
Ballindamm 3, 20095 Hamburg
""".strip()

# ── HTML Signatur ──
HTML_SIGNATURE = """
<p style="margin-top:24px;color:#555;font-size:13px;border-top:1px solid #ddd;padding-top:12px;">
Milad Hamdard<br>
Senior Personalberater | Rechnungswesen &amp; Controlling<br>
040 238 345 320 &nbsp;|&nbsp; +49 176 8000 47 41<br>
<a href="mailto:hamdard@sincirus.com" style="color:#2563eb;">hamdard@sincirus.com</a><br>
<a href="https://www.sincirus.com" style="color:#2563eb;">www.sincirus.com</a><br>
Ballindamm 3, 20095 Hamburg
</p>
""".strip()


def _build_skills_html_table(skills_comparison: dict) -> str:
    """Baut eine HTML-Tabelle aus den Skills-Vergleich-Daten."""
    matches = skills_comparison.get("matches", [])
    if not matches:
        return ""

    status_icons = {
        "erfuellt": ("&#10003;", "#16a34a", "#f0fdf4"),       # Gruen
        "teilweise": ("&#9675;", "#d97706", "#fffbeb"),        # Amber
        "nicht_vorhanden": ("&#10007;", "#dc2626", "#fef2f2"), # Rot
    }

    rows = ""
    for m in matches:
        req = m.get("requirement", "")
        evidence = m.get("candidate_evidence", "")
        status = m.get("status", "nicht_vorhanden")
        icon, color, bg = status_icons.get(status, status_icons["nicht_vorhanden"])

        rows += f"""<tr style="border-bottom:1px solid #e5e7eb;">
<td style="padding:8px 12px;font-size:13px;">{req}</td>
<td style="padding:8px 12px;font-size:13px;">{evidence}</td>
<td style="padding:8px 12px;text-align:center;font-size:16px;color:{color};background:{bg};font-weight:bold;">{icon}</td>
</tr>"""

    return f"""<table style="width:100%;border-collapse:collapse;border:1px solid #d1d5db;margin:16px 0;font-family:Arial,sans-serif;">
<thead>
<tr style="background:#f3f4f6;">
<th style="padding:10px 12px;text-align:left;font-size:13px;font-weight:600;border-bottom:2px solid #d1d5db;width:35%;">Anforderung</th>
<th style="padding:10px 12px;text-align:left;font-size:13px;font-weight:600;border-bottom:2px solid #d1d5db;width:50%;">Kandidat</th>
<th style="padding:10px 12px;text-align:center;font-size:13px;font-weight:600;border-bottom:2px solid #d1d5db;width:15%;">Bewertung</th>
</tr>
</thead>
<tbody>{rows}</tbody>
</table>"""


def _strip_opus_signature(text: str) -> str:
    """Entfernt von Opus generierte Grussformeln/Signaturen am Ende.

    Opus generiert oft 'Mit freundlichen Gruessen / Milad Hamdard / Sincirus...'
    Das muss raus, weil die echte Signatur automatisch angehaengt wird.
    """
    import re
    patterns = [
        r'\n*Mit freundlichen Gr[uü][sß]en\s*\n.*$',
        r'\n*Freundliche Gr[uü][sß]e\s*\n.*$',
        r'\n*Beste Gr[uü][sß]e\s*\n.*$',
        r'\n*Viele Gr[uü][sß]e\s*\n.*$',
        r'\n*Herzliche Gr[uü][sß]e\s*\n.*$',
        r'\n*Mit besten Gr[uü][sß]en\s*\n.*$',
    ]
    for pat in patterns:
        text = re.sub(pat, '', text, flags=re.DOTALL | re.IGNORECASE)
    return text.rstrip()


def _plaintext_to_html(text: str) -> str:
    """Konvertiert Plain-Text E-Mail in minimales HTML.

    Jede Textzeile wird ein eigener <p>-Absatz. Bullets werden zu <ul><li>.
    Abgleich-Ueberschriften werden fett. Leerzeilen erzeugen Abstand.
    """
    from html import escape

    text = escape(text)
    lines = text.split('\n')
    html_parts = []
    in_list = False
    in_abgleich = False

    for line in lines:
        s = line.strip()

        # Leerzeile = Abstand
        if s == '':
            if in_list:
                html_parts.append('</ul>')
                in_list = False
                in_abgleich = False
            continue

        # Abgleich-Ueberschrift erkennen
        if ('abgleich' in s.lower() or 'anforderungen' in s.lower()) and len(s) < 80:
            if in_list:
                html_parts.append('</ul>')
                in_list = False
            html_parts.append(f'<p style="margin-top:16px;margin-bottom:4px;"><b>{s}</b></p>')
            in_abgleich = True
            continue

        # Explizite Bullets (•, *, -)
        is_bullet = s.startswith('•') or s.startswith('* ') or (s.startswith('- ') and len(s) > 3)

        # Implizite Bullets: Im Abgleich-Block, Zeile hat "Label: Text" Format
        if not is_bullet and in_abgleich and ':' in s and len(s) > 20:
            colon_pos = s.index(':')
            before_colon = s[:colon_pos].strip()
            word_count = len(before_colon.split())
            if 1 <= word_count <= 8 and before_colon and not before_colon[0].isdigit():
                is_bullet = True

        if is_bullet:
            if not in_list:
                html_parts.append('<ul style="margin:8px 0;padding-left:24px;">')
                in_list = True
            bullet = s.lstrip('•*-').strip()
            if ':' in bullet:
                k, v = bullet.split(':', 1)
                bullet = f'<b>{k}</b>:{v}'
            html_parts.append(f'<li style="margin-bottom:8px;">{bullet}</li>')
        else:
            if in_list:
                html_parts.append('</ul>')
                in_list = False
                in_abgleich = False
            # Jede Textzeile = eigener Absatz (kein Buffering mehr)
            html_parts.append(f'<p style="margin:0 0 10px 0;">{s}</p>')

    if in_list:
        html_parts.append('</ul>')

    return '\n'.join(html_parts)


# ── Pydantic Models fuer GPT-Responses ──

class ExtractedJobData(BaseModel):
    """Strukturierte Daten aus einem Stellentext."""
    company_name: str = ""
    city: str = ""
    plz: str = ""
    address: str = ""
    domain: str = ""
    contact_name: str = ""        # VOLLSTAENDIGER Name (Vorname + Nachname)
    contact_firstname: str = ""   # Vorname (separat)
    contact_salutation: str = ""  # Herr / Frau / leer
    contact_email: str = ""
    contact_phone: str = ""       # Telefonnummer des Ansprechpartners
    job_title: str = ""
    requirements: list[str] = Field(default_factory=list)
    description_summary: str = ""


class SkillMatch(BaseModel):
    """Einzelner Skill-Vergleich."""
    requirement: str
    candidate_evidence: str
    status: str  # erfuellt / teilweise / nicht_vorhanden


class SkillsComparison(BaseModel):
    """Qualitativer Skills-Vergleich."""
    matches: list[SkillMatch] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    overall_assessment: str = ""


class CandidatePresentationService:
    """Service fuer direkte Kandidaten-Vorstellungen (ohne Match)."""

    # ═══════════════════════════════════════════════════════════════
    # 1. JOB-TEXT ANALYSIEREN (GPT-4o)
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def extract_job_data(job_posting_text: str) -> ExtractedJobData:
        """Extrahiert strukturierte Daten aus einem rohen Stellentext.

        GPT-4o analysiert den Text und extrahiert: Firma, Stadt, PLZ,
        Ansprechpartner, E-Mail, Jobtitel, Anforderungen.
        """
        prompt = """Du bist ein Recruiting-Analyst. Extrahiere aus dem folgenden Stellentext alle relevanten Informationen.

WICHTIG — ANSPRECHPARTNER FINDEN:
Der Ansprechpartner kann in VERSCHIEDENEN Formaten stehen:
- "Ansprechpartner: Max Mustermann" (klassisch)
- "Ihr Kontakt: Frau Mueller" (mit Label)
- "Vorname\\nMax\\nNachname\\nMustermann" (Zeile fuer Zeile, z.B. auf Jobportalen)
- Name + E-Mail nebeneinander: "Anna Schmidt a.schmidt@firma.de"
- Im Impressum oder Footer der Anzeige
Suche AKTIV nach Namen und E-Mail-Adressen im gesamten Text.

contact_salutation BESTIMMEN:
- Wenn "Herr" oder "Frau" im Text steht → uebernehmen
- Wenn NUR ein Vorname vorhanden ist → aus dem Vornamen ableiten:
  Weibliche Vornamen (Beispiele): Anna, Maria, Vera, Julia, Claudia, Sabine, Sandra, Nicole, Andrea, Christina, Daniela, Katharina, Stefanie, Petra, Monika, Susanne, Birgit, Lucia, Simone, Nadine, Anja, Elena, Sophie
  Maennliche Vornamen (Beispiele): Max, Thomas, Michael, Stefan, Andreas, Peter, Martin, Markus, Christian, Daniel, Tobias, Matthias, Alexander, Jan, Frank, Jens, Dirk, Marco, Lars, Ralf, Oliver, Sven, Florian
  Bei unklaren/internationalen Vornamen → "contact_salutation": "" leer lassen

Antworte NUR mit einem JSON-Objekt (kein Markdown, keine Erklaerung):
{
    "company_name": "Firmenname",
    "city": "Stadt",
    "plz": "PLZ (5-stellig)",
    "address": "Strasse + Hausnummer (wenn vorhanden)",
    "domain": "Website-Domain (ohne https://, wenn erkennbar)",
    "contact_name": "VOLLSTAENDIGER Name: Vorname + Nachname (z.B. 'Andreas Medicus', NICHT nur Nachname)",
    "contact_firstname": "NUR der Vorname (z.B. 'Andreas')",
    "contact_salutation": "Herr oder Frau (aus Text oder Vorname ableiten)",
    "contact_email": "E-Mail des Ansprechpartners (wenn vorhanden)",
    "contact_phone": "Telefonnummer des Ansprechpartners (wenn vorhanden)",
    "job_title": "Exakter Jobtitel",
    "requirements": ["Anforderung 1", "Anforderung 2", ...],
    "description_summary": "1-2 Saetze Zusammenfassung der Stelle"
}

Wenn ein Feld nicht im Text vorkommt, verwende einen leeren String "" bzw. leeres Array [].
Extrahiere ALLE genannten fachlichen Anforderungen/Qualifikationen als separate Eintraege."""

        try:
            result = await _call_opus(
                system_prompt=prompt,
                user_message=job_posting_text[:8000],  # Max 8k Zeichen
                max_tokens=1000,
            )
            data = _parse_json_safe(result)
            return ExtractedJobData(**data)
        except Exception as e:
            logger.error(f"extract_job_data fehlgeschlagen: {e}")
            return ExtractedJobData()

    # ═══════════════════════════════════════════════════════════════
    # 2. SKILLS-MATCH BERECHNEN (GPT-4o)
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def calculate_skills_match(
        candidate_data: dict,
        extracted_job_data: dict,
    ) -> SkillsComparison:
        """Qualitativer Skills-Vergleich: Kandidat vs. Job-Anforderungen.

        Args:
            candidate_data: Dict mit work_history, skills, classification_data etc.
                           (KEINE persoenlichen Daten! Nur Berufsdaten + candidate_id)
            extracted_job_data: Dict aus extract_job_data()
        """
        prompt = """Du bist ein erfahrener Personalberater im Finance-Bereich (FiBu, BiBu, LohnBu, StFA, KrediBu, DebiBu).

Vergleiche den Kandidaten mit den Anforderungen der Stelle. Bewerte jede Anforderung qualitativ:
- "erfuellt" (✓) — Kandidat erfuellt die Anforderung voll
- "teilweise" (○) — Kandidat hat aehnliche/verwandte Erfahrung
- "nicht_vorhanden" (✗) — Keine passende Erfahrung erkennbar

WICHTIGE REGELN:
- UEBERSPRINGE Anforderungen zu MS-Office/Word/Excel/PowerPoint/Outlook — das ist Standard und wird NICHT aufgelistet
- UEBERSPRINGE Anforderungen die nur Soft-Skills beschreiben ("teamfaehig", "strukturiert", "selbstaendig") — nicht bewertbar
- "candidate_evidence" MUSS KONKRET sein: mit Zahlen (Jahre, Anzahl, Volumen) oder spezifischer Taetigkeit. NIEMALS "langjährige Erfahrung" oder "fundierte Kenntnisse" schreiben
- KEIN Firmenname des Kandidaten in der Evidence — nur WAS er tut, nicht WO
- Maximal 5-6 Anforderungen bewerten (nur die fachlich relevanten)

Antworte NUR mit einem JSON-Objekt:
{
    "matches": [
        {"requirement": "...", "candidate_evidence": "...", "status": "erfuellt/teilweise/nicht_vorhanden"}
    ],
    "strengths": ["Staerke 1", "Staerke 2"],
    "gaps": ["Luecke 1 (wenn vorhanden)"],
    "overall_assessment": "1-2 Saetze Gesamtbewertung"
}"""

        # Optionale Felder nur einfuegen wenn vorhanden
        extra_sections = ""
        if candidate_data.get("call_transcript"):
            extra_sections += f"\nQualifizierungsgespraech: {candidate_data['call_transcript']}"
        elif candidate_data.get("key_activities"):
            extra_sections += f"\nKerntaetigkeiten: {candidate_data['key_activities']}"
        if candidate_data.get("desired_positions"):
            extra_sections += f"\nGewuenschte Positionen: {candidate_data['desired_positions']}"
        if candidate_data.get("change_motivation"):
            extra_sections += f"\nWechselmotivation: {candidate_data['change_motivation']}"
        if candidate_data.get("education"):
            extra_sections += f"\nAusbildung: {json.dumps(candidate_data['education'], ensure_ascii=False)[:500]}"
        if candidate_data.get("languages"):
            extra_sections += f"\nSprachen: {json.dumps(candidate_data['languages'], ensure_ascii=False)}"

        user_msg = f"""STELLENANFORDERUNGEN:
Titel: {extracted_job_data.get('job_title', '')}
Anforderungen: {json.dumps(extracted_job_data.get('requirements', []), ensure_ascii=False)}
Zusammenfassung: {extracted_job_data.get('description_summary', '')}

KANDIDAT (ID: {candidate_data.get('candidate_id', 'unbekannt')}):
Berufserfahrung: {json.dumps(candidate_data.get('work_history', []), ensure_ascii=False)[:3000]}
Skills: {json.dumps(candidate_data.get('skills', []), ensure_ascii=False)[:1000]}
Klassifizierung: {json.dumps(candidate_data.get('classification_data', {}), ensure_ascii=False)[:500]}
ERP-Systeme: {candidate_data.get('erp', '')}
IT-Skills: {candidate_data.get('it_skills', '')}{extra_sections}"""

        try:
            result = await _call_opus(
                system_prompt=prompt,
                user_message=user_msg,
                max_tokens=1500,
            )
            data = _parse_json_safe(result)
            return SkillsComparison(**data)
        except Exception as e:
            logger.error(f"calculate_skills_match fehlgeschlagen: {e}")
            return SkillsComparison(overall_assessment="Vergleich konnte nicht erstellt werden.")

    # ═══════════════════════════════════════════════════════════════
    # 3. E-MAIL GENERIEREN (GPT-4o, Plain-Text, ICH-Form)
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def generate_presentation_email(
        candidate_data: dict,
        extracted_job_data: dict,
        skills_comparison: dict,
        drive_time: Optional[dict] = None,
        step: int = 1,
    ) -> dict:
        """Generiert eine Plain-Text E-Mail fuer die Kandidaten-Vorstellung.

        Args:
            step: 1 = Initial, 2 = Follow-Up Tag 3, 3 = Follow-Up Tag 7

        Returns:
            {"subject": "...", "body_text": "..."}
        """
        contact_name = extracted_job_data.get("contact_name", "")
        contact_firstname = extracted_job_data.get("contact_firstname", "")
        contact_salutation = extracted_job_data.get("contact_salutation", "")

        # Nachnamen extrahieren: contact_name = "Christin Timm" → "Timm"
        if contact_name and contact_salutation:
            # Vorname entfernen um nur den Nachnamen zu bekommen
            last_name = contact_name.strip()
            if contact_firstname and last_name.lower().startswith(contact_firstname.strip().lower()):
                last_name = last_name[len(contact_firstname):].strip()
            elif " " in last_name:
                # Fallback: letztes Wort = Nachname
                last_name = last_name.split()[-1]
            anrede = f"Hallo {contact_salutation} {last_name}"
        else:
            anrede = "Sehr geehrte Damen und Herren"

        # Verfuegbarkeit berechnen
        notice_period = candidate_data.get("notice_period", "")
        salary_range = candidate_data.get("salary", "")
        primary_role = candidate_data.get("classification_data", {}).get("primary_role", "Fachkraft")

        # Drive-Time Info
        drive_info = ""
        if drive_time and drive_time.get("car_min"):
            drive_info = f"Fahrzeit: ca. {drive_time['car_min']} Min. (Auto)"
            if drive_time.get("transit_min"):
                drive_info += f", ca. {drive_time['transit_min']} Min. (OEPNV)"

        # Qualifizierungs-Daten aufbereiten (nur wenn vorhanden)
        change_motivation = candidate_data.get("change_motivation", "")
        call_transcript = candidate_data.get("call_transcript", "")
        call_summary = candidate_data.get("call_summary", "")
        desired_positions = candidate_data.get("desired_positions", "")
        home_office = candidate_data.get("home_office_days", "")
        education = candidate_data.get("education", [])
        languages = candidate_data.get("languages", {})

        # Kontext-Block nur mit vorhandenen Daten
        # Transkript hat Prioritaet, dann call_summary als Fallback
        # Qualifizierungsdaten als eigener Block — DAS ist die Geheimwaffe
        qualification_context = ""
        has_conversation_data = False

        if call_transcript or call_summary or key_activities or change_motivation:
            qualification_context += "\n═══ QUALIFIZIERUNGSGESPRAECH (Milad ↔ Kandidat) ═══"
            qualification_context += "\nDiese Daten haben HOECHSTE PRIORITAET — sie kommen direkt aus dem persoenlichen Gespraech. Nutze bevorzugt diese Informationen. Wenn etwas im Werdegang/CV steht aber hier NICHT erwaehnt wird, ist es moeglicherweise veraltet oder uebertrieben.\n"
            has_conversation_data = True

        if call_transcript:
            qualification_context += f"\nTRANSKRIPT DES GESPRAECHS:\n{call_transcript}\n"
        elif call_summary:
            qualification_context += f"\nZUSAMMENFASSUNG DES GESPRAECHS:\n{call_summary}\n"

        key_activities = candidate_data.get("key_activities", "")
        if key_activities:
            qualification_context += f"\nKERNTAETIGKEITEN (Kandidat hat im Gespraech beschrieben was er taeglich tut):\n{key_activities}\n"

        if change_motivation:
            qualification_context += f"\nWECHSELMOTIVATION (warum der Kandidat wechseln will): {change_motivation}\n"

        if desired_positions:
            qualification_context += f"\nGEWUENSCHTE POSITIONEN: {desired_positions}\n"

        candidate_notes = candidate_data.get("candidate_notes", "")
        if candidate_notes:
            qualification_context += f"\nRECRUITER-NOTIZEN (Milads persoenliche Einschaetzung): {candidate_notes}\n"

        preferred_industries = candidate_data.get("preferred_industries", "")
        if preferred_industries:
            qualification_context += f"\nBEVORZUGTE BRANCHEN: {preferred_industries}\n"

        # ── Follow-Up Templates (Step 2 + 3) ──
        followup_instructions = {
            2: f"""Schreibe das ERSTE Follow-Up (Tag 3). Kurz und direkt:
- Beginne mit "{anrede},"
- "Ich melde mich kurz zurueck bezueglich meiner Nachricht von letzter Woche."
- "Der Kandidat ist weiterhin verfuegbar und an einer Taetigkeit in {extracted_job_data.get('city', 'Ihrer Naehe')} interessiert."
- 2-3 Saetze max, kein Skills-Vergleich wiederholen
- Frage: "Haetten Sie Interesse an einem kurzen Austausch?"
- Maximal 60 Woerter""",

            3: f"""Schreibe das LETZTE Follow-Up (Tag 7). Soft-Close:
- Beginne mit "{anrede},"
- "Ich verstehe, dass Sie viel zu tun haben."
- "Falls aktuell kein Bedarf besteht, melde ich mich gerne zu einem spaeteren Zeitpunkt."
- Weicher Ton, kein Druck
- Maximal 50 Woerter""",
        }

        try:
            if step in (2, 3):
                # ── Follow-Ups: Einfacher Single-Call (kurz genug) ──
                followup_prompt = f"""Du bist Milad Hamdard, Senior Personalberater bei Sincirus. ICH-Form, kein HTML, kein Markdown.
{followup_instructions[step]}
Antworte NUR mit JSON: {{"subject": "Re: ...", "body_text": "..."}}"""
                result = await _call_opus(
                    system_prompt=followup_prompt,
                    user_message="Generiere das Follow-Up.",
                    max_tokens=500,
                    temperature=0.5,
                )
                data = _parse_json_safe(result)
                body_text = data.get("body_text", "")

            else:
                # ══════════════════════════════════════════════════
                # SINGLE-CALL: Kandidaten-Vorstellungs-E-Mail
                # Opus bekommt den vollen Kontext und schreibt die
                # E-Mail in einem Durchgang.
                # ══════════════════════════════════════════════════

                home_office_info = f"Home-Office-Wunsch: {home_office}" if home_office else ""

                email_prompt = f"""Du bist Milad Hamdard, Personalberater bei Sincirus in Hamburg (Buchhaltung & Rechnungswesen). Du hast diesen Kandidaten persoenlich qualifiziert — du kennst ihn aus einem Telefonat, nicht nur vom Lebenslauf.

ABSOLUTE PFLICHT — HALLUZINATIONS-SCHUTZ (WICHTIGSTE REGEL):
- Verwende AUSSCHLIESSLICH Informationen die in den bereitgestellten Daten stehen. ERFINDE KEINE Taetigkeiten, Zahlen, Zeitraeume, Systeme oder Qualifikationen.
- Wenn du fuer eine Anforderung keinen konkreten Beleg in den Daten findest, lass diese Anforderung KOMPLETT weg. Lieber weniger Punkte als falsche Punkte.
- DATENQUELLEN-HIERARCHIE (strikt einhalten!):
  1. HOECHSTE PRIORITAET: Transkript/Gespraechszusammenfassung — was der Kandidat im Telefonat SELBST gesagt hat, ist die zuverlaessigste Quelle
  2. MITTLERE PRIORITAET: Kerntaetigkeiten (vom Recruiter nach dem Gespraech eingetragen)
  3. NIEDRIGSTE PRIORITAET: Werdegang/CV-Daten — CVs koennen uebertrieben oder falsch strukturiert sein
- WICHTIG: Wenn etwas NUR im CV/Werdegang steht aber im Transkript NICHT erwaehnt wurde (z.B. ein System oder eine Taetigkeit), dann erwaehne es NICHT als bestaetigte Kompetenz. Der Kandidat haette es im Gespraech erwaehnt wenn es wirklich relevant waere.
- Wenn KEIN Transkript vorhanden ist, nutze die CV-Daten — aber formuliere vorsichtiger (keine absoluten Aussagen).
- ACHTUNG bei work_history: Bullet-Points koennen unter der FALSCHEN Firma stehen (CV-Parsing-Fehler). Ordne Taetigkeiten NICHT automatisch der aktuellen Stelle zu, nur weil sie inhaltlich dazu passen koennten.

DEINE AUFGABE: Schreibe eine E-Mail, die dem Kunden zeigt: "Dieser Personalberater hat meine Stelle verstanden UND den Kandidaten wirklich kennengelernt." Der Kunde soll bei JEDER Anforderung seiner Stelle sehen, was der Kandidat konkret mitbringt.

SO GEHST DU VOR (intern, BEVOR du schreibst):
1. Lies die Stellenausschreibung und extrahiere JEDE einzelne Taetigkeit/Anforderung
2. Durchsuche dann die Kandidaten-Daten nach Belegen — BEACHTE DIE HIERARCHIE:
   - ZUERST Transkript/Gespraechszusammenfassung: Hat der Kandidat sich zu dieser Taetigkeit geaeussert? Wie detailliert? Zahlen? NUR was er selbst gesagt hat zaehlt als bestaetigt.
   - DANN Kerntaetigkeiten: Wird die Taetigkeit als aktuelle Kernaufgabe genannt?
   - DANN Skills + ERP/IT: Welche Systeme beherrscht der Kandidat? Bevorzuge Angaben aus dem Transkript.
   - ZULETZT Werdegang: NUR als Ergaenzung, wenn das Transkript die Taetigkeit bestaetigt. Wenn etwas NUR im Werdegang steht und nirgendwo sonst erwaehnt wird, IGNORIERE ES.
3. Bewerte: Wie SENIOR ist der Kandidat in jeder Taetigkeit? (eigenstaendig vs. unterstuetzend, Jahre, Volumen) — NUR basierend auf bestaetigten Daten

AUFBAU DER E-MAIL (exakt diese Reihenfolge):

1. "{anrede},"
   "Ich hoffe, es geht Ihnen gut."

2. Einleitung (2-3 Saetze):
   Nenne den genauen Stellentitel und sage, dass du einen passenden Kandidaten vorstellen moechtest. Dann in 1-2 Saetzen das Wichtigste: Wer ist der Kandidat (Rolle, Erfahrungslevel, Art des aktuellen Arbeitgebers)? Was macht ihn fuer DIESE Stelle relevant?
   Beispiel: "Fuer Ihre Vakanz als Internationaler Finanzbuchhalter moechte ich Ihnen eine Kandidatin vorstellen, die aktuell die operative Finanzbuchhaltung fuer 17 Gesellschaften einer Energieunternehmensgruppe eigenstaendig verantwortet."

3. "Im Abgleich mit Ihren Anforderungen:" (genau diese Ueberschrift)
   Dann JEDE Anforderung als Bullet-Point. JEDE Zeile MUSS mit dem Zeichen • beginnen. Format:
   • Anforderung: Was der Kandidat dazu konkret kann
   • Naechste Anforderung: Beleg aus Werdegang/Transkript

   Beispiele (beachte: JEDE Zeile beginnt mit •):
   • Debitoren- und Kreditorenbuchhaltung: aktueller Schwerpunkt Kreditoren bei 30 Mandanten, zusaetzlich debitorische Erfahrung aus der Kanzlei
   • Monats- und Jahresabschluesse: erstellt eigenstaendig Jahresabschluesse, arbeitet aktuell am Monatsabschluss mit
   • Umsatzsteuervoranmeldungen: fester Bestandteil der bisherigen Taetigkeit in der Steuerkanzlei

   PFLICHT: JEDE Anforderung beginnt mit • (Aufzaehlungszeichen). KEINE Anforderung ohne •. KEIN Fliesstext fuer Anforderungen.
   Nur Anforderungen auflisten, zu denen der Kandidat etwas vorweisen kann. Keine Luecken zeigen.

4. IT/ERP-Kenntnisse:
   Liste die relevanten Systeme auf — sowohl was die Stelle fordert als auch was der Kandidat beherrscht.
   Beispiel: "Systeme: DATEV FIBU, DATEV Lodas, SAP — insbesondere DATEV setzt sie sehr sicher ein."
   Wenn im Transkript steht, wie gut der Kandidat ein System beherrscht, nutze das.

5. Wenn vorhanden — ein besonderes Detail das den Kandidaten von anderen abhebt (z.B. Sprachkenntnisse fuer internationale Stelle, Branchenerfahrung die perfekt passt, Weiterbildung die Entwicklungspotenzial zeigt). ABER NUR wenn dieses Detail im Transkript oder in der Gespraechszusammenfassung bestaetigt wurde. Wenn kein bestaetigtes besonderes Detail vorliegt, ueberspringe diesen Abschnitt KOMPLETT — schreibe NICHTS dazu.

6. Rahmendaten — als Aufzaehlungsliste mit • (Bullet-Points), JEDER Punkt in einer eigenen Zeile:
   • Fahrweg: Wenn konkrete Fahrzeiten vorliegen (Auto/OEPNV in Minuten), schreibe z.B. "Fahrweg: ca. 25 Min. mit dem Auto, ca. 35 Min. mit oeffentlichen Verkehrsmitteln". Wenn KEINE berechnete Fahrzeit vorliegt aber Pendelzeit-Praeferenzen existieren, schreibe stattdessen die Pendelbereitschaft des Kandidaten, z.B. "Pendelbereitschaft: bis 30 Min. mit dem Auto und oeffentlichen Verkehrsmitteln"
   • Verfuegbarkeit: Kuendigungsfrist oder fruehester Starttermin (wenn vorhanden)
   • Gehalt: Gehaltsvorstellung brutto p.a. (wenn vorhanden)
   • Home-Office: Wunsch des Kandidaten (wenn vorhanden)
   Nur Punkte auflisten zu denen tatsaechlich Daten vorliegen. Nichts erfinden. JEDER Punkt beginnt mit •.

7. "Bei Interesse sende ich Ihnen gerne das vollstaendige Profil zu — eine kurze Rueckmeldung genuegt."

STIL:
- Ich-Form, professionell, sachlich aber engagiert
- Konkret statt abstrakt — Zahlen, Taetigkeiten, Systeme statt Adjektive
- Aufzaehlungszeichen (•) fuer den Taetigkeitsabgleich
- Plain-Text (kein HTML, kein Markdown ausser •)
- WICHTIG FORMATIERUNG: Setze eine LEERZEILE zwischen jedem Abschnitt (Begruessing, Einleitung, Abgleich-Ueberschrift, nach dem letzten Bullet, Systeme, Besonderes, Rahmendaten, Schlusssatz). Ohne Leerzeilen klebt alles zusammen.
- Der Kunde soll das Gefuehl haben: "Der hat sich wirklich mit meiner Stelle befasst"

VERBOTE:
- NIEMALS den Namen des Kandidaten nennen ("der Kandidat" / "die Kandidatin")
- NIEMALS den Firmennamen des aktuellen Arbeitgebers nennen (stattdessen: Art des Unternehmens)
- NIEMALS Floskeln: "umfangreiche Erfahrung", "fundierte Kenntnisse", "ideale Besetzung", "breites Spektrum", "zeichnet sich aus", "bringt mit", "in der Lage", "hervorzuheben ist", "solide Grundlage", "wertvolle Bereicherung", "ueberzeugendes Profil"
- NIEMALS Word/Excel/MS-Office erwaehnen
- NIEMALS Wir-Form
- NIEMALS Anforderungen zeigen, die der Kandidat NICHT erfuellt
- NIEMALS eine Signatur oder Grussformel am Ende schreiben (KEINE "Mit freundlichen Gruessen", KEIN Name, KEINE Kontaktdaten) — die Signatur wird automatisch angehaengt
- NIEMALS Systeme/Tools/Software erwaehnen die NUR im CV stehen und im Transkript NICHT bestaetigt wurden — das sind potenzielle CV-Uebertreibungen
- NIEMALS Taetigkeiten als "aktuell" oder "in seiner jetzigen Rolle" darstellen wenn sie nur im CV unter einer ANDEREN Firma stehen

Antworte NUR mit JSON: {{"subject": "Betreffzeile (max 8 Woerter)", "body_text": "Der E-Mail-Text (OHNE Signatur/Grussformel am Ende)"}}"""

                # Zusaetzliche Kandidaten-Infos zusammenbauen
                current_pos = candidate_data.get('current_position', '')
                erp_main = candidate_data.get('erp_main', '')
                employment = candidate_data.get('employment_type', '')
                part_time = candidate_data.get('part_time_hours', '')
                industries = candidate_data.get('v2_industries', [])

                extra_info = ""
                if current_pos:
                    extra_info += f"Aktuelle Position: {current_pos}\n"
                if erp_main:
                    extra_info += f"Haupt-ERP: {erp_main}\n"
                if employment:
                    extra_info += f"Beschaeftigungsart: {employment}"
                    if part_time:
                        extra_info += f" ({part_time})"
                    extra_info += "\n"
                if industries:
                    extra_info += f"Branchen: {json.dumps(industries, ensure_ascii=False)}\n"

                email_user = f"""═══ STELLENAUSSCHREIBUNG ═══
Titel: {extracted_job_data.get('job_title', '')}
Firma: {extracted_job_data.get('company_name', '')}
Taetigkeiten/Anforderungen: {json.dumps(extracted_job_data.get('requirements', []), ensure_ascii=False)[:2000]}
Stellenbeschreibung: {extracted_job_data.get('description_summary', '')}

═══ KANDIDAT: WERDEGANG (chronologisch) ═══
{extra_info}Rolle: {primary_role}
{json.dumps(candidate_data.get('work_history', []), ensure_ascii=False)[:6000]}

═══ KANDIDAT: FACHLICHE SKILLS ═══
Skills: {json.dumps(candidate_data.get('skills', []), ensure_ascii=False)[:1000]}
ERP-Systeme: {candidate_data.get('erp', '')}
IT-Skills: {candidate_data.get('it_skills', '')}

═══ KANDIDAT: AUSBILDUNG & ZERTIFIKATE ═══
{json.dumps(candidate_data.get('education', []), ensure_ascii=False)[:600]}
Weiterbildungen: {json.dumps(candidate_data.get('further_education', []), ensure_ascii=False)[:600]}
Zertifikate: {json.dumps(candidate_data.get('v2_certifications', []), ensure_ascii=False)[:400]}
Sprachen: {json.dumps(candidate_data.get('languages') or dict(), ensure_ascii=False)}

═══ STANDORT & FAHRZEIT ═══
Standort Firma: {extracted_job_data.get('city', 'unbekannt')}
{f"BERECHNETE Fahrzeit (Google Maps, exakt): {drive_info}" if drive_info else "Fahrzeit: nicht berechnet (keine Koordinaten verfuegbar)"}
{f"Pendelzeit-Praeferenz des Kandidaten: max. {candidate_data.get('commute_max', '')}" if candidate_data.get('commute_max') else ""}
{f"Verkehrsmittel des Kandidaten: {candidate_data.get('commute_transport', '')}" if candidate_data.get('commute_transport') else ""}
WICHTIG: Wenn eine BERECHNETE Fahrzeit vorliegt, verwende DIESE (exakt in Minuten). Wenn KEINE berechnete Fahrzeit vorliegt, verwende stattdessen die Pendelzeit-Praeferenz als Fallback.

═══ RAHMENDATEN ═══
{f"Verfuegbarkeit: {notice_period}" if notice_period else ""}
{f"Gehaltsvorstellung: {salary_range}" if salary_range else ""}
{f"Home-Office-Wunsch: {home_office}" if home_office else ""}

═══ ABGLEICH-ERGEBNIS (Staerken des Kandidaten) ═══
{json.dumps(skills_comparison.get('strengths', []), ensure_ascii=False)}
{qualification_context}"""

                logger.info("Generating presentation email (single-call approach)...")

                result = await _call_opus(
                    system_prompt=email_prompt,
                    user_message=email_user,
                    max_tokens=2000,
                    temperature=0.5,
                )
                data = _parse_json_safe(result)
                body_text = data.get("body_text", "")

            # Qualitaets-Check: Verbotene Phrasen im Output?
            found_violations = _check_forbidden_phrases(body_text)
            if found_violations:
                logger.warning(f"E-Mail enthaelt {len(found_violations)} verbotene Phrasen: {found_violations[:5]}. Rewrite...")
                rewrite_result = await _call_opus(
                    system_prompt="Du bist ein Lektor fuer professionelle Geschaefts-E-Mails im Recruiting-Bereich. Deine Aufgabe: Ersetze generische Floskeln durch konkrete, spezifische Aussagen.",
                    user_message=f"""Schreibe den folgenden E-Mail-Text um. Ersetze JEDE der markierten Phrasen durch eine konkrete, spezifische Formulierung.

VERBOTENE PHRASEN DIE ERSETZT WERDEN MUESSEN:
{chr(10).join(f'- "{v}"' for v in found_violations)}

REGELN FUER DEN REWRITE:
- Ersetze "umfangreiche Erfahrung in X" durch eine konkrete Beschreibung WAS der Kandidat in X tut (z.B. "erstellt seit 3 Jahren eigenstaendig...")
- Ersetze "fundierte Kenntnisse" durch den konkreten Einsatzbereich (z.B. "nutzt SAGE taeglich fuer die Kreditorenbuchhaltung")
- Ersetze "kommunikativ und teamfaehig" ERSATZLOS — einfach loeschen, das ist Fuelltext
- Ersetze "optimal abdeckt" / "von Vorteil" durch eine direkte Verbindung zur Stellenanforderung
- NIEMALS Word/Excel/MS-Office erwaehnen
- NIEMALS den Firmennamen des Arbeitgebers nennen
- Der Platzhalter {{{{SKILLS_TABLE}}}} MUSS erhalten bleiben
- Antworte NUR mit dem umgeschriebenen Text, KEIN JSON, KEIN Kommentar

ORIGINAL-TEXT:
{body_text}""",
                    max_tokens=2000,
                    temperature=0.5,
                )
                # Rewrite ist Plain-Text, kein JSON
                rewritten = rewrite_result.strip()
                # Sicherheitscheck: Hat der Rewrite den Platzhalter behalten?
                if "SKILLS_TABLE" in rewritten and len(rewritten) > 100:
                    body_text = rewritten
                    logger.info("E-Mail erfolgreich umgeschrieben (Floskeln entfernt)")
                    # Zweiter Check
                    remaining = _check_forbidden_phrases(body_text)
                    if remaining:
                        logger.warning(f"Rewrite enthaelt noch {len(remaining)} Phrasen: {remaining[:3]}")

            # Plain-Text Skills-Tabelle bauen
            skills_table_text = _build_skills_plain_table(skills_comparison)

            # {{SKILLS_TABLE}} Platzhalter durch Plain-Text-Tabelle ersetzen
            plain_body = body_text.replace("{{SKILLS_TABLE}}", skills_table_text).replace("{SKILLS_TABLE}", skills_table_text)

            # Opus-generierte Signatur/Grussformel entfernen (Doppel-Signatur verhindern)
            plain_body = _strip_opus_signature(plain_body)

            # Echte Signatur anhaengen
            if PLAIN_TEXT_SIGNATURE not in plain_body:
                plain_body = plain_body.rstrip() + "\n\n--\n" + PLAIN_TEXT_SIGNATURE

            # Minimales HTML generieren (nur <p>, <ul>, <b> — kein Spam-Trigger)
            html_body = _plaintext_to_html(_strip_opus_signature(body_text.replace("{{SKILLS_TABLE}}", skills_table_text).replace("{SKILLS_TABLE}", skills_table_text)))
            html_body += "\n" + HTML_SIGNATURE

            # Kandidaten-Nummer (interne ID aus MT) in Betreffzeile
            cand_nr = candidate_data.get("candidate_number")
            ref_tag = f" [Kandidaten-ID: {cand_nr}]" if cand_nr else ""
            opus_subject = data.get("subject", f"{primary_role} fuer {extracted_job_data.get('company_name', 'Ihre Stelle')}")
            subject_with_id = f"{opus_subject}{ref_tag}"

            return {
                "subject": subject_with_id,
                "body_text": plain_body,
                "body_html": html_body,
            }
        except Exception as e:
            logger.error(f"generate_presentation_email fehlgeschlagen: {e}", exc_info=True)
            cand_nr = candidate_data.get("candidate_number")
            ref_tag = f" [Kandidaten-ID: {cand_nr}]" if cand_nr else ""
            return {
                "subject": f"{primary_role} - Kandidatenvorstellung{ref_tag}",
                "body_text": f"{anrede},\n\nIch moechte Ihnen einen qualifizierten Kandidaten vorstellen.\n\n[FEHLER: Opus-Call fehlgeschlagen: {str(e)[:200]}]\n\n--\n{PLAIN_TEXT_SIGNATURE}",
                "body_html": "",
            }

    # ═══════════════════════════════════════════════════════════════
    # 4. PRESENTATION ERSTELLEN (Draft-Status)
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def create_direct_presentation(
        db: AsyncSession,
        candidate_id: UUID,
        company_id: UUID,
        contact_id: Optional[UUID],
        email_to: str,
        email_from: str,
        email_subject: str,
        email_body_text: str,
        mailbox_used: str,
        source: str = "candidate_direct",
        job_posting_text: Optional[str] = None,
        extracted_job_data: Optional[dict] = None,
        skills_comparison: Optional[dict] = None,
        batch_id: Optional[UUID] = None,
        email_body_html: str = "",
    ) -> ClientPresentation:
        """Erstellt eine neue Presentation mit status=draft.

        Returns:
            ClientPresentation (noch nicht gesendet, wartet auf n8n confirm_sent)
        """
        presentation = ClientPresentation(
            candidate_id=candidate_id,
            company_id=company_id,
            contact_id=contact_id,
            match_id=None,  # Keine Match-Verknuepfung bei Direkt-Vorstellung
            email_to=email_to,
            email_from=email_from,
            email_subject=email_subject,
            email_body_text=email_body_text,
            email_body_html=email_body_html or None,
            mailbox_used=mailbox_used,
            presentation_mode="ai_generated",
            pdf_attached=False,  # Kein PDF bei Erstmail (Spam-Trigger)
            status="draft",
            sequence_active=True,
            sequence_step=1,
            source=source,
            job_posting_text=job_posting_text,
            extracted_job_data=extracted_job_data,
            skills_comparison=skills_comparison,
            reply_to_email="hamdard@sincirus.com",
            batch_id=batch_id,
        )
        db.add(presentation)
        await db.flush()

        # Triple-Doku: CompanyCorrespondence erstellen
        # ACHTUNG: CompanyCorrespondence hat KEIN candidate_id, content, channel, notes Feld!
        # Nur: company_id, contact_id, direction, subject, body
        correspondence = CompanyCorrespondence(
            company_id=company_id,
            contact_id=contact_id,
            direction=CorrespondenceDirection.OUTBOUND,
            subject=email_subject,
            body=email_body_text[:500],
        )
        db.add(correspondence)
        await db.flush()

        # Link setzen
        presentation.correspondence_id = correspondence.id
        await db.flush()

        return presentation

    # ═══════════════════════════════════════════════════════════════
    # 5. VORSTELLUNGEN FUER KANDIDAT LADEN
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def get_presentations_for_candidate(
        db: AsyncSession,
        candidate_id: UUID,
    ) -> list[dict]:
        """Laedt alle Vorstellungen eines Kandidaten (chronologisch, neueste zuerst).

        Ersetzt die alte presented_at_companies JSONB-Query.
        """
        result = await db.execute(
            select(
                ClientPresentation.id,
                ClientPresentation.company_id,
                ClientPresentation.contact_id,
                ClientPresentation.email_to,
                ClientPresentation.email_from,
                ClientPresentation.email_subject,
                ClientPresentation.email_body_text,
                ClientPresentation.mailbox_used,
                ClientPresentation.status,
                ClientPresentation.source,
                ClientPresentation.sequence_step,
                ClientPresentation.created_at,
                ClientPresentation.sent_at,
                ClientPresentation.responded_at,
                ClientPresentation.client_response_category,
                ClientPresentation.response_type,
                Company.name.label("company_name"),
                Company.city.label("company_city"),
            )
            .outerjoin(Company, Company.id == ClientPresentation.company_id)
            .where(ClientPresentation.candidate_id == candidate_id)
            .order_by(ClientPresentation.created_at.desc())
        )
        rows = result.all()

        presentations = []
        for row in rows:
            presentations.append({
                "id": str(row.id),
                "company_id": str(row.company_id) if row.company_id else None,
                "company_name": row.company_name or "Unbekannt",
                "company_city": row.company_city or "",
                "email_to": row.email_to,
                "email_from": row.email_from,
                "email_subject": row.email_subject,
                "email_body_text": row.email_body_text,
                "mailbox_used": row.mailbox_used,
                "status": row.status,
                "source": row.source,
                "sequence_step": row.sequence_step,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "sent_at": row.sent_at.isoformat() if row.sent_at else None,
                "responded_at": row.responded_at.isoformat() if row.responded_at else None,
                "response_category": row.client_response_category,
                "response_type": row.response_type,
            })
        return presentations

    # ═══════════════════════════════════════════════════════════════
    # 6. SPAM-CHECK (Cooldown pro Firma+Rolle)
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def check_spam_block(
        db: AsyncSession,
        company_name: str,
        city: str = "",
        cooldown_days: int = 7,
    ) -> dict:
        """Prueft ob eine Firma in den letzten X Tagen bereits kontaktiert wurde.

        Returns:
            {
                "blocked": bool,
                "level": "green" / "yellow" / "red",
                "reason": str,
                "last_contacted_at": datetime | None,
                "has_genuine_reply": bool,
            }
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)

        # Suche Firma (case-insensitive)
        company_result = await db.execute(
            select(Company.id)
            .where(func.lower(Company.name) == company_name.strip().lower())
            .where(
                func.lower(Company.city) == city.strip().lower()
                if city.strip() else True
            )
        )
        company_ids = [row[0] for row in company_result.all()]

        if not company_ids:
            return {"blocked": False, "level": "green", "reason": "Neue Firma", "last_contacted_at": None, "has_genuine_reply": False}

        # Letzte Vorstellung finden
        last_presentation = await db.execute(
            select(
                ClientPresentation.created_at,
                ClientPresentation.status,
                ClientPresentation.response_type,
            )
            .where(
                and_(
                    ClientPresentation.company_id.in_(company_ids),
                    ClientPresentation.status != "cancelled",
                )
            )
            .order_by(ClientPresentation.created_at.desc())
            .limit(1)
        )
        last = last_presentation.first()

        if not last:
            return {"blocked": False, "level": "green", "reason": "Noch nie kontaktiert", "last_contacted_at": None, "has_genuine_reply": False}

        last_contacted = last.created_at
        has_genuine_reply = last.response_type == "genuine_reply"

        # Rot: Firma hat geantwortet (echte Antwort, kein Bounce/Auto-Reply)
        if has_genuine_reply:
            return {
                "blocked": True,
                "level": "red",
                "reason": f"Firma hat geantwortet (Status: {last.status}). Aktive Konversation.",
                "last_contacted_at": last_contacted,
                "has_genuine_reply": True,
            }

        # Gelb: Innerhalb Cooldown-Periode
        if last_contacted and last_contacted > cutoff:
            days_ago = (datetime.now(timezone.utc) - last_contacted).days
            return {
                "blocked": False,
                "level": "yellow",
                "reason": f"Vor {days_ago} Tagen kontaktiert (Cooldown: {cooldown_days} Tage).",
                "last_contacted_at": last_contacted,
                "has_genuine_reply": False,
            }

        return {"blocked": False, "level": "green", "reason": "Cooldown abgelaufen", "last_contacted_at": last_contacted, "has_genuine_reply": False}

    # ═══════════════════════════════════════════════════════════════
    # 6b. ALREADY-PRESENTED CHECK (Kandidat+Firma Duplikat)
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def check_already_presented(
        db: AsyncSession,
        candidate_id: UUID,
        company_name: str,
        city: str = "",
    ) -> dict:
        """Prueft ob ein Kandidat dieser Firma bereits vorgestellt wurde.

        Returns:
            {
                "already_presented": bool,
                "reason": str,
                "presented_at": datetime | None,
            }
        """
        # Firma(en) finden (case-insensitive)
        company_query = select(Company.id).where(
            func.lower(Company.name) == company_name.strip().lower()
        )
        if city.strip():
            company_query = company_query.where(
                func.lower(Company.city) == city.strip().lower()
            )
        company_result = await db.execute(company_query)
        company_ids = [row[0] for row in company_result.all()]

        if not company_ids:
            return {"already_presented": False, "reason": "", "presented_at": None}

        # Bestehende Vorstellung finden (nicht cancelled)
        presentation_result = await db.execute(
            select(ClientPresentation.created_at)
            .where(
                and_(
                    ClientPresentation.candidate_id == candidate_id,
                    ClientPresentation.company_id.in_(company_ids),
                    ClientPresentation.status != "cancelled",
                )
            )
            .order_by(ClientPresentation.created_at.desc())
            .limit(1)
        )
        existing = presentation_result.first()

        if existing:
            presented_at = existing.created_at
            date_str = presented_at.strftime("%d.%m.%Y") if presented_at else "unbekannt"
            return {
                "already_presented": True,
                "reason": f"Kandidat wurde dieser Firma bereits vorgestellt am {date_str}",
                "presented_at": presented_at,
            }

        return {"already_presented": False, "reason": "", "presented_at": None}

    # ═══════════════════════════════════════════════════════════════
    # 7. SEQUENZEN STORNIEREN (bei Kandidat-Statusaenderung)
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def cancel_all_sequences_for_candidate(
        db: AsyncSession,
        candidate_id: UUID,
    ) -> int:
        """Stoppt alle aktiven Sequenzen fuer einen Kandidaten.

        Aufgerufen wenn Kandidat platziert/pausiert/nicht-interessiert wird.

        Returns:
            Anzahl der gestoppten Sequenzen.
        """
        result = await db.execute(
            update(ClientPresentation)
            .where(
                and_(
                    ClientPresentation.candidate_id == candidate_id,
                    ClientPresentation.sequence_active == True,
                    ClientPresentation.status.in_(["sent", "followup_1", "followup_2", "draft"]),
                )
            )
            .values(
                sequence_active=False,
                status="cancelled",
                updated_at=func.now(),
            )
        )
        count = result.rowcount
        if count > 0:
            await db.commit()
            logger.info(f"cancel_all_sequences: {count} Sequenzen fuer Kandidat {candidate_id} gestoppt")
        return count

    # ═══════════════════════════════════════════════════════════════
    # HELPER: Kandidaten-Daten extrahieren (Privacy-konform)
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def extract_candidate_data(db: AsyncSession, candidate_id: UUID) -> dict:
        """Extrahiert DSGVO-konforme Berufsdaten eines Kandidaten.

        KEINE persoenlichen Daten (Name, Email, Telefon, Adresse, Geburtsdatum).
        NUR: candidate_id, work_history, skills, classification_data, erp, it_skills, salary, notice_period.
        """
        result = await db.execute(
            select(Candidate).where(Candidate.id == candidate_id)
        )
        candidate = result.scalar_one_or_none()
        if not candidate:
            return {}

        return {
            "candidate_id": str(candidate_id),
            "candidate_number": candidate.candidate_number,
            "work_history": candidate.work_history or [],
            "skills": candidate.skills or [],
            "classification_data": candidate.classification_data or {},
            "erp": candidate.erp or "",
            "erp_main": candidate.erp_main or "",
            "it_skills": candidate.it_skills or "",
            "salary": candidate.salary or "",
            "notice_period": candidate.notice_period or "",
            "current_position": candidate.current_position or "",
            "employment_type": candidate.employment_type or "",
            "part_time_hours": candidate.part_time_hours or "",
            # Qualifizierungsgespraech-Daten
            "change_motivation": candidate.change_motivation or "",
            "desired_positions": candidate.desired_positions or "",
            "key_activities": candidate.key_activities or "",
            "call_summary": candidate.call_summary or "",
            "call_transcript": candidate.call_transcript or "",
            "home_office_days": candidate.home_office_days or "",
            "preferred_industries": candidate.preferred_industries or "",
            "candidate_notes": candidate.candidate_notes or "",
            # Ausbildung + Weiterbildung + Zertifikate
            "languages": candidate.languages or {},
            "education": candidate.education or [],
            "further_education": candidate.further_education or [],
            "v2_certifications": candidate.v2_certifications or [],
            "v2_industries": candidate.v2_industries or [],
            # Pendelzeit + Verkehrsmittel (Fallback wenn keine Fahrzeit berechnet)
            "commute_max": candidate.commute_max or "",
            "commute_transport": candidate.commute_transport or "",
        }


# ═══════════════════════════════════════════════════════════════
# PRIVATE HELPERS
# ═══════════════════════════════════════════════════════════════

# Verbotene Phrasen — werden nach dem GPT-Call im Python-Code geprueft
FORBIDDEN_PHRASES = [
    "umfangreiche erfahrung", "fundierte kenntnisse", "hervorragender kandidat",
    "ideale ergaenzung", "ideale ergänzung", "bringt alles mit",
    "erfuellt alle anforderungen", "erfüllt alle anforderungen",
    "bereichern koennte", "bereichern könnte", "umfassende kenntnisse",
    "kommunikativ und teamfaehig", "kommunikativ und teamfähig",
    "arbeitet gerne im team", "ist zudem kommunikativ",
    "hat umfangreiche erfahrung", "sehr gut deutsch und englisch",
    "nicht nur sondern auch", "darueber hinaus verfuegt", "darüber hinaus verfügt",
    "zusaetzlich hat er", "zusätzlich hat er", "zusaetzlich hat sie", "zusätzlich hat sie",
    "was ihn zu einem idealen", "was sie zu einer idealen",
    "die sich direkt decken", "insbesondere auch", "wenn es erforderlich war",
    "ich beziehe mich auf", "ich habe einen kandidaten identifiziert",
    "schaetzt es als das beste", "schätzt es als das beste",
    "was ihm besonders wichtig ist", "was ihr besonders wichtig ist",
    "optimal abdeckt", "von vorteil ist", "ms-office", "ms office",
    "microsoft office", "word und excel", "word, excel",
    "fundierte erfahrung", "breite erfahrung", "solide erfahrung",
    "idealer kandidat", "ideale kandidatin",
    "umfassende erfahrung", "beeindruckende kombination", "ideale besetzung",
    "unterstreicht", "hervorzuheben", "besonders hervorzuheben",
    "breites spektrum", "hat erfahrung in", "in der lage",
    "bringt mit", "zeichnet sich aus", "spiegelt wider",
    # Neue Floskeln die durchgekommen sind (Session 16.03.2026)
    "theoretische fundierung", "erforderliche sorgfalt",
    "systematische herangehensweise", "tiefgreifende expertise",
    "umfassendes verstaendnis", "umfassendes verständnis",
    "wertvolle bereicherung", "solide grundlage",
    "reibungslose integration", "nahtlose einarbeitung",
    "ueberzeugendes profil", "überzeugendes profil",
    "ideale ergaenzung", "ideale ergänzung",
    "ich erkenne in ihr", "ich erkenne in ihm",
    "erforderlich ist", "erforderlich sind",
    "genau die qualifikationen", "genau das richtige profil",
    "faehigkeit komplexe", "fähigkeit komplexe",
]


_SKIP_KEYWORDS = [
    "ms-office", "ms office", "microsoft office", "word", "excel",
    "powerpoint", "outlook", "office-anwendungen", "office anwendungen",
    "teamfaehig", "teamfähig", "teamarbeit", "kommunikativ",
    "selbstaendig", "selbständig", "strukturiert", "zuverlaessig", "zuverlässig",
]

_GENERIC_EVIDENCE = [
    "langjährige erfahrung", "langjaehrige erfahrung",
    "fundierte kenntnisse", "umfangreiche erfahrung",
    "breite erfahrung", "solide erfahrung",
]


def _build_skills_plain_table(skills_comparison: dict) -> str:
    """Baut eine Plain-Text-Darstellung des Skills-Vergleichs.

    Filtert MS-Office, Soft-Skills und generische Evidence raus.
    """
    matches = skills_comparison.get("matches", [])
    if not matches:
        return ""

    status_symbols = {
        "erfuellt": "OK",
        "teilweise": "~",
        "nicht_vorhanden": "-",
    }

    lines = ["Fachlicher Abgleich:", ""]
    for m in matches:
        req = m.get("requirement", "")
        evidence = m.get("candidate_evidence", "")
        status = m.get("status", "nicht_vorhanden")

        # NUR volle Treffer zeigen — keine Schwaechen, keine Teilmatches
        if status in ("nicht_vorhanden", "teilweise"):
            continue

        # Skip MS-Office und Soft-Skill Anforderungen
        req_lower = req.lower()
        if any(kw in req_lower for kw in _SKIP_KEYWORDS):
            continue

        # Evidence bereinigen: generische Phrasen entfernen
        evidence_lower = evidence.lower()
        if any(gen in evidence_lower for gen in _GENERIC_EVIDENCE):
            evidence = ""  # Lieber leer als generisch

        # Firmennamen aus Evidence entfernen (haeufiges GPT-Problem)
        # Erkenne Muster wie "bei XY GmbH", "bei der XY AG" etc.
        import re
        evidence = re.sub(r'\bbei\s+(der\s+)?[A-Z][A-Za-zäöüÄÖÜß&\-\s]+(GmbH|AG|SE|KG|e\.V\.|mbH|OHG|UG|Ltd|Inc)\b', 'beim aktuellen Arbeitgeber', evidence)

        symbol = status_symbols.get(status, "~")
        lines.append(f"[{symbol}] {req}")
        if evidence:
            lines.append(f"    → {evidence}")
        lines.append("")

    # Wenn nach Filter nichts uebrig bleibt
    if len(lines) <= 2:
        return ""

    return "\n".join(lines).rstrip()


def _check_forbidden_phrases(text: str) -> list[str]:
    """Prueft ob der Text verbotene Floskeln enthaelt. Gibt Liste der gefundenen zurueck."""
    text_lower = text.lower()
    return [phrase for phrase in FORBIDDEN_PHRASES if phrase in text_lower]


async def _call_opus(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 2000,
    temperature: float = 0.5,
) -> str:
    """Claude Opus API-Call via Anthropic SDK (kein DB-Session waehrend Call!)."""
    from anthropic import AsyncAnthropic

    # Opus-Key hat Vorrang, Fallback auf normalen Key
    api_key = settings.anthropic_opus_api_key or settings.anthropic_api_key
    if not api_key:
        raise ValueError("Anthropic API Key nicht konfiguriert (ANTHROPIC_OPUS_API_KEY oder ANTHROPIC_API_KEY)")

    client = AsyncAnthropic(api_key=api_key)

    try:
        response = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        content = response.content[0].text.strip()
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        logger.info(f"Claude Call erfolgreich — Model: claude-opus-4-6, Input: {input_tokens}, Output: {output_tokens} Tokens")
        return content
    except Exception as e:
        logger.error(f"Claude Call fehlgeschlagen: {e}")
        raise


def _parse_json_safe(text: str) -> dict:
    """Parst JSON aus Opus-Response (mit Fallback fuer ```json``` Bloecke und Regex)."""
    import re
    text = text.strip()

    # Versuch 1: Direkt JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Versuch 2: ```json ... ```
    if "```json" in text:
        try:
            start = text.index("```json") + 7
            end = text.index("```", start)
            return json.loads(text[start:end].strip())
        except (json.JSONDecodeError, ValueError):
            pass

    # Versuch 3: { ... } extrahieren
    if "{" in text:
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError):
            pass

    # Versuch 4: Regex-Fallback — subject und body_text einzeln extrahieren
    # Opus gibt manchmal body_text mit unescaped Zeichen zurueck die JSON brechen
    logger.warning("JSON-Parse fehlgeschlagen, versuche Regex-Extraktion...")
    result = {}

    # Subject extrahieren: "subject": "..."
    subject_match = re.search(r'"subject"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if subject_match:
        result["subject"] = subject_match.group(1).replace('\\"', '"').replace('\\n', '\n')

    # body_text extrahieren: Alles zwischen "body_text": " und dem letzten "} oder "\n}
    # Strategie: Finde den Start von body_text und nimm alles bis zum Ende des JSON-Objekts
    body_match = re.search(r'"body_text"\s*:\s*"', text)
    if body_match:
        body_start = body_match.end()
        # Finde das Ende: letztes "\n} oder "} im Text
        # Gehe rueckwaerts vom Ende des Textes und suche das schliessende Pattern
        remaining = text[body_start:]
        # Suche das letzte unescaped " gefolgt von optional Whitespace und }
        # Rueckwaerts suchen: letztes Vorkommen von "\n}" oder "}"
        end_patterns = [
            remaining.rfind('"\n}'),
            remaining.rfind('"}'),
            remaining.rfind('" }'),
            remaining.rfind('"\r\n}'),
        ]
        end_pos = max(p for p in end_patterns if p >= 0) if any(p >= 0 for p in end_patterns) else -1

        if end_pos >= 0:
            raw_body = remaining[:end_pos]
            # Unescape was wir koennen
            body_text = raw_body.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
            result["body_text"] = body_text

    if result.get("body_text"):
        logger.info(f"Regex-Extraktion erfolgreich: subject={'subject' in result}, body_text={len(result.get('body_text', ''))} Zeichen")
        return result

    raise ValueError(f"Konnte kein JSON aus Opus-Response parsen: {text[:200]}")
