"""Matching Pipeline V3 — Classify First, Match Second.

Komplett-Ueberarbeitung des Matching-Systems. Ersetzt alle bisherigen Engines
(V1, Pre-Score, Smart-Match, V2) durch eine saubere 3-Phasen-Pipeline:

Phase 1: Klassifizierung (existiert bereits — FinanceClassifierService)
         Bestimmt die tatsaechliche Rolle basierend auf Taetigkeiten, nicht Titeln.

Phase 2: Rollen-Gated Filterung (NEU — deterministische SQL)
         Harte Filter: role_compatibility.json + PostGIS 30km.
         Ergebnis: 5-20 Kandidaten pro Job statt 500+.

Phase 3: KI Deep-Evaluation (wiederverwendet aus SmartMatchingService)
         GPT-4o-mini liest vollen Werdegang + volle Stellenbeschreibung.
         Score 0-100 mit konkreter Begruendung.
         Nur Matches >= 50 werden gespeichert.

Design-Prinzipien:
  - Titel luegen, Taetigkeiten definieren die Rolle
  - Distanz 30km = harter Filter, kein Score
  - Rollen-Kompatibilitaet = harter Gate
  - Software (DATEV/SAP) = Praeferenz, kein Ausschluss
  - 5-15 Matches pro Job, alle hochrelevant
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus

# Wiederverwendung des vollstaendigen Branchenwissen-Prompts
from app.services.smart_matching_service import SMART_MATCH_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# KONSTANTEN
# ═══════════════════════════════════════════════════════════════

MAX_DISTANCE_KM = 30
MAX_CANDIDATES_PER_JOB = 20
MIN_AI_SCORE = 0.50  # Nur Matches >= 50% speichern
AI_MODEL = "gpt-4o-mini"

# Normalisierung: Display-Name ↔ Kompatibilitaets-Key
ROLE_DISPLAY_TO_KEY = {
    "Bilanzbuchhalter/in": "bilanzbuchhalter",
    "Finanzbuchhalter/in": "finanzbuchhalter",
    "Kreditorenbuchhalter/in": "kreditorenbuchhalter",
    "Debitorenbuchhalter/in": "debitorenbuchhalter",
    "Lohnbuchhalter/in": "lohnbuchhalter",
    "Steuerfachangestellte/r": "steuerfachangestellte",
}

ROLE_KEY_TO_DISPLAY = {v: k for k, v in ROLE_DISPLAY_TO_KEY.items()}


# ═══════════════════════════════════════════════════════════════
# ROLE COMPATIBILITY LADEN
# ═══════════════════════════════════════════════════════════════

_role_compat_cache: dict | None = None


def _load_role_compatibility() -> dict:
    """Laedt die Rollen-Kompatibilitaetsmatrix (gecached)."""
    global _role_compat_cache
    if _role_compat_cache is not None:
        return _role_compat_cache

    config_path = Path(__file__).parent.parent / "config" / "role_compatibility.json"
    with open(config_path, "r", encoding="utf-8") as f:
        _role_compat_cache = json.load(f)
    return _role_compat_cache


def get_allowed_candidate_roles(job_role_key: str) -> set[str]:
    """Findet alle Kandidaten-Rollen, die auf einen Job-Typ matchen duerfen.

    Reverse-Lookup: Fuer einen gegebenen Job-Rollen-Key, welche
    Kandidaten-Rollen-Keys listen diesen Job in ihren allowed_job_roles?

    Beispiel: job_role_key="finanzbuchhalter"
    → finanzbuchhalter.allowed = [..., "finanzbuchhalter", ...]  ✓
    → bilanzbuchhalter.allowed = [..., "finanzbuchhalter", ...]  ✓
    → steuerfachangestellte.allowed = [..., "finanzbuchhalter", ...] ✓
    → Ergebnis: {finanzbuchhalter, bilanzbuchhalter, steuerfachangestellte}
    """
    compat = _load_role_compatibility()
    allowed = set()
    for cand_role_key, config in compat.items():
        if cand_role_key.startswith("_"):
            continue  # Skip comments
        if job_role_key in config.get("allowed_job_roles", []):
            allowed.add(cand_role_key)
    return allowed


# ═══════════════════════════════════════════════════════════════
# DATENKLASSEN
# ═══════════════════════════════════════════════════════════════


@dataclass
class PipelineResult:
    """Ergebnis der Pipeline fuer einen Job."""

    job_id: UUID
    job_position: str
    job_company: str
    job_role: str | None = None
    phase2_candidates_found: int = 0
    phase3_evaluated: int = 0
    matches_created: int = 0
    matches_updated: int = 0
    matches_skipped_low_score: int = 0
    total_cost_usd: float = 0.0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# PIPELINE SERVICE
# ═══════════════════════════════════════════════════════════════


class MatchingPipelineV3:
    """Classify First, Match Second — 3-Phasen-Pipeline."""

    def __init__(self, db: AsyncSession, api_key: str | None = None):
        self.db = db
        self.api_key = api_key or settings.openai_api_key
        self._client: httpx.AsyncClient | None = None
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://api.openai.com/v1",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(90.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @property
    def total_cost_usd(self) -> float:
        chat_cost = (
            (self._total_input_tokens / 1_000_000) * 0.15
            + (self._total_output_tokens / 1_000_000) * 0.60
        )
        return round(chat_cost, 6)

    # ═══════════════════════════════════════════════════════════════
    # PHASE 2: ROLLEN-GATED FILTERUNG
    # ═══════════════════════════════════════════════════════════════

    def _get_job_role_key(self, job: Job) -> str | None:
        """Extrahiert den normalisierten Rollen-Key eines Jobs.

        Primaerquelle: classification_data.primary_role
        Fallback: hotlist_job_title
        """
        # Primaer: classification_data
        cd = job.classification_data
        if cd and isinstance(cd, dict):
            primary = cd.get("primary_role", "")
            key = ROLE_DISPLAY_TO_KEY.get(primary)
            if key:
                return key

        # Fallback: hotlist_job_title
        if job.hotlist_job_title:
            key = ROLE_DISPLAY_TO_KEY.get(job.hotlist_job_title)
            if key:
                return key

        return None

    async def find_compatible_candidates(
        self,
        job: Job,
    ) -> list[tuple[Candidate, float | None]]:
        """Phase 2: Findet kompatible Kandidaten per SQL.

        Harte Filter:
        1. Kandidat muss FINANCE + klassifiziert sein
        2. Kandidaten-Rolle muss mit Job-Rolle kompatibel sein
        3. Distanz <= 30km (PostGIS), es sei denn Job ist remote
        4. Nicht geloescht/versteckt

        Returns:
            Liste von (Candidate, distance_km) Tupeln
        """
        job_role_key = self._get_job_role_key(job)
        if not job_role_key:
            logger.warning(
                f"Job {job.id} ({job.position}): Keine klassifizierte Rolle — skip"
            )
            return []

        # Kompatible Kandidaten-Rollen
        allowed_cand_keys = get_allowed_candidate_roles(job_role_key)
        if not allowed_cand_keys:
            return []

        # Display-Namen fuer SQL IN-Clause
        allowed_display_roles = []
        for key in allowed_cand_keys:
            display = ROLE_KEY_TO_DISPLAY.get(key)
            if display:
                allowed_display_roles.append(display)

        if not allowed_display_roles:
            return []

        # SQL-Bedingungen
        conditions = [
            Candidate.hotlist_category == "FINANCE",
            Candidate.deleted_at.is_(None),
            Candidate.hidden == False,  # noqa: E712
            Candidate.classification_data.isnot(None),
            or_(
                *[
                    Candidate.hotlist_job_title == role
                    for role in allowed_display_roles
                ]
            ),
        ]

        # Distanz: Hard-Filter 30km (ausser Remote-Jobs)
        job_is_remote = getattr(job, "work_arrangement", None) == "remote"
        has_coords = job.location_coords is not None

        if has_coords and not job_is_remote:
            conditions.append(
                Candidate.address_coords.isnot(None),
            )
            conditions.append(
                func.ST_DWithin(
                    Candidate.address_coords,
                    job.location_coords,
                    MAX_DISTANCE_KM * 1000,  # Meter
                    True,  # use_spheroid
                ),
            )

        # Distanz-Berechnung (fuer Sortierung + Anzeige)
        if has_coords:
            distance_expr = (
                func.ST_Distance(
                    Candidate.address_coords,
                    job.location_coords,
                    True,  # use_spheroid
                )
                / 1000.0
            ).label("distance_km")
        else:
            from sqlalchemy.sql.expression import literal_column

            distance_expr = literal_column("NULL::float").label("distance_km")

        query = (
            select(Candidate, distance_expr)
            .where(and_(*conditions))
            .order_by(distance_expr.asc().nullslast())
            .limit(MAX_CANDIDATES_PER_JOB)
        )

        result = await self.db.execute(query)
        rows = result.all()

        candidates = []
        for candidate, dist_km in rows:
            distance = round(dist_km, 1) if dist_km is not None else None
            candidates.append((candidate, distance))

        return candidates

    # ═══════════════════════════════════════════════════════════════
    # PHASE 3: KI DEEP-EVALUATION
    # ═══════════════════════════════════════════════════════════════

    def _build_user_prompt(self, job: Job, candidate: Candidate) -> str:
        """Baut den User-Prompt fuer GPT.

        KRITISCH: KEINE DATEN ABSCHNEIDEN.
        Voller Stellentext + voller Werdegang.

        Wiederverwendung des Patterns aus SmartMatchingService.
        """
        # === JOB-TEIL ===
        job_text = job.job_text or "Keine Stellenbeschreibung vorhanden"

        # Job-Klassifizierung
        job_cd = job.classification_data or {}
        job_classified_role = job_cd.get("primary_role", "Nicht klassifiziert")
        job_quality = job_cd.get("quality_score", "unbekannt")

        job_section = f"""═══ STELLENANGEBOT ═══
Position: {job.position}
Unternehmen: {job.company_name}
Branche: {job.industry or 'Nicht angegeben'}
Standort: {job.city or 'Nicht angegeben'}
Beschaeftigungsart: {job.employment_type or 'Nicht angegeben'}
Klassifizierte Rolle: {job_classified_role}
Qualitaet: {job_quality}
Klassifizierte Rollen: {', '.join(job.hotlist_job_titles) if job.hotlist_job_titles else 'Nicht klassifiziert'}

Stellenbeschreibung:
{job_text}"""

        # === KANDIDAT-TEIL ===

        # Werdegang MIT kompletten Taetigkeitsbeschreibungen
        work_history = candidate.work_history or []
        work_lines = []
        for i, entry in enumerate(work_history):
            if not isinstance(entry, dict):
                continue
            position = entry.get("position", "Position unbekannt")
            company = entry.get("company", "Firma unbekannt")
            start = entry.get("start_date", "?")
            end = entry.get("end_date", "aktuell")
            desc = entry.get("description", "")

            work_lines.append(f"\n--- Station {i + 1} ---")
            work_lines.append(f"Position: {position}")
            work_lines.append(f"Unternehmen: {company}")
            work_lines.append(f"Zeitraum: {start} bis {end}")
            if desc:
                work_lines.append(f"Taetigkeiten:\n{desc}")

        work_text = "\n".join(work_lines) if work_lines else "Kein Werdegang vorhanden"

        # Ausbildung
        education = candidate.education or []
        edu_lines = []
        for entry in education:
            if isinstance(entry, dict):
                parts = [
                    p
                    for p in [
                        entry.get("degree", ""),
                        entry.get("field_of_study", ""),
                        entry.get("institution", ""),
                    ]
                    if p
                ]
                if parts:
                    edu_lines.append("- " + ", ".join(parts))
        edu_text = "\n".join(edu_lines) if edu_lines else "Keine Angaben"

        # Weiterbildungen
        further_edu = candidate.further_education or []
        further_lines = []
        for entry in further_edu:
            if isinstance(entry, dict):
                parts = [
                    p
                    for p in [
                        entry.get("title", entry.get("name", "")),
                        entry.get("institution", entry.get("provider", "")),
                    ]
                    if p
                ]
                if parts:
                    further_lines.append("- " + ", ".join(parts))
            elif isinstance(entry, str) and entry.strip():
                further_lines.append(f"- {entry}")
        further_text = "\n".join(further_lines) if further_lines else "Keine Angaben"

        # Skills & IT
        skills_text = ", ".join(candidate.skills) if candidate.skills else "Keine"
        it_text = ", ".join(candidate.it_skills) if candidate.it_skills else "Keine"
        erp_text = ", ".join(candidate.erp) if candidate.erp else "Keine"

        # Sprachen
        languages = candidate.languages or []
        lang_parts = []
        for entry in languages:
            if isinstance(entry, dict):
                lang_parts.append(
                    f"{entry.get('language', '?')} ({entry.get('level', '?')})"
                )
            elif isinstance(entry, str):
                lang_parts.append(entry)
        lang_text = ", ".join(lang_parts) if lang_parts else "Keine"

        # Kandidaten-Klassifizierung
        cand_cd = candidate.classification_data or {}
        cand_role = cand_cd.get("primary_role", "Nicht klassifiziert")
        titles_text = (
            ", ".join(candidate.hotlist_job_titles)
            if candidate.hotlist_job_titles
            else "Nicht klassifiziert"
        )

        # CV-Fallback
        cv_fallback = ""
        if not work_lines and candidate.cv_text:
            cv_fallback = f"\n\nCV-Volltext (kein strukturierter Werdegang):\n{candidate.cv_text}"

        candidate_section = f"""═══ KANDIDAT ═══
Aktuelle Position: {candidate.current_position or 'Nicht angegeben'}
Aktuelles Unternehmen: {candidate.current_company or 'Nicht angegeben'}
Wohnort: {candidate.city or 'Nicht angegeben'}
Klassifizierte Rolle: {cand_role}
Alle Rollen: {titles_text}

Skills: {skills_text}
IT-Kenntnisse: {it_text}
ERP-Systeme: {erp_text}
Sprachen: {lang_text}

Berufserfahrung (chronologisch, neueste zuerst):
{work_text}

Ausbildung:
{edu_text}

Weiterbildungen / Zertifikate:
{further_text}{cv_fallback}"""

        return f"""{job_section}

{candidate_section}

═══ AUFGABE ═══
Bewerte die Passung zwischen diesem Kandidaten und der Stelle.
Pruefe ZUERST: Handelt es sich bei den Taetigkeiten um eigenstaendige Erstellung oder nur Mitwirkung/Zuarbeit?
Pruefe die Software-Passung: DATEV vs. SAP vs. andere.
Bewerte realistisch — kein Wunschdenken."""

    async def _deep_ai_evaluate(
        self, job: Job, candidate: Candidate
    ) -> dict:
        """GPT-4o-mini Bewertung eines Kandidaten gegen einen Job.

        Returns:
            {"score", "explanation", "strengths", "weaknesses", "risks", "success", "error"}
        """
        if not self.api_key:
            return {
                "score": 0.0,
                "explanation": "OpenAI nicht konfiguriert",
                "strengths": [],
                "weaknesses": [],
                "risks": [],
                "success": False,
                "error": "API-Key fehlt",
            }

        user_prompt = self._build_user_prompt(job, candidate)

        try:
            client = await self._get_client()
            response = await client.post(
                "/chat/completions",
                json={
                    "model": AI_MODEL,
                    "messages": [
                        {"role": "system", "content": SMART_MATCH_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 1000,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            result = response.json()

            usage = result.get("usage", {})
            self._total_input_tokens += usage.get("prompt_tokens", 0)
            self._total_output_tokens += usage.get("completion_tokens", 0)

            content = result["choices"][0]["message"]["content"]
            parsed = json.loads(content)

            return {
                "score": min(1.0, max(0.0, float(parsed.get("score", 0.5)))),
                "explanation": parsed.get("explanation", "Keine Erklaerung"),
                "strengths": parsed.get("strengths", [])[:3],
                "weaknesses": parsed.get("weaknesses", [])[:3],
                "risks": parsed.get("risks", [])[:3],
                "success": True,
                "error": None,
            }

        except httpx.TimeoutException:
            logger.warning(f"V3 AI Timeout: {candidate.full_name} ↔ {job.position}")
            return {
                "score": 0.0, "explanation": "KI-Bewertung: Timeout",
                "strengths": [], "weaknesses": [], "risks": [],
                "success": False, "error": "Timeout",
            }
        except json.JSONDecodeError as e:
            logger.error(f"V3 AI JSON-Fehler: {e}")
            return {
                "score": 0.0, "explanation": "KI-Bewertung: Ungueltige Antwort",
                "strengths": [], "weaknesses": [], "risks": [],
                "success": False, "error": f"JSON: {e}",
            }
        except Exception as e:
            logger.error(f"V3 AI Fehler: {e}")
            return {
                "score": 0.0, "explanation": f"KI-Fehler: {str(e)[:100]}",
                "strengths": [], "weaknesses": [], "risks": [],
                "success": False, "error": str(e),
            }

    # ═══════════════════════════════════════════════════════════════
    # MATCH-RECORD ERSTELLEN / AKTUALISIEREN
    # ═══════════════════════════════════════════════════════════════

    async def _upsert_match(
        self,
        job: Job,
        candidate: Candidate,
        distance_km: float | None,
        ai_result: dict,
    ) -> str:
        """Erstellt oder aktualisiert einen Match-Record.

        Returns:
            "created", "updated", oder "skipped"
        """
        score = ai_result.get("score", 0.0)

        # Nur Matches >= MIN_AI_SCORE speichern
        if score < MIN_AI_SCORE:
            return "skipped"

        existing = await self.db.execute(
            select(Match).where(
                and_(
                    Match.job_id == job.id,
                    Match.candidate_id == candidate.id,
                )
            )
        )
        match = existing.scalar_one_or_none()
        now = datetime.now(timezone.utc)

        if match:
            match.distance_km = distance_km
            match.ai_score = score
            match.ai_explanation = ai_result.get("explanation", "")
            match.ai_strengths = ai_result.get("strengths", [])
            match.ai_weaknesses = ai_result.get("weaknesses", [])
            match.ai_checked_at = now
            match.matching_method = "pipeline_v3"
            match.pre_score = round(score * 100, 1)
            if match.status == MatchStatus.NEW:
                match.status = MatchStatus.AI_CHECKED
            match.stale = False
            match.stale_reason = None
            match.stale_since = None
            return "updated"
        else:
            new_match = Match(
                job_id=job.id,
                candidate_id=candidate.id,
                distance_km=distance_km,
                ai_score=score,
                ai_explanation=ai_result.get("explanation", ""),
                ai_strengths=ai_result.get("strengths", []),
                ai_weaknesses=ai_result.get("weaknesses", []),
                ai_checked_at=now,
                status=MatchStatus.AI_CHECKED,
                matching_method="pipeline_v3",
                pre_score=round(score * 100, 1),
                stale=False,
            )
            self.db.add(new_match)
            return "created"

    # ═══════════════════════════════════════════════════════════════
    # PIPELINE: EINEN JOB MATCHEN
    # ═══════════════════════════════════════════════════════════════

    async def run_for_job(
        self,
        job_id: UUID,
        progress_callback=None,
    ) -> PipelineResult:
        """Volle 3-Phasen-Pipeline fuer einen Job.

        1. Lade Job + pruefe Klassifizierung
        2. Phase 2: Finde kompatible Kandidaten (SQL)
        3. Phase 3: KI-Bewertung fuer jeden
        4. Speichere Matches >= 50%
        """
        start = time.time()

        job = await self.db.get(Job, job_id)
        if not job:
            return PipelineResult(
                job_id=job_id, job_position="?", job_company="?",
                errors=["Job nicht gefunden"],
            )

        result = PipelineResult(
            job_id=job_id,
            job_position=job.position,
            job_company=job.company_name,
        )

        # Pruefen: Job muss klassifiziert sein
        job_role_key = self._get_job_role_key(job)
        if not job_role_key:
            result.errors.append("Job nicht klassifiziert (keine Rolle)")
            result.duration_seconds = round(time.time() - start, 1)
            return result

        result.job_role = job_role_key

        # Quality-Gate
        cd = job.classification_data or {}
        if cd.get("quality_score") == "low":
            result.errors.append("Job-Qualitaet zu niedrig (low)")
            result.duration_seconds = round(time.time() - start, 1)
            return result

        # Geloescht?
        if job.deleted_at is not None:
            result.errors.append("Job ist geloescht")
            result.duration_seconds = round(time.time() - start, 1)
            return result

        if progress_callback:
            progress_callback(
                "phase2",
                f"Suche kompatible Kandidaten fuer {job.position} ({job_role_key})...",
            )

        # ── Phase 2: Rollen-Gated Filterung ──
        candidates = await self.find_compatible_candidates(job)
        result.phase2_candidates_found = len(candidates)

        if not candidates:
            if progress_callback:
                progress_callback("done", "Keine kompatiblen Kandidaten gefunden")
            result.duration_seconds = round(time.time() - start, 1)
            return result

        if progress_callback:
            progress_callback(
                "phase3",
                f"{len(candidates)} Kandidaten gefunden — starte KI-Bewertung...",
            )

        # ── Phase 3: KI Deep-Evaluation ──
        for i, (candidate, distance_km) in enumerate(candidates):
            try:
                if progress_callback:
                    progress_callback(
                        "phase3",
                        f"Bewerte {i + 1}/{len(candidates)}: {candidate.full_name}",
                    )

                ai_result = await self._deep_ai_evaluate(job, candidate)
                result.phase3_evaluated += 1

                action = await self._upsert_match(job, candidate, distance_km, ai_result)
                if action == "created":
                    result.matches_created += 1
                elif action == "updated":
                    result.matches_updated += 1
                elif action == "skipped":
                    result.matches_skipped_low_score += 1

            except Exception as e:
                logger.error(f"V3 Fehler: {candidate.id}: {e}")
                result.errors.append(f"{candidate.full_name}: {str(e)[:100]}")

        await self.db.commit()

        result.total_cost_usd = self.total_cost_usd
        result.duration_seconds = round(time.time() - start, 1)

        if progress_callback:
            progress_callback(
                "done",
                f"Fertig! {result.matches_created} neue + {result.matches_updated} "
                f"aktualisierte Matches (Kosten: ~${result.total_cost_usd:.3f})",
            )

        logger.info(
            f"V3 Pipeline '{job.position}': "
            f"{result.phase2_candidates_found} Kandidaten → "
            f"{result.matches_created} neue Matches, "
            f"{result.matches_skipped_low_score} unter Schwelle, "
            f"Dauer: {result.duration_seconds}s, "
            f"Kosten: ~${result.total_cost_usd:.4f}"
        )

        return result

    # ═══════════════════════════════════════════════════════════════
    # PIPELINE: EINEN KANDIDATEN MATCHEN (reverse)
    # ═══════════════════════════════════════════════════════════════

    async def run_for_candidate(
        self,
        candidate_id: UUID,
        progress_callback=None,
    ) -> dict:
        """Reverse-Pipeline: Matcht einen Kandidaten gegen alle kompatiblen Jobs.

        Nützlich nach Kandidaten-Erstellung oder -Aktualisierung.
        """
        start = time.time()

        candidate = await self.db.get(Candidate, candidate_id)
        if not candidate:
            return {"error": "Kandidat nicht gefunden", "matches_created": 0}

        if candidate.hidden or candidate.deleted_at:
            return {"error": "Kandidat versteckt/geloescht", "matches_created": 0}

        if candidate.hotlist_category != "FINANCE":
            return {"error": "Kein FINANCE-Kandidat", "matches_created": 0}

        if not candidate.classification_data:
            return {"error": "Kandidat nicht klassifiziert", "matches_created": 0}

        # Kandidaten-Rolle
        cand_role_key = ROLE_DISPLAY_TO_KEY.get(candidate.hotlist_job_title or "")
        if not cand_role_key:
            return {"error": "Keine normalisierte Rolle", "matches_created": 0}

        # Kompatible Job-Rollen
        compat = _load_role_compatibility()
        cand_config = compat.get(cand_role_key, {})
        allowed_job_keys = cand_config.get("allowed_job_roles", [])

        if not allowed_job_keys:
            return {"error": "Keine kompatiblen Job-Rollen", "matches_created": 0}

        # Display-Namen
        allowed_job_displays = [
            ROLE_KEY_TO_DISPLAY.get(k)
            for k in allowed_job_keys
            if ROLE_KEY_TO_DISPLAY.get(k)
        ]

        # Jobs finden
        conditions = [
            Job.hotlist_category == "FINANCE",
            Job.deleted_at.is_(None),
            Job.classification_data.isnot(None),
            or_(*[Job.hotlist_job_title == r for r in allowed_job_displays]),
        ]

        # Distanz
        has_cand_coords = candidate.address_coords is not None
        if has_cand_coords:
            conditions.append(Job.location_coords.isnot(None))
            conditions.append(
                func.ST_DWithin(
                    Job.location_coords,
                    candidate.address_coords,
                    MAX_DISTANCE_KM * 1000,
                    True,
                )
            )

            distance_expr = (
                func.ST_Distance(
                    Job.location_coords, candidate.address_coords, True
                )
                / 1000.0
            ).label("distance_km")
        else:
            from sqlalchemy.sql.expression import literal_column
            distance_expr = literal_column("NULL::float").label("distance_km")

        query = (
            select(Job, distance_expr)
            .where(and_(*conditions))
            .order_by(distance_expr.asc().nullslast())
            .limit(30)  # Max 30 Jobs pro Kandidat
        )

        jobs_result = await self.db.execute(query)
        jobs = jobs_result.all()

        stats = {
            "candidate": candidate.full_name,
            "candidate_role": cand_role_key,
            "jobs_found": len(jobs),
            "matches_created": 0,
            "matches_updated": 0,
            "matches_skipped": 0,
            "errors": [],
        }

        for job, dist_km in jobs:
            try:
                distance = round(dist_km, 1) if dist_km is not None else None
                ai_result = await self._deep_ai_evaluate(job, candidate)
                action = await self._upsert_match(job, candidate, distance, ai_result)
                if action == "created":
                    stats["matches_created"] += 1
                elif action == "updated":
                    stats["matches_updated"] += 1
                elif action == "skipped":
                    stats["matches_skipped"] += 1
            except Exception as e:
                stats["errors"].append(f"{job.position}: {str(e)[:100]}")

        await self.db.commit()

        stats["cost_usd"] = self.total_cost_usd
        stats["duration_seconds"] = round(time.time() - start, 1)

        logger.info(
            f"V3 Reverse fuer '{candidate.full_name}': "
            f"{len(jobs)} Jobs → {stats['matches_created']} Matches"
        )

        return stats

    # ═══════════════════════════════════════════════════════════════
    # BATCH: ALLE FINANCE-JOBS MATCHEN
    # ═══════════════════════════════════════════════════════════════

    async def run_all(
        self,
        skip_already_matched: bool = True,
        progress_callback=None,
    ) -> dict:
        """Batch-Lauf: Pipeline V3 fuer alle aktiven Finance-Jobs.

        Args:
            skip_already_matched: Wenn True, Jobs die bereits pipeline_v3
                                  Matches haben werden uebersprungen.
            progress_callback: Optional callback(step, detail)
        """
        # Alle aktiven FINANCE-Jobs
        query = (
            select(Job.id, Job.position, Job.company_name)
            .where(
                and_(
                    Job.hotlist_category == "FINANCE",
                    Job.deleted_at.is_(None),
                    Job.classification_data.isnot(None),
                )
            )
            .order_by(Job.created_at.desc())
        )

        if skip_already_matched:
            already_matched = (
                select(Match.job_id)
                .where(Match.matching_method == "pipeline_v3")
                .group_by(Match.job_id)
            )
            query = query.where(Job.id.notin_(already_matched))

        result = await self.db.execute(query)
        jobs = result.all()

        stats = {
            "total_jobs": len(jobs),
            "jobs_matched": 0,
            "jobs_skipped": 0,
            "jobs_failed": 0,
            "total_matches_created": 0,
            "total_matches_updated": 0,
            "total_candidates_evaluated": 0,
            "total_cost_usd": 0.0,
            "errors": [],
        }

        if not jobs:
            if progress_callback:
                progress_callback("done", "Keine ungematchten Finance-Jobs gefunden")
            return stats

        if progress_callback:
            progress_callback("init", f"{len(jobs)} Jobs zu matchen...")

        for i, (job_id, position, company) in enumerate(jobs):
            try:
                if progress_callback:
                    progress_callback(
                        "matching",
                        f"Job {i + 1}/{len(jobs)}: {position} ({company})",
                    )

                job_result = await self.run_for_job(job_id)

                if job_result.errors and not job_result.matches_created:
                    stats["jobs_skipped"] += 1
                else:
                    stats["jobs_matched"] += 1

                stats["total_matches_created"] += job_result.matches_created
                stats["total_matches_updated"] += job_result.matches_updated
                stats["total_candidates_evaluated"] += job_result.phase3_evaluated
                stats["total_cost_usd"] = self.total_cost_usd

                if job_result.errors:
                    stats["errors"].extend(job_result.errors[:2])

            except Exception as e:
                logger.error(f"V3 Batch Fehler Job {job_id}: {e}")
                stats["jobs_failed"] += 1
                stats["errors"].append(f"{position}: {str(e)[:100]}")
                try:
                    await self.db.rollback()
                except Exception:
                    pass

        if progress_callback:
            progress_callback(
                "done",
                f"Fertig! {stats['jobs_matched']}/{stats['total_jobs']} Jobs, "
                f"{stats['total_matches_created']} Matches, "
                f"Kosten: ~${stats['total_cost_usd']:.2f}",
            )

        logger.info(
            f"V3 Batch: {stats['jobs_matched']}/{stats['total_jobs']} Jobs, "
            f"{stats['total_matches_created']} Matches, "
            f"Kosten: ~${stats['total_cost_usd']:.3f}"
        )

        return stats

    # ═══════════════════════════════════════════════════════════════
    # CONTEXT MANAGER
    # ═══════════════════════════════════════════════════════════════

    async def __aenter__(self) -> "MatchingPipelineV3":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
