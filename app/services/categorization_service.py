"""Categorization Service - Hotlist-Kategorisierung für Kandidaten und Jobs.

Ordnet Kandidaten und Jobs einer Kategorie zu:
- FINANCE (Buchhalter, Controller, Steuerfachangestellte, ...)
- ENGINEERING (Servicetechniker, Elektriker, SHK, ...)
- SONSTIGE (alles andere → kein Matching)

Zusätzlich: PLZ → Stadt-Mapping, Job-Title-Normalisierung.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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
    # Berufsbezeichnungen (stark — reichen allein fuer FINANCE-Zuordnung)
    "buchhalter", "buchhalterin", "finanzbuchhalter", "finanzbuchhalterin",
    "bilanzbuchhalter", "bilanzbuchhalterin", "lohnbuchhalter", "lohnbuchhalterin",
    "debitorenbuchhalter", "kreditorenbuchhalter",
    "controller", "controllerin", "financial controller",
    "steuerfachangestellte", "steuerfachangestellter", "steuerfachwirt",
    "steuerberater", "steuerberaterin",
    "wirtschaftsprüfer", "wirtschaftsprüferin",
    "accountant", "accounts payable", "accounts receivable",
    "finanzwirt", "finanzbuchhaltung", "lohnbuchhaltung",
    "rechnungswesen", "hauptbuchhalter",
    # Tätigkeiten (mittel — brauchen Kontext)
    "buchführung", "bilanzierung", "jahresabschluss",
    "monatsabschluss", "kontenabstimmung", "kontenklärung",
    "debitorenbuchhaltung", "kreditorenbuchhaltung", "anlagenbuchhaltung",
    "kostenrechnung",
    "umsatzsteuer", "vorsteuer", "steuererklärung",
    "zahlungsverkehr", "mahnwesen", "rechnungsprüfung",
    # Software (nur als Verstärker — nicht allein ausreichend)
    "datev", "sap fi", "sap co", "lexware",
    # ENTFERNT: "buchhaltung" (zu generisch, auch "Abteilung Buchhaltung"),
    # "controlling" (zu generisch), "reporting" (zu generisch),
    # "accounting" (zu generisch), "sage" (auch Eigenname)
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
# PLZ → STADT MAPPING (vollständig, 8.255 Einträge)
# ═══════════════════════════════════════════════════════════════

def _load_plz_map() -> dict[str, str]:
    """Lädt die vollständige PLZ→Stadt-Zuordnung aus der JSON-Datei.

    Enthält alle 8.255 deutschen 5-stelligen Postleitzahlen mit zugehörigem Ort.
    Quelle: OpenData (CC BY 4.0)
    """
    data_path = Path(__file__).parent.parent / "data" / "plz_ort.json"
    # Korrekturen: Datenquelle hat teilweise verkürzte Namen
    CITY_NAME_FIXES = {
        "Frankfurt": "Frankfurt am Main",
        "Freiburg": "Freiburg im Breisgau",
        "Offenbach": "Offenbach am Main",
    }
    try:
        with open(data_path, "r", encoding="utf-8") as f:
            plz_map = json.load(f)
        # Städtenamen korrigieren
        for plz, city in plz_map.items():
            if city in CITY_NAME_FIXES:
                plz_map[plz] = CITY_NAME_FIXES[city]
        logger.info(f"PLZ-Tabelle geladen: {len(plz_map)} Einträge")
        return plz_map
    except FileNotFoundError:
        logger.warning(f"PLZ-Tabelle nicht gefunden: {data_path}")
        return {}
    except Exception as e:
        logger.error(f"Fehler beim Laden der PLZ-Tabelle: {e}")
        return {}


# Einmal beim Import laden (Singleton)
PLZ_CITY_MAP: dict[str, str] = _load_plz_map()


# ═══════════════════════════════════════════════════════════════
# JOB-TITLE NORMALISIERUNG
# ═══════════════════════════════════════════════════════════════

# Mapping: Keyword im Titel → normalisierter Job-Title
# WICHTIG: Spezifischere Titel MÜSSEN vor allgemeineren stehen!
# "finanzbuchhalter" muss VOR "buchhalter" kommen, weil Substring-Match.
JOB_TITLE_NORMALIZATION: dict[str, str] = {
    # FINANCE — spezifische Buchhalter-Typen ZUERST
    "finanzbuchhalter": "Finanzbuchhalter/in",
    "finanzbuchhalterin": "Finanzbuchhalter/in",
    "bilanzbuchhalter": "Bilanzbuchhalter/in",
    "bilanzbuchhalterin": "Bilanzbuchhalter/in",
    "lohnbuchhalter": "Lohnbuchhalter/in",
    "lohnbuchhalterin": "Lohnbuchhalter/in",
    "debitorenbuchhalter": "Debitorenbuchhalter/in",
    "debitorenbuchhalterin": "Debitorenbuchhalter/in",
    "kreditorenbuchhalter": "Kreditorenbuchhalter/in",
    "kreditorenbuchhalterin": "Kreditorenbuchhalter/in",
    "hauptbuchhalter": "Hauptbuchhalter/in",
    "hauptbuchhalterin": "Hauptbuchhalter/in",
    "accountant": "Finanzbuchhalter/in",  # Accountant = Finanzbuchhalter auf Englisch
    # FINANCE — allgemeiner Buchhalter ZULETZT (fängt alles auf was nicht spezifischer ist)
    "buchhalter": "Buchhalter/in",
    "buchhalterin": "Buchhalter/in",
    "controller": "Controller/in",
    "controllerin": "Controller/in",
    "steuerfachangestellte": "Steuerfachangestellte/r",
    "steuerfachangestellter": "Steuerfachangestellte/r",
    "steuerberater": "Steuerberater/in",
    "steuerberaterin": "Steuerberater/in",
    "wirtschaftsprüfer": "Wirtschaftsprüfer/in",
    "wirtschaftsprüferin": "Wirtschaftsprüfer/in",
    # FINANCE — zusaetzliche Bezeichnungen
    "entgeltabrechnung": "Lohnbuchhalter/in",
    "payroll": "Lohnbuchhalter/in",
    "lohn- und gehalt": "Lohnbuchhalter/in",
    "lohn und gehalt": "Lohnbuchhalter/in",
    "gehaltsabrechnung": "Lohnbuchhalter/in",
    "rechnungswesen": "Buchhalter/in",
    "financial accountant": "Finanzbuchhalter/in",
    "tax consultant": "Steuerberater/in",
    "tax advisor": "Steuerberater/in",
    "finanzanalyst": "Controller/in",
    "financial analyst": "Controller/in",
    "treasurer": "Controller/in",
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
    # ENGINEERING — zusaetzliche Bezeichnungen
    "shk": "Anlagenmechaniker/in SHK",
    "sanitär": "Sanitärinstallateur/in",
    "sanitaer": "Sanitärinstallateur/in",
    "klimatechniker": "Kältetechniker/in",
    "lüftungstechniker": "Kältetechniker/in",
    "kältemonteur": "Kältetechniker/in",
    "haustechniker": "Servicetechniker/in",
    "wartungstechniker": "Servicetechniker/in",
    "instandhalter": "Servicetechniker/in",
    "automatisierungstechniker": "Mechatroniker/in",
    "sps-programmierer": "Mechatroniker/in",
    "schweißer": "Schlosser/in",
    "schweisser": "Schlosser/in",
    "zerspanungsmechaniker": "Industriemechaniker/in",
    "cnc": "Industriemechaniker/in",
    "werkzeugmechaniker": "Industriemechaniker/in",
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

        # Kategorie bestimmen: wer hat MEHR Treffer?
        # Bei Gleichstand → SONSTIGE (nicht mehr automatisch FINANCE)
        if finance_matches and engineering_matches:
            if len(finance_matches) > len(engineering_matches):
                return HotlistCategory.FINANCE, finance_matches
            elif len(engineering_matches) > len(finance_matches):
                return HotlistCategory.ENGINEERING, engineering_matches
            else:
                # Gleichstand → SONSTIGE (manuell entscheiden)
                return HotlistCategory.SONSTIGE, finance_matches + engineering_matches
        elif finance_matches:
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
        2. PLZ → Stadt-Mapping (vollständige 5-stellige PLZ, 8.255 Einträge)
        """
        if city and city.strip():
            return city.strip()

        if postal_code and postal_code.strip():
            plz = postal_code.strip()
            # Erst exakte 5-stellige PLZ suchen
            if plz in PLZ_CITY_MAP:
                return PLZ_CITY_MAP[plz]
            # Fallback: mit führender Null auffüllen (z.B. "1067" → "01067")
            if len(plz) == 4:
                padded = "0" + plz
                if padded in PLZ_CITY_MAP:
                    return PLZ_CITY_MAP[padded]

        return None

    # ──────────────────────────────────────────────────
    # Job-Title normalisieren
    # ──────────────────────────────────────────────────

    def normalize_job_title(self, position: str | None) -> str | None:
        """
        Normalisiert einen Job-Titel auf eine Standard-Bezeichnung.

        Sucht im Position-Feld nach bekannten Keywords und
        gibt die normalisierte Form zurück.
        Fallback: Original-Titel (gekuerzt) statt None.
        """
        if not position:
            return None

        position_lower = position.lower()

        for keyword, normalized in JOB_TITLE_NORMALIZATION.items():
            if keyword in position_lower:
                return normalized

        # Fallback: Original-Titel zurueckgeben (max. 100 Zeichen)
        # Damit Kandidaten/Jobs nicht ohne Titel im Pre-Match verschwinden
        return position.strip()[:100]

    # ──────────────────────────────────────────────────
    # Einzelne Kandidaten/Jobs kategorisieren
    # ──────────────────────────────────────────────────

    def categorize_candidate(self, candidate: Candidate) -> CategorizationResult:
        """
        Kategorisiert einen einzelnen Kandidaten.

        LOGIK (Priorität):
        1. current_position entscheidet ZUERST — wenn die Position klar einer
           Kategorie zuzuordnen ist, gilt diese.
        2. Nur bei uneindeutiger Position: skills + cv_text als Tiebreaker.
        3. Bei Gleichstand: SONSTIGE (nicht mehr automatisch FINANCE).
        """
        city = self.resolve_city(candidate.postal_code, candidate.city)
        job_title = self.normalize_job_title(candidate.current_position)

        # STUFE 1: Position allein prüfen (stärkster Indikator)
        if candidate.current_position:
            pos_category, pos_keywords = self.detect_category(candidate.current_position)
            if pos_category != HotlistCategory.SONSTIGE:
                return CategorizationResult(
                    category=pos_category,
                    city=city,
                    job_title=job_title,
                    matched_keywords=pos_keywords,
                )

        # STUFE 2: Skills prüfen (zweitstärkster Indikator)
        if candidate.skills:
            skills_text = " ".join(candidate.skills)
            skills_category, skills_keywords = self.detect_category(skills_text)
            if skills_category != HotlistCategory.SONSTIGE:
                return CategorizationResult(
                    category=skills_category,
                    city=city,
                    job_title=job_title,
                    matched_keywords=skills_keywords,
                )

        # STUFE 3: CV-Text als Fallback (schwächster Indikator, nur erste 2000 Zeichen)
        if candidate.cv_text:
            cv_category, cv_keywords = self.detect_category(candidate.cv_text[:2000])
            if cv_category != HotlistCategory.SONSTIGE:
                return CategorizationResult(
                    category=cv_category,
                    city=city,
                    job_title=job_title,
                    matched_keywords=cv_keywords,
                )

        return CategorizationResult(
            category=HotlistCategory.SONSTIGE,
            city=city,
            job_title=job_title,
            matched_keywords=[],
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

        # PIPELINE-SCHUTZ: Wenn manuelle Jobtitel gesetzt, diese NIEMALS ueberschreiben!
        has_manual = (
            hasattr(candidate, "manual_job_titles")
            and candidate.manual_job_titles
            and len(candidate.manual_job_titles) > 0
        )
        if has_manual:
            # Manuelle Titel haben Vorrang — hotlist-Felder synchronisieren
            candidate.hotlist_job_title = candidate.manual_job_titles[0]
            candidate.hotlist_job_titles = list(candidate.manual_job_titles)
            candidate.categorized_at = datetime.now(timezone.utc)
            return  # Keine automatische Klassifizierung

        candidate.hotlist_job_title = result.job_title
        # Array mit einzelnem Titel setzen (wird später durch RulesEngine/OpenAI überschrieben)
        if result.job_title:
            candidate.hotlist_job_titles = [result.job_title]
        candidate.categorized_at = datetime.now(timezone.utc)

        # FINANCE-Kandidaten: Feinklassifizierung NUR wenn KEINE OpenAI-Ergebnisse vorhanden
        # OpenAI-Ergebnisse sind die Ground-Truth und dürfen NICHT überschrieben werden.
        # Der RulesEngine wird nur für NEUE Kandidaten eingesetzt (nach Training).
        if result.category == HotlistCategory.FINANCE:
            # Prüfe ob OpenAI schon klassifiziert hat (classification_data.source == "openai")
            has_openai = False
            if hasattr(candidate, "classification_data") and candidate.classification_data:
                if isinstance(candidate.classification_data, dict):
                    has_openai = candidate.classification_data.get("source") == "openai"
            if not has_openai:
                try:
                    from app.services.finance_rules_engine import FinanceRulesEngine
                    engine = FinanceRulesEngine()
                    classification = engine.classify_candidate(candidate)
                    if classification.roles and not classification.is_leadership:
                        candidate.hotlist_job_title = classification.primary_role
                        candidate.hotlist_job_titles = classification.roles
                except Exception as e:
                    logger.warning(f"FinanceRulesEngine Kandidat {getattr(candidate, 'id', '?')}: {e}")

    def apply_to_job(self, job: Job, result: CategorizationResult) -> None:
        """Setzt die Hotlist-Felder auf dem Job-Model."""
        job.hotlist_category = result.category
        job.hotlist_city = result.city

        # PIPELINE-SCHUTZ: Wenn manueller Jobtitel gesetzt, NIEMALS ueberschreiben!
        has_manual = (
            hasattr(job, "manual_job_title")
            and job.manual_job_title
        )
        if has_manual:
            job.hotlist_job_title = job.manual_job_title
            job.hotlist_job_titles = [job.manual_job_title]
            job.categorized_at = datetime.now(timezone.utc)
            return  # Keine automatische Klassifizierung

        job.hotlist_job_title = result.job_title
        if result.job_title:
            job.hotlist_job_titles = [result.job_title]
        job.categorized_at = datetime.now(timezone.utc)

        # FINANCE-Jobs: Feinklassifizierung NUR wenn KEINE OpenAI-Ergebnisse vorhanden
        # (Jobs haben aktuell kein classification_data, aber Schutzlogik ist vorbereitet)
        if result.category == HotlistCategory.FINANCE:
            has_openai = False
            if hasattr(job, "classification_data") and job.classification_data:
                if isinstance(job.classification_data, dict):
                    has_openai = job.classification_data.get("source") == "openai"
            if not has_openai:
                try:
                    from app.services.finance_rules_engine import FinanceRulesEngine
                    engine = FinanceRulesEngine()
                    classification = engine.classify_job(job)
                    if classification.roles:
                        job.hotlist_job_title = classification.primary_role
                        job.hotlist_job_titles = classification.roles
                except Exception as e:
                    logger.warning(f"FinanceRulesEngine Job {getattr(job, 'id', '?')}: {e}")

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
