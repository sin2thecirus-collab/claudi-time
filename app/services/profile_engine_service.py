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
from datetime import date, datetime, timezone
from uuid import UUID

import httpx
from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import limits, settings
from app.models.candidate import Candidate
from app.models.job import Job

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# GPT SYSTEM PROMPTS
# ══════════════════════════════════════════════════════════════════

CANDIDATE_PROFILE_PROMPT = """Du bist ein erfahrener Personalberater im Bereich Finance & Accounting in Deutschland.

Analysiere den Werdegang eines Kandidaten in 7 Schritten und erstelle ein strukturiertes Profil.

═══════════════════════════════════════════════════════════════
SCHRITT 1: ROLLE ERKENNEN (Haupt-Job)
═══════════════════════════════════════════════════════════════

Welche Rolle hat der Kandidat AKTUELL? Erkenne anhand des Jobtitels:

LEVEL 6 — Leiter/Head (Gesamtverantwortung, Reporting an GF):
  Titel: Leiter Rechnungswesen, Leiter Buchhaltung, Leiter Finanzbuchhaltung,
  Head of Accounting, Head of Finance, Director Accounting,
  Kaufmaennischer Leiter (wenn Schwerpunkt Buchhaltung), VP Finance

LEVEL 5 — Teamleiter (Personalverantwortung fuer ein Team):
  Titel: Teamleiter Buchhaltung, Teamleiter Finanzbuchhaltung,
  Teamlead Accounting, Team Lead Finance, Gruppenleiter Rechnungswesen,
  Abteilungsleiter Buchhaltung, Stellv. Leiter Buchhaltung

LEVEL 4 — Bilanzbuchhalter (eigenstaendige Abschlusserstellung):
  Titel: Bilanzbuchhalter, Senior Accountant, Hauptbuchhalter,
  Konzernbuchhalter, Group Accountant, Accounting Manager (ohne Personal),
  Abschlussersteller, Jahresabschlussersteller, Senior Bilanzbuchhalter

LEVEL 3 — Finanzbuchhalter (breit aufgestellt, Kredi+Debi+mehr):
  Titel: Finanzbuchhalter, Accountant, Alleinbuchhalter,
  Sachbearbeiter Finanzbuchhaltung (wenn breites Spektrum),
  Buchhalter (wenn mehrere Bereiche), Senior Finanzbuchhalter

LEVEL 2 — Sachbearbeiter (NUR ein Teilbereich):
  Titel: Kreditorenbuchhalter, Debitorenbuchhalter,
  Sachbearbeiter Kreditoren, Sachbearbeiter Debitoren,
  Accounts Payable Clerk, Accounts Receivable Clerk,
  Buchhalter (wenn NUR Kreditoren ODER NUR Debitoren)

WICHTIG: Der Titel gibt nur den ERSTEN HINWEIS! Schritte 2-4 koennen das Level korrigieren.
ACHTUNG: "Senior" = erfahren, NICHT = Teamleiter!
NICHT in der Skala: Junior/Praktikant → Level 2, CFO → gehoert nicht hierher

═══════════════════════════════════════════════════════════════
SCHRITT 2: KONTEXT VERSTEHEN
═══════════════════════════════════════════════════════════════

In welchem Umfeld arbeitet der Kandidat?
- Kanzlei/Steuerberatung: Mandantenbetreuung, DATEV-Umfeld
- KMU (unter 250 MA): Oft Alleinbuchhalter, breites Aufgabenspektrum
- Mittelstand (250-1000 MA): Spezialisierte Abteilungen
- Konzern (1000+ MA): SAP-Umfeld, IFRS, Konsolidierung, Shared Services

Der Kontext beeinflusst die Bewertung:
- Alleinbuchhalter im KMU der alles macht = MINDESTENS Level 3
- "Buchhalter" im Konzern der nur AP bucht = Level 2

═══════════════════════════════════════════════════════════════
SCHRITT 3: AUFGABEN ANALYSIEREN
═══════════════════════════════════════════════════════════════

Analysiere die VERBEN in den Aufgabenbeschreibungen:
- "Eigenstaendige Erstellung" von Abschluessen → Level 4+
- "Vorbereitung" von Abschluessen → Level 3
- "Mitwirkung bei der Erstellung" → Level 3 (NICHT Level 4!)
- "Unterstuetzung bei" → Level 2-3

Achte auf die REIHENFOLGE: Was steht zuerst = Hauptaufgabe
Achte auf AUSFUEHRLICHKEIT: Detailliert beschrieben = Schwerpunkt

Aufgaben-Checkliste (je mehr davon, desto hoeher das Level):
□ Kreditoren → Basis (Level 2+)
□ Debitoren → Basis (Level 2+)
□ Kreditoren + Debitoren gleichzeitig → Level 3+
□ Umsatzsteuer/USt-Voranmeldungen → Level 3+
□ Anlagenbuchhaltung → Level 3+
□ Intercompany-Abstimmung → Level 3-4
□ JA-Vorbereitung → Level 3
□ Eigenstaendige Monats-/Jahresabschluesse HGB → Level 4+
□ IFRS-Abschluesse → Level 4+
□ Konsolidierung → Level 4+
□ Personalverantwortung/Teamleitung → Level 5+
□ Reporting an Geschaeftsfuehrung → Level 5-6
□ Gesamtverantwortung Rechnungswesen → Level 6

═══════════════════════════════════════════════════════════════
SCHRITT 4: QUALIFIKATION PRUEFEN
═══════════════════════════════════════════════════════════════

Qualifikationen setzen ein MINIMUM-Level (Floor):

IHK Bilanzbuchhalter / Gepr. Bilanzbuchhalter → MINDESTENS Level 4
  (egal welcher Jobtitel — wer die Pruefung hat, ist min. Level 4)

Steuerfachangestellte (abgeschlossene 3-jaehrige Ausbildung) → MINDESTENS Level 3
  Die Ausbildung umfasst: Kreditoren, Debitoren, USt, Anlagenbuchhaltung,
  JA-Vorbereitung. Das ist eine VOLLWERTIGE FiBu-Qualifikation!
  Auch wenn KEINE Taetigkeiten im CV stehen → trotzdem Level 3!
  Erkenne: "Steuerfachangestellte", "StFA", "gelernte Steuerfachangestellte",
  "Ausbildung zur/zum Steuerfachangestellten"

IHK Fachkraft Rechnungswesen → Level 2-3
Studium BWL/Accounting → je nach Aufgaben Level 3+
Fernlehrgang Buchhaltung → kein Floor, Aufgaben entscheiden

ACHTUNG: Finanzwirt ≠ Buchhaltung! (Finanzwirt = Finanzamt/oeffentlicher Dienst)

═══════════════════════════════════════════════════════════════
SCHRITT 5: ERFAHRUNG RICHTIG ZAEHLEN
═══════════════════════════════════════════════════════════════

- NUR relevante Buchhaltungserfahrung zaehlen!
- Quereinsteiger: Ab wann echte Buchhaltung? Vorher zaehlt NICHT
- Letzte 5-10 Jahre sind am relevantesten, nicht uralte Praktika
- Elternzeit/Luecken: Nicht als Erfahrung zaehlen
- Kanzlei-Erfahrung zaehlt als Buchhaltungserfahrung

═══════════════════════════════════════════════════════════════
SCHRITT 6: LEVEL BESTAETIGEN
═══════════════════════════════════════════════════════════════

Jetzt Level aus Schritten 1-5 zusammenfuehren:
1. Titel (Schritt 1) gibt den HINWEIS
2. Aufgaben (Schritt 3) BESTAETIGEN oder KORRIGIEREN das Level
3. Qualifikation (Schritt 4) setzt den FLOOR (Minimum)
4. Das hoechste der drei Werte gewinnt

Beispiele:
- Titel "Buchhalter" + macht nur Kreditoren → Level 2
- Titel "Buchhalter" + macht Kredi+Debi+USt+Anlagen → Level 3
- Titel "Finanzbuchhalter" + erstellt eigenstaendig Abschluesse → Level 4
- Titel "Sachbearbeiter" + hat IHK Bilanzbuchhalter → Level 4 (Floor!)
- Titel "Steuerfachangestellte" + keine Aufgaben → Level 3 (Floor!)
- Titel "Senior Accountant" + macht nur Debitoren → Level 2 (Aufgaben korrigieren!)

═══════════════════════════════════════════════════════════════
SCHRITT 7: BESONDERHEITEN ERKENNEN
═══════════════════════════════════════════════════════════════

Software-Oekosysteme (wichtig fuers Matching!):
- DATEV-Welt: DATEV, DATEV Unternehmen online, Kanzlei-Rechnungswesen → Kanzlei/KMU
- SAP-Welt: SAP FI, SAP CO, SAP S/4HANA → Konzern/Grossunternehmen
- Umstieg DATEV↔SAP = 6-12 Monate Einarbeitung!
- Andere: Lexware, Addison, Sage, Microsoft Dynamics, Oracle, Navision, BMD, Diamant, Lucanet

Besondere Kenntnisse:
- IFRS (International Accounting) → wertvoller Zusatz
- Konsolidierung → Konzern-Erfahrung
- Englisch (verhandlungssicher) → internationales Umfeld
- Lohnbuchhaltung = ANDERES Fachgebiet, nicht Finanzbuchhaltung

Karriere-Trajektorie:
- "aufsteigend": Klarer Aufstieg (mehr Verantwortung ueber die Jahre)
- "lateral": Gleichbleibendes Niveau, Wechsel zwischen aehnlichen Rollen
- "absteigend": Rueckgang der Verantwortung (selten)
- "einstieg": Berufseinsteiger oder erste 1-2 Jahre

Zertifikate normalisieren:
- Alle Varianten von "Bilanzbuchhalter IHK" / "Gepr. Bilanzbuchhalter" → "Bilanzbuchhalter"
- Egal ob unter Ausbildung, Weiterbildung oder Zertifikate

Branchen erkennen aus Firmennamen + Taetigkeiten:
z.B. Maschinenbau, Automotive, Pharma, IT, Handel, Logistik, Immobilien,
Versicherung, Bank/Finanzdienstleistung, Kanzlei, Steuerberatung,
Gesundheit, Energieversorgung etc.

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
  ],
  "certifications": ["Bilanzbuchhalter"],
  "industries": ["Maschinenbau", "Automotive"]
}

REGELN:
- seniority_level: Ergebnis aus den 7 Schritten. Skala 2-6.
- career_trajectory: "aufsteigend" / "lateral" / "absteigend" / "einstieg"
- years_experience: NUR relevante Buchhaltungs-Erfahrung in Jahren
- current_role_summary: 1-2 Saetze ueber AKTUELLE Taetigkeiten (nicht Historie!)
- structured_skills: Max 15 Skills, nur die relevantesten
  - proficiency: "grundlagen" / "fortgeschritten" / "experte"
  - recency: "aktuell" (letzte 2 Jahre) / "kuerzlich" (2-5 Jahre) / "veraltet" (5+ Jahre)
  - category: "fachlich" / "software" / "taetigkeitsfeld" / "zertifizierung" / "branche"
- certifications: Normalisierte Zertifikate. Leeres Array wenn keine.
- industries: Branchen aus work_history. Leeres Array wenn unklar."""


JOB_PROFILE_PROMPT = """Du bist ein erfahrener Personalberater mit tiefem Fachwissen im Bereich Finance & Accounting in Deutschland.

Deine Aufgabe: Analysiere eine Stellenanzeige und erstelle ein strukturiertes Profil.

═══════════════════════════════════════════════════════════════
SENIORITY-LEVELS (Finance/Buchhaltung-spezifisch, Skala 2-6)
═══════════════════════════════════════════════════════════════

2 = Sachbearbeiter / Buchhalter (operative Taetigkeiten, Teilbereiche)
3 = Finanzbuchhalter (eigenstaendig, Mitwirkung bei Abschluessen)
    → "Senior Finanzbuchhalter" = erfahrener Level 3, NICHT Teamleiter!
4 = Bilanzbuchhalter (eigenstaendige Erstellung von Abschluessen)
5 = Teamleiter Buchhaltung (Fuehrungsverantwortung)
6 = Leiter Rechnungswesen / Head of Accounting

WICHTIG BEI DER EINSTUFUNG DER STELLE:
- Eine Stelle ist NUR Level 4+, wenn die AUFGABEN klar eigenstaendige Abschlusserstellung fordern
- "Bilanzbuchhalter wuenschenswert" in Anforderungen macht die Stelle NICHT zu Level 4
- Entscheidend sind die AUFGABEN, nicht der Jobtitel oder gewuenschte Qualifikationen
- "Mitwirkung bei Abschluessen" = Level 3, NICHT Level 4
- Junior/Praktikant → Level 2 vergeben. CFO → gehoert nicht hierher.

═══════════════════════════════════════════════════════════════
WORK ARRANGEMENT (Arbeitsmodell)
═══════════════════════════════════════════════════════════════

Erkenne aus dem Stellentext:
- "100% Remote" / "komplett remote" / "Homeoffice" → "remote"
- "Hybrid" / "2-3 Tage Buero" / "flexible Arbeitszeiten" → "hybrid"
- Kein Hinweis oder "Praesenz" / "vor Ort" → "vor_ort"

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
    {"skill": "Umsatzsteuer", "importance": "essential", "category": "fachlich"}
  ],
  "required_certifications": [{"name": "Bilanzbuchhalter", "importance": "essential"}],
  "detected_erp": ["DATEV"],
  "work_arrangement": "vor_ort"
}

REGELN:
- seniority_level: Basierend auf den AUFGABEN der Stelle, NICHT auf dem Titel. Skala 2-6.
- role_summary: 1-2 Saetze ueber die Kern-Taetigkeiten der Stelle
- required_skills: Jeder geforderte Skill mit Wichtigkeit
  - importance: "essential" (Muss-Kriterium) / "preferred" (Wunsch-Kriterium)
  - category: "fachlich" / "software" / "taetigkeitsfeld" / "zertifizierung" / "branche"
- Maximal 12 Skills, nur die relevantesten
- Trenne klar zwischen echten Anforderungen und "nice-to-have"
- required_certifications: Wenn Zertifikate gefordert (z.B. "Bilanzbuchhalter IHK"). Leeres Array wenn keine.
  - name: Normalisierter Name (z.B. "Bilanzbuchhalter")
  - importance: "essential" oder "preferred"
- detected_erp: Im Stellentext erwaehnte ERP/Software-Systeme (DATEV, SAP FI, SAP CO, Addison, Lexware, Sage, etc.). Leeres Array wenn keine.
- work_arrangement: "remote" / "hybrid" / "vor_ort" — aus Stellentext ableiten."""


# ══════════════════════════════════════════════════════════════════
# DATENKLASSEN
# ══════════════════════════════════════════════════════════════════

@dataclass
class CandidateProfile:
    """Ergebnis der Kandidaten-Profil-Extraktion."""
    candidate_id: UUID
    seniority_level: int  # 2-6
    career_trajectory: str  # aufsteigend/lateral/absteigend/einstieg
    years_experience: int
    current_role_summary: str
    structured_skills: list[dict]  # [{skill, proficiency, recency, category}]
    certifications: list[str] = field(default_factory=list)  # z.B. ["Bilanzbuchhalter"]
    industries: list[str] = field(default_factory=list)  # z.B. ["Maschinenbau", "Pharma"]
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
    required_certifications: list[dict] = field(default_factory=list)  # [{name, importance}]
    detected_erp: list[str] = field(default_factory=list)  # z.B. ["DATEV", "SAP FI"]
    work_arrangement: str = "vor_ort"  # "remote" / "hybrid" / "vor_ort"
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
        if c.erp:
            parts.append(f"ERP-Systeme: {', '.join(c.erp[:10])}")

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
        if not isinstance(seniority, int) or seniority < 2 or seniority > 6:
            seniority = 2  # Default: Sachbearbeiter

        trajectory = data.get("career_trajectory", "lateral")
        if trajectory not in ("aufsteigend", "lateral", "absteigend", "einstieg"):
            trajectory = "lateral"

        years = data.get("years_experience", 0)
        if not isinstance(years, int) or years < 0:
            years = 0

        # Zertifikate + Branchen aus neuen GPT-Feldern
        certifications = data.get("certifications", [])
        if not isinstance(certifications, list):
            certifications = []
        industries = data.get("industries", [])
        if not isinstance(industries, list):
            industries = []

        profile = CandidateProfile(
            candidate_id=candidate_id,
            seniority_level=seniority,
            career_trajectory=trajectory,
            years_experience=years,
            current_role_summary=data.get("current_role_summary", "")[:500],
            structured_skills=data.get("structured_skills", [])[:15],
            certifications=certifications[:10],
            industries=industries[:10],
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
        candidate.v2_certifications = profile.certifications
        candidate.v2_industries = profile.industries
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
        if not isinstance(seniority, int) or seniority < 2 or seniority > 6:
            seniority = 2

        # Neue Felder aus GPT-Antwort
        required_certs = data.get("required_certifications", [])
        if not isinstance(required_certs, list):
            required_certs = []
        detected_erp = data.get("detected_erp", [])
        if not isinstance(detected_erp, list):
            detected_erp = []
        work_arr = data.get("work_arrangement", "vor_ort")
        if work_arr not in ("remote", "hybrid", "vor_ort"):
            work_arr = "vor_ort"

        profile = JobProfile(
            job_id=job_id,
            seniority_level=seniority,
            role_summary=data.get("role_summary", "")[:500],
            required_skills=data.get("required_skills", [])[:12],
            required_certifications=required_certs[:5],
            detected_erp=detected_erp[:10],
            work_arrangement=work_arr,
            input_tokens=result["input_tokens"],
            output_tokens=result["output_tokens"],
        )

        # In DB speichern
        now = datetime.now(timezone.utc)
        job.v2_seniority_level = profile.seniority_level
        job.v2_required_skills = profile.required_skills
        job.v2_role_summary = profile.role_summary
        job.v2_profile_created_at = now
        # Neue Felder: work_arrangement wird direkt auf dem Job gesetzt
        if not job.work_arrangement:
            job.work_arrangement = profile.work_arrangement
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
        force_reprofile: bool = False,
    ) -> BackfillResult:
        """Backfill: Erstellt Profile fuer alle Kandidaten OHNE v2-Profil.

        Args:
            batch_size: Wie viele pro Commit-Batch
            max_total: Maximum (0 = alle)
            progress_callback: Optional callback(processed, total)
            force_reprofile: Wenn True, werden ALLE Profile neu erstellt (fuer v2.5 Upgrade)

        Returns:
            BackfillResult mit Statistiken
        """
        result = BackfillResult()

        # Zaehle fehlende Profile — NUR FINANCE-Kandidaten, max 58 Jahre
        max_age = 58
        cutoff_date = date(date.today().year - max_age, date.today().month, date.today().day)
        conditions = [
            Candidate.deleted_at.is_(None),
            Candidate.hidden == False,
            Candidate.hotlist_category == "FINANCE",
            # Kandidaten aelter als 58 ausschliessen (nicht vermittelbar)
            or_(
                Candidate.birth_date.is_(None),  # Kein Geburtsdatum → trotzdem profilen
                Candidate.birth_date >= cutoff_date,  # Geboren nach Cutoff = juenger als 58
            ),
        ]
        if not force_reprofile:
            conditions.append(Candidate.v2_profile_created_at.is_(None))

        count_result = await self.db.execute(
            select(func.count(Candidate.id)).where(*conditions)
        )
        total_missing = count_result.scalar() or 0
        result.total = min(total_missing, max_total) if max_total > 0 else total_missing

        if result.total == 0:
            logger.info("Backfill Kandidaten: Alle FINANCE-Profile vorhanden.")
            return result

        mode = "Re-Profiling (v2.5)" if force_reprofile else "Backfill"
        logger.info(f"{mode} Kandidaten: {result.total} FINANCE-Profile zu erstellen...")

        # FINANCE-Kandidaten laden (aelteste zuerst)
        query = (
            select(Candidate.id)
            .where(*conditions)
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
        force_reprofile: bool = False,
    ) -> BackfillResult:
        """Backfill: Erstellt Profile fuer alle Jobs OHNE v2-Profil.

        Args:
            batch_size: Wie viele pro Commit-Batch
            max_total: Maximum (0 = alle)
            progress_callback: Optional callback(processed, total)
            force_reprofile: Wenn True, werden ALLE Profile neu erstellt (fuer v2.5 Upgrade)

        Returns:
            BackfillResult mit Statistiken
        """
        result = BackfillResult()

        # Zaehle fehlende Profile — NUR FINANCE-Jobs
        conditions = [
            Job.deleted_at.is_(None),
            Job.hotlist_category == "FINANCE",
        ]
        if not force_reprofile:
            conditions.append(Job.v2_profile_created_at.is_(None))

        count_result = await self.db.execute(
            select(func.count(Job.id)).where(*conditions)
        )
        total_missing = count_result.scalar() or 0
        result.total = min(total_missing, max_total) if max_total > 0 else total_missing

        if result.total == 0:
            logger.info("Backfill Jobs: Alle FINANCE-Profile vorhanden.")
            return result

        mode = "Re-Profiling (v2.5)" if force_reprofile else "Backfill"
        logger.info(f"{mode} Jobs: {result.total} FINANCE-Profile zu erstellen...")

        # FINANCE-Jobs laden (aelteste zuerst)
        query = (
            select(Job.id)
            .where(*conditions)
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
