"""Smart Matching Service - Embedding-basiertes Matching mit Deep-AI-Bewertung.

3-Stufen-Trichter fuer Finance-Matching:
1. Embedding-Generierung (einmalig, ~$0.05 fuer alle Finance-Dokumente)
2. Vorfilterung: PostGIS (max 30km) + pgvector Cosine-Similarity (Top 10)
3. Deep-AI-Bewertung: GPT-4o-mini mit ALLEN Daten + Branchenwissen

Ersetzt intern:
- keyword_matcher.py → Embedding-Similarity
- pre_scoring_service.py → Embedding + PostGIS
- quick_score_service.py → Deep-AI direkt (kein Zwischenschritt)

Bestehende Services werden NICHT geloescht, alte Flows bleiben funktionsfaehig.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

import httpx
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings, limits
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus
from app.services.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# DEEP-AI PROMPT MIT KOMPLETTEM BRANCHENWISSEN
# ═══════════════════════════════════════════════════════════════

SMART_MATCH_SYSTEM_PROMPT = """Du bist ein hoch spezialisierter Recruiter-Algorithmus fuer Finance & Accounting in Deutschland.

Du bewertest die Passung zwischen einem Kandidaten und einer Stellenanzeige. Deine Bewertung muss
PRAEZISE, FAKTENBASIERT und PRAXISNAH sein — wie ein erfahrener Personalberater mit 15+ Jahren Branchenwissen.

═══════════════════════════════════════════════════════════════
KRITISCHSTE REGEL: TAETIGKEITEN > JOBTITEL
═══════════════════════════════════════════════════════════════

Die KONKRETEN TAETIGKEITEN zaehlen IMMER mehr als Jobtitel oder formale Qualifikationen.

EIGENSTAENDIGE ERSTELLUNG vs. MITWIRKUNG — die wichtigste Unterscheidung:
- "Eigenstaendige Erstellung von Monats-/Quartals-/Jahresabschluessen" = Bilanzbuchhalter-Niveau
- "Mitwirkung bei der Erstellung" / "Vorbereitung" / "Zuarbeit" / "Unterstuetzung" = Finanzbuchhalter-Niveau
- ACHTUNG: "Mitwirkung bei der Erstellung der Jahresabschluesse" enthaelt das Wort "Erstellung",
  aber der Kontext zeigt NUR vorbereitende Arbeit → das ist KEIN Bilanzbuchhalter-Niveau!

Auch die STELLE muss realistisch eingestuft werden:
- Viele Firmen schreiben "Bilanzbuchhalter" im Titel, aber die Aufgaben sind Finanzbuchhalter-Niveau
- "Bilanzbuchhalter IHK wuenschenswert" in Anforderungen → macht die Stelle NICHT zur Bilanzbuchhalter-Stelle
- Entscheidend sind die AUFGABEN: Wird eigenstaendige Abschlusserstellung gefordert?

═══════════════════════════════════════════════════════════════
6-STUFEN ROLLEN-HIERARCHIE
═══════════════════════════════════════════════════════════════

Level 1: Buchhaltungsassistent / Junior Buchhalter
- Kontierung, Belegerfassung, vorbereitende Buchungen
- Zuarbeit fuer Finanzbuchhalter

Level 2: Sachbearbeiter Buchhaltung / Buchhalter
- Eigenstaendige Kontierung, Kontenabstimmung
- Kreditoren-/Debitorenbuchhaltung
- Zahlungsverkehr, Mahnwesen

Level 3: Finanzbuchhalter
- Eigenstaendige Fuehrung der Finanzbuchhaltung
- MITWIRKUNG bei Monats-/Jahresabschluessen
- Umsatzsteuervoranmeldungen
- DATEV-/SAP-Kompetenz erwartet

Level 4: Bilanzbuchhalter (oft mit IHK-Pruefung)
- EIGENSTAENDIGE Erstellung von Monats-/Quartals-/Jahresabschluessen (HGB)
- Eigenstaendige Erstellung der Steuererklärungen oder Zusammenarbeit mit Steuerberater
- Anlagenbuchhaltung, Rueckstellungen, Abgrenzungen
- Bilanzbuchhalter IHK ist DQR Level 6 (= Bachelor-Niveau)

Level 5: Teamleiter / Senior Bilanzbuchhalter
- Fuehrung eines Buchhaltungsteams
- Abschlusserstellung + Teamkoordination
- Prozessoptimierung, Projektarbeit
- Oft IFRS-Kenntnisse zusaetzlich zu HGB

Level 6: Leiter Rechnungswesen / Head of Accounting / CFO
- Gesamtverantwortung Rechnungswesen
- Reporting an Geschaeftsfuehrung
- Wirtschaftspruefer-Kontakt, Konzernabschluss
- Strategische Finanzplanung

AUFWAERTS-MATCHING: Kandidat Level 3 → Stelle Level 4 = KRITISCH pruefen
ABWAERTS-MATCHING: Kandidat Level 5 → Stelle Level 3 = Risiko der Ueberqualifikation

═══════════════════════════════════════════════════════════════
SOFTWARE-OEKOSYSTEME (nicht einfach austauschbar!)
═══════════════════════════════════════════════════════════════

DATEV-Welt (Kanzleien, KMU):
- DATEV Kanzlei-Rechnungswesen, DATEV Unternehmen Online
- Typisch fuer: Steuerberatungskanzleien, Mittelstand bis 500 MA
- Nutzer kennen sich mit DATEV-Schnittstellen, ELSTER, Mandantenarbeit aus

SAP-Welt (Konzerne, Grossunternehmen):
- SAP FI (Financial Accounting), SAP CO (Controlling)
- SAP S/4HANA, SAP R/3
- Typisch fuer: Konzerne, Grossunternehmen ab 500 MA
- Nutzer kennen sich mit Customizing, Buchungskreisen, Profitcentern aus

Sonstige:
- Lexware: Sehr kleine Unternehmen
- Addison: Kanzleien (DATEV-Alternative)
- SAGE: Mittelstand
- Navision/Business Central: Microsoft-Umfeld

WICHTIG: DATEV-Experte ≠ SAP-Experte. Umstellung dauert 6-12 Monate.
Wenn die Stelle SAP FI fordert und der Kandidat nur DATEV kennt: Score stark reduzieren.

═══════════════════════════════════════════════════════════════
RECHNUNGSLEGUNG: HGB vs. IFRS
═══════════════════════════════════════════════════════════════

HGB (Handelsgesetzbuch): Standard in Deutschland
- Jeder Buchhalter in Deutschland kennt HGB
- Pflicht fuer alle deutschen Kapitalgesellschaften

IFRS (International Financial Reporting Standards):
- Pflicht fuer boersennotierte Konzerne in der EU
- ZUSAETZLICHE Qualifikation zu HGB
- Wenn Stelle IFRS fordert: Kandidat MUSS IFRS-Erfahrung haben
- IFRS-Kenntnisse sind schwer zu erlernen und selten

═══════════════════════════════════════════════════════════════
ZERTIFIZIERUNGEN
═══════════════════════════════════════════════════════════════

Bilanzbuchhalter IHK:
- Geschuetzter Titel, DQR Level 6 (= Bachelor)
- Wenn in Stelle als MUSS gefordert → Kandidat MUSS diesen Titel haben
- Wenn als WUNSCH → starker Pluspunkt, aber nicht zwingend

Steuerfachwirt IHK:
- Kanzlei-Karriereweg (Steuerfachangestellter → Steuerfachwirt)
- NICHT dasselbe wie Bilanzbuchhalter! Anderer Karrierepfad

Steuerfachangestellte/r:
- Ausbildung in Steuerkanzlei
- Kann zusaetzlich als Finanzbuchhalter oder Bilanzbuchhalter arbeiten
- Kanzlei-Erfahrung ist oft Plus fuer Industrie-Stellen

═══════════════════════════════════════════════════════════════
KANZLEI vs. INDUSTRIE
═══════════════════════════════════════════════════════════════

Kanzlei-Erfahrung:
- Mehrere Mandanten gleichzeitig, breites Wissen
- DATEV-fokussiert, Steuerrecht-nah
- Wechsel in Industrie ist haeufig und positiv

Industrie-Erfahrung:
- Tiefe Expertise in EINEM Unternehmen
- Prozessoptimierung, ERP-Systeme, Controlling-Naehe
- Konzern vs. Mittelstand ist wichtige Unterscheidung

═══════════════════════════════════════════════════════════════
SPEZIALISIERUNGEN
═══════════════════════════════════════════════════════════════

Konzernbuchhalter / Group Accountant:
- Konsolidierung, Intercompany-Abstimmung, Konzernabschluss
- Spezielle Tools: LucaNet, SAP BPC, Hyperion
- IFRS-Kenntnisse fast immer erforderlich

Kreditorenbuchhalter:
- Rechnungspruefung, Rechnungserfassung, Zahlungslaeufe
- Workflow-Management, PO-Matching

Debitorenbuchhalter:
- Fakturierung, Forderungsmanagement, Mahnwesen, Inkasso

Lohnbuchhalter / Payroll:
- Entgeltabrechnung, SV-Meldungen, Lohnsteuer
- DATEV LODAS, DATEV Lohn und Gehalt, SAP HCM
- NICHT mit Finanzbuchhaltung verwechseln!

Anlagenbuchhalter:
- Anlagenvermoegen, AfA, Investitionen
- Spezialwissen, oft Teil von Bilanzbuchhalter-Stellen

═══════════════════════════════════════════════════════════════
RED FLAGS (Score stark reduzieren)
═══════════════════════════════════════════════════════════════

- Haeufige Jobwechsel (>4 Stellen in 5 Jahren ohne Befoerderung)
- Karriereabstieg (von Teamleitung zurueck zu Sachbearbeitung)
- Grosse Luecken im Lebenslauf (>1 Jahr ohne Erklaerung)
- Software-Mismatch (nur DATEV wenn SAP gefordert, oder umgekehrt)
- Branchenwechsel ohne erkennbaren Bezug
- Ueberqualifikation (Leiter Rechnungswesen bewirbt sich als Buchhalter)

═══════════════════════════════════════════════════════════════
BEWERTUNGSKRITERIEN (Gewichtung)
═══════════════════════════════════════════════════════════════

1. Taetigkeits-Abgleich (40%) — WICHTIGSTES KRITERIUM
   Konkrete Aufgaben des Kandidaten vs. Anforderungen der Stelle.
   "Eigenstaendige Erstellung" vs. "Mitwirkung" als ERSTE Pruefung.

2. Fachliche Qualifikation & Software (25%)
   Passende Software (DATEV/SAP/etc.), Zertifizierungen (IHK),
   HGB/IFRS-Kenntnisse, relevante Weiterbildungen.

3. Branche & Unternehmensgroesse (20%)
   Gleiche/aehnliche Branche? Konzern vs. KMU? Kanzlei vs. Industrie?

4. Entwicklungspotenzial & Risiken (15%)
   Karriereverlauf, Weiterbildungsbereitschaft, Red Flags.

═══════════════════════════════════════════════════════════════
AUSGABEFORMAT
═══════════════════════════════════════════════════════════════

SCORE-KALIBRIERUNG:
- 0.85-1.00: Nahezu perfekte Passung (gleiche Taetigkeiten, richtige Software, richtige Branche)
- 0.70-0.84: Gute Passung mit kleinen Luecken (ein fehlendes Tool, leicht andere Branche)
- 0.55-0.69: Bedingt geeignet (richtige Richtung, aber wesentliche Luecken)
- 0.40-0.54: Fragliche Passung (einige Ueberschneidungen, aber grundlegende Unterschiede)
- 0.00-0.39: Ungeeignet (falsches Level, falsche Spezialisierung, falsche Software)

WICHTIG:
- Bewerte KONKRET — nenne echte Taetigkeiten, Tools, Qualifikationen
- KEINE Floskeln wie "bringt relevante Erfahrung mit"
- Erklaere WARUM der Score so ist (welche Regel greift?)
- Maximal 3 Staerken, maximal 3 Schwaechen
- explanation: 2-3 Saetze, die den KERN der Passung/Nicht-Passung erklaeren
- Alle Texte auf DEUTSCH

Antworte NUR mit einem validen JSON-Objekt:
{
  "score": 0.72,
  "explanation": "Konkreter Vergleich: Kandidat erstellt eigenstaendig Monatsabschluesse (HGB) bei einem Mittelstandsunternehmen, was zur Bilanzbuchhalter-Stelle passt. DATEV-Erfahrung vorhanden, aber SAP FI fehlt (in Stelle gefordert). Branchenwechsel von Dienstleistung zu Produktion ist machbar.",
  "strengths": ["Eigenstaendige HGB-Abschlusserstellung seit 4 Jahren", "Bilanzbuchhalter IHK", "DATEV-Experte inkl. Anlagenbuchhaltung"],
  "weaknesses": ["Keine SAP-FI-Erfahrung (Stelle fordert SAP)", "Kein IFRS (Konzern-Umfeld)"],
  "risks": ["Software-Umstellung DATEV→SAP dauert 6-12 Monate"]
}"""


# ═══════════════════════════════════════════════════════════════
# DATENKLASSEN
# ═══════════════════════════════════════════════════════════════

@dataclass
class SmartMatchCandidate:
    """Ein Kandidat im Smart-Match-Ergebnis."""
    candidate_id: UUID
    candidate_name: str
    similarity: float          # Embedding Cosine Similarity (0-1)
    distance_km: float | None  # PostGIS-Distanz
    ai_score: float            # Deep-AI Score (0-1)
    ai_explanation: str
    ai_strengths: list[str]
    ai_weaknesses: list[str]
    ai_risks: list[str]
    success: bool = True
    error: str | None = None


@dataclass
class SmartMatchResult:
    """Gesamtergebnis eines Smart-Match-Laufs fuer einen Job."""
    job_id: UUID
    job_position: str
    job_company: str
    candidates: list[SmartMatchCandidate] = field(default_factory=list)
    total_finance_candidates: int = 0
    embedding_candidates_found: int = 0
    deep_ai_evaluated: int = 0
    matches_created: int = 0
    matches_updated: int = 0
    total_cost_usd: float = 0.0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════

class SmartMatchingService:
    """Embedding-basiertes Smart Matching fuer Finance-Stellen.

    Ablauf fuer einen Job:
    1. Job-Embedding pruefen (ggf. generieren)
    2. pgvector-Suche: Top N aehnlichste Kandidaten (mit PostGIS-Filter)
    3. Fuer jeden Treffer: Deep-AI-Bewertung mit Branchenwissen-Prompt
    4. Match-Records erstellen/aktualisieren (kompatibel mit bestehendem Match-Model)
    5. Ergebnis: Liste sortiert nach AI-Score
    """

    MODEL = "gpt-4o-mini"

    def __init__(self, db: AsyncSession, api_key: str | None = None):
        self.db = db
        self.api_key = api_key or settings.openai_api_key
        self._embedding_service = EmbeddingService(db, self.api_key)
        self._client: httpx.AsyncClient | None = None
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    async def _get_client(self) -> httpx.AsyncClient:
        """HTTP-Client fuer OpenAI Chat API."""
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
        """Schliesst alle Clients."""
        await self._embedding_service.close()
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @property
    def total_cost_usd(self) -> float:
        """Gesamtkosten dieser Session (Embeddings + Chat)."""
        # Chat: gpt-4o-mini Preise
        chat_cost = (
            (self._total_input_tokens / 1_000_000) * 0.15
            + (self._total_output_tokens / 1_000_000) * 0.60
        )
        return round(self._embedding_service.total_cost_usd + chat_cost, 6)

    # ═══════════════════════════════════════════════════════════════
    # HAUPT-FUNKTION: Einen Job matchen
    # ═══════════════════════════════════════════════════════════════

    async def match_job(
        self,
        job_id: UUID,
        top_n: int = 10,
        max_distance_km: float = 30.0,
        progress_callback: Callable[[str, str], None] | None = None,
    ) -> SmartMatchResult:
        """Fuehrt Smart-Matching fuer einen einzelnen Job durch.

        1. Job-Embedding pruefen/generieren
        2. Top N aehnlichste Kandidaten finden (Embedding + PostGIS)
        3. Jeden Kandidaten durch Deep-AI bewerten
        4. Match-Records erstellen/aktualisieren

        Args:
            job_id: Die Job-ID
            top_n: Anzahl der Top-Kandidaten (default: 10)
            max_distance_km: Maximale Entfernung (default: 30km)
            progress_callback: Optional callback(step, detail)

        Returns:
            SmartMatchResult mit allen bewerteten Kandidaten
        """
        import time
        start_time = time.time()

        # Job laden
        job_result = await self.db.execute(select(Job).where(Job.id == job_id))
        job = job_result.scalar_one_or_none()

        if not job:
            return SmartMatchResult(
                job_id=job_id,
                job_position="Nicht gefunden",
                job_company="",
                errors=["Job nicht gefunden"],
            )

        result = SmartMatchResult(
            job_id=job_id,
            job_position=job.position,
            job_company=job.company_name,
        )

        if progress_callback:
            progress_callback("init", f"Smart-Match fuer: {job.position} bei {job.company_name}")

        # ── Schritt 1: Job-Embedding sicherstellen ──
        if job.embedding is None:
            if progress_callback:
                progress_callback("embedding", "Generiere Job-Embedding...")
            success = await self._embedding_service.embed_job(job_id)
            if not success:
                result.errors.append("Job-Embedding konnte nicht generiert werden")
                return result
            await self.db.commit()
            # Job neu laden (mit Embedding)
            await self.db.refresh(job)

        # ── Schritt 2: Aehnlichste Kandidaten finden ──
        if progress_callback:
            progress_callback(
                "similarity",
                f"Suche Top {top_n} Kandidaten (max {max_distance_km}km)...",
            )

        similar_candidates = await self._embedding_service.find_similar_candidates(
            job_id=job_id,
            limit=top_n,
            max_distance_km=max_distance_km,
        )

        result.embedding_candidates_found = len(similar_candidates)

        if not similar_candidates:
            if progress_callback:
                progress_callback("done", "Keine passenden Kandidaten gefunden")
            result.errors.append("Keine Kandidaten mit Embedding im Umkreis gefunden")
            result.duration_seconds = round(time.time() - start_time, 1)
            return result

        # ── Schritt 3: Deep-AI-Bewertung fuer jeden Kandidaten ──
        if progress_callback:
            progress_callback(
                "deep_ai",
                f"{len(similar_candidates)} Kandidaten gefunden — starte Deep-AI-Bewertung...",
            )

        for i, sim_candidate in enumerate(similar_candidates):
            cid = sim_candidate["candidate_id"]
            similarity = sim_candidate["similarity"]
            distance_km = sim_candidate["distance_km"]

            try:
                # Kandidat laden
                cand_result = await self.db.execute(
                    select(Candidate).where(Candidate.id == cid)
                )
                candidate = cand_result.scalar_one_or_none()

                if not candidate:
                    result.errors.append(f"Kandidat {cid} nicht gefunden")
                    continue

                if progress_callback:
                    progress_callback(
                        "deep_ai",
                        f"Bewerte {i + 1}/{len(similar_candidates)}: "
                        f"{candidate.full_name} (Similarity: {similarity:.2f})",
                    )

                # Deep-AI-Bewertung
                ai_result = await self._deep_ai_evaluate(job, candidate)

                smart_candidate = SmartMatchCandidate(
                    candidate_id=cid,
                    candidate_name=candidate.full_name,
                    similarity=similarity,
                    distance_km=distance_km,
                    ai_score=ai_result.get("score", 0.0),
                    ai_explanation=ai_result.get("explanation", ""),
                    ai_strengths=ai_result.get("strengths", []),
                    ai_weaknesses=ai_result.get("weaknesses", []),
                    ai_risks=ai_result.get("risks", []),
                    success=ai_result.get("success", False),
                    error=ai_result.get("error"),
                )
                result.candidates.append(smart_candidate)
                result.deep_ai_evaluated += 1

                # ── Schritt 4: Match-Record erstellen/aktualisieren ──
                match_record = await self._upsert_match(
                    job=job,
                    candidate=candidate,
                    similarity=similarity,
                    distance_km=distance_km,
                    ai_result=ai_result,
                )
                if match_record == "created":
                    result.matches_created += 1
                elif match_record == "updated":
                    result.matches_updated += 1

            except Exception as e:
                logger.error(f"Smart-Match Fehler fuer Kandidat {cid}: {e}")
                result.errors.append(f"Kandidat {cid}: {str(e)[:100]}")

        # Commit
        await self.db.commit()

        # Sortieren nach AI-Score DESC
        result.candidates.sort(key=lambda c: c.ai_score, reverse=True)

        result.total_cost_usd = self.total_cost_usd
        result.duration_seconds = round(time.time() - start_time, 1)

        if progress_callback:
            progress_callback(
                "done",
                f"Fertig! {result.deep_ai_evaluated} Kandidaten bewertet, "
                f"{result.matches_created} neue + {result.matches_updated} aktualisierte Matches, "
                f"Kosten: ~${result.total_cost_usd:.3f}",
            )

        logger.info(
            f"Smart-Match fuer '{job.position}': "
            f"{result.deep_ai_evaluated} bewertet, "
            f"{result.matches_created} neue Matches, "
            f"Dauer: {result.duration_seconds}s, "
            f"Kosten: ~${result.total_cost_usd:.4f}"
        )

        return result

    # ═══════════════════════════════════════════════════════════════
    # BATCH: Alle Finance-Jobs matchen
    # ═══════════════════════════════════════════════════════════════

    async def match_all_finance_jobs(
        self,
        top_n: int = 10,
        max_distance_km: float = 30.0,
        progress_callback: Callable[[str, str], None] | None = None,
    ) -> dict:
        """Fuehrt Smart-Matching fuer ALLE aktiven Finance-Jobs durch.

        Args:
            top_n: Kandidaten pro Job
            max_distance_km: Maximale Entfernung
            progress_callback: Optional callback(step, detail)

        Returns:
            Dict mit Gesamtstatistiken
        """
        # Alle aktiven Finance-Jobs laden
        query = (
            select(Job.id, Job.position)
            .where(
                and_(
                    Job.hotlist_category == "FINANCE",
                    Job.deleted_at.is_(None),
                )
            )
            .order_by(Job.created_at.desc())
        )
        result = await self.db.execute(query)
        jobs = result.all()

        stats = {
            "total_jobs": len(jobs),
            "jobs_matched": 0,
            "jobs_failed": 0,
            "total_matches_created": 0,
            "total_matches_updated": 0,
            "total_candidates_evaluated": 0,
            "total_cost_usd": 0.0,
            "errors": [],
        }

        if not jobs:
            if progress_callback:
                progress_callback("done", "Keine aktiven Finance-Jobs gefunden")
            return stats

        if progress_callback:
            progress_callback("init", f"{len(jobs)} Finance-Jobs gefunden — starte Matching...")

        for i, (job_id, position) in enumerate(jobs):
            try:
                if progress_callback:
                    progress_callback(
                        "matching",
                        f"Job {i + 1}/{len(jobs)}: {position}",
                    )

                job_result = await self.match_job(
                    job_id=job_id,
                    top_n=top_n,
                    max_distance_km=max_distance_km,
                )

                stats["jobs_matched"] += 1
                stats["total_matches_created"] += job_result.matches_created
                stats["total_matches_updated"] += job_result.matches_updated
                stats["total_candidates_evaluated"] += job_result.deep_ai_evaluated
                stats["total_cost_usd"] = self.total_cost_usd

                if job_result.errors:
                    stats["errors"].extend(job_result.errors[:3])

            except Exception as e:
                logger.error(f"Smart-Match Batch-Fehler fuer Job {job_id}: {e}")
                stats["jobs_failed"] += 1
                stats["errors"].append(f"Job {position}: {str(e)[:100]}")

        if progress_callback:
            progress_callback(
                "done",
                f"Fertig! {stats['jobs_matched']}/{stats['total_jobs']} Jobs gematcht, "
                f"{stats['total_candidates_evaluated']} Kandidaten bewertet, "
                f"Kosten: ~${stats['total_cost_usd']:.2f}",
            )

        logger.info(
            f"Smart-Match Batch: {stats['jobs_matched']}/{stats['total_jobs']} Jobs, "
            f"{stats['total_candidates_evaluated']} Kandidaten, "
            f"Kosten: ~${stats['total_cost_usd']:.3f}"
        )

        return stats

    # ═══════════════════════════════════════════════════════════════
    # DEEP-AI BEWERTUNG (ein Kandidat gegen einen Job)
    # ═══════════════════════════════════════════════════════════════

    async def _deep_ai_evaluate(
        self,
        job: Job,
        candidate: Candidate,
    ) -> dict:
        """Bewertet einen Kandidaten gegen einen Job via GPT-4o-mini.

        Schickt ALLE Daten (KEINE Truncation!):
        - Voller Stellentext
        - Voller Werdegang mit Taetigkeitsbeschreibungen
        - Alle Qualifikationen, Skills, Weiterbildungen

        Returns:
            Dict: {"score", "explanation", "strengths", "weaknesses", "risks", "success", "error"}
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

        # User-Prompt bauen (ALLE Daten, NICHTS abschneiden)
        user_prompt = self._build_user_prompt(job, candidate)

        try:
            client = await self._get_client()

            response = await client.post(
                "/chat/completions",
                json={
                    "model": self.MODEL,
                    "messages": [
                        {"role": "system", "content": SMART_MATCH_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2,  # Niedrig fuer konsistente Bewertungen
                    "max_tokens": 1000,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            result = response.json()

            # Token-Tracking
            usage = result.get("usage", {})
            self._total_input_tokens += usage.get("prompt_tokens", 0)
            self._total_output_tokens += usage.get("completion_tokens", 0)

            # Response parsen
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
            logger.warning(
                f"Deep-AI Timeout: {candidate.full_name} ↔ {job.position}"
            )
            return {
                "score": 0.0,
                "explanation": "KI-Bewertung: Timeout",
                "strengths": [],
                "weaknesses": [],
                "risks": [],
                "success": False,
                "error": "Timeout",
            }
        except json.JSONDecodeError as e:
            logger.error(f"Deep-AI JSON-Fehler: {e}")
            return {
                "score": 0.0,
                "explanation": "KI-Bewertung: Ungueltige Antwort",
                "strengths": [],
                "weaknesses": [],
                "risks": [],
                "success": False,
                "error": f"JSON-Fehler: {e}",
            }
        except Exception as e:
            logger.error(f"Deep-AI Fehler: {e}")
            return {
                "score": 0.0,
                "explanation": f"KI-Bewertung fehlgeschlagen: {str(e)[:100]}",
                "strengths": [],
                "weaknesses": [],
                "risks": [],
                "success": False,
                "error": str(e),
            }

    def _build_user_prompt(self, job: Job, candidate: Candidate) -> str:
        """Baut den vollstaendigen User-Prompt fuer Deep-AI-Bewertung.

        KRITISCH: KEINE DATEN ABSCHNEIDEN!
        Der volle Stellentext und der volle Werdegang muessen rein.
        """
        # === JOB-TEIL ===
        job_text = job.job_text or "Keine Stellenbeschreibung vorhanden"

        job_section = f"""═══ STELLENANGEBOT ═══
Position: {job.position}
Unternehmen: {job.company_name}
Branche: {job.industry or 'Nicht angegeben'}
Standort: {job.display_city}
Kategorie: {job.hotlist_category or 'Nicht angegeben'}
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
                # NICHT ABSCHNEIDEN — komplette Taetigkeitsbeschreibung
                work_lines.append(f"Taetigkeiten:\n{desc}")

        work_text = "\n".join(work_lines) if work_lines else "Kein Werdegang vorhanden"

        # Ausbildung
        education = candidate.education or []
        edu_lines = []
        for entry in education:
            if isinstance(entry, dict):
                degree = entry.get("degree", "")
                institution = entry.get("institution", "")
                field_of_study = entry.get("field_of_study", "")
                parts = [p for p in [degree, field_of_study, institution] if p]
                if parts:
                    edu_lines.append("- " + ", ".join(parts))
        edu_text = "\n".join(edu_lines) if edu_lines else "Keine Angaben"

        # Weiterbildungen
        further_edu = candidate.further_education or []
        further_lines = []
        for entry in further_edu:
            if isinstance(entry, dict):
                title = entry.get("title", entry.get("name", ""))
                institution = entry.get("institution", entry.get("provider", ""))
                parts = [p for p in [title, institution] if p]
                if parts:
                    further_lines.append("- " + ", ".join(parts))
            elif isinstance(entry, str) and entry.strip():
                further_lines.append(f"- {entry}")
        further_text = "\n".join(further_lines) if further_lines else "Keine Angaben"

        # Skills
        skills_text = ", ".join(candidate.skills) if candidate.skills else "Keine angegeben"

        # IT-Kenntnisse
        it_text = ", ".join(candidate.it_skills) if candidate.it_skills else "Keine angegeben"

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
        lang_text = ", ".join(lang_parts) if lang_parts else "Keine angegeben"

        # Klassifizierte Rollen
        titles_text = (
            ", ".join(candidate.hotlist_job_titles)
            if candidate.hotlist_job_titles
            else "Nicht klassifiziert"
        )

        candidate_section = f"""═══ KANDIDAT ═══
Aktuelle Position: {candidate.current_position or 'Nicht angegeben'}
Aktuelles Unternehmen: {candidate.current_company or 'Nicht angegeben'}
Wohnort: {candidate.hotlist_city or candidate.city or 'Nicht angegeben'}
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

        # CV-Text Fallback (nur wenn kein strukturierter Werdegang)
        cv_fallback = ""
        if not work_lines and candidate.cv_text:
            cv_fallback = f"\n\nCV-Volltext (kein strukturierter Werdegang):\n{candidate.cv_text}"

        return f"""{job_section}

{candidate_section}{cv_fallback}

═══ AUFGABE ═══
Bewerte die Passung zwischen diesem Kandidaten und der Stelle.
Pruefe ZUERST: Handelt es sich bei den Taetigkeiten um eigenstaendige Erstellung oder nur Mitwirkung/Zuarbeit?
Pruefe die Software-Passung: DATEV vs. SAP vs. andere.
Bewerte realistisch — kein Wunschdenken."""

    # ═══════════════════════════════════════════════════════════════
    # MATCH-RECORD ERSTELLEN / AKTUALISIEREN
    # ═══════════════════════════════════════════════════════════════

    async def _upsert_match(
        self,
        job: Job,
        candidate: Candidate,
        similarity: float,
        distance_km: float | None,
        ai_result: dict,
    ) -> str:
        """Erstellt oder aktualisiert einen Match-Record.

        Befuellt die bestehenden Match-Felder kompatibel:
        - distance_km → aus PostGIS
        - keyword_score → Embedding-Similarity (0-1)
        - pre_score → Embedding-Similarity × 100 (kompatibel mit UI-Filtern)
        - ai_score → Deep-AI-Score (0-1)
        - ai_explanation, ai_strengths, ai_weaknesses → aus Deep-AI
        - status → AI_CHECKED (automatisch)

        Returns:
            "created" oder "updated"
        """
        # Pruefen ob Match bereits existiert
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
            # Bestehendes Match aktualisieren
            match.distance_km = distance_km
            match.keyword_score = similarity
            match.pre_score = round(similarity * 100, 1)
            match.ai_score = ai_result.get("score", 0.0)
            match.ai_explanation = ai_result.get("explanation", "")
            match.ai_strengths = ai_result.get("strengths", [])
            match.ai_weaknesses = ai_result.get("weaknesses", [])
            match.ai_checked_at = now
            # Status nur aktualisieren wenn noch NEW
            if match.status == MatchStatus.NEW:
                match.status = MatchStatus.AI_CHECKED
            # Stale-Flag zuruecksetzen
            match.stale = False
            match.stale_reason = None
            match.stale_since = None
            return "updated"
        else:
            # Neues Match erstellen
            new_match = Match(
                job_id=job.id,
                candidate_id=candidate.id,
                distance_km=distance_km,
                keyword_score=similarity,
                pre_score=round(similarity * 100, 1),
                ai_score=ai_result.get("score", 0.0),
                ai_explanation=ai_result.get("explanation", ""),
                ai_strengths=ai_result.get("strengths", []),
                ai_weaknesses=ai_result.get("weaknesses", []),
                ai_checked_at=now,
                status=MatchStatus.AI_CHECKED,
                stale=False,
            )
            self.db.add(new_match)
            return "created"

    # ═══════════════════════════════════════════════════════════════
    # KOSTEN-SCHAETZUNG
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def estimate_cost(num_jobs: int, candidates_per_job: int = 10) -> dict:
        """Schaetzt die Kosten fuer einen Smart-Match-Lauf.

        Args:
            num_jobs: Anzahl Jobs
            candidates_per_job: Kandidaten pro Job (default: 10)

        Returns:
            Dict mit Kosten-Schaetzung
        """
        total_evaluations = num_jobs * candidates_per_job

        # Chat: ~2000 Input-Tokens + ~300 Output-Tokens pro Bewertung
        chat_input = total_evaluations * 2000
        chat_output = total_evaluations * 300
        chat_cost = (chat_input / 1_000_000) * 0.15 + (chat_output / 1_000_000) * 0.60

        # Embeddings: ~500 Tokens pro Dokument (nur fuer fehlende)
        # Im Worst-Case alle Jobs + alle Kandidaten
        embedding_cost = ((num_jobs + total_evaluations) * 500 / 1_000_000) * 0.02

        return {
            "num_jobs": num_jobs,
            "candidates_per_job": candidates_per_job,
            "total_evaluations": total_evaluations,
            "estimated_chat_cost_usd": round(chat_cost, 4),
            "estimated_embedding_cost_usd": round(embedding_cost, 4),
            "estimated_total_cost_usd": round(chat_cost + embedding_cost, 4),
        }

    # ═══════════════════════════════════════════════════════════════
    # CONTEXT MANAGER
    # ═══════════════════════════════════════════════════════════════

    async def __aenter__(self) -> "SmartMatchingService":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
