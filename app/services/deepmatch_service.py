"""DeepMatch Service - KI-gestützte Tiefenanalyse für Hotlisten-Matches.

Stufe 3 des Hotlisten-Systems:
- Nutzt OpenAI (gpt-4o-mini) für detaillierten Tätigkeits-Abgleich
- Vergleicht Kandidaten-Erfahrung mit Job-Anforderungen
- Wird nur ON-DEMAND ausgelöst (Benutzer wählt Kandidaten mit Checkboxen)
- Pre-Filter: Nur Kandidaten mit pre_score >= THRESHOLD

Ablauf:
1. Benutzer wählt Kandidaten in der Hotliste (Checkboxen)
2. System prüft pre_score >= 40
3. OpenAI bewertet: Tätigkeiten, Erfahrung, Qualifikationen
4. Ergebnis wird in Match gespeichert (ai_score, ai_explanation, etc.)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus
from app.services.openai_service import OpenAIService, MatchEvaluation

logger = logging.getLogger(__name__)

# Mindest-Pre-Score für DeepMatch (Kandidaten darunter werden übersprungen)
DEEPMATCH_PRE_SCORE_THRESHOLD = 40.0


# ═══════════════════════════════════════════════════════════════
# DEEPMATCH SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════

DEEPMATCH_SYSTEM_PROMPT = """Du bist ein sehr erfahrener Senior-Recruiter und Personalberater mit tiefem Fachwissen im Bereich Finance & Accounting in Deutschland.

Du bewertest, ob ein Kandidat zu einer konkreten Stellenanzeige passt. Deine Bewertung muss praezise, faktenbasiert und praxisnah sein — so wie ein erfahrener Recruiter, der seinen Kunden beraten wuerde.

═══════════════════════════════════════════════════════════════
VERBINDLICHE FACHLICHE REGELN
═══════════════════════════════════════════════════════════════

1. TAETIGKEITEN > JOBTITEL (WICHTIGSTE REGEL)
   Die konkreten Taetigkeiten und Aufgaben des Kandidaten zaehlen MEHR als sein Jobtitel.
   - Ein "Finanzbuchhalter" der eigenstaendig Monats-/Jahresabschluesse erstellt → passt zu Bilanzbuchhalter-Stelle
   - Ein "Bilanzbuchhalter" der nur vorbereitend zuarbeitet → passt NICHT zu Bilanzbuchhalter-Stelle
   - Entscheidend ist WAS jemand TATSAECHLICH GETAN hat, nicht wie seine Position heisst

2. ABSCHLUSSERSTELLUNG = Kernkriterium fuer Bilanzbuchhalter
   - Erstellt der Kandidat EIGENSTAENDIG Monats-/Quartals-/Jahresabschluesse nach HGB (oder IFRS)?
   - Wenn JA → qualifiziert fuer Bilanzbuchhalter-Stellen
   - Wenn NEIN (nur Vorbereitung/Zuarbeit/Unterstuetzung) → qualifiziert fuer Finanzbuchhalter-Stellen
   - "Erstellung" ≠ "Vorbereitung" oder "Mitwirkung" — diesen Unterschied IMMER beachten
   - Formale Qualifikation "Bilanzbuchhalter IHK" ist ein starkes Signal, aber nicht allein entscheidend

3. KONSOLIDIERUNG = Group Accountant / Konzernbuchhalter
   - Arbeitet der Kandidat mit Konzernabschluessen, Intercompany-Abstimmungen, Konsolidierung?
   - JA → qualifiziert fuer Group Accountant / Konzernbuchhalter Stellen

4. AP/AR Spezialisierung
   - Ueberwiegend Kreditorenbuchhaltung (Accounts Payable) → Kreditorenbuchhalter
   - Ueberwiegend Debitorenbuchhaltung (Accounts Receivable) → Debitorenbuchhalter
   - Kreditoren UND Debitoren gemeinsam → Finanzbuchhalter

5. LOHN & GEHALT
   - Entgeltabrechnung, Payroll, Lohn-/Gehaltsabrechnung → Lohnbuchhalter
   - Nicht verwechseln mit Finanzbuchhaltung

6. STEUERFACHANGESTELLTE
   - Ausbildung in Steuerkanzlei oder Qualifikation als Steuerfachangestellte/r
   - Kann zusaetzlich Finanzbuchhalter oder Bilanzbuchhalter sein (Mehrfachrolle)

═══════════════════════════════════════════════════════════════
BEWERTUNGSKRITERIEN (Gewichtung)
═══════════════════════════════════════════════════════════════

1. Taetigkeits-Abgleich (45%) — WICHTIGSTES KRITERIUM
   Vergleiche die konkreten Taetigkeiten des Kandidaten mit den Anforderungen der Stelle.
   Achte besonders auf: eigenstaendige vs. vorbereitende Taetigkeiten, Komplexitaet,
   Verantwortungsgrad.

2. Fachliche Qualifikation (25%)
   Relevante Kenntnisse und Tools: DATEV, SAP, Lexware, Addison, HGB, IFRS, UStG,
   Bilanzbuchhalter IHK, Steuerfachangestellte, BWL-Studium etc.

3. Branchenerfahrung (15%)
   Hat der Kandidat Erfahrung in der gleichen oder aehnlichen Branche?
   Beruecksichtige Unternehmensgroesse (Konzern vs. Mittelstand vs. Kanzlei).

4. Entwicklungspotenzial (15%)
   Karriereverlauf, Weiterbildungen, erkennbare Entwicklungsrichtung.
   Kann sich der Kandidat realistisch in die Stelle hineinentwickeln?

═══════════════════════════════════════════════════════════════
ENGINEERING & TECHNIK (wenn Kategorie nicht FINANCE)
═══════════════════════════════════════════════════════════════

Fuer technische Stellen gelten analoge Regeln:
- Praktische Erfahrung > Zertifikate
- Spezifische Maschinen-/Software-Kenntnisse beachten (CNC, SPS, AutoCAD etc.)
- Branchenkenntnisse wichtig (Automobilindustrie, Maschinenbau, Anlagenbau etc.)
- Schichtbereitschaft, Reisebereitschaft, Fuehrerschein beruecksichtigen wenn in der Stelle gefordert

═══════════════════════════════════════════════════════════════
AUSGABEFORMAT
═══════════════════════════════════════════════════════════════

WICHTIG:
- Bewerte KONKRET und SPEZIFISCH — nenne echte Taetigkeiten, Tools, Qualifikationen
- KEINE allgemeinen Floskeln wie "bringt relevante Erfahrung mit"
- Score 0.0 (keine Passung) bis 1.0 (perfekte Passung)
- Alle Texte auf DEUTSCH
- Maximal 3 Staerken, maximal 3 Schwaechen/Luecken
- explanation: 2-3 Saetze, die den KERN der Passung/Nicht-Passung erklaeren

Antworte NUR mit einem validen JSON-Objekt:
{
  "score": 0.72,
  "explanation": "Konkreter Vergleich: Kandidat erstellt eigenstaendig Monatsabschluesse (HGB), was direkt zur Bilanzbuchhalter-Stelle passt. Allerdings fehlt IFRS-Erfahrung, die in der Stelle gefordert wird.",
  "strengths": ["Eigenstaendige Abschlusserstellung nach HGB seit 3 Jahren", "DATEV-Experte mit FiBu- und Anlagenbuchhaltung", "Branchenerfahrung im Mittelstand"],
  "weaknesses": ["Keine IFRS-Kenntnisse, obwohl in Stelle gefordert", "Keine SAP-Erfahrung"]
}"""


# ═══════════════════════════════════════════════════════════════
# DATENKLASSEN
# ═══════════════════════════════════════════════════════════════

@dataclass
class DeepMatchResult:
    """Ergebnis eines einzelnen DeepMatch."""
    match_id: UUID
    candidate_name: str
    job_position: str
    ai_score: float
    explanation: str
    strengths: list[str]
    weaknesses: list[str]
    success: bool
    error: str | None = None


@dataclass
class DeepMatchBatchResult:
    """Ergebnis einer Batch-DeepMatch-Operation."""
    total_requested: int
    evaluated: int
    skipped_low_score: int
    skipped_error: int
    avg_ai_score: float
    results: list[DeepMatchResult]
    total_cost_usd: float


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════

class DeepMatchService:
    """
    KI-gestützte Tiefenanalyse für Kandidat-Job-Matches.

    Erweitert den bestehenden OpenAI-Service mit:
    - Spezialisiertem Prompt für Tätigkeits-Abgleich
    - Pre-Score-Filter
    - Batch-Verarbeitung
    - Ergebnis-Speicherung in der DB
    """

    def __init__(self, db: AsyncSession, openai_service: OpenAIService | None = None):
        self.db = db
        self._openai = openai_service or OpenAIService()

    async def close(self) -> None:
        """Schließt den OpenAI-Client."""
        await self._openai.close()

    # ──────────────────────────────────────────────────
    # Einzelnes Match bewerten
    # ──────────────────────────────────────────────────

    async def evaluate_match(self, match_id: UUID) -> DeepMatchResult:
        """
        Führt eine DeepMatch-Analyse für ein einzelnes Match durch.

        Args:
            match_id: ID des Match-Eintrags

        Returns:
            DeepMatchResult mit KI-Bewertung
        """
        # Match mit Kandidat und Job laden
        result = await self.db.execute(
            select(Match, Candidate, Job)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .join(Job, Match.job_id == Job.id)
            .where(Match.id == match_id)
        )
        row = result.first()

        if not row:
            return DeepMatchResult(
                match_id=match_id,
                candidate_name="Unbekannt",
                job_position="Unbekannt",
                ai_score=0.0,
                explanation="Match nicht gefunden",
                strengths=[],
                weaknesses=[],
                success=False,
                error="Match nicht gefunden",
            )

        match, candidate, job = row

        # Pre-Score-Check
        if match.pre_score is not None and match.pre_score < DEEPMATCH_PRE_SCORE_THRESHOLD:
            return DeepMatchResult(
                match_id=match_id,
                candidate_name=candidate.full_name,
                job_position=job.position,
                ai_score=0.0,
                explanation=f"Pre-Score zu niedrig ({match.pre_score:.0f} < {DEEPMATCH_PRE_SCORE_THRESHOLD:.0f})",
                strengths=[],
                weaknesses=[],
                success=False,
                error="Pre-Score unter Schwellenwert",
            )

        # Job-Daten vorbereiten
        job_data = {
            "position": job.position,
            "company_name": job.company_name,
            "industry": job.industry,
            "job_text": job.job_text,
            "city": job.display_city,
            "hotlist_category": job.hotlist_category,
        }

        # Kandidaten-Daten vorbereiten
        candidate_data = {
            "full_name": candidate.full_name,
            "current_position": candidate.current_position,
            "current_company": candidate.current_company,
            "skills": candidate.skills,
            "work_history": candidate.work_history,
            "education": candidate.education,
            "further_education": candidate.further_education,
            "languages": candidate.languages,
            "it_skills": candidate.it_skills,
            "hotlist_category": candidate.hotlist_category,
            "cv_text": candidate.cv_text,
            "hotlist_job_titles": candidate.hotlist_job_titles,
            "city": candidate.hotlist_city or candidate.city,
        }

        # Detaillierten User-Prompt bauen
        user_prompt = self._build_deepmatch_user_prompt(job_data, candidate_data)

        # OpenAI-Bewertung mit spezialisiertem DeepMatch-Prompt
        evaluation = await self._openai.evaluate_match(
            job_data=job_data,
            candidate_data=candidate_data,
            system_prompt=DEEPMATCH_SYSTEM_PROMPT,
            user_prompt_override=user_prompt,
        )

        # Ergebnis in DB speichern
        match.ai_score = evaluation.score
        match.ai_explanation = evaluation.explanation
        match.ai_strengths = evaluation.strengths
        match.ai_weaknesses = evaluation.weaknesses
        match.ai_checked_at = datetime.now(timezone.utc)

        if match.status == MatchStatus.NEW:
            match.status = MatchStatus.AI_CHECKED

        await self.db.commit()

        logger.info(
            f"DeepMatch für {candidate.full_name} ↔ {job.position}: "
            f"Score={evaluation.score:.2f}"
        )

        return DeepMatchResult(
            match_id=match_id,
            candidate_name=candidate.full_name,
            job_position=job.position,
            ai_score=evaluation.score,
            explanation=evaluation.explanation,
            strengths=evaluation.strengths,
            weaknesses=evaluation.weaknesses,
            success=evaluation.success,
            error=evaluation.error,
        )

    # ──────────────────────────────────────────────────
    # User-Prompt Builder
    # ──────────────────────────────────────────────────

    def _build_deepmatch_user_prompt(
        self,
        job_data: dict,
        candidate_data: dict,
    ) -> str:
        """Baut einen detaillierten User-Prompt fuer DeepMatch.

        Schickt ALLE relevanten Informationen an OpenAI:
        - Job: Position, Unternehmen, Branche, vollstaendige Stellenbeschreibung
        - Kandidat: Werdegang MIT Taetigkeitsbeschreibungen, Ausbildung,
          Weiterbildungen, IT-Skills, Sprachen, Klassifizierung
        """
        # === JOB-TEIL ===
        job_text = job_data.get("job_text") or ""
        if len(job_text) > 4000:
            job_text = job_text[:4000] + "\n[... gekuerzt]"

        job_section = f"""═══ STELLENANGEBOT ═══
Position: {job_data.get('position', 'Nicht angegeben')}
Unternehmen: {job_data.get('company_name', 'Nicht angegeben')}
Branche: {job_data.get('industry', 'Nicht angegeben')}
Standort: {job_data.get('city', 'Nicht angegeben')}
Kategorie: {job_data.get('hotlist_category', 'Nicht angegeben')}

Stellenbeschreibung:
{job_text or 'Keine Stellenbeschreibung vorhanden'}"""

        # === KANDIDAT-TEIL ===

        # Werdegang MIT Taetigkeitsbeschreibungen (das Wichtigste!)
        work_history = candidate_data.get("work_history") or []
        work_lines = []
        for i, entry in enumerate(work_history[:8]):  # Max 8 Stationen
            if not isinstance(entry, dict):
                continue
            position = entry.get("position", "Position unbekannt")
            company = entry.get("company", "Firma unbekannt")
            start = entry.get("start_date", "?")
            end = entry.get("end_date", "heute")
            desc = entry.get("description", "")

            work_lines.append(f"\n--- Station {i+1} ---")
            work_lines.append(f"Position: {position}")
            work_lines.append(f"Unternehmen: {company}")
            work_lines.append(f"Zeitraum: {start} bis {end}")
            if desc:
                # Taetigkeitsbeschreibung max 500 Zeichen pro Eintrag
                desc_trimmed = desc[:500] + ("..." if len(desc) > 500 else "")
                work_lines.append(f"Taetigkeiten:\n{desc_trimmed}")

        work_text = "\n".join(work_lines) if work_lines else "Kein Werdegang vorhanden"

        # Ausbildung
        education = candidate_data.get("education") or []
        edu_lines = []
        for entry in education[:4]:
            if isinstance(entry, dict):
                degree = entry.get("degree", "")
                institution = entry.get("institution", "")
                field = entry.get("field_of_study", "")
                start = entry.get("start_date", "")
                end = entry.get("end_date", "")
                parts = [p for p in [degree, field, institution, f"{start}-{end}"] if p]
                edu_lines.append("- " + ", ".join(parts))
        edu_text = "\n".join(edu_lines) if edu_lines else "Keine Angaben"

        # Weiterbildungen (wichtig fuer Bilanzbuchhalter IHK etc.)
        further_edu = candidate_data.get("further_education") or []
        further_lines = []
        for entry in further_edu[:5]:
            if isinstance(entry, dict):
                title = entry.get("title", entry.get("name", ""))
                institution = entry.get("institution", entry.get("provider", ""))
                year = entry.get("year", entry.get("end_date", ""))
                parts = [p for p in [title, institution, str(year)] if p]
                further_lines.append("- " + ", ".join(parts))
            elif isinstance(entry, str):
                further_lines.append(f"- {entry}")
        further_text = "\n".join(further_lines) if further_lines else "Keine Angaben"

        # Skills
        skills = candidate_data.get("skills") or []
        skills_text = ", ".join(skills[:20]) if skills else "Keine angegeben"

        # IT-Skills
        it_skills = candidate_data.get("it_skills") or []
        it_text = ", ".join(it_skills[:15]) if it_skills else "Keine angegeben"

        # Sprachen
        languages = candidate_data.get("languages") or []
        lang_lines = []
        for entry in languages[:5]:
            if isinstance(entry, dict):
                lang_lines.append(f"{entry.get('language', '?')}: {entry.get('level', '?')}")
            elif isinstance(entry, str):
                lang_lines.append(entry)
        lang_text = ", ".join(lang_lines) if lang_lines else "Keine angegeben"

        # Klassifizierung (falls vorhanden)
        job_titles = candidate_data.get("hotlist_job_titles") or []
        titles_text = ", ".join(job_titles) if job_titles else "Nicht klassifiziert"

        candidate_section = f"""═══ KANDIDAT ═══
Name: {candidate_data.get('full_name', 'Unbekannt')}
Aktuelle Position: {candidate_data.get('current_position', 'Nicht angegeben')}
Aktuelles Unternehmen: {candidate_data.get('current_company', 'Nicht angegeben')}
Wohnort: {candidate_data.get('city', 'Nicht angegeben')}
Klassifizierte Rollen: {titles_text}

Skills: {skills_text}
IT-Kenntnisse: {it_text}
Sprachen: {lang_text}

Berufserfahrung (chronologisch, neueste zuerst):
{work_text}

Ausbildung:
{edu_text}

Weiterbildungen / Zertifikate:
{further_text}"""

        # === CV-TEXT als Fallback (nur wenn kein Werdegang) ===
        cv_fallback = ""
        if not work_lines and candidate_data.get("cv_text"):
            cv_raw = candidate_data["cv_text"][:3000]
            cv_fallback = f"\n\nCV-Volltext (kein strukturierter Werdegang vorhanden):\n{cv_raw}"

        return f"""{job_section}

{candidate_section}{cv_fallback}

═══ AUFGABE ═══
Bewerte die Passung zwischen diesem Kandidaten und der Stelle.
Beruecksichtige dabei vor allem die KONKRETEN TAETIGKEITEN aus dem Werdegang.
Vergleiche diese mit den Anforderungen der Stellenbeschreibung."""

    # ──────────────────────────────────────────────────
    # Batch-Bewertung (Benutzer wählt Kandidaten)
    # ──────────────────────────────────────────────────

    async def evaluate_selected_matches(
        self,
        match_ids: list[UUID],
    ) -> DeepMatchBatchResult:
        """
        Führt DeepMatch für eine Auswahl von Matches durch.

        Dies ist die Hauptfunktion, die der Benutzer auslöst,
        wenn er Kandidaten mit Checkboxen auswählt.

        Args:
            match_ids: Liste der ausgewählten Match-IDs

        Returns:
            DeepMatchBatchResult mit allen Ergebnissen
        """
        results: list[DeepMatchResult] = []
        evaluated = 0
        skipped_low = 0
        skipped_error = 0
        score_sum = 0.0

        for match_id in match_ids:
            try:
                result = await self.evaluate_match(match_id)
                results.append(result)

                if result.success:
                    evaluated += 1
                    score_sum += result.ai_score
                elif result.error == "Pre-Score unter Schwellenwert":
                    skipped_low += 1
                else:
                    skipped_error += 1

            except Exception as e:
                logger.error(f"DeepMatch Fehler für Match {match_id}: {e}")
                skipped_error += 1
                results.append(DeepMatchResult(
                    match_id=match_id,
                    candidate_name="Fehler",
                    job_position="Fehler",
                    ai_score=0.0,
                    explanation=str(e),
                    strengths=[],
                    weaknesses=[],
                    success=False,
                    error=str(e),
                ))

        avg_score = score_sum / evaluated if evaluated > 0 else 0.0

        logger.info(
            f"DeepMatch Batch abgeschlossen: {evaluated}/{len(match_ids)} bewertet, "
            f"{skipped_low} zu niedriger Pre-Score, "
            f"{skipped_error} Fehler, Ø Score={avg_score:.2f}"
        )

        return DeepMatchBatchResult(
            total_requested=len(match_ids),
            evaluated=evaluated,
            skipped_low_score=skipped_low,
            skipped_error=skipped_error,
            avg_ai_score=round(avg_score, 2),
            results=results,
            total_cost_usd=self._openai.total_usage.cost_usd,
        )

    # ──────────────────────────────────────────────────
    # User-Feedback speichern
    # ──────────────────────────────────────────────────

    async def save_feedback(
        self,
        match_id: UUID,
        feedback: str,
        note: str | None = None,
    ) -> bool:
        """
        Speichert Benutzer-Feedback zu einem DeepMatch-Ergebnis.

        Args:
            match_id: Match-ID
            feedback: "good", "neutral", "bad"
            note: Optionale Notiz

        Returns:
            True bei Erfolg
        """
        result = await self.db.execute(
            select(Match).where(Match.id == match_id)
        )
        match = result.scalar_one_or_none()

        if not match:
            return False

        if feedback is not None:
            match.user_feedback = feedback
        if note is not None:
            match.feedback_note = note
        match.feedback_at = datetime.now(timezone.utc)
        await self.db.commit()

        logger.info(f"Feedback für Match {match_id}: {feedback}")
        return True

    # ──────────────────────────────────────────────────
    # Kosten-Schätzung
    # ──────────────────────────────────────────────────

    def estimate_cost(self, num_candidates: int) -> dict:
        """
        Schätzt die Kosten für einen DeepMatch-Batch.

        Args:
            num_candidates: Anzahl der Kandidaten

        Returns:
            Dict mit Kosten-Informationen
        """
        cost = self._openai.estimate_cost(num_candidates)
        return {
            "num_candidates": num_candidates,
            "estimated_cost_usd": cost,
            "estimated_cost_eur": round(cost * 0.92, 4),  # Grobe USD→EUR Umrechnung
            "model": self._openai.MODEL,
        }

    async def __aenter__(self) -> "DeepMatchService":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
