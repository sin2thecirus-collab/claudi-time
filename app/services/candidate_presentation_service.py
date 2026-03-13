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


# ── Pydantic Models fuer GPT-Responses ──

class ExtractedJobData(BaseModel):
    """Strukturierte Daten aus einem Stellentext."""
    company_name: str = ""
    city: str = ""
    plz: str = ""
    address: str = ""
    domain: str = ""
    contact_name: str = ""
    contact_salutation: str = ""  # Herr / Frau / leer
    contact_email: str = ""
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
    "contact_name": "NACHNAME des Ansprechpartners (ohne Vorname, ohne Herr/Frau)",
    "contact_salutation": "Herr oder Frau (aus Text oder Vorname ableiten)",
    "contact_email": "E-Mail des Ansprechpartners (wenn vorhanden)",
    "job_title": "Exakter Jobtitel",
    "requirements": ["Anforderung 1", "Anforderung 2", ...],
    "description_summary": "1-2 Saetze Zusammenfassung der Stelle"
}

Wenn ein Feld nicht im Text vorkommt, verwende einen leeren String "" bzw. leeres Array [].
Extrahiere ALLE genannten fachlichen Anforderungen/Qualifikationen als separate Eintraege."""

        try:
            result = await _call_gpt4o(
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
            result = await _call_gpt4o(
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
        contact_salutation = extracted_job_data.get("contact_salutation", "")

        if contact_name and contact_salutation:
            anrede = f"Hallo {contact_salutation} {contact_name}"
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
        qualification_context = ""
        if call_transcript:
            qualification_context += f"\nQUALIFIZIERUNGSGESPRAECH (Transkript):\n{call_transcript}\n"
        elif call_summary:
            qualification_context += f"\nGESPRAECHSZUSAMMENFASSUNG (aus Qualifizierungsgespraech):\n{call_summary}\n"
        if change_motivation:
            qualification_context += f"\nWECHSELMOTIVATION: {change_motivation}\n"
        if desired_positions:
            qualification_context += f"\nGEWUENSCHTE POSITIONEN: {desired_positions}\n"
        if home_office:
            qualification_context += f"\nHOME-OFFICE WUNSCH: {home_office}\n"
        if education:
            edu_str = json.dumps(education, ensure_ascii=False)[:600]
            qualification_context += f"\nAUSBILDUNG: {edu_str}\n"
        further_education = candidate_data.get("further_education", [])
        if further_education:
            qualification_context += f"\nWEITERBILDUNGEN: {json.dumps(further_education, ensure_ascii=False)[:600]}\n"
        v2_certs = candidate_data.get("v2_certifications", [])
        if v2_certs:
            qualification_context += f"\nZERTIFIKATE: {json.dumps(v2_certs, ensure_ascii=False)}\n"
        if languages:
            qualification_context += f"\nSPRACHEN: {json.dumps(languages, ensure_ascii=False)}\n"
        key_activities = candidate_data.get("key_activities", "")
        if key_activities:
            qualification_context += f"\nKERNTAETIGKEITEN (vom Kandidaten im Gespraech genannt): {key_activities}\n"
        candidate_notes = candidate_data.get("candidate_notes", "")
        if candidate_notes:
            qualification_context += f"\nRECRUITER-NOTIZEN: {candidate_notes}\n"
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
                result = await _call_gpt4o(
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
                # GPT bekommt den vollen Kontext und schreibt die
                # E-Mail in einem Durchgang.
                # ══════════════════════════════════════════════════

                home_office_info = f"Home-Office-Wunsch: {home_office}" if home_office else ""

                email_prompt = f"""Du bist ein Personalberater fuer den Bereich Buchhaltung und Rechnungswesen in Deutschland mit 20 Jahren Berufserfahrung. Du heisst Milad Hamdard und arbeitest bei Sincirus.

Du hast mit dem Kandidaten ein persoenliches Qualifizierungsgespraech gefuehrt. Schau dir den Werdegang, die Ausbildung, das Gespraechstranskript und die Stellenausschreibung an.

Schreibe eine E-Mail, mit der du den Kandidaten bestmoeglich auf diese Vakanz vorstellst. Der Kunde soll merken: Du hast dich intensiv mit der Vakanz UND dem Kandidaten beschaeftigt.

Die E-Mail hat GENAU diesen Aufbau — halte die Reihenfolge exakt ein:

1. "{anrede},"
   "Ich hoffe, es geht Ihnen gut."

2. Kandidaten-Portrait (5-8 Saetze):
   Erzaehle wer dieser Kandidat ist, was er taeglich tut, wie tief seine Erfahrung ist, in welcher Art Unternehmen er arbeitet. Nutze konkrete Zahlen (Jahre, Anzahl Gesellschaften, Mandanten, Volumen). Wenn das Gespraechstranskript Details enthaelt die nicht im Lebenslauf stehen, nutze diese — das ist dein Insider-Wissen.

3. Dann schreibe auf einer eigenen Zeile GENAU diesen Platzhalter: {{{{SKILLS_TABLE}}}}

4. Dann die Rahmendaten (nur wenn vorhanden, jeweils eigene Zeile):
{"   Verfuegbarkeit: " + notice_period if notice_period else ""}
{"   Gehaltsvorstellung: " + salary_range if salary_range else ""}
{"   " + drive_info if drive_info else ""}
{"   " + home_office_info if home_office_info else ""}

5. "Unter welchen Voraussetzungen darf ich Ihnen das vollstaendige Profil unseres Kandidaten weiterleiten?"

VERBOTE:
- NIEMALS den Namen des Kandidaten nennen. Schreibe immer "der Kandidat" oder "mein Kandidat".
- NIEMALS den Namen des Arbeitgebers des Kandidaten nennen. Beschreibe stattdessen die Art des Unternehmens (z.B. "mittelstaendisches Produktionsunternehmen").
- NIEMALS Floskeln wie: "umfangreiche Erfahrung", "fundierte Kenntnisse", "beeindruckende Kombination", "ideale Besetzung", "ideale Ergaenzung", "breites Spektrum", "unterstreicht", "in der Lage", "hat Erfahrung in", "hervorzuheben ist".
- NIEMALS Word, Excel, MS-Office erwaehnen.
- NIEMALS HTML, Markdown oder Aufzaehlungszeichen verwenden.
- IMMER Ich-Form, NIE Wir-Form.
- NIEMALS Inhalte wiederholen die in der Skills-Tabelle stehen. Das Portrait erzaehlt das Gesamtbild, die Tabelle zeigt den Detailabgleich.

Antworte NUR mit JSON: {{"subject": "Betreffzeile (max 8 Woerter)", "body_text": "Der E-Mail-Text"}}"""

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

                email_user = f"""STELLE:
Titel: {extracted_job_data.get('job_title', '')}
Firma: {extracted_job_data.get('company_name', '')}
Anforderungen: {json.dumps(extracted_job_data.get('requirements', []), ensure_ascii=False)[:1500]}
Zusammenfassung: {extracted_job_data.get('description_summary', '')}

KANDIDAT:
{extra_info}Rolle: {primary_role}
Berufserfahrung: {json.dumps(candidate_data.get('work_history', []), ensure_ascii=False)[:5000]}
Skills: {json.dumps(candidate_data.get('skills', []), ensure_ascii=False)[:800]}
ERP-Systeme: {candidate_data.get('erp', '')}
IT-Skills: {candidate_data.get('it_skills', '')}
Staerken aus Abgleich: {json.dumps(skills_comparison.get('strengths', []), ensure_ascii=False)}
{qualification_context}"""

                logger.info("Generating presentation email (single-call approach)...")

                result = await _call_gpt4o(
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
                rewrite_result = await _call_gpt4o(
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

            # Falls kein Platzhalter im Text war, Tabelle nach dem zweiten Absatz einfuegen
            if skills_table_text and skills_table_text not in plain_body:
                paragraphs = plain_body.split("\n\n")
                insert_pos = min(2, len(paragraphs))
                paragraphs.insert(insert_pos, skills_table_text)
                plain_body = "\n\n".join(paragraphs)

            if PLAIN_TEXT_SIGNATURE not in plain_body:
                plain_body = plain_body.rstrip() + "\n\n--\n" + PLAIN_TEXT_SIGNATURE

            return {
                "subject": data.get("subject", f"{primary_role} fuer {extracted_job_data.get('company_name', 'Ihre Stelle')}"),
                "body_text": plain_body,
                "body_html": "",  # KEIN HTML — Plain-Text E-Mail
            }
        except Exception as e:
            logger.error(f"generate_presentation_email fehlgeschlagen: {e}", exc_info=True)
            return {
                "subject": f"{primary_role} - Kandidatenvorstellung",
                "body_text": f"{anrede},\n\nIch moechte Ihnen einen qualifizierten Kandidaten vorstellen.\n\n[FEHLER: GPT-Call fehlgeschlagen: {str(e)[:200]}]\n\n--\n{PLAIN_TEXT_SIGNATURE}",
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
        correspondence = CompanyCorrespondence(
            company_id=company_id,
            contact_id=contact_id,
            candidate_id=candidate_id,
            direction=CorrespondenceDirection.OUTGOING,
            channel="email",
            subject=email_subject,
            content=email_body_text[:500],
            notes=f"Direkte Vorstellung (source={source})",
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

        # Skip Anforderungen die NICHT passen — nur Staerken zeigen
        if status == "nicht_vorhanden":
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


async def _call_gpt4o(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 1000,
    temperature: float = 0.5,
) -> str:
    """GPT-4o API-Call ueber httpx (kein DB-Session waehrend Call!)."""
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except httpx.HTTPStatusError as e:
        logger.error(f"GPT-4o API Error {e.response.status_code}: {e.response.text[:200]}")
        raise
    except Exception as e:
        logger.error(f"GPT-4o Call fehlgeschlagen: {e}")
        raise


def _parse_json_safe(text: str) -> dict:
    """Parst JSON aus GPT-Response (mit Fallback fuer ```json``` Bloecke)."""
    text = text.strip()

    # Direkt JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # ```json ... ```
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        return json.loads(text[start:end].strip())

    # { ... } extrahieren
    if "{" in text:
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])

    raise ValueError(f"Konnte kein JSON aus GPT-Response parsen: {text[:200]}")
