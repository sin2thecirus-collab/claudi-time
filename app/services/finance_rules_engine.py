"""Finance Rules Engine — Lokaler Algorithmus für Finance-Rollen-Klassifizierung.

Trainiert aus OpenAI-Ergebnissen. Klassifiziert Kandidaten und Jobs
OHNE OpenAI anhand von Tätigkeits-Keywords und Qualifikations-Prüfung.

Rollen:
1. Bilanzbuchhalter/in — Erstellung Abschlüsse + Bilanzbuchhalter-Qualifikation
2. Finanzbuchhalter/in — Kreditoren+Debitoren, laufende Buchhaltung, vorbereitende Abschlüsse
3. Kreditorenbuchhalter/in — Nur/überwiegend Kreditoren
4. Debitorenbuchhalter/in — Nur/überwiegend Debitoren
5. Lohnbuchhalter/in — Lohn- und Gehaltsabrechnung
6. Steuerfachangestellte/r — Immer + Finanzbuchhalter (oder + Bilanzbuchhalter)
"""

import logging
import re
from dataclasses import dataclass, field

from app.models.candidate import Candidate
from app.models.job import Job

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# ROLLEN-DEFINITIONEN — Trainiert aus OpenAI-Ergebnissen
# ═══════════════════════════════════════════════════════════════

# Leadership-Keywords (Ausschluss)
LEADERSHIP_TITLE_KEYWORDS = [
    "leiter", "leiterin", "head of", "teamleiter", "teamleiterin",
    "abteilungsleiter", "abteilungsleiterin", "director", "cfo",
    "finance manager", "chief financial", "vice president finance",
    "vp finance", "bereichsleiter", "gruppenleiter",
]

LEADERSHIP_ACTIVITY_KEYWORDS = [
    "disziplinarische führung", "fachliche führung",
    "mitarbeiterverantwortung", "budgetverantwortung",
    "leitung des teams", "aufbau eines teams", "personalverantwortung",
    "führung von mitarbeitern", "teamführung",
]

# Bilanzbuchhalter: Erstellung von Abschlüssen
BILANZ_CREATION_KEYWORDS = [
    "erstellung jahresabschluss", "erstellung des jahresabschluss",
    "erstellung monatsabschluss", "erstellung des monatsabschluss",
    "erstellung quartalsabschluss", "erstellung des quartalsabschluss",
    "erstellung von jahresabschlüssen", "erstellung von monatsabschlüssen",
    "erstellung von quartalsabschlüssen",
    "jahresabschlüsse erstellen", "monatsabschlüsse erstellen",
    "quartalsabschlüsse erstellen",
    "konzernabschluss erstellen", "erstellung konzernabschluss",
    "erstellung des konzernabschluss",
    "eigenständige erstellung", "selbständige erstellung",
    "verantwortlich für die erstellung",
]

# Bilanzbuchhalter: Qualifikation (MUSS vorhanden sein)
BILANZ_QUALIFICATION_KEYWORDS = [
    "bilanzbuchhalter", "bilanzbuchhalterin",
    "geprüfter bilanzbuchhalter", "geprüfte bilanzbuchhalterin",
    "bilanzbuchhalter ihk", "bilanzbuchhalter (ihk)",
    "bilanzbuchhalter lehrgang", "bilanzbuchhalter weiterbildung",
    "bilanzbuchhalter zertifikat",
]

# Finanzbuchhalter: Tätigkeiten
FIBU_ACTIVITY_KEYWORDS = [
    "kreditoren", "debitoren", "kreditorenbuchhaltung", "debitorenbuchhaltung",
    "kontenabstimmung", "kontenabstimmungen", "kontenpflege",
    "laufende buchhaltung", "laufende finanzbuchhaltung",
    "hauptbuchhaltung", "sachkontenbuchhaltung",
    "umsatzsteuervoranmeldung", "umsatzsteuer",
    "zahlungsverkehr", "bankbuchhaltung",
    "vorbereitung jahresabschluss", "vorbereitung des jahresabschluss",
    "vorbereitung monatsabschluss", "vorbereitung quartalsabschluss",
    "unterstützung jahresabschluss", "unterstützung bei der erstellung",
    "zuarbeit jahresabschluss", "zuarbeit zum jahresabschluss",
    "mitwirkung jahresabschluss", "mitwirkung bei der erstellung",
    "mitbearbeitung",
    "accounts payable", "accounts receivable",
]

# Nur-Kreditoren Keywords
KREDITOR_ONLY_KEYWORDS = [
    "kreditorenbuchhaltung", "kreditoren", "accounts payable",
    "eingangsrechnungsprüfung", "eingangsrechnungen",
    "zahlungsverkehr lieferanten", "lieferantenrechnungen",
    "kreditorenstammdaten",
]

# Nur-Debitoren Keywords
DEBITOR_ONLY_KEYWORDS = [
    "debitorenbuchhaltung", "debitoren", "accounts receivable",
    "fakturierung", "mahnwesen", "forderungsmanagement",
    "ausgangsrechnungen", "debitorenstammdaten",
    "offene posten debitoren",
]

# Lohnbuchhalter Keywords
LOHN_KEYWORDS = [
    "lohn- und gehaltsabrechnung", "lohnabrechnung", "gehaltsabrechnung",
    "entgeltabrechnung", "payroll", "lohnbuchhaltung",
    "sozialversicherungsmeldungen", "sozialversicherungsbeiträge",
    "lohnsteueranmeldung", "lohnsteuer",
    "personalabrechnung", "monatliche entgeltabrechnung",
]

# Steuerfachangestellte Keywords
STEUFA_KEYWORDS = [
    "steuerfachangestellte", "steuerfachangestellter",
    "steuerkanzlei", "steuerberatung",
    "steuerberater", "steuerberaterin",
    "steuererklärungen", "finanzbehörde",
    "mandantenbetreuung", "mandanten",
]


# ═══════════════════════════════════════════════════════════════
# DATACLASS
# ═══════════════════════════════════════════════════════════════

@dataclass
class RulesClassificationResult:
    """Ergebnis der lokalen Rollen-Klassifizierung."""

    is_leadership: bool = False
    roles: list[str] = field(default_factory=list)
    primary_role: str | None = None
    reasoning: str = ""
    confidence: float = 0.0  # 0.0–1.0, wie sicher ist die Klassifizierung


# ═══════════════════════════════════════════════════════════════
# ENGINE
# ═══════════════════════════════════════════════════════════════

class FinanceRulesEngine:
    """Lokaler regelbasierter Finance-Classifier — trainiert aus OpenAI-Ergebnissen."""

    # ──────────────────────────────────────────────────
    # Hilfs-Methoden
    # ──────────────────────────────────────────────────

    @staticmethod
    def _extract_all_text(candidate: Candidate) -> str:
        """Extrahiert den gesamten Text aus allen Kandidaten-Feldern."""
        parts = []

        if candidate.current_position:
            parts.append(candidate.current_position)

        # Work History — alle Positionen + Beschreibungen
        if candidate.work_history and isinstance(candidate.work_history, list):
            for entry in candidate.work_history:
                if isinstance(entry, dict):
                    if entry.get("position"):
                        parts.append(entry["position"])
                    if entry.get("description"):
                        parts.append(entry["description"])

        return " ".join(parts).lower()

    @staticmethod
    def _extract_qualifications_text(candidate: Candidate) -> str:
        """Extrahiert Text aus Bildung und Weiterbildung."""
        parts = []

        if candidate.education and isinstance(candidate.education, list):
            for entry in candidate.education:
                if isinstance(entry, dict):
                    for key in ("degree", "field_of_study", "institution"):
                        if entry.get(key):
                            parts.append(entry[key])

        if candidate.further_education and isinstance(candidate.further_education, list):
            for entry in candidate.further_education:
                if isinstance(entry, dict):
                    for key in ("degree", "field_of_study", "institution"):
                        if entry.get(key):
                            parts.append(entry[key])

        return " ".join(parts).lower()

    @staticmethod
    def _extract_job_text(job: Job) -> str:
        """Extrahiert den gesamten Text aus einem Job."""
        parts = []
        if job.position:
            parts.append(job.position)
        if job.job_text:
            parts.append(job.job_text)
        return " ".join(parts).lower()

    @staticmethod
    def _has_any_keyword(text: str, keywords: list[str]) -> bool:
        """Prüft ob mindestens ein Keyword im Text vorkommt."""
        for kw in keywords:
            if kw in text:
                return True
        return False

    @staticmethod
    def _count_keywords(text: str, keywords: list[str]) -> int:
        """Zählt wie viele Keywords im Text vorkommen."""
        return sum(1 for kw in keywords if kw in text)

    # ──────────────────────────────────────────────────
    # Leadership-Check
    # ──────────────────────────────────────────────────

    def _is_leadership(self, candidate: Candidate) -> bool:
        """Prüft ob die aktuelle Position eine Führungsposition ist."""
        title = (candidate.current_position or "").lower()
        for kw in LEADERSHIP_TITLE_KEYWORDS:
            if kw in title:
                return True

        # Aktuelle Position Tätigkeiten prüfen (nur erste/aktuelle)
        if candidate.work_history and isinstance(candidate.work_history, list):
            for entry in candidate.work_history[:1]:  # Nur aktuelle Position
                if isinstance(entry, dict) and entry.get("description"):
                    desc = entry["description"].lower()
                    for kw in LEADERSHIP_ACTIVITY_KEYWORDS:
                        if kw in desc:
                            return True
        return False

    def _is_leadership_job(self, job: Job) -> bool:
        """Prüft ob ein Job eine Führungsposition ist."""
        text = self._extract_job_text(job)
        return self._has_any_keyword(text, LEADERSHIP_TITLE_KEYWORDS + LEADERSHIP_ACTIVITY_KEYWORDS)

    # ──────────────────────────────────────────────────
    # Rollen-Erkennung
    # ──────────────────────────────────────────────────

    def _has_bilanz_qualification(self, candidate: Candidate) -> bool:
        """Prüft ob Bilanzbuchhalter-Qualifikation vorhanden ist."""
        qual_text = self._extract_qualifications_text(candidate)
        return self._has_any_keyword(qual_text, BILANZ_QUALIFICATION_KEYWORDS)

    def _has_bilanz_activities(self, text: str) -> bool:
        """Prüft ob Erstellung von Abschlüssen in Tätigkeiten vorkommt."""
        return self._has_any_keyword(text, BILANZ_CREATION_KEYWORDS)

    def _has_fibu_activities(self, text: str) -> bool:
        """Prüft ob Finanzbuchhalter-Tätigkeiten vorkommen."""
        return self._has_any_keyword(text, FIBU_ACTIVITY_KEYWORDS)

    def _has_kredi_activities(self, text: str) -> bool:
        """Prüft ob überwiegend Kreditoren-Tätigkeiten vorkommen."""
        kredi_count = self._count_keywords(text, KREDITOR_ONLY_KEYWORDS)
        debi_count = self._count_keywords(text, DEBITOR_ONLY_KEYWORDS)
        return kredi_count > 0 and debi_count == 0

    def _has_debi_activities(self, text: str) -> bool:
        """Prüft ob überwiegend Debitoren-Tätigkeiten vorkommen."""
        debi_count = self._count_keywords(text, DEBITOR_ONLY_KEYWORDS)
        kredi_count = self._count_keywords(text, KREDITOR_ONLY_KEYWORDS)
        return debi_count > 0 and kredi_count == 0

    def _has_both_kredi_debi(self, text: str) -> bool:
        """Prüft ob sowohl Kreditoren als auch Debitoren vorkommen."""
        kredi = self._has_any_keyword(text, KREDITOR_ONLY_KEYWORDS)
        debi = self._has_any_keyword(text, DEBITOR_ONLY_KEYWORDS)
        return kredi and debi

    def _has_lohn_activities(self, text: str) -> bool:
        """Prüft ob Lohnbuchhalter-Tätigkeiten vorkommen."""
        return self._has_any_keyword(text, LOHN_KEYWORDS)

    def _has_steufa_qualification(self, candidate: Candidate) -> bool:
        """Prüft ob Steuerfachangestellte-Qualifikation vorhanden ist."""
        qual_text = self._extract_qualifications_text(candidate)
        activity_text = self._extract_all_text(candidate)
        return (
            self._has_any_keyword(qual_text, STEUFA_KEYWORDS)
            or self._has_any_keyword(activity_text, ["steuerfachangestellte", "steuerfachangestellter"])
        )

    # ──────────────────────────────────────────────────
    # Hauptmethode: Kandidat klassifizieren
    # ──────────────────────────────────────────────────

    def classify_candidate(self, candidate: Candidate) -> RulesClassificationResult:
        """Klassifiziert einen FINANCE-Kandidaten anhand regelbasierter Analyse."""

        # Kein Werdegang → überspringen
        if not candidate.work_history and not candidate.current_position:
            return RulesClassificationResult(
                reasoning="Kein Werdegang vorhanden",
            )

        # Schritt 1: Leadership-Check
        if self._is_leadership(candidate):
            return RulesClassificationResult(
                is_leadership=True,
                reasoning=f"Leitende Position: {candidate.current_position}",
            )

        # Texte extrahieren
        all_text = self._extract_all_text(candidate)
        roles = []
        reasons = []

        # Schritt 2: Rollen erkennen

        # 2a. Bilanzbuchhalter (strengste Regel: Erstellung + Qualifikation)
        has_bilanz_qual = self._has_bilanz_qualification(candidate)
        has_bilanz_act = self._has_bilanz_activities(all_text)

        if has_bilanz_qual and has_bilanz_act:
            roles.append("Bilanzbuchhalter/in")
            reasons.append("Erstellung von Abschlüssen + Bilanzbuchhalter-Qualifikation")
        elif has_bilanz_act and not has_bilanz_qual:
            # Erstellt Abschlüsse ABER keine Qualifikation → Finanzbuchhalter
            roles.append("Finanzbuchhalter/in")
            reasons.append("Erstellung von Abschlüssen ohne Bilanzbuchhalter-Qualifikation")

        # 2b. Finanzbuchhalter (wenn nicht schon durch Bilanz erkannt)
        if "Finanzbuchhalter/in" not in roles and "Bilanzbuchhalter/in" not in roles:
            if self._has_fibu_activities(all_text):
                roles.append("Finanzbuchhalter/in")
                reasons.append("Finanzbuchhalter-Tätigkeiten erkannt")

        # 2c. Kreditorenbuchhalter
        if self._has_kredi_activities(all_text):
            roles.append("Kreditorenbuchhalter/in")
            reasons.append("Überwiegend Kreditoren-Tätigkeiten")
        elif self._has_both_kredi_debi(all_text):
            # Kredi + Debi → Finanzbuchhalter + Kreditorenbuchhalter
            if "Finanzbuchhalter/in" not in roles:
                roles.append("Finanzbuchhalter/in")
            roles.append("Kreditorenbuchhalter/in")
            reasons.append("Kreditoren + Debitoren über längeren Zeitraum")

        # 2d. Debitorenbuchhalter
        if self._has_debi_activities(all_text):
            roles.append("Debitorenbuchhalter/in")
            reasons.append("Überwiegend Debitoren-Tätigkeiten")
        elif self._has_both_kredi_debi(all_text) and "Debitorenbuchhalter/in" not in roles:
            roles.append("Debitorenbuchhalter/in")
            reasons.append("Kreditoren + Debitoren über längeren Zeitraum")

        # 2e. Lohnbuchhalter
        if self._has_lohn_activities(all_text):
            roles.append("Lohnbuchhalter/in")
            reasons.append("Lohnbuchhalter-Tätigkeiten erkannt")

        # 2f. Steuerfachangestellte (immer + Finanzbuchhalter oder Bilanzbuchhalter)
        if self._has_steufa_qualification(candidate):
            roles.append("Steuerfachangestellte/r")
            reasons.append("Steuerfachangestellte-Qualifikation")
            # Sonderregel: Immer mit Finanzbuchhalter oder Bilanzbuchhalter
            if "Bilanzbuchhalter/in" not in roles and "Finanzbuchhalter/in" not in roles:
                if has_bilanz_qual:
                    roles.insert(0, "Bilanzbuchhalter/in")
                else:
                    roles.insert(0, "Finanzbuchhalter/in")

        # Duplikate entfernen, Reihenfolge beibehalten
        seen = set()
        unique_roles = []
        for r in roles:
            if r not in seen:
                seen.add(r)
                unique_roles.append(r)
        roles = unique_roles

        # Primary Role bestimmen (erste in der Liste = basiert auf aktueller Position)
        primary_role = roles[0] if roles else None

        # Confidence berechnen
        total_keywords_found = sum(
            self._count_keywords(all_text, kw_list)
            for kw_list in [
                BILANZ_CREATION_KEYWORDS, FIBU_ACTIVITY_KEYWORDS,
                KREDITOR_ONLY_KEYWORDS, DEBITOR_ONLY_KEYWORDS,
                LOHN_KEYWORDS, STEUFA_KEYWORDS,
            ]
        )
        confidence = min(1.0, total_keywords_found / 5.0) if roles else 0.0

        return RulesClassificationResult(
            roles=roles,
            primary_role=primary_role,
            reasoning="; ".join(reasons) if reasons else "Keine Finance-Rolle erkannt",
            confidence=round(confidence, 2),
        )

    # ──────────────────────────────────────────────────
    # Job klassifizieren
    # ──────────────────────────────────────────────────

    def classify_job(self, job: Job) -> RulesClassificationResult:
        """Klassifiziert einen FINANCE-Job anhand regelbasierter Analyse."""
        if not job.job_text and not job.position:
            return RulesClassificationResult(reasoning="Keine Stellenbeschreibung vorhanden")

        text = self._extract_job_text(job)
        roles = []
        reasons = []

        # Bilanzbuchhalter: Erstellung von Abschlüssen + Bilanzbuchhalter gefordert
        has_bilanz_keyword = self._has_any_keyword(text, BILANZ_QUALIFICATION_KEYWORDS)
        has_bilanz_act = self._has_any_keyword(text, BILANZ_CREATION_KEYWORDS)

        if has_bilanz_keyword and has_bilanz_act:
            roles.append("Bilanzbuchhalter/in")
            reasons.append("Erstellung Abschlüsse + Bilanzbuchhalter gefordert")
        elif has_bilanz_act and not has_bilanz_keyword:
            roles.append("Finanzbuchhalter/in")
            reasons.append("Abschlüsse erwähnt aber kein Bilanzbuchhalter gefordert")

        # Finanzbuchhalter
        if "Finanzbuchhalter/in" not in roles and "Bilanzbuchhalter/in" not in roles:
            if self._has_any_keyword(text, FIBU_ACTIVITY_KEYWORDS):
                roles.append("Finanzbuchhalter/in")
                reasons.append("Finanzbuchhalter-Tätigkeiten")

        # Kreditorenbuchhalter
        kredi = self._has_any_keyword(text, KREDITOR_ONLY_KEYWORDS)
        debi = self._has_any_keyword(text, DEBITOR_ONLY_KEYWORDS)
        if kredi and not debi:
            roles.append("Kreditorenbuchhalter/in")
            reasons.append("Nur Kreditoren")
        elif debi and not kredi:
            roles.append("Debitorenbuchhalter/in")
            reasons.append("Nur Debitoren")

        # Lohnbuchhalter
        if self._has_any_keyword(text, LOHN_KEYWORDS):
            roles.append("Lohnbuchhalter/in")
            reasons.append("Lohnbuchhalter-Tätigkeiten")

        # Steuerfachangestellte
        if self._has_any_keyword(text, STEUFA_KEYWORDS):
            roles.append("Steuerfachangestellte/r")
            reasons.append("Steuerfachangestellte")

        # Duplikate entfernen
        seen = set()
        unique_roles = []
        for r in roles:
            if r not in seen:
                seen.add(r)
                unique_roles.append(r)

        primary_role = unique_roles[0] if unique_roles else None

        return RulesClassificationResult(
            roles=unique_roles,
            primary_role=primary_role,
            reasoning="; ".join(reasons) if reasons else "Keine Finance-Rolle erkannt",
        )

    # ──────────────────────────────────────────────────
    # Ergebnis anwenden
    # ──────────────────────────────────────────────────

    def apply_to_candidate(self, candidate: Candidate, result: RulesClassificationResult) -> None:
        """Setzt die Klassifizierungsergebnisse auf dem Kandidaten."""
        if result.roles:
            candidate.hotlist_job_title = result.primary_role or result.roles[0]
            candidate.hotlist_job_titles = result.roles

    def apply_to_job(self, job: Job, result: RulesClassificationResult) -> None:
        """Setzt die Klassifizierungsergebnisse auf dem Job."""
        if result.roles:
            job.hotlist_job_title = result.primary_role or result.roles[0]
            job.hotlist_job_titles = result.roles
