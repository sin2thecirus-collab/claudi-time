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

# Leadership-Keywords (Ausschluss) — trainiert aus OpenAI-Vergleich
LEADERSHIP_TITLE_KEYWORDS = [
    # Deutsch
    "leiter", "leiterin", "teamleiter", "teamleiterin", "teamlead", "team lead",
    "abteilungsleiter", "abteilungsleiterin", "bereichsleiter", "bereichsleiterin",
    "gruppenleiter", "gruppenleiterin", "finanzleitung", "stv. finanzleitung",
    "leitung buchhaltung", "leitung rechnungswesen", "leitung der buchhaltung",
    "fachliche leitung", "kaufmännischer leiter", "kaufmännische leiterin",
    "geschäftsführer", "geschäftsführerin", "unternehmensleitung",
    # Englisch
    "head of", "director", "cfo", "chief financial",
    "vice president", "vp ", "finance manager", "accounting manager",
    # NICHT: "supervisor", "manager controlling", "manager finance" — zu generisch
]

LEADERSHIP_ACTIVITY_KEYWORDS = [
    "disziplinarische führung", "fachliche führung", "fachliche und disziplinarische",
    "mitarbeiterverantwortung", "budgetverantwortung", "personalverantwortung",
    "leitung des teams", "aufbau eines teams", "leitung eines teams",
    "führung von mitarbeitern", "teamführung", "mitarbeiterführung",
    "führung eines teams", "verantwortliche leitung",
    # NICHT "führung von" oder "leitung von" allein — zu generisch!
    # "Führung von Konten" oder "Leitung von Projekten" ist kein Leadership
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

# NEGATIVE Kontexte: Phrasen die "Erstellung" enthalten aber KEINE echte
# eigenstaendige Erstellung bedeuten — nur Vorbereitung/Zuarbeit/Mitwirkung.
# Diese muessen VOR dem positiven Match geprueft werden.
BILANZ_CREATION_NEGATIONS = [
    "mitwirkung bei der erstellung",
    "mitwirkung an der erstellung",
    "mithilfe bei der erstellung",
    "unterstützung bei der erstellung",
    "unterstützung der erstellung",
    "zuarbeit bei der erstellung",
    "zuarbeit zur erstellung",
    "vorbereitung und erstellung",  # "Vorbereitung und Erstellung" = nur vorbereiten
    "vorbereitung der erstellung",
    "assistenz bei der erstellung",
    "mitarbeit bei der erstellung",
    "mitarbeit an der erstellung",
]

# Anforderungs-Kontexte fuer Jobs: Bilanzbuchhalter wird nur als WUNSCH
# in den Anforderungen/Qualifikationen erwaehnt, nicht als echte Taetigkeit.
# z.B. "Weiterbildung zum Bilanzbuchhalter wuenschenswert"
JOB_BILANZ_WISH_KEYWORDS = [
    "weiterbildung zum bilanzbuchhalter",
    "weiterbildung zur bilanzbuchhalterin",
    "bilanzbuchhalter wünschenswert",
    "bilanzbuchhalter von vorteil",
    "bilanzbuchhalter erwünscht",
    "idealerweise bilanzbuchhalter",
    "optimalerweise bilanzbuchhalter",
    "gerne bilanzbuchhalter",
    "oder bilanzbuchhalter",
    "zum/zur bilanzbuchhalter",
    "fortbildung bilanzbuchhalter",
    "angehender bilanzbuchhalter",
    "angehende bilanzbuchhalterin",
    "auf dem weg zum bilanzbuchhalter",
]

# Bilanzbuchhalter: Qualifikation (MUSS vorhanden sein) — erweitert
BILANZ_QUALIFICATION_KEYWORDS = [
    "bilanzbuchhalter", "bilanzbuchhalterin",
    "geprüfter bilanzbuchhalter", "geprüfte bilanzbuchhalterin",
    "staatlich geprüfter bilanzbuchhalter", "staatlich geprüfte bilanzbuchhalterin",
    "bilanzbuchhalter ihk", "bilanzbuchhalter (ihk)",
    "bilanzbuchhalter lehrgang", "bilanzbuchhalter weiterbildung",
    "bilanzbuchhalter zertifikat",
    "bilanzbuchhalter international", "bilanzbuchhalter ifrs",
    # Auch in current_position oder work_history title prüfen
    "bilanzbuchhalter/controller", "bilanzbuchhalter / controller",
]

# Finanzbuchhalter: Tätigkeiten — erweitert aus OpenAI-Vergleich
FIBU_ACTIVITY_KEYWORDS = [
    # Kernbegriffe
    "kreditoren", "debitoren", "kreditorenbuchhaltung", "debitorenbuchhaltung",
    "kontenabstimmung", "kontenabstimmungen", "kontenpflege", "kontenklärung",
    "laufende buchhaltung", "laufende finanzbuchhaltung",
    "hauptbuchhaltung", "hauptbuch", "sachkontenbuchhaltung", "sachkonten",
    "finanzbuchhaltung", "finanzbuchhalter", "finanzbuchhalterin",
    # Steuern / Abgaben (NICHT "steuererklärung" — das ist SteuFa-spezifisch!)
    "umsatzsteuervoranmeldung", "umsatzsteuer", "vorsteuer",
    "steuerliche meldungen",
    # Zahlungsverkehr
    "zahlungsverkehr", "bankbuchhaltung", "zahlungslauf", "bankabstimmung",
    # Abschlüsse (vorbereitend — NICHT erstellend!)
    "vorbereitung jahresabschluss", "vorbereitung des jahresabschluss",
    "vorbereitung monatsabschluss", "vorbereitung quartalsabschluss",
    "unterstützung jahresabschluss", "unterstützung bei der erstellung",
    "zuarbeit jahresabschluss", "zuarbeit zum jahresabschluss",
    "mitwirkung jahresabschluss", "mitwirkung bei der erstellung",
    "mitwirkung bei monats", "mitwirkung bei quartals",
    "mitbearbeitung", "mithilfe bei",
    # Englische Begriffe
    "accounts payable", "accounts receivable", "accountant",
    "general ledger", "financial accounting", "bookkeeping",
    # Allgemeine Buchhaltungsbegriffe
    "buchhaltung", "rechnungswesen", "anlagenbuchhaltung",
    "reisekostenabrechnung", "intercompany",
]

# Nur-Kreditoren Keywords
KREDITOR_ONLY_KEYWORDS = [
    "kreditorenbuchhaltung", "kreditoren", "kreditorbuchhalter",
    "accounts payable", "ap-buchhaltung",
    "eingangsrechnungsprüfung", "eingangsrechnungen",
    "zahlungsverkehr lieferanten", "lieferantenrechnungen",
    "kreditorenstammdaten", "rechnungsprüfung",
]

# Nur-Debitoren Keywords
DEBITOR_ONLY_KEYWORDS = [
    "debitorenbuchhaltung", "debitoren", "debitorbuchhalter",
    "accounts receivable", "ar-buchhaltung",
    "fakturierung", "mahnwesen", "forderungsmanagement",
    "ausgangsrechnungen", "debitorenstammdaten",
    "offene posten debitoren", "offene posten",
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
    "steuerfachgehilfin", "steuerfachgehilfe", "steuerfachwirt",
    "steuerkanzlei", "steuerberatung", "steuerberatungsgesellschaft",
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
        """Prüft ob die aktuelle Position eine Führungsposition ist.

        Leadership-TITEL: current_position + erste work_history Position (= aktuelle).
        Leadership-AKTIVITÄTEN: Nur in erster/aktueller Position.
        Alte Positionen zählen NICHT — jemand kann von Teamleiter zu Buchhalter gewechselt haben.
        """
        title = (candidate.current_position or "").lower()
        for kw in LEADERSHIP_TITLE_KEYWORDS:
            if kw in title:
                return True

        # Nur aktuelle/erste work_history Position prüfen
        if candidate.work_history and isinstance(candidate.work_history, list):
            for entry in candidate.work_history[:2]:  # Nur die 2 aktuellsten Positionen
                if isinstance(entry, dict):
                    # Jobtitel prüfen
                    pos = (entry.get("position") or "").lower()
                    for kw in LEADERSHIP_TITLE_KEYWORDS:
                        if kw in pos:
                            return True
                    # Tätigkeiten prüfen (nur erste Position)
            if candidate.work_history and isinstance(candidate.work_history[0], dict):
                desc = (candidate.work_history[0].get("description") or "").lower()
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
        """Prüft ob Bilanzbuchhalter-Qualifikation vorhanden ist.

        Sucht in: education, further_education UND Jobtitel/Position.
        OpenAI-Training zeigt: viele haben 'Bilanzbuchhalter' nur im Jobtitel.
        """
        qual_text = self._extract_qualifications_text(candidate)
        if self._has_any_keyword(qual_text, BILANZ_QUALIFICATION_KEYWORDS):
            return True
        # Auch im Jobtitel / aktueller Position prüfen
        title = (candidate.current_position or "").lower()
        if "bilanzbuchhalter" in title or "bilanzbuchhalterin" in title:
            return True
        # Work-History Jobtitel prüfen
        if candidate.work_history and isinstance(candidate.work_history, list):
            for entry in candidate.work_history:
                if isinstance(entry, dict) and entry.get("position"):
                    pos = entry["position"].lower()
                    if "bilanzbuchhalter" in pos or "bilanzbuchhalterin" in pos:
                        return True
        return False

    def _has_bilanz_activities(self, text: str) -> bool:
        """Prüft ob EIGENSTAENDIGE Erstellung von Abschluessen in Taetigkeiten vorkommt.

        WICHTIG: 'Mitwirkung bei der Erstellung' zaehlt NICHT als eigenstaendige
        Erstellung. Dort steckt zwar 'Erstellung' drin, aber der Kontext ist
        nur vorbereitend/zuarbeitend → Finanzbuchhalter, nicht Bilanzbuchhalter.
        """
        if not self._has_any_keyword(text, BILANZ_CREATION_KEYWORDS):
            return False
        # Negations-Check: Wenn NUR negierte Formen vorkommen, ist es KEINE
        # echte Erstellung. Wir prüfen ob nach Entfernung aller Negationen
        # noch ein positives Match übrig bleibt.
        cleaned = text
        for neg in BILANZ_CREATION_NEGATIONS:
            cleaned = cleaned.replace(neg, "")
        return self._has_any_keyword(cleaned, BILANZ_CREATION_KEYWORDS)

    def _has_fibu_activities(self, text: str) -> bool:
        """Prüft ob Finanzbuchhalter-Tätigkeiten vorkommen."""
        return self._has_any_keyword(text, FIBU_ACTIVITY_KEYWORDS)

    def _has_kredi_activities(self, text: str) -> bool:
        """Prüft ob überwiegend Kreditoren-Tätigkeiten vorkommen.

        Strenger als vorher: Mindestens 2 Kreditoren-Keywords UND keine Debitoren.
        Nur 'kreditoren' allein reicht NICHT — das kann auch ein FiBu sein.
        """
        kredi_count = self._count_keywords(text, KREDITOR_ONLY_KEYWORDS)
        debi_count = self._count_keywords(text, DEBITOR_ONLY_KEYWORDS)
        # Mindestens 2 spezifische Kreditoren-Keywords UND keine Debitoren
        return kredi_count >= 2 and debi_count == 0

    def _has_debi_activities(self, text: str) -> bool:
        """Prüft ob überwiegend Debitoren-Tätigkeiten vorkommen.

        Strenger: Mindestens 2 Debitoren-Keywords UND keine Kreditoren.
        """
        debi_count = self._count_keywords(text, DEBITOR_ONLY_KEYWORDS)
        kredi_count = self._count_keywords(text, KREDITOR_ONLY_KEYWORDS)
        return debi_count >= 2 and kredi_count == 0

    def _has_both_kredi_debi(self, text: str) -> bool:
        """Prüft ob sowohl Kreditoren als auch Debitoren vorkommen.

        Lockerer als Standalone: Mindestens 1 Keyword von jedem reicht,
        weil beide zusammen → FiBu + Kredi + Debi als Multi-Rollen.
        """
        kredi_count = self._count_keywords(text, KREDITOR_ONLY_KEYWORDS)
        debi_count = self._count_keywords(text, DEBITOR_ONLY_KEYWORDS)
        return kredi_count >= 1 and debi_count >= 1

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

        # 2b. Finanzbuchhalter
        # OpenAI vergibt FiBu auch ZUSÄTZLICH zu Bilanz wenn FiBu-Tätigkeiten vorhanden
        if "Finanzbuchhalter/in" not in roles:
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
            # Sonderregel: Immer mit Finanzbuchhalter oder Bilanzbuchhalter pairen
            if has_bilanz_qual and "Bilanzbuchhalter/in" not in roles:
                # Bilanz-Qualifikation vorhanden → Bilanzbuchhalter statt Finanzbuchhalter
                if "Finanzbuchhalter/in" in roles:
                    # FiBu durch Bilanz ersetzen (Bilanz ist die höherwertige Rolle)
                    idx = roles.index("Finanzbuchhalter/in")
                    roles[idx] = "Bilanzbuchhalter/in"
                else:
                    roles.insert(0, "Bilanzbuchhalter/in")
            elif "Bilanzbuchhalter/in" not in roles and "Finanzbuchhalter/in" not in roles:
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
        """Klassifiziert einen FINANCE-Job anhand regelbasierter Analyse.

        WICHTIGE REGEL fuer Bilanzbuchhalter-Vakanzen:
        Eine Stelle ist NUR dann eine echte Bilanzbuchhalter-Vakanz wenn:
        1. Die AUFGABEN eigenstaendige Erstellung von Abschluessen fordern
           (nicht nur Mitwirkung/Vorbereitung/Zuarbeit)
        2. UND Bilanzbuchhalter als ECHTE Anforderung steht (nicht nur
           'Weiterbildung zum Bilanzbuchhalter wuenschenswert')

        Viele Unternehmen suchen Finanzbuchhalter, erwaehnen aber
        'Bilanzbuchhalter-Weiterbildung' als Nice-to-have → das macht
        die Vakanz NICHT zur Bilanzbuchhalter-Stelle.
        """
        if not job.job_text and not job.position:
            return RulesClassificationResult(reasoning="Keine Stellenbeschreibung vorhanden")

        text = self._extract_job_text(job)
        roles = []
        reasons = []

        # Bilanzbuchhalter-Vakanz: STRENGE Pruefung
        # 1. Hat der Job eigenstaendige Abschluss-Erstellung in den Aufgaben?
        has_bilanz_act = self._has_bilanz_activities(text)  # nutzt jetzt Negations-Check

        # 2. Wird "Bilanzbuchhalter" als echte Anforderung erwaehnt?
        has_bilanz_keyword = self._has_any_keyword(text, BILANZ_QUALIFICATION_KEYWORDS)

        # 3. Wird "Bilanzbuchhalter" NUR als Wunsch/Nice-to-have erwaehnt?
        is_bilanz_only_wish = self._has_any_keyword(text, JOB_BILANZ_WISH_KEYWORDS)

        # Wenn Bilanzbuchhalter nur als Wunsch erwaehnt wird UND der Jobtitel
        # kein Bilanzbuchhalter ist → es ist KEINE Bilanzbuchhalter-Vakanz
        job_title_is_bilanz = "bilanzbuchhalter" in (job.position or "").lower()

        if has_bilanz_act and has_bilanz_keyword and (job_title_is_bilanz or not is_bilanz_only_wish):
            roles.append("Bilanzbuchhalter/in")
            reasons.append("Eigenstaendige Erstellung Abschluesse + Bilanzbuchhalter gefordert")
        elif has_bilanz_act and not has_bilanz_keyword:
            roles.append("Finanzbuchhalter/in")
            reasons.append("Abschluesse in Aufgaben aber kein Bilanzbuchhalter gefordert")
        elif has_bilanz_keyword and is_bilanz_only_wish and not job_title_is_bilanz:
            # Bilanzbuchhalter nur als Wunsch → Finanzbuchhalter-Vakanz
            roles.append("Finanzbuchhalter/in")
            reasons.append("Bilanzbuchhalter nur als Weiterbildungswunsch, kein echtes Anforderungsprofil")

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
