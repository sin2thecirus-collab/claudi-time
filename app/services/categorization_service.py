"""Categorization Service - Hotlist-Kategorisierung für Kandidaten und Jobs.

Ordnet Kandidaten und Jobs einer Kategorie zu:
- FINANCE (Buchhalter, Controller, Steuerfachangestellte, ...)
- ENGINEERING (Servicetechniker, Elektriker, SHK, ...)
- SONSTIGE (alles andere → kein Matching)

Zusätzlich: PLZ → Stadt-Mapping, Job-Title-Normalisierung.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# KATEGORIE-DEFINITIONEN
# ═══════════════════════════════════════════════════════════════

class HotlistCategory:
    """Konstanten für Hotlist-Kategorien."""
    FINANCE = "FINANCE"
    ENGINEERING = "ENGINEERING"
    SONSTIGE = "SONSTIGE"


# Keywords pro Kategorie (lowercase, Regex-fähig)
FINANCE_KEYWORDS: list[str] = [
    # Berufsbezeichnungen
    "buchhalter", "buchhalterin", "finanzbuchhalter", "finanzbuchhalterin",
    "bilanzbuchhalter", "bilanzbuchhalterin", "lohnbuchhalter", "lohnbuchhalterin",
    "debitorenbuchhalter", "kreditorenbuchhalter",
    "controller", "controllerin", "financial controller",
    "steuerfachangestellte", "steuerfachangestellter", "steuerfachwirt",
    "steuerberater", "steuerberaterin",
    "wirtschaftsprüfer", "wirtschaftsprüferin",
    "accountant", "accounting", "accounts payable", "accounts receivable",
    "finanzwirt", "finanzbuchhaltung", "lohnbuchhaltung",
    "rechnungswesen", "hauptbuchhalter",
    # Tätigkeiten
    "buchhaltung", "buchführung", "bilanzierung", "jahresabschluss",
    "monatsabschluss", "kontenabstimmung", "kontenklärung",
    "debitorenbuchhaltung", "kreditorenbuchhaltung", "anlagenbuchhaltung",
    "kostenrechnung", "controlling", "reporting",
    "umsatzsteuer", "vorsteuer", "steuererklärung",
    "zahlungsverkehr", "mahnwesen", "rechnungsprüfung",
    # Software
    "datev", "sap fi", "sap co", "lexware", "sage",
]

ENGINEERING_KEYWORDS: list[str] = [
    # Berufsbezeichnungen
    "servicetechniker", "servicetechnikerin",
    "elektrotechniker", "elektrotechnikerin",
    "elektriker", "elektrikerin", "elektrofachkraft",
    "elektroniker", "elektronikerin", "elektroinstallateur",
    "elektromonteur", "elektromonteurin",
    "anlagenmechaniker", "anlagenmechanikerin",
    "mechatroniker", "mechatronikerin",
    "industriemechaniker", "industriemechanikerin",
    "kältetechniker", "kältetechnikerin",
    "heizungsbauer", "heizungsmonteur", "heizungsinstallateur",
    "sanitärinstallateur", "sanitärmonteur",
    "klempner", "rohrleger", "rohrschlosser",
    "techniker", "meister",
    "schlosser", "metallbauer",
    # Tätigkeiten / Fachgebiete
    "elektroinstallation", "elektrotechnik", "elektronik",
    "shk", "sanitär", "heizung", "klima", "lüftung",
    "kältetechnik", "kälteanlagen", "klimaanlagen",
    "wärmepumpe", "wärmepumpen", "solarthermie",
    "photovoltaik", "solar",
    "sps-programmierung", "steuerungstechnik", "automatisierung",
    "schaltschrankbau", "gebäudetechnik", "gebäudeautomation",
    "instandhaltung", "wartung", "reparatur",
    "brandmeldeanlagen", "brandschutz",
    "gas-wasser-installation",
    # Qualifikationen
    "gesellenbrief", "meisterbrief",
    "schweißen", "wig", "mig", "mag",
]

# Kompilierte Patterns für schnelleres Matching
_FINANCE_PATTERNS: list[re.Pattern] = [
    re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE)
    for kw in FINANCE_KEYWORDS
]
_ENGINEERING_PATTERNS: list[re.Pattern] = [
    re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE)
    for kw in ENGINEERING_KEYWORDS
]


# ═══════════════════════════════════════════════════════════════
# PLZ → STADT MAPPING (Top-Bereiche Deutschland)
# ═══════════════════════════════════════════════════════════════

# PLZ-Prefix (2-stellig) → Standard-Stadt
PLZ_CITY_MAP: dict[str, str] = {
    # Berlin
    "10": "Berlin", "12": "Berlin", "13": "Berlin", "14": "Berlin",
    # Hamburg
    "20": "Hamburg", "21": "Hamburg", "22": "Hamburg",
    # München
    "80": "München", "81": "München", "82": "München", "83": "München", "85": "München",
    # Köln / Bonn
    "50": "Köln", "51": "Köln", "53": "Bonn",
    # Frankfurt
    "60": "Frankfurt am Main", "61": "Frankfurt am Main", "63": "Offenbach",
    # Stuttgart
    "70": "Stuttgart", "71": "Stuttgart", "73": "Esslingen",
    # Düsseldorf
    "40": "Düsseldorf", "41": "Düsseldorf",
    # Dortmund / Essen / Ruhrgebiet
    "44": "Dortmund", "45": "Essen", "46": "Oberhausen", "47": "Duisburg",
    # Hannover
    "30": "Hannover", "31": "Hannover",
    # Nürnberg
    "90": "Nürnberg", "91": "Nürnberg",
    # Dresden / Leipzig
    "01": "Dresden", "04": "Leipzig",
    # Bremen
    "28": "Bremen",
    # Mannheim / Heidelberg
    "68": "Mannheim", "69": "Heidelberg",
    # Wiesbaden / Mainz
    "55": "Mainz", "65": "Wiesbaden",
    # Karlsruhe
    "76": "Karlsruhe",
    # Augsburg
    "86": "Augsburg",
    # Freiburg
    "79": "Freiburg",
    # Kassel
    "34": "Kassel",
    # Kiel
    "24": "Kiel",
    # Rostock
    "18": "Rostock",
    # Erfurt
    "99": "Erfurt",
    # Magdeburg
    "39": "Magdeburg",
    # Potsdam
    "14": "Potsdam",
    # Saarbrücken
    "66": "Saarbrücken",
    # Bielefeld
    "33": "Bielefeld",
    # Wuppertal
    "42": "Wuppertal",
    # Aachen
    "52": "Aachen",
    # Münster
    "48": "Münster",
    # Braunschweig
    "38": "Braunschweig",
}


# ═══════════════════════════════════════════════════════════════
# JOB-TITLE NORMALISIERUNG
# ═══════════════════════════════════════════════════════════════

# Mapping: Keyword im Titel → normalisierter Job-Title
JOB_TITLE_NORMALIZATION: dict[str, str] = {
    # FINANCE
    "buchhalter": "Buchhalter/in",
    "buchhalterin": "Buchhalter/in",
    "finanzbuchhalter": "Finanzbuchhalter/in",
    "finanzbuchhalterin": "Finanzbuchhalter/in",
    "bilanzbuchhalter": "Bilanzbuchhalter/in",
    "bilanzbuchhalterin": "Bilanzbuchhalter/in",
    "lohnbuchhalter": "Lohnbuchhalter/in",
    "lohnbuchhalterin": "Lohnbuchhalter/in",
    "controller": "Controller/in",
    "controllerin": "Controller/in",
    "steuerfachangestellte": "Steuerfachangestellte/r",
    "steuerfachangestellter": "Steuerfachangestellte/r",
    "steuerberater": "Steuerberater/in",
    "steuerberaterin": "Steuerberater/in",
    "wirtschaftsprüfer": "Wirtschaftsprüfer/in",
    "wirtschaftsprüferin": "Wirtschaftsprüfer/in",
    "accountant": "Accountant",
    # ENGINEERING
    "servicetechniker": "Servicetechniker/in",
    "servicetechnikerin": "Servicetechniker/in",
    "elektriker": "Elektriker/in",
    "elektrikerin": "Elektriker/in",
    "elektroniker": "Elektroniker/in",
    "elektronikerin": "Elektroniker/in",
    "elektrotechniker": "Elektrotechniker/in",
    "elektroinstallateur": "Elektroinstallateur/in",
    "elektromonteur": "Elektromonteur/in",
    "anlagenmechaniker": "Anlagenmechaniker/in SHK",
    "anlagenmechanikerin": "Anlagenmechaniker/in SHK",
    "mechatroniker": "Mechatroniker/in",
    "mechatronikerin": "Mechatroniker/in",
    "industriemechaniker": "Industriemechaniker/in",
    "kältetechniker": "Kältetechniker/in",
    "heizungsbauer": "Heizungsbauer/in",
    "heizungsmonteur": "Heizungsmonteur/in",
    "sanitärinstallateur": "Sanitärinstallateur/in",
    "sanitärmonteur": "Sanitärmonteur/in",
    "klempner": "Klempner/in",
    "schlosser": "Schlosser/in",
    "metallbauer": "Metallbauer/in",
}


# ═══════════════════════════════════════════════════════════════
# DATENKLASSEN
# ═══════════════════════════════════════════════════════════════

@dataclass
class CategorizationResult:
    """Ergebnis einer Kategorisierung."""
    category: str
    city: str | None
    job_title: str | None
    matched_keywords: list[str]


@dataclass
class BatchCategorizationResult:
    """Ergebnis einer Batch-Kategorisierung."""
    total: int
    categorized: int
    finance: int
    engineering: int
    sonstige: int
    skipped: int


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════

class CategorizationService:
    """
    Service für Hotlist-Kategorisierung.

    Analysiert Position/Job-Text und ordnet eine Kategorie zu:
    - FINANCE: Buchhaltung, Controlling, Steuern
    - ENGINEERING: Elektro, SHK, Mechanik, Technik
    - SONSTIGE: alles andere (kein Matching)
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    # ──────────────────────────────────────────────────
    # Kern-Logik: Kategorie erkennen
    # ──────────────────────────────────────────────────

    def detect_category(self, text: str) -> tuple[str, list[str]]:
        """
        Erkennt die Kategorie anhand von Keywords im Text.

        Args:
            text: Position, Job-Titel oder Job-Text

        Returns:
            (kategorie, liste_gematchter_keywords)
        """
        if not text:
            return HotlistCategory.SONSTIGE, []

        text_lower = text.lower()

        # FINANCE prüfen
        finance_matches = []
        for pattern, keyword in zip(_FINANCE_PATTERNS, FINANCE_KEYWORDS):
            if pattern.search(text_lower):
                finance_matches.append(keyword)

        # ENGINEERING prüfen
        engineering_matches = []
        for pattern, keyword in zip(_ENGINEERING_PATTERNS, ENGINEERING_KEYWORDS):
            if pattern.search(text_lower):
                engineering_matches.append(keyword)

        # Kategorie bestimmen: wer hat mehr Treffer?
        if finance_matches and len(finance_matches) >= len(engineering_matches):
            return HotlistCategory.FINANCE, finance_matches
        elif engineering_matches:
            return HotlistCategory.ENGINEERING, engineering_matches
        else:
            return HotlistCategory.SONSTIGE, []

    # ──────────────────────────────────────────────────
    # PLZ → Stadt
    # ──────────────────────────────────────────────────

    def resolve_city(self, postal_code: str | None, city: str | None) -> str | None:
        """
        Bestimmt die Stadt aus PLZ oder vorhandenem city-Feld.

        Priorität:
        1. Vorhandenes city-Feld (wenn nicht leer)
        2. PLZ → Stadt-Mapping (erste 2 Stellen)
        """
        if city and city.strip():
            return city.strip()

        if postal_code and len(postal_code) >= 2:
            prefix = postal_code[:2]
            return PLZ_CITY_MAP.get(prefix)

        return None

    # ──────────────────────────────────────────────────
    # Job-Title normalisieren
    # ──────────────────────────────────────────────────

    def normalize_job_title(self, position: str | None) -> str | None:
        """
        Normalisiert einen Job-Titel auf eine Standard-Bezeichnung.

        Sucht im Position-Feld nach bekannten Keywords und
        gibt die normalisierte Form zurück.
        """
        if not position:
            return None

        position_lower = position.lower()

        for keyword, normalized in JOB_TITLE_NORMALIZATION.items():
            if keyword in position_lower:
                return normalized

        return None

    # ──────────────────────────────────────────────────
    # Einzelne Kandidaten/Jobs kategorisieren
    # ──────────────────────────────────────────────────

    def categorize_candidate(self, candidate: Candidate) -> CategorizationResult:
        """
        Kategorisiert einen einzelnen Kandidaten.

        Analysiert: current_position, skills, cv_text
        """
        # Texte sammeln für Analyse
        texts = []
        if candidate.current_position:
            texts.append(candidate.current_position)
        if candidate.skills:
            texts.append(" ".join(candidate.skills))
        if candidate.cv_text:
            # Nur die ersten 2000 Zeichen des CV
            texts.append(candidate.cv_text[:2000])

        combined_text = " ".join(texts)
        category, matched_keywords = self.detect_category(combined_text)
        city = self.resolve_city(candidate.postal_code, candidate.city)
        job_title = self.normalize_job_title(candidate.current_position)

        return CategorizationResult(
            category=category,
            city=city,
            job_title=job_title,
            matched_keywords=matched_keywords,
        )

    def categorize_job(self, job: Job) -> CategorizationResult:
        """
        Kategorisiert einen einzelnen Job.

        Analysiert: position, job_text
        """
        texts = []
        if job.position:
            texts.append(job.position)
        if job.job_text:
            texts.append(job.job_text[:2000])

        combined_text = " ".join(texts)
        category, matched_keywords = self.detect_category(combined_text)

        # Stadt: work_location_city hat Priorität, dann city, dann PLZ
        city = self.resolve_city(
            job.postal_code,
            job.work_location_city or job.city,
        )
        job_title = self.normalize_job_title(job.position)

        return CategorizationResult(
            category=category,
            city=city,
            job_title=job_title,
            matched_keywords=matched_keywords,
        )

    # ──────────────────────────────────────────────────
    # Felder auf Model setzen
    # ──────────────────────────────────────────────────

    def apply_to_candidate(self, candidate: Candidate, result: CategorizationResult) -> None:
        """Setzt die Hotlist-Felder auf dem Kandidaten-Model."""
        candidate.hotlist_category = result.category
        candidate.hotlist_city = result.city
        candidate.hotlist_job_title = result.job_title
        candidate.categorized_at = datetime.now(timezone.utc)

    def apply_to_job(self, job: Job, result: CategorizationResult) -> None:
        """Setzt die Hotlist-Felder auf dem Job-Model."""
        job.hotlist_category = result.category
        job.hotlist_city = result.city
        job.hotlist_job_title = result.job_title
        job.categorized_at = datetime.now(timezone.utc)

    # ──────────────────────────────────────────────────
    # Batch-Kategorisierung (alle)
    # ──────────────────────────────────────────────────

    async def categorize_all_candidates(
        self,
        force: bool = False,
    ) -> BatchCategorizationResult:
        """
        Kategorisiert alle Kandidaten.

        Args:
            force: True = auch bereits kategorisierte neu bewerten
        """
        query = select(Candidate).where(Candidate.deleted_at.is_(None))
        if not force:
            query = query.where(Candidate.categorized_at.is_(None))

        result = await self.db.execute(query)
        candidates = result.scalars().all()

        total = len(candidates)
        finance = engineering = sonstige = skipped = 0

        for candidate in candidates:
            try:
                cat_result = self.categorize_candidate(candidate)
                self.apply_to_candidate(candidate, cat_result)

                if cat_result.category == HotlistCategory.FINANCE:
                    finance += 1
                elif cat_result.category == HotlistCategory.ENGINEERING:
                    engineering += 1
                else:
                    sonstige += 1
            except Exception as e:
                logger.error(f"Fehler bei Kandidat {candidate.id}: {e}")
                skipped += 1

        await self.db.commit()

        logger.info(
            f"Kandidaten kategorisiert: {total} gesamt, "
            f"{finance} FINANCE, {engineering} ENGINEERING, "
            f"{sonstige} SONSTIGE, {skipped} übersprungen"
        )

        return BatchCategorizationResult(
            total=total,
            categorized=finance + engineering + sonstige,
            finance=finance,
            engineering=engineering,
            sonstige=sonstige,
            skipped=skipped,
        )

    async def categorize_all_jobs(
        self,
        force: bool = False,
    ) -> BatchCategorizationResult:
        """
        Kategorisiert alle Jobs.

        Args:
            force: True = auch bereits kategorisierte neu bewerten
        """
        query = select(Job).where(Job.deleted_at.is_(None))
        if not force:
            query = query.where(Job.categorized_at.is_(None))

        result = await self.db.execute(query)
        jobs = result.scalars().all()

        total = len(jobs)
        finance = engineering = sonstige = skipped = 0

        for job in jobs:
            try:
                cat_result = self.categorize_job(job)
                self.apply_to_job(job, cat_result)

                if cat_result.category == HotlistCategory.FINANCE:
                    finance += 1
                elif cat_result.category == HotlistCategory.ENGINEERING:
                    engineering += 1
                else:
                    sonstige += 1
            except Exception as e:
                logger.error(f"Fehler bei Job {job.id}: {e}")
                skipped += 1

        await self.db.commit()

        logger.info(
            f"Jobs kategorisiert: {total} gesamt, "
            f"{finance} FINANCE, {engineering} ENGINEERING, "
            f"{sonstige} SONSTIGE, {skipped} übersprungen"
        )

        return BatchCategorizationResult(
            total=total,
            categorized=finance + engineering + sonstige,
            finance=finance,
            engineering=engineering,
            sonstige=sonstige,
            skipped=skipped,
        )

    async def categorize_all(
        self,
        force: bool = False,
    ) -> dict:
        """
        Kategorisiert alle Kandidaten UND Jobs.

        Returns:
            Dict mit Ergebnissen für beide Typen.
        """
        candidates_result = await self.categorize_all_candidates(force=force)
        jobs_result = await self.categorize_all_jobs(force=force)

        return {
            "candidates": {
                "total": candidates_result.total,
                "categorized": candidates_result.categorized,
                "finance": candidates_result.finance,
                "engineering": candidates_result.engineering,
                "sonstige": candidates_result.sonstige,
                "skipped": candidates_result.skipped,
            },
            "jobs": {
                "total": jobs_result.total,
                "categorized": jobs_result.categorized,
                "finance": jobs_result.finance,
                "engineering": jobs_result.engineering,
                "sonstige": jobs_result.sonstige,
                "skipped": jobs_result.skipped,
            },
        }
