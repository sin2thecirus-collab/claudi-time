"""Keyword Matcher Service - Extrahiert Keywords und berechnet Matching-Score."""

import re
from dataclasses import dataclass

# Branchen-spezifische Keywords für Buchhaltung und technische Berufe
ACCOUNTING_KEYWORDS = {
    # Software
    "sap", "datev", "lexware", "sage", "exact", "xero", "quickbooks",
    "sap r/3", "sap s/4hana", "sap fi", "sap co", "sap mm",
    "datev unternehmen online", "datev pro", "datev comfort",
    "microsoft dynamics", "navision", "oracle financials",

    # Tätigkeiten
    "buchhaltung", "buchführung", "finanzbuchhaltung", "lohnbuchhaltung",
    "debitorenbuchhaltung", "kreditorenbuchhaltung", "anlagenbuchhaltung",
    "hauptbuch", "nebenbuch", "kontenabstimmung", "kontenklärung",
    "zahlungsverkehr", "mahnwesen", "rechnungsprüfung", "rechnungsstellung",
    "umsatzsteuer", "vorsteuer", "jahresabschluss", "monatsabschluss",
    "bilanzierung", "bilanz", "guv", "gewinn- und verlustrechnung",
    "kostenrechnung", "controlling", "reporting", "budgetierung",
    "steuererklärung", "einkommensteuer", "körperschaftsteuer",
    "gewerbesteuer", "umsatzsteuervoranmeldung",

    # Qualifikationen
    "bilanzbuchhalter", "steuerfachangestellte", "steuerfachangestellter",
    "finanzbuchhalter", "buchhalter", "accountant", "controller",
    "wirtschaftsprüfer", "steuerberater", "finanzwirt",

    # Normen und Standards
    "hgb", "ifrs", "us-gaap", "gobd", "gobd-konform", "gaap",
}

TECHNICAL_KEYWORDS = {
    # Elektro
    "elektriker", "elektrotechnik", "elektroinstallation", "elektrofachkraft",
    "elektroniker", "elektromonteur", "elektroinstallateur",
    "schaltschrankbau", "sps", "sps-programmierung", "simatic",
    "niederspannung", "mittelspannung", "hochspannung", "gleichstrom",
    "wechselstrom", "drehstrom", "steuerungstechnik", "automatisierung",
    "gebäudetechnik", "gebäudeautomation", "knx", "eib", "bus-systeme",
    "photovoltaik", "pv", "solar", "wechselrichter", "energietechnik",
    "brandmeldeanlagen", "bma", "rwa", "einbruchmeldeanlage", "ema",
    "netzwerktechnik", "cat5", "cat6", "cat7", "glasfaser", "lwl",
    "vde", "din", "vds", "tüv", "dekra",

    # Anlagenmechanik / SHK
    "anlagenmechaniker", "shk", "sanitär", "heizung", "klima", "lüftung",
    "heizungsbau", "heizungsinstallation", "heizungsmonteur",
    "sanitärinstallation", "sanitärmonteur", "klempner", "rohrleger",
    "gas-wasser-installation", "gas", "wasser", "abwasser",
    "wärmepumpe", "wärmepumpen", "solarthermie", "brennwerttechnik",
    "ölheizung", "gasheizung", "pelletheizung", "fernwärme",
    "trinkwasserverordnung", "legionellenprüfung", "rohrnetzberechnung",
    "kälteanlagen", "kältetechnik", "klimaanlagen", "split-klimaanlage",
    "rlt", "raumlufttechnik", "lüftungsanlagen", "luftkanalbau",

    # Allgemein Technik
    "meister", "gesellenbrief", "ausbildereignung", "aevo",
    "schweißen", "wig", "mig", "mag", "schweißschein",
    "metallbau", "schlosser", "industriemechaniker",
    "wartung", "instandhaltung", "reparatur", "service",
    "kundendienst", "notdienst", "bereitschaftsdienst",
    "führerschein klasse b", "führerschein", "pkw",
}

# Kombinierte Keywords
ALL_KEYWORDS = ACCOUNTING_KEYWORDS | TECHNICAL_KEYWORDS


@dataclass
class KeywordMatchResult:
    """Ergebnis eines Keyword-Matchings."""

    matched_keywords: list[str]
    total_candidate_skills: int
    keyword_score: float

    @property
    def match_count(self) -> int:
        """Anzahl gematchter Keywords."""
        return len(self.matched_keywords)


class KeywordMatcher:
    """Service für Keyword-Extraktion und Matching."""

    def __init__(self):
        """Initialisiert den KeywordMatcher."""
        # Kompiliere Regex-Patterns für bessere Performance
        self._keyword_patterns: dict[str, re.Pattern] = {}
        for keyword in ALL_KEYWORDS:
            # Erstelle Pattern mit Wortgrenzen
            pattern = r'\b' + re.escape(keyword) + r'\b'
            self._keyword_patterns[keyword] = re.compile(pattern, re.IGNORECASE)

    def extract_keywords_from_text(self, text: str) -> list[str]:
        """
        Extrahiert relevante Keywords aus einem Text.

        Args:
            text: Der zu analysierende Text (z.B. Job-Beschreibung)

        Returns:
            Liste der gefundenen Keywords (lowercase)
        """
        if not text:
            return []

        found_keywords = []
        text_lower = text.lower()

        for keyword, pattern in self._keyword_patterns.items():
            if pattern.search(text_lower):
                found_keywords.append(keyword)

        return sorted(set(found_keywords))

    def find_matching_keywords(
        self,
        candidate_skills: list[str] | None,
        job_text: str | None,
    ) -> list[str]:
        """
        Findet Keywords, die sowohl beim Kandidaten als auch im Job vorkommen.

        Args:
            candidate_skills: Skills des Kandidaten
            job_text: Beschreibungstext des Jobs

        Returns:
            Liste der gematchten Keywords
        """
        if not candidate_skills or not job_text:
            return []

        matched = []
        job_text_lower = job_text.lower()

        for skill in candidate_skills:
            skill_lower = skill.lower().strip()
            if not skill_lower:
                continue

            # Direkte Suche im Job-Text
            # Suche mit Wortgrenzen um Teilwort-Matches zu vermeiden
            pattern = r'\b' + re.escape(skill_lower) + r'\b'
            if re.search(pattern, job_text_lower, re.IGNORECASE):
                matched.append(skill_lower)

        return sorted(set(matched))

    def calculate_score(
        self,
        matched_keywords: list[str],
        total_skills: int,
    ) -> float:
        """
        Berechnet den Keyword-Score.

        Score = Anzahl Matches / max(Anzahl Skills, 1)
        Ergebnis zwischen 0.0 und 1.0

        Args:
            matched_keywords: Liste der gematchten Keywords
            total_skills: Gesamtanzahl der Kandidaten-Skills

        Returns:
            Score zwischen 0.0 und 1.0
        """
        if total_skills <= 0:
            return 0.0

        match_count = len(matched_keywords)
        score = match_count / total_skills

        # Score auf 1.0 begrenzen (falls mehr Matches als Skills)
        return min(score, 1.0)

    def match(
        self,
        candidate_skills: list[str] | None,
        job_text: str | None,
    ) -> KeywordMatchResult:
        """
        Führt ein vollständiges Keyword-Matching durch.

        Args:
            candidate_skills: Skills des Kandidaten
            job_text: Beschreibungstext des Jobs

        Returns:
            KeywordMatchResult mit allen Matching-Daten
        """
        # Normalisiere Skills
        skills = candidate_skills or []
        skills = [s.strip() for s in skills if s and s.strip()]
        total_skills = len(skills)

        # Finde Matches
        matched = self.find_matching_keywords(skills, job_text)

        # Berechne Score
        score = self.calculate_score(matched, total_skills)

        return KeywordMatchResult(
            matched_keywords=matched,
            total_candidate_skills=total_skills,
            keyword_score=score,
        )

    def extract_job_requirements(self, job_text: str | None) -> dict[str, list[str]]:
        """
        Extrahiert strukturierte Anforderungen aus einem Job-Text.

        Args:
            job_text: Beschreibungstext des Jobs

        Returns:
            Dict mit kategorisierten Keywords:
            - software: Software-Kenntnisse (SAP, DATEV, etc.)
            - tasks: Tätigkeiten (Buchhaltung, etc.)
            - qualifications: Qualifikationen (Bilanzbuchhalter, etc.)
            - technical: Technische Skills (SPS, etc.)
        """
        if not job_text:
            return {
                "software": [],
                "tasks": [],
                "qualifications": [],
                "technical": [],
            }

        found = self.extract_keywords_from_text(job_text)

        # Kategorisiere gefundene Keywords
        software = []
        tasks = []
        qualifications = []
        technical = []

        software_keywords = {
            "sap", "datev", "lexware", "sage", "exact", "xero", "quickbooks",
            "sap r/3", "sap s/4hana", "sap fi", "sap co", "sap mm",
            "datev unternehmen online", "datev pro", "datev comfort",
            "microsoft dynamics", "navision", "oracle financials",
            "simatic", "sps",
        }

        qualification_keywords = {
            "bilanzbuchhalter", "steuerfachangestellte", "steuerfachangestellter",
            "finanzbuchhalter", "buchhalter", "accountant", "controller",
            "wirtschaftsprüfer", "steuerberater", "finanzwirt",
            "elektriker", "elektrotechnik", "elektroniker", "elektromonteur",
            "anlagenmechaniker", "meister", "gesellenbrief",
        }

        for keyword in found:
            if keyword in software_keywords:
                software.append(keyword)
            elif keyword in qualification_keywords:
                qualifications.append(keyword)
            elif keyword in TECHNICAL_KEYWORDS:
                technical.append(keyword)
            else:
                tasks.append(keyword)

        return {
            "software": software,
            "tasks": tasks,
            "qualifications": qualifications,
            "technical": technical,
        }


# Singleton-Instanz für einfache Verwendung
keyword_matcher = KeywordMatcher()
