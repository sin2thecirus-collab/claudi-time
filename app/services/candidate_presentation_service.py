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

Antworte NUR mit einem JSON-Objekt (kein Markdown, keine Erklaerung):
{
    "company_name": "Firmenname",
    "city": "Stadt",
    "plz": "PLZ (5-stellig)",
    "address": "Strasse + Hausnummer (wenn vorhanden)",
    "domain": "Website-Domain (ohne https://, wenn erkennbar)",
    "contact_name": "Ansprechpartner Name (wenn vorhanden)",
    "contact_salutation": "Herr oder Frau (wenn erkennbar)",
    "contact_email": "E-Mail des Ansprechpartners (wenn vorhanden)",
    "job_title": "Exakter Jobtitel",
    "requirements": ["Anforderung 1", "Anforderung 2", ...],
    "description_summary": "1-2 Saetze Zusammenfassung der Stelle"
}

Wenn ein Feld nicht im Text vorkommt, verwende einen leeren String "" bzw. leeres Array [].
Extrahiere ALLE genannten Anforderungen/Qualifikationen als separate Eintraege."""

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

Antworte NUR mit einem JSON-Objekt:
{
    "matches": [
        {"requirement": "...", "candidate_evidence": "...", "status": "erfuellt/teilweise/nicht_vorhanden"}
    ],
    "strengths": ["Staerke 1", "Staerke 2"],
    "gaps": ["Luecke 1 (wenn vorhanden)"],
    "overall_assessment": "1-2 Saetze Gesamtbewertung"
}"""

        user_msg = f"""STELLENANFORDERUNGEN:
Titel: {extracted_job_data.get('job_title', '')}
Anforderungen: {json.dumps(extracted_job_data.get('requirements', []), ensure_ascii=False)}
Zusammenfassung: {extracted_job_data.get('description_summary', '')}

KANDIDAT (ID: {candidate_data.get('candidate_id', 'unbekannt')}):
Berufserfahrung: {json.dumps(candidate_data.get('work_history', []), ensure_ascii=False)[:3000]}
Skills: {json.dumps(candidate_data.get('skills', []), ensure_ascii=False)[:1000]}
Klassifizierung: {json.dumps(candidate_data.get('classification_data', {}), ensure_ascii=False)[:500]}
ERP-Systeme: {candidate_data.get('erp', '')}
IT-Skills: {candidate_data.get('it_skills', '')}"""

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

        step_instructions = {
            1: f"""Schreibe die ERSTE Vorstellungs-E-Mail. WICHTIG: KEINE Tabelle schreiben!

Inhalt:
- Beginne mit "{anrede},"
- Beginne den Text mit "Ich hoffe, es geht Ihnen gut."
- Stelle den Kandidaten in 2-3 Saetzen kurz vor (Rolle: {primary_role})
- Schreibe dann GENAU den Platzhalter: {{{{SKILLS_TABLE}}}}
  (Die Tabelle wird automatisch eingefuegt, du musst sie NICHT schreiben!)
- Nach dem Platzhalter: Fahrzeit ({drive_info or 'noch nicht berechnet'}), Verfuegbarkeit ({notice_period or 'auf Anfrage'}), Gehaltsrahmen ({salary_range or 'auf Anfrage'})
- Schliesse mit: "Ich wuerde mich freuen, Ihnen weitere Details zukommen zu lassen."
- KEIN PDF-Anhang erwaehnen (wird erst bei Interesse geschickt)
- WICHTIG: Reiner Text fuer die Absaetze. Die Tabelle wird automatisch als {{{{SKILLS_TABLE}}}} eingefuegt.""",

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

        system_prompt = f"""Du bist Milad Hamdard, Senior Personalberater bei Sincirus (Rechnungswesen & Controlling).

WICHTIGE REGELN:
- IMMER ICH-Form ("Ich betreue...", "Ich erkenne..."), NIEMALS Wir-Form
- Professionell aber persoenlich
- Keine Floskeln wie "im Auftrag meines Kunden"
- KEINE Tabelle schreiben! Stattdessen den Platzhalter {{{{SKILLS_TABLE}}}} verwenden
- KEIN Markdown, keine **fett** Formatierung

{step_instructions.get(step, step_instructions[1])}

KANDIDATEN-DATEN:
Rolle: {primary_role}
Gesamtbewertung: {skills_comparison.get('overall_assessment', '')}

JOB-DATEN:
Titel: {extracted_job_data.get('job_title', '')}
Firma: {extracted_job_data.get('company_name', '')}
Stadt: {extracted_job_data.get('city', '')}

Antworte NUR mit einem JSON-Objekt:
{{"subject": "Betreffzeile (max 8 Woerter)", "body_text": "Der E-Mail-Text mit {{{{SKILLS_TABLE}}}} Platzhalter"}}"""

        try:
            result = await _call_gpt4o(
                system_prompt=system_prompt,
                user_message="Generiere die E-Mail.",
                max_tokens=1500,
            )
            data = _parse_json_safe(result)
            body_text = data.get("body_text", "")

            # HTML-Tabelle aus Skills-Vergleich bauen
            skills_table_html = _build_skills_html_table(skills_comparison)

            # Text in HTML konvertieren: Absaetze -> <p> Tags
            # {{SKILLS_TABLE}} Platzhalter durch echte HTML-Tabelle ersetzen
            paragraphs = body_text.split("\n\n")
            html_parts = []
            for p in paragraphs:
                p = p.strip()
                if not p:
                    continue
                if "SKILLS_TABLE" in p or "{SKILLS_TABLE}" in p:
                    html_parts.append(skills_table_html)
                else:
                    # Zeilenumbrueche innerhalb eines Absatzes als <br> behalten
                    p_html = p.replace("\n", "<br>")
                    html_parts.append(f'<p style="margin:0 0 12px 0;font-size:14px;line-height:1.6;color:#1f2937;">{p_html}</p>')

            # Falls kein Platzhalter im Text war, Tabelle nach dem ersten Absatz einfuegen
            if skills_table_html and skills_table_html not in "\n".join(html_parts):
                html_parts.insert(min(2, len(html_parts)), skills_table_html)

            body_html = f"""<div style="font-family:Arial,sans-serif;max-width:650px;">
{"".join(html_parts)}
{HTML_SIGNATURE}
</div>"""

            # Plain-Text Fallback (fuer Vorschau + Alt-Text)
            plain_body = body_text.replace("{{SKILLS_TABLE}}", "").replace("{SKILLS_TABLE}", "")
            if PLAIN_TEXT_SIGNATURE not in plain_body:
                plain_body = plain_body.rstrip() + "\n\n--\n" + PLAIN_TEXT_SIGNATURE

            return {
                "subject": data.get("subject", f"{primary_role} fuer {extracted_job_data.get('company_name', 'Ihre Stelle')}"),
                "body_text": plain_body,
                "body_html": body_html,
            }
        except Exception as e:
            logger.error(f"generate_presentation_email fehlgeschlagen: {e}")
            return {
                "subject": f"{primary_role} - Kandidatenvorstellung",
                "body_text": f"{anrede},\n\nIch moechte Ihnen einen qualifizierten Kandidaten vorstellen.\n\n--\n{PLAIN_TEXT_SIGNATURE}",
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
            "it_skills": candidate.it_skills or "",
            "salary": candidate.salary or "",
            "notice_period": candidate.notice_period or "",
        }


# ═══════════════════════════════════════════════════════════════
# PRIVATE HELPERS
# ═══════════════════════════════════════════════════════════════

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
