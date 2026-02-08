"""Profile Engine Service — Strukturierte Profil-Extraktion für Matching Engine v2.

Extrahiert 1x pro Dokument ein strukturiertes Profil via GPT-4o-mini.
Danach ist Matching reine Mathematik — $0 pro Match.

Kosten-Schaetzung:
- ~5.000 Kandidaten × ~500 Token Input × ~200 Token Output = ~$1.73
- ~1.500 Jobs × ~800 Token Input × ~200 Token Output = ~$1.13
- Gesamt Backfill: ~$3 (einmalig!)
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

import httpx
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import limits, settings
from app.models.candidate import Candidate
from app.models.job import Job

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# GPT SYSTEM PROMPTS
# ══════════════════════════════════════════════════════════════════

CANDIDATE_PROFILE_PROMPT = """Du bist ein erfahrener Personalberater mit tiefem Fachwissen im Bereich Finance & Accounting in Deutschland.

Deine Aufgabe: Analysiere den Werdegang eines Kandidaten und erstelle ein strukturiertes Profil.

═══════════════════════════════════════════════════════════════
SENIORITY-LEVELS (ISCO-basiert, 6 Stufen)
═══════════════════════════════════════════════════════════════

1 = Buchhaltungsassistent / Junior / Werkstudent / Praktikant
    → Einfache Zuarbeit, keine eigenstaendige Verantwortung
2 = Sachbearbeiter / Buchhalter / Kreditorenbuchhalter / Debitorenbuchhalter
    → Operative Taetigkeiten, eigenstaendig in Teilbereichen
3 = Finanzbuchhalter / Hauptbuchhalter
    → Eigenstaendig, MITWIRKUNG bei Abschluessen, aber NICHT eigenstaendige Erstellung
4 = Bilanzbuchhalter / Senior Finanzbuchhalter
    → Eigenstaendige ERSTELLUNG von Monats-/Quartals-/Jahresabschluessen (HGB, ggf. IFRS)
5 = Teamleiter / Senior Bilanzbuchhalter / Group Accountant
    → Fuehrung eines Teams + eigenstaendige Abschlusserstellung + Konsolidierung
6 = Leiter Rechnungswesen / Head of Accounting / CFO
    → Gesamtverantwortung Rechnungswesen, strategisch, Reporting an Geschaeftsfuehrung

WICHTIGE UNTERSCHEIDUNG:
- "Eigenstaendige Erstellung" von Abschluessen → Level 4+
- "Mitwirkung bei der Erstellung" oder "Vorbereitung" → Level 3
- "Mitwirkung bei der Erstellung" enthaelt das Wort "Erstellung", ist aber NUR Level 3!
- IHK Bilanzbuchhalter-Zertifikat = Mindestens Level 4
- Lohnbuchhaltung = komplett anderes Fachgebiet, NICHT Finanzbuchhaltung

═══════════════════════════════════════════════════════════════
SOFTWARE-OEKOSYSTEME (gegenseitiger Ausschluss)
═══════════════════════════════════════════════════════════════

- DATEV-Welt: DATEV, DATEV Unternehmen online, DATEV Kanzlei-Rechnungswesen → Kanzlei/KMU
- SAP-Welt: SAP FI, SAP CO, SAP S/4HANA → Konzern/Grossunternehmen
- Umstieg DATEV↔SAP = 6-12 Monate Einarbeitung (wichtig fuer Matching!)
- Andere: Lexware, Addison, Sage, Microsoft Dynamics, Oracle, Navision

═══════════════════════════════════════════════════════════════
KARRIERE-TRAJEKTORIE
═══════════════════════════════════════════════════════════════

- "aufsteigend": Klarer Aufstieg ueber die Jahre (mehr Verantwortung, hoehere Positionen)
- "lateral": Gleichbleibendes Niveau, Wechsel zwischen aehnlichen Positionen
- "absteigend": Rueckgang der Verantwortung/Position (selten, aber kommt vor)
- "einstieg": Berufseinsteiger oder erste 1-2 Jahre

═══════════════════════════════════════════════════════════════
AUSGABE (JSON)
═══════════════════════════════════════════════════════════════

Antworte NUR mit validem JSON:
{
  "seniority_level": 3,
  "career_trajectory": "aufsteigend",
  "years_experience": 7,
  "current_role_summary": "Finanzbuchhalter mit Schwerpunkt Debitoren-/Kreditorenbuchhaltung und Mitwirkung bei HGB-Monatsabschluessen. Eigenstaendige Kontenpflege und USt-Voranmeldungen.",
  "structured_skills": [
    {"skill": "HGB-Abschlusserstellung", "proficiency": "fortgeschritten", "recency": "aktuell", "category": "fachlich"},
    {"skill": "DATEV", "proficiency": "experte", "recency": "aktuell", "category": "software"},
    {"skill": "Kreditorenbuchhaltung", "proficiency": "experte", "recency": "aktuell", "category": "taetigkeitsfeld"},
    {"skill": "SAP FI", "proficiency": "grundlagen", "recency": "veraltet", "category": "software"},
    {"skill": "Umsatzsteuer", "proficiency": "fortgeschritten", "recency": "aktuell", "category": "fachlich"}
  ]
}

REGELN:
- seniority_level: Basierend auf AKTUELLER Rolle, NICHT auf der hoechsten jemals erreichten Position!
- career_trajectory: Basierend auf dem Gesamtverlauf der Karriere
- years_experience: Gesamte einschlaegige Berufserfahrung in Jahren
- current_role_summary: 1-2 Saetze ueber die AKTUELLE Taetigkeiten (nicht Historie!)
- structured_skills: Jeder relevante Skill mit Proficiency und Recency
  - proficiency: "grundlagen" / "fortgeschritten" / "experte"
  - recency: "aktuell" (letzte 2 Jahre) / "kuerzlich" (2-5 Jahre) / "veraltet" (5+ Jahre)
  - category: "fachlich" / "software" / "taetigkeitsfeld" / "zertifizierung" / "branche"
- Maximal 15 Skills, nur die relevantesten"""


JOB_PROFILE_PROMPT = """Du bist ein erfahrener Personalberater mit tiefem Fachwissen im Bereich Finance & Accounting in Deutschland.

Deine Aufgabe: Analysiere eine Stellenanzeige und erstelle ein strukturiertes Profil.

═══════════════════════════════════════════════════════════════
SENIORITY-LEVELS (ISCO-basiert, 6 Stufen)
═══════════════════════════════════════════════════════════════

1 = Buchhaltungsassistent / Junior / Werkstudent
2 = Sachbearbeiter / Buchhalter (operativ)
3 = Finanzbuchhalter (eigenstaendig, Mitwirkung bei Abschluessen)
4 = Bilanzbuchhalter (eigenstaendige Erstellung von Abschluessen)
5 = Teamleiter / Senior Bilanzbuchhalter / Group Accountant
6 = Leiter Rechnungswesen / Head of Accounting / CFO

WICHTIG BEI DER EINSTUFUNG DER STELLE:
- Eine Stelle ist NUR Level 4+, wenn die AUFGABEN klar eigenstaendige Abschlusserstellung fordern
- "Bilanzbuchhalter wuenschenswert" in Anforderungen macht die Stelle NICHT zu Level 4
- Entscheidend sind die AUFGABEN, nicht der Jobtitel oder gewuenschte Qualifikationen
- "Mitwirkung bei Abschluessen" = Level 3, NICHT Level 4

═══════════════════════════════════════════════════════════════
AUSGABE (JSON)
═══════════════════════════════════════════════════════════════

Antworte NUR mit validem JSON:
{
  "seniority_level": 4,
  "role_summary": "Bilanzbuchhalter mit eigenstaendiger Erstellung von Monats-, Quartals- und Jahresabschluessen nach HGB. Umsatzsteuer-Voranmeldungen und Kontenabstimmung.",
  "required_skills": [
    {"skill": "HGB-Abschlusserstellung", "importance": "essential", "category": "fachlich"},
    {"skill": "DATEV", "importance": "essential", "category": "software"},
    {"skill": "SAP FI", "importance": "preferred", "category": "software"},
    {"skill": "Bilanzbuchhalter IHK", "importance": "preferred", "category": "zertifizierung"},
    {"skill": "Umsatzsteuer", "importance": "essential", "category": "fachlich"}
  ]
}

REGELN:
- seniority_level: Basierend auf den AUFGABEN der Stelle, NICHT auf dem Titel
- role_summary: 1-2 Saetze ueber die Kern-Taetigkeiten der Stelle
- required_skills: Jeder geforderte Skill mit Wichtigkeit
  - importance: "essential" (Muss-Kriterium) / "preferred" (Wunsch-Kriterium)
  - category: "fachlich" / "software" / "taetigkeitsfeld" / "zertifizierung" / "branche"
- Maximal 12 Skills, nur die relevantesten
- Trenne klar zwischen echten Anforderungen und "nice-to-have" """


# ══════════════════════════════════════════════════════════════════
# DATENKLASSEN
# ══════════════════════════════════════════════════════════════════

@dataclass
class CandidateProfile:
    """Ergebnis der Kandidaten-Profil-Extraktion."""
    candidate_id: UUID
    seniority_level: int  # 1-6
    career_trajectory: str  # aufsteigend/lateral/absteigend/einstieg
    years_experience: int
    current_role_summary: str
    structured_skills: list[dict]  # [{skill, proficiency, recency, category}]
    success: bool = True
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return round(
            (self.input_tokens / 1_000_000) * 0.15 +
            (self.output_tokens / 1_000_000) * 0.60,
            6
        )


@dataclass
class JobProfile:
    """Ergebnis der Job-Profil-Extraktion."""
    job_id: UUID
    seniority_level: int
    role_summary: str
    required_skills: list[dict]  # [{skill, importance, category}]
    success: bool = True
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return round(
            (self.input_tokens / 1_000_000) * 0.15 +
            (self.output_tokens / 1_000_000) * 0.60,
            6
        )


@dataclass
class BackfillResult:
    """Ergebnis eines Backfill-Laufs."""
    total: int = 0
    profiled: int = 0
    skipped: int = 0
    failed: int = 0
    total_cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════
# PROFILE ENGINE SERVICE
# ══════════════════════════════════════════════════════════════════

class ProfileEngineService:
    """Extrahiert strukturierte Profile aus Kandidaten/Job-Daten.

    Nutzt GPT-4o-mini einmalig pro Dokument.
    Kosten: ~$0.0003 pro Kandidat, ~$0.0005 pro Job.
    """

    MODEL = "gpt-4o-mini"

    def __init__(self, db: AsyncSession):
        self.db = db
        self.api_key = settings.openai_api_key
        self._client: httpx.AsyncClient | None = None

        if not self.api_key:
            logger.warning("OpenAI API-Key nicht konfiguriert — Profile Engine deaktiviert")

    async def _get_client(self) -> httpx.AsyncClient:
        """HTTP-Client fuer OpenAI API (Singleton)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://api.openai.com/v1",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(limits.TIMEOUT_OPENAI),
            )
        return self._client

    async def close(self):
        """Schliesst den HTTP-Client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _call_gpt(self, system_prompt: str, user_message: str) -> dict | None:
        """Sendet einen Prompt an GPT-4o-mini und gibt die JSON-Antwort zurueck.

        Returns:
            Tuple (parsed_json, input_tokens, output_tokens) oder None bei Fehler
        """
        if not self.api_key:
            return None

        client = await self._get_client()

        try:
            response = await client.post(
                "/chat/completions",
                json={
                    "model": self.MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    "temperature": 0.1,  # Deterministisch fuer konsistente Profile
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            data = response.json()

            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            parsed = json.loads(content)

            return {
                "data": parsed,
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            }
        except httpx.TimeoutException:
            logger.warning("GPT-4o-mini Timeout")
            return None
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"GPT Response Parse-Fehler: {e}")
            return None
        except httpx.HTTPStatusError as e:
            logger.error(f"GPT API-Fehler: {e.response.status_code}")
            return None
        except Exception as e:
            logger.error(f"GPT Fehler: {e}")
            return None

    # ── Kandidaten-Profil ────────────────────────────────

    def _build_candidate_input(self, c: Candidate) -> str:
        """Baut den User-Prompt fuer die Kandidaten-Profil-Extraktion."""
        parts = []

        # Aktuelle Position
        if c.current_position:
            parts.append(f"Aktuelle Position: {c.current_position}")
        if c.current_company:
            parts.append(f"Aktuelles Unternehmen: {c.current_company}")

        # Berufserfahrung
        if c.work_history:
            parts.append("\nBerufserfahrung:")
            if isinstance(c.work_history, list):
                for entry in c.work_history[:10]:  # Max 10 Eintraege
                    if isinstance(entry, dict):
                        period = entry.get("period", "")
                        title = entry.get("title", entry.get("position", ""))
                        company = entry.get("company", "")
                        tasks = entry.get("tasks", entry.get("description", ""))
                        parts.append(f"  - {period}: {title} bei {company}")
                        if tasks:
                            if isinstance(tasks, list):
                                parts.append(f"    Taetigkeiten: {'; '.join(tasks[:5])}")
                            else:
                                parts.append(f"    Taetigkeiten: {str(tasks)[:300]}")
                    else:
                        parts.append(f"  - {str(entry)[:200]}")
            elif isinstance(c.work_history, dict):
                for key, val in list(c.work_history.items())[:10]:
                    parts.append(f"  - {key}: {str(val)[:200]}")

        # Ausbildung
        if c.education:
            parts.append("\nAusbildung:")
            if isinstance(c.education, list):
                for entry in c.education[:5]:
                    if isinstance(entry, dict):
                        parts.append(f"  - {entry.get('degree', '')} - {entry.get('institution', '')} ({entry.get('year', '')})")
                    else:
                        parts.append(f"  - {str(entry)[:200]}")
            elif isinstance(c.education, dict):
                for key, val in list(c.education.items())[:5]:
                    parts.append(f"  - {key}: {str(val)[:200]}")

        # Weiterbildungen
        if c.further_education:
            parts.append("\nWeiterbildungen:")
            if isinstance(c.further_education, list):
                for entry in c.further_education[:5]:
                    parts.append(f"  - {str(entry)[:200]}")
            elif isinstance(c.further_education, dict):
                for key, val in list(c.further_education.items())[:5]:
                    parts.append(f"  - {key}: {str(val)[:200]}")

        # Skills
        if c.skills:
            parts.append(f"\nSkills: {', '.join(c.skills[:20])}")
        if c.it_skills:
            parts.append(f"IT-Skills: {', '.join(c.it_skills[:15])}")

        # Sprachen
        if c.languages:
            if isinstance(c.languages, list):
                parts.append(f"Sprachen: {', '.join(str(l) for l in c.languages[:5])}")
            elif isinstance(c.languages, dict):
                lang_str = ", ".join(f"{k}: {v}" for k, v in c.languages.items())
                parts.append(f"Sprachen: {lang_str}")

        # Hotlist-Info (falls vorhanden)
        if c.hotlist_category:
            parts.append(f"\nKategorie: {c.hotlist_category}")
        if c.hotlist_job_titles:
            parts.append(f"Zugewiesene Rollen: {', '.join(c.hotlist_job_titles)}")

        return "\n".join(parts) if parts else "Keine Daten vorhanden"

    async def create_candidate_profile(self, candidate_id: UUID) -> CandidateProfile:
        """Erstellt ein strukturiertes Profil fuer einen Kandidaten.

        Args:
            candidate_id: UUID des Kandidaten

        Returns:
            CandidateProfile mit allen extrahierten Daten
        """
        candidate = await self.db.get(Candidate, candidate_id)
        if not candidate:
            return CandidateProfile(
                candidate_id=candidate_id,
                seniority_level=0, career_trajectory="", years_experience=0,
                current_role_summary="", structured_skills=[],
                success=False, error="Kandidat nicht gefunden"
            )

        # Input fuer GPT bauen
        user_input = self._build_candidate_input(candidate)

        # Wenn zu wenig Daten → Skip
        if len(user_input) < 50:
            return CandidateProfile(
                candidate_id=candidate_id,
                seniority_level=0, career_trajectory="", years_experience=0,
                current_role_summary="", structured_skills=[],
                success=False, error="Zu wenig Daten fuer Profil-Extraktion"
            )

        # GPT aufrufen
        result = await self._call_gpt(CANDIDATE_PROFILE_PROMPT, user_input)
        if not result:
            return CandidateProfile(
                candidate_id=candidate_id,
                seniority_level=0, career_trajectory="", years_experience=0,
                current_role_summary="", structured_skills=[],
                success=False, error="GPT-Aufruf fehlgeschlagen"
            )

        data = result["data"]

        # Validierung
        seniority = data.get("seniority_level", 0)
        if not isinstance(seniority, int) or seniority < 1 or seniority > 6:
            seniority = 2  # Default: Sachbearbeiter

        trajectory = data.get("career_trajectory", "lateral")
        if trajectory not in ("aufsteigend", "lateral", "absteigend", "einstieg"):
            trajectory = "lateral"

        years = data.get("years_experience", 0)
        if not isinstance(years, int) or years < 0:
            years = 0

        profile = CandidateProfile(
            candidate_id=candidate_id,
            seniority_level=seniority,
            career_trajectory=trajectory,
            years_experience=years,
            current_role_summary=data.get("current_role_summary", "")[:500],
            structured_skills=data.get("structured_skills", [])[:15],
            input_tokens=result["input_tokens"],
            output_tokens=result["output_tokens"],
        )

        # In DB speichern
        now = datetime.now(timezone.utc)
        candidate.v2_seniority_level = profile.seniority_level
        candidate.v2_career_trajectory = profile.career_trajectory
        candidate.v2_years_experience = profile.years_experience
        candidate.v2_current_role_summary = profile.current_role_summary
        candidate.v2_structured_skills = profile.structured_skills
        candidate.v2_profile_created_at = now
        await self.db.flush()

        logger.info(
            f"Kandidaten-Profil erstellt: {candidate.full_name} "
            f"(Level {profile.seniority_level}, {profile.career_trajectory}, "
            f"{profile.years_experience}J, ${profile.cost_usd:.4f})"
        )

        return profile

    # ── Job-Profil ───────────────────────────────────────

    def _build_job_input(self, job: Job) -> str:
        """Baut den User-Prompt fuer die Job-Profil-Extraktion."""
        parts = []

        if job.position:
            parts.append(f"Position: {job.position}")
        if job.company_name:
            parts.append(f"Unternehmen: {job.company_name}")
        if job.industry:
            parts.append(f"Branche: {job.industry}")
        if job.company_size:
            parts.append(f"Unternehmensgroesse: {job.company_size}")
        if job.employment_type:
            parts.append(f"Beschaeftigungsart: {job.employment_type}")

        # Stellentext (max 3000 Zeichen)
        if job.job_text:
            text = job.job_text[:3000]
            parts.append(f"\nStellenanzeige:\n{text}")

        # Hotlist-Info
        if job.hotlist_category:
            parts.append(f"\nKategorie: {job.hotlist_category}")
        if job.hotlist_job_titles:
            parts.append(f"Zugewiesene Rollen: {', '.join(job.hotlist_job_titles)}")
        elif job.hotlist_job_title:
            parts.append(f"Zugewiesene Rolle: {job.hotlist_job_title}")

        return "\n".join(parts) if parts else "Keine Daten vorhanden"

    async def create_job_profile(self, job_id: UUID) -> JobProfile:
        """Erstellt ein strukturiertes Profil fuer einen Job.

        Args:
            job_id: UUID des Jobs

        Returns:
            JobProfile mit allen extrahierten Daten
        """
        job = await self.db.get(Job, job_id)
        if not job:
            return JobProfile(
                job_id=job_id,
                seniority_level=0, role_summary="", required_skills=[],
                success=False, error="Job nicht gefunden"
            )

        # Input fuer GPT bauen
        user_input = self._build_job_input(job)

        if len(user_input) < 30:
            return JobProfile(
                job_id=job_id,
                seniority_level=0, role_summary="", required_skills=[],
                success=False, error="Zu wenig Daten fuer Profil-Extraktion"
            )

        # GPT aufrufen
        result = await self._call_gpt(JOB_PROFILE_PROMPT, user_input)
        if not result:
            return JobProfile(
                job_id=job_id,
                seniority_level=0, role_summary="", required_skills=[],
                success=False, error="GPT-Aufruf fehlgeschlagen"
            )

        data = result["data"]

        # Validierung
        seniority = data.get("seniority_level", 0)
        if not isinstance(seniority, int) or seniority < 1 or seniority > 6:
            seniority = 2

        profile = JobProfile(
            job_id=job_id,
            seniority_level=seniority,
            role_summary=data.get("role_summary", "")[:500],
            required_skills=data.get("required_skills", [])[:12],
            input_tokens=result["input_tokens"],
            output_tokens=result["output_tokens"],
        )

        # In DB speichern
        now = datetime.now(timezone.utc)
        job.v2_seniority_level = profile.seniority_level
        job.v2_required_skills = profile.required_skills
        job.v2_role_summary = profile.role_summary
        job.v2_profile_created_at = now
        await self.db.flush()

        logger.info(
            f"Job-Profil erstellt: {job.position} bei {job.company_name} "
            f"(Level {profile.seniority_level}, ${profile.cost_usd:.4f})"
        )

        return profile

    # ── Backfill ─────────────────────────────────────────

    async def backfill_candidates(
        self,
        batch_size: int = 50,
        max_total: int = 0,
        progress_callback=None,
    ) -> BackfillResult:
        """Backfill: Erstellt Profile fuer alle Kandidaten OHNE v2-Profil.

        Args:
            batch_size: Wie viele pro Commit-Batch
            max_total: Maximum (0 = alle)
            progress_callback: Optional callback(processed, total)

        Returns:
            BackfillResult mit Statistiken
        """
        result = BackfillResult()

        # Zaehle fehlende Profile — NUR FINANCE-Kandidaten
        count_result = await self.db.execute(
            select(func.count(Candidate.id)).where(
                Candidate.v2_profile_created_at.is_(None),
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.hotlist_category == "FINANCE",
            )
        )
        total_missing = count_result.scalar() or 0
        result.total = min(total_missing, max_total) if max_total > 0 else total_missing

        if result.total == 0:
            logger.info("Backfill Kandidaten: Alle FINANCE-Profile vorhanden.")
            return result

        logger.info(f"Backfill Kandidaten: {result.total} FINANCE-Profile zu erstellen...")

        # FINANCE-Kandidaten laden (aelteste zuerst)
        query = (
            select(Candidate.id)
            .where(
                Candidate.v2_profile_created_at.is_(None),
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.hotlist_category == "FINANCE",
            )
            .order_by(Candidate.created_at.asc())
        )
        if max_total > 0:
            query = query.limit(max_total)

        ids_result = await self.db.execute(query)
        candidate_ids = [row[0] for row in ids_result.all()]

        for i, cid in enumerate(candidate_ids):
            try:
                profile = await self.create_candidate_profile(cid)
                if profile.success:
                    result.profiled += 1
                    result.total_cost_usd += profile.cost_usd
                else:
                    result.skipped += 1
            except Exception as e:
                result.failed += 1
                if len(result.errors) < 20:
                    result.errors.append(f"Kandidat {cid}: {str(e)[:100]}")

            # Commit alle batch_size Eintraege
            if (i + 1) % batch_size == 0:
                await self.db.commit()
                if progress_callback:
                    progress_callback(i + 1, result.total)
                logger.info(
                    f"Backfill Kandidaten: {i + 1}/{result.total} "
                    f"(${result.total_cost_usd:.4f})"
                )

        # Final commit
        await self.db.commit()

        logger.info(
            f"Backfill Kandidaten abgeschlossen: {result.profiled} erstellt, "
            f"{result.skipped} uebersprungen, {result.failed} fehlgeschlagen, "
            f"${result.total_cost_usd:.4f} Kosten"
        )
        return result

    async def backfill_jobs(
        self,
        batch_size: int = 50,
        max_total: int = 0,
        progress_callback=None,
    ) -> BackfillResult:
        """Backfill: Erstellt Profile fuer alle Jobs OHNE v2-Profil.

        Args:
            batch_size: Wie viele pro Commit-Batch
            max_total: Maximum (0 = alle)
            progress_callback: Optional callback(processed, total)

        Returns:
            BackfillResult mit Statistiken
        """
        result = BackfillResult()

        # Zaehle fehlende Profile — NUR FINANCE-Jobs
        count_result = await self.db.execute(
            select(func.count(Job.id)).where(
                Job.v2_profile_created_at.is_(None),
                Job.deleted_at.is_(None),
                Job.hotlist_category == "FINANCE",
            )
        )
        total_missing = count_result.scalar() or 0
        result.total = min(total_missing, max_total) if max_total > 0 else total_missing

        if result.total == 0:
            logger.info("Backfill Jobs: Alle FINANCE-Profile vorhanden.")
            return result

        logger.info(f"Backfill Jobs: {result.total} FINANCE-Profile zu erstellen...")

        # FINANCE-Jobs laden (aelteste zuerst)
        query = (
            select(Job.id)
            .where(
                Job.v2_profile_created_at.is_(None),
                Job.deleted_at.is_(None),
                Job.hotlist_category == "FINANCE",
            )
            .order_by(Job.created_at.asc())
        )
        if max_total > 0:
            query = query.limit(max_total)

        ids_result = await self.db.execute(query)
        job_ids = [row[0] for row in ids_result.all()]

        for i, jid in enumerate(job_ids):
            try:
                profile = await self.create_job_profile(jid)
                if profile.success:
                    result.profiled += 1
                    result.total_cost_usd += profile.cost_usd
                else:
                    result.skipped += 1
            except Exception as e:
                result.failed += 1
                if len(result.errors) < 20:
                    result.errors.append(f"Job {jid}: {str(e)[:100]}")

            # Commit alle batch_size Eintraege
            if (i + 1) % batch_size == 0:
                await self.db.commit()
                if progress_callback:
                    progress_callback(i + 1, result.total)
                logger.info(
                    f"Backfill Jobs: {i + 1}/{result.total} "
                    f"(${result.total_cost_usd:.4f})"
                )

        # Final commit
        await self.db.commit()

        logger.info(
            f"Backfill Jobs abgeschlossen: {result.profiled} erstellt, "
            f"{result.skipped} uebersprungen, {result.failed} fehlgeschlagen, "
            f"${result.total_cost_usd:.4f} Kosten"
        )
        return result

    # ── Stats ────────────────────────────────────────────

    async def get_profile_stats(self) -> dict:
        """Gibt Statistiken ueber den Profil-Status zurueck."""
        # Kandidaten
        total_cand = await self.db.execute(
            select(func.count(Candidate.id)).where(
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
            )
        )
        profiled_cand = await self.db.execute(
            select(func.count(Candidate.id)).where(
                Candidate.v2_profile_created_at.isnot(None),
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
            )
        )

        # Jobs
        total_jobs = await self.db.execute(
            select(func.count(Job.id)).where(Job.deleted_at.is_(None))
        )
        profiled_jobs = await self.db.execute(
            select(func.count(Job.id)).where(
                Job.v2_profile_created_at.isnot(None),
                Job.deleted_at.is_(None),
            )
        )

        tc = total_cand.scalar() or 0
        pc = profiled_cand.scalar() or 0
        tj = total_jobs.scalar() or 0
        pj = profiled_jobs.scalar() or 0

        return {
            "candidates": {"total": tc, "profiled": pc, "missing": tc - pc},
            "jobs": {"total": tj, "profiled": pj, "missing": tj - pj},
            "coverage_pct": round(
                ((pc + pj) / (tc + tj) * 100) if (tc + tj) > 0 else 0, 1
            ),
        }
