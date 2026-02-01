"""Pre-Scoring Service v3 — Mit KI-Kalibrierung.

Berechnet Pre-Scores fuer Kandidat-Job-Matches anhand von:
1. Rollen-Aehnlichkeit (35 Pkt) — Matrix-basiert, primaer vs. sekundaer
2. Keyword-Match (25 Pkt) — Absolute Anzahl, inline berechnet
3. Distanz (15 Pkt) — Kontinuierlich, naeher = besser
4. Kategorie (15 Pkt) — Gate: bei Mismatch wird Gesamt gedeckelt
5. Stadt (10 Pkt) — Abgestuft: gleich / Metropolregion / anders

NEU in v3 — Kalibrierung:
- Rollen-Matrix-Werte werden durch AI-gelernte Overrides ueberschrieben
- Power-Keywords zaehlen doppelt, Penalty-Keywords halb
- Ausschluss-Paare werden erkannt und mit Score 0 bewertet

Der Pre-Score ist KOSTENLOS (kein OpenAI) und dient als Vorfilter
vor dem teuren DeepMatch (OpenAI).

Score-Bereich: 0.0 – 100.0
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match
from app.services.categorization_service import HotlistCategory
from app.services.keyword_matcher import keyword_matcher

logger = logging.getLogger(__name__)


# ===================================================================
# GEWICHTUNGEN (Summe = 100)
# ===================================================================

WEIGHT_ROLE_SIMILARITY: float = 35.0   # Rollen-Aehnlichkeit via Matrix
WEIGHT_KEYWORDS: float = 25.0          # Keyword-Match (absolute Anzahl)
WEIGHT_DISTANCE: float = 15.0          # Distanz (naeher = besser)
WEIGHT_CATEGORY: float = 15.0          # Kategorie-Gate
WEIGHT_CITY: float = 10.0              # Stadt (abgestuft)

# Wenn Kategorien nicht uebereinstimmen: Gesamt-Score gedeckelt
CATEGORY_MISMATCH_CAP: float = 10.0

# Abzug fuer Nebentitel (40% Penalty)
SECONDARY_TITLE_PENALTY: float = 0.6


# ===================================================================
# FINANCE ROLLEN-AEHNLICHKEITSMATRIX
# ===================================================================
# Trainiert aus fachlichem Wissen:
# - Bilanz <-> Fibu (0.80): Bilu = Fibu + Qualifikation + eigenstaendige Erstellung
# - Fibu <-> Kredi (0.60): Fibu macht ganzheitliche Buchhaltung (Kredi+Debi)
# - Fibu <-> Debi (0.60): Debi ist Teilbereich von Fibu
# - Kredi <-> Debi (0.50): Beide Sub-Ledger, unterschiedliche Seite
# - Lohn <-> alle (0.00-0.10): Komplett anderes Fachgebiet!
# - SteuFa <-> Fibu/Bilu (0.50-0.55): SteuFa wird immer mit Fibu/Bilu gepaart

_FINANCE_ROLES = [
    "Bilanzbuchhalter/in",
    "Finanzbuchhalter/in",
    "Kreditorenbuchhalter/in",
    "Debitorenbuchhalter/in",
    "Lohnbuchhalter/in",
    "Steuerfachangestellte/r",
]

_FINANCE_MATRIX = [
    # Bilanz  Fibu   Kredi  Debi   Lohn   SteuFa
    [1.00,   0.80,  0.40,  0.40,  0.05,  0.50],   # Bilanzbuchhalter/in
    [0.80,   1.00,  0.60,  0.60,  0.05,  0.55],   # Finanzbuchhalter/in
    [0.40,   0.60,  1.00,  0.50,  0.00,  0.20],   # Kreditorenbuchhalter/in
    [0.40,   0.60,  0.50,  1.00,  0.00,  0.20],   # Debitorenbuchhalter/in
    [0.05,   0.05,  0.00,  0.00,  1.00,  0.10],   # Lohnbuchhalter/in
    [0.50,   0.55,  0.20,  0.20,  0.10,  1.00],   # Steuerfachangestellte/r
]

# Baue Dict fuer schnellen Lookup: (role_a, role_b) -> similarity
FINANCE_ROLE_SIMILARITY: dict[tuple[str, str], float] = {}
for i, r1 in enumerate(_FINANCE_ROLES):
    for j, r2 in enumerate(_FINANCE_ROLES):
        FINANCE_ROLE_SIMILARITY[(r1, r2)] = _FINANCE_MATRIX[i][j]


# ===================================================================
# METROPOLREGIONEN (fuer Stadt-Scoring)
# ===================================================================

METRO_AREAS: dict[str, set[str]] = {
    "muenchen": {
        "muenchen", "münchen", "garching", "unterhaching", "ottobrunn",
        "ismaning", "unterschleissheim", "unterschleißheim", "haar",
        "grasbrunn", "aschheim", "feldkirchen", "oberhaching",
        "taufkirchen", "pullach", "gruenwald", "grünwald",
    },
    "frankfurt": {
        "frankfurt", "offenbach", "eschborn", "bad homburg",
        "neu-isenburg", "oberursel", "kronberg", "dreieich",
        "langen", "dietzenbach",
    },
    "hamburg": {
        "hamburg", "norderstedt", "pinneberg", "wedel",
        "ahrensburg", "reinbek", "schenefeld",
    },
    "berlin": {
        "berlin", "potsdam", "teltow", "kleinmachnow",
        "bernau", "falkensee",
    },
    "stuttgart": {
        "stuttgart", "esslingen", "ludwigsburg", "boeblingen",
        "böblingen", "sindelfingen", "fellbach", "waiblingen",
        "leinfelden-echterdingen",
    },
    "koeln_duesseldorf": {
        "koeln", "köln", "duesseldorf", "düsseldorf", "leverkusen",
        "bonn", "bergisch gladbach", "neuss", "ratingen", "dormagen",
    },
    "nuernberg": {
        "nuernberg", "nürnberg", "fuerth", "fürth", "erlangen",
        "schwabach",
    },
}


# ===================================================================
# DATENKLASSEN
# ===================================================================

@dataclass
class PreScoreBreakdown:
    """Detaillierte Aufschluesselung des Pre-Scores."""

    # Gewichtete Punkte (fuer Anzeige)
    category_score: float       # 0 - 15
    city_score: float           # 0 - 10
    role_similarity_score: float  # 0 - 35
    keyword_score: float        # 0 - 25
    distance_score: float       # 0 - 15

    # Diagnostik
    matched_role: str | None = None      # Welcher Kandidaten-Titel am besten passt
    role_match_type: str = "none"        # "primary", "secondary", "none"
    keyword_count: int = 0               # Anzahl gematchter Keywords

    # Gesamt
    total: float = 0.0

    @property
    def is_good_match(self) -> bool:
        """Pre-Score >= 50 ist ein guter Match."""
        return self.total >= 50.0


@dataclass
class PreScoringResult:
    """Ergebnis einer Batch Pre-Scoring Operation."""
    total_matches: int
    scored: int
    skipped: int
    avg_score: float


# ===================================================================
# SERVICE
# ===================================================================

class PreScoringService:
    """
    Berechnet Pre-Scores fuer Kandidat-Job-Matches.

    Verwendet nur lokale Daten (keine API-Calls).
    5 Komponenten: Rollen-Aehnlichkeit + Keywords + Distanz + Kategorie + Stadt.

    Optionale Kalibrierung aus DeepMatch-Ergebnissen:
    - Rollen-Matrix-Overrides (AI-gelernte Werte)
    - Power-Keywords (zaehlen doppelt)
    - Ausschluss-Paare (Score = 0)
    """

    def __init__(self, db: AsyncSession, calibration_data=None):
        self.db = db
        # Kalibrierungsdaten (CalibrationResult oder None)
        self._calibration = calibration_data
        # Aufbereitete Caches fuer schnellen Zugriff
        self._role_overrides: dict[tuple[str, str], float] = {}
        self._keyword_weights: dict[str, float] = {}
        self._exclusion_set: set[tuple[str, str]] = set()
        if calibration_data:
            self._apply_calibration(calibration_data)

    def _apply_calibration(self, cal) -> None:
        """Bereitet Kalibrierungsdaten fuer schnellen Zugriff auf."""
        # Rollen-Overrides: "Fibu|Kredi" → (Fibu, Kredi) = 0.45
        overrides = getattr(cal, 'role_matrix_overrides', None) or {}
        if isinstance(overrides, dict):
            for key, value in overrides.items():
                parts = key.split("|")
                if len(parts) == 2:
                    self._role_overrides[(parts[0], parts[1])] = value

        # Keyword-Gewichte: {"datev": 2.0, "sap": 0.5}
        weights = getattr(cal, 'keyword_weight_boost', None) or {}
        if isinstance(weights, dict):
            self._keyword_weights = {k.lower(): v for k, v in weights.items()}

        # Ausschluss-Paare
        exclusions = getattr(cal, 'exclusion_pairs', None) or []
        for pair in exclusions:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                self._exclusion_set.add((pair[0], pair[1]))

        if self._role_overrides or self._keyword_weights or self._exclusion_set:
            logger.info(
                f"Kalibrierung geladen: {len(self._role_overrides)} Rollen-Overrides, "
                f"{len(self._keyword_weights)} Keyword-Gewichte, "
                f"{len(self._exclusion_set)} Ausschluss-Paare"
            )

    async def load_calibration(self) -> bool:
        """Laedt Kalibrierungsdaten aus der DB (lazy loading)."""
        if self._calibration is not None:
            return True  # Bereits geladen

        try:
            from app.services.calibration_service import CalibrationService
            cal = await CalibrationService.load_calibration_data(self.db)
            if cal:
                self._calibration = cal
                self._apply_calibration(cal)
                return True
        except Exception as e:
            logger.debug(f"Keine Kalibrierungsdaten: {e}")
        return False

    # --------------------------------------------------
    # Komponente 1: Rollen-Aehnlichkeit (35 Punkte)
    # --------------------------------------------------

    def _calculate_role_similarity(
        self,
        candidate: Candidate, job: Job,
    ) -> tuple[float, str | None, str]:
        """
        Berechnet die Rollen-Aehnlichkeit zwischen Kandidat und Job.

        Verwendet die FINANCE_ROLE_SIMILARITY Matrix.
        Kalibrierungs-Overrides haben Vorrang vor der Standard-Matrix.
        Primaertitel zaehlt voll, Nebentitel mit 40% Abzug.

        Returns:
            (score 0.0-1.0, matched_role, match_type)
        """
        job_primary = (job.hotlist_job_title or "").strip()
        if not job_primary:
            return 0.0, None, "none"

        cand_primary = (candidate.hotlist_job_title or "").strip()
        cand_all = candidate.hotlist_job_titles or []
        if not cand_all and cand_primary:
            cand_all = [cand_primary]

        if not cand_all:
            return 0.0, None, "none"

        best_score = 0.0
        best_role = None
        best_type = "none"

        for i, cand_title in enumerate(cand_all):
            if not cand_title:
                continue

            is_primary = (cand_title == cand_primary) or (i == 0)
            multiplier = 1.0 if is_primary else SECONDARY_TITLE_PENALTY

            # Pruefe zuerst Kalibrierungs-Overrides (AI-gelernt)
            override_sim = self._role_overrides.get((job_primary, cand_title))
            if override_sim is not None:
                matrix_sim = override_sim
            else:
                # Fallback: Standard-Matrix
                matrix_sim = FINANCE_ROLE_SIMILARITY.get((job_primary, cand_title))

            if matrix_sim is not None:
                score = multiplier * matrix_sim
            else:
                # Fallback fuer ENGINEERING oder unbekannte Rollen:
                # Exakter String-Vergleich
                if cand_title.lower() == job_primary.lower():
                    score = multiplier * 1.0
                else:
                    score = 0.0

            if score > best_score:
                best_score = score
                best_role = cand_title
                best_type = "primary" if is_primary else "secondary"

        return min(best_score, 1.0), best_role, best_type

    # --------------------------------------------------
    # Komponente 2: Keyword-Match (25 Punkte)
    # --------------------------------------------------

    def _calculate_keyword_score(
        self,
        candidate: Candidate, job: Job, match: Match,
    ) -> tuple[float, int]:
        """
        Berechnet den Keyword-Score basierend auf absoluter Anzahl.

        KRITISCH: Pre-Matches haben oft keyword_score = NULL.
        In dem Fall berechnen wir Keywords inline.

        Kalibrierung: Power-Keywords zaehlen doppelt, Penalty-Keywords halb.
        Effektiver Count wird statt absolutem Count verwendet.

        Skalierung nach Anzahl (nicht Ratio):
        0 = 0.0, 1 = 0.15, 2 = 0.30, ... 5 = 0.75, 6-7 = 0.85, 8+ = 1.0

        Returns:
            (score 0.0-1.0, keyword_count)
        """
        matched_keywords = match.matched_keywords or []

        # Wenn keine Keywords vorhanden: inline berechnen
        if not matched_keywords and job.job_text:
            candidate_skills = candidate.skills or []
            if candidate_skills:
                result = keyword_matcher.match(candidate_skills, job.job_text)
                matched_keywords = result.matched_keywords
                # Auf Match-Objekt speichern fuer spaetere Nutzung
                match.keyword_score = result.keyword_score
                match.matched_keywords = result.matched_keywords

        raw_count = len(matched_keywords)

        if raw_count == 0:
            return 0.0, 0

        # Kalibrierung: Gewichteter Count (Power x2, Penalty x0.5)
        if self._keyword_weights:
            weighted_count = 0.0
            for kw in matched_keywords:
                weight = self._keyword_weights.get(kw.strip().lower(), 1.0)
                weighted_count += weight
            count = weighted_count
        else:
            count = float(raw_count)

        if count <= 0:
            return 0.0, raw_count
        elif count <= 5:
            # Linear: 1=0.15, 2=0.30, 3=0.45, 4=0.60, 5=0.75
            score = count * 0.15
        elif count <= 7:
            # 6=0.80, 7=0.85
            score = 0.75 + (count - 5) * 0.05
        else:
            # 8+ = voll
            score = 1.0

        return min(score, 1.0), raw_count

    # --------------------------------------------------
    # Komponente 3: Distanz (15 Punkte)
    # --------------------------------------------------

    @staticmethod
    def _calculate_distance_score(match: Match) -> float:
        """
        Berechnet den Distanz-Score (naeher = besser).

        <=5km = 1.0, 5-15km = 0.8->0.5, 15-30km = 0.5->0.0, >30km = 0.0

        Returns:
            score 0.0-1.0
        """
        if match.distance_km is None:
            return 0.0

        km = match.distance_km
        if km <= 5:
            return 1.0
        elif km <= 15:
            # Linear: 5km=0.8, 15km=0.5
            return 0.8 - (km - 5) * 0.03
        elif km <= 30:
            # Linear: 15km=0.5, 30km=0.0
            return 0.5 - (km - 15) * (0.5 / 15)
        else:
            return 0.0

    # --------------------------------------------------
    # Komponente 4: Kategorie (15 Punkte, Gate)
    # --------------------------------------------------

    @staticmethod
    def _calculate_category_score(
        candidate: Candidate, job: Job,
    ) -> float:
        """
        Kategorie-Gate: Gleiche Kategorie = 1.0, sonst 0.0.
        Bei 0.0 wird der Gesamt-Score auf max 10 gedeckelt.

        Returns:
            0.0 oder 1.0
        """
        if (
            candidate.hotlist_category
            and job.hotlist_category
            and candidate.hotlist_category == job.hotlist_category
            and candidate.hotlist_category != HotlistCategory.SONSTIGE
        ):
            return 1.0
        return 0.0

    # --------------------------------------------------
    # Komponente 5: Stadt (10 Punkte, abgestuft)
    # --------------------------------------------------

    @staticmethod
    def _calculate_city_score(
        candidate: Candidate, job: Job,
    ) -> float:
        """
        Stadt-Score: gleiche Stadt = 1.0, Metropolregion = 0.5, anders = 0.0.

        Returns:
            0.0, 0.5, oder 1.0
        """
        if not candidate.hotlist_city or not job.hotlist_city:
            return 0.0

        cand_city = candidate.hotlist_city.lower().strip()
        job_city = job.hotlist_city.lower().strip()

        # Exakte Uebereinstimmung
        if cand_city == job_city:
            return 1.0

        # Metropolregion-Check
        for _metro_name, cities in METRO_AREAS.items():
            if cand_city in cities and job_city in cities:
                return 0.5

        return 0.0

    # --------------------------------------------------
    # Haupt-Scoring-Methode
    # --------------------------------------------------

    def calculate_pre_score(
        self,
        candidate: Candidate,
        job: Job,
        match: Match,
    ) -> PreScoreBreakdown:
        """
        Berechnet den Pre-Score fuer ein Kandidat-Job-Paar.

        5 Komponenten:
        1. Rollen-Aehnlichkeit (35 Pkt) — Matrix + primaer/sekundaer
        2. Keyword-Match (25 Pkt) — Absolute Anzahl, inline berechnet
        3. Distanz (15 Pkt) — Naeher = besser
        4. Kategorie (15 Pkt) — Gate (bei Mismatch: max 10 gesamt)
        5. Stadt (10 Pkt) — Gleich / Metropolregion / anders

        Kalibrierung (wenn geladen):
        - Rollen-Overrides ueberschreiben Matrix-Werte
        - Power-Keywords zaehlen doppelt
        - Ausschluss-Paare = Score 0

        Args:
            candidate: Kandidat
            job: Job
            match: Match-Objekt (fuer distance_km, keyword_score)

        Returns:
            PreScoreBreakdown mit Einzelwerten und Gesamt-Score
        """
        # Ausschluss-Paare Check (Kalibrierung)
        if self._exclusion_set:
            job_role = (job.hotlist_job_title or "").strip()
            cand_role = (candidate.hotlist_job_title or "").strip()
            if job_role and cand_role and (job_role, cand_role) in self._exclusion_set:
                return PreScoreBreakdown(
                    category_score=0.0,
                    city_score=0.0,
                    role_similarity_score=0.0,
                    keyword_score=0.0,
                    distance_score=0.0,
                    matched_role=cand_role,
                    role_match_type="excluded",
                    keyword_count=0,
                    total=0.0,
                )

        # 1. Rollen-Aehnlichkeit (35 Punkte)
        role_sim, matched_role, match_type = self._calculate_role_similarity(
            candidate, job,
        )
        role_points = role_sim * WEIGHT_ROLE_SIMILARITY

        # 2. Keywords (25 Punkte)
        kw_score, kw_count = self._calculate_keyword_score(
            candidate, job, match,
        )
        kw_points = kw_score * WEIGHT_KEYWORDS

        # 3. Distanz (15 Punkte)
        dist_score = self._calculate_distance_score(match)
        dist_points = dist_score * WEIGHT_DISTANCE

        # 4. Kategorie (15 Punkte, Gate)
        cat_score = self._calculate_category_score(candidate, job)
        cat_points = cat_score * WEIGHT_CATEGORY

        # 5. Stadt (10 Punkte)
        city_score = self._calculate_city_score(candidate, job)
        city_points = city_score * WEIGHT_CITY

        # Gesamt
        total = role_points + kw_points + dist_points + cat_points + city_points

        # Kategorie-Gate: Bei Mismatch deckeln
        if cat_score == 0.0:
            total = min(total, CATEGORY_MISMATCH_CAP)

        return PreScoreBreakdown(
            category_score=round(cat_points, 1),
            city_score=round(city_points, 1),
            role_similarity_score=round(role_points, 1),
            keyword_score=round(kw_points, 1),
            distance_score=round(dist_points, 1),
            matched_role=matched_role,
            role_match_type=match_type,
            keyword_count=kw_count,
            total=round(total, 1),
        )

    # --------------------------------------------------
    # Einzelnes Match scoren
    # --------------------------------------------------

    async def score_match(self, match_id) -> PreScoreBreakdown | None:
        """
        Berechnet den Pre-Score fuer ein einzelnes Match.

        Laedt automatisch Kalibrierungsdaten (falls vorhanden).

        Returns:
            PreScoreBreakdown oder None bei Fehler
        """
        await self.load_calibration()

        result = await self.db.execute(
            select(Match, Candidate, Job)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .join(Job, Match.job_id == Job.id)
            .where(Match.id == match_id)
        )
        row = result.first()
        if not row:
            return None

        match, candidate, job = row
        breakdown = self.calculate_pre_score(candidate, job, match)

        # Score auf Match speichern
        match.pre_score = breakdown.total
        await self.db.commit()

        return breakdown

    # --------------------------------------------------
    # Batch-Scoring: Alle Matches einer Kategorie/Stadt
    # --------------------------------------------------

    async def score_matches_for_category(
        self,
        category: str,
        city: str | None = None,
        job_title: str | None = None,
        force: bool = False,
    ) -> PreScoringResult:
        """
        Berechnet Pre-Scores fuer alle Matches einer Kategorie.

        Laedt automatisch Kalibrierungsdaten (falls vorhanden).

        Args:
            category: FINANCE oder ENGINEERING
            city: Optional: nur Matches in dieser Stadt
            job_title: Optional: nur Matches mit diesem Beruf
            force: True = auch bereits gescorte Matches neu bewerten
        """
        # Kalibrierungsdaten laden (lazy, einmalig)
        await self.load_calibration()

        from sqlalchemy import or_

        # Query: Matches mit Kandidaten und Jobs der gleichen Kategorie
        query = (
            select(Match, Candidate, Job)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .join(Job, Match.job_id == Job.id)
            .where(
                and_(
                    Candidate.hotlist_category == category,
                    Job.hotlist_category == category,
                    Candidate.deleted_at.is_(None),
                    Job.deleted_at.is_(None),
                )
            )
        )

        if city:
            query = query.where(Candidate.hotlist_city == city)

        if job_title:
            query = query.where(
                or_(
                    Candidate.hotlist_job_titles.any(job_title),
                    Candidate.hotlist_job_title == job_title,
                )
            ).where(
                or_(
                    Job.hotlist_job_titles.any(job_title),
                    Job.hotlist_job_title == job_title,
                )
            )

        if not force:
            query = query.where(Match.pre_score.is_(None))

        result = await self.db.execute(query)
        rows = result.all()

        total = len(rows)
        scored = 0
        skipped = 0
        score_sum = 0.0

        for match, candidate, job in rows:
            try:
                breakdown = self.calculate_pre_score(candidate, job, match)
                match.pre_score = breakdown.total
                score_sum += breakdown.total
                scored += 1
            except Exception as e:
                logger.error(f"Pre-Scoring Fehler fuer Match {match.id}: {e}")
                skipped += 1

        await self.db.commit()

        avg = score_sum / scored if scored > 0 else 0.0

        logger.info(
            f"Pre-Scoring fuer {category}"
            f"{f' / {city}' if city else ''}: "
            f"{scored}/{total} gescort, Oe {avg:.1f}"
        )

        return PreScoringResult(
            total_matches=total,
            scored=scored,
            skipped=skipped,
            avg_score=round(avg, 1),
        )

    async def score_all_matches(self, force: bool = False) -> dict:
        """
        Berechnet Pre-Scores fuer ALLE Matches (FINANCE + ENGINEERING).

        Returns:
            Dict mit Ergebnissen pro Kategorie
        """
        finance_result = await self.score_matches_for_category(
            HotlistCategory.FINANCE, force=force
        )
        engineering_result = await self.score_matches_for_category(
            HotlistCategory.ENGINEERING, force=force
        )

        return {
            "finance": {
                "total": finance_result.total_matches,
                "scored": finance_result.scored,
                "avg_score": finance_result.avg_score,
            },
            "engineering": {
                "total": engineering_result.total_matches,
                "scored": engineering_result.scored,
                "avg_score": engineering_result.avg_score,
            },
        }
