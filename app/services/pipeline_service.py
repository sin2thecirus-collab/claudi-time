"""Pipeline Service - Automatische Reactive Pipeline nach CRM-Sync.

Fuehrt 3 Schritte sequentiell aus:
1. Kategorisierung geaenderter Kandidaten (kostenlos, lokal)
2. Klassifizierung geaenderter FINANCE-Kandidaten (OpenAI, ~$0.001/Kandidat)
3. Stale-Markierung betroffener Matches (kostenlos, lokal)

Erkennung "geaendert":
- Step 1: crm_synced_at > categorized_at ODER categorized_at IS NULL
- Step 2: categorized_at > classification_data->>'classified_at' ODER classification_data IS NULL
- Step 3: Vergleiche hotlist_job_titles VOR und NACH Klassifizierung
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, update, and_, or_, func, cast, String
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# DATENKLASSEN
# ═══════════════════════════════════════════════════════════════

@dataclass
class Step1Result:
    """Ergebnis der Kategorisierung."""
    candidates_checked: int = 0
    candidates_categorized: int = 0
    jobs_checked: int = 0
    jobs_categorized: int = 0
    errors: int = 0


@dataclass
class Step2Result:
    """Ergebnis der OpenAI-Klassifizierung."""
    candidates_checked: int = 0
    classified: int = 0
    skipped_no_cv: int = 0
    skipped_leadership: int = 0
    skipped_error: int = 0
    changed_candidate_ids: list = field(default_factory=list)
    cost_usd: float = 0.0


@dataclass
class Step3Result:
    """Ergebnis der Stale-Markierung."""
    matches_marked_stale: int = 0
    pre_scores_reset: int = 0


@dataclass
class PipelineResult:
    """Gesamtergebnis der Pipeline."""
    step1: Step1Result
    step2: Step2Result
    step3: Step3Result
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "step1_categorize": {
                "candidates_checked": self.step1.candidates_checked,
                "candidates_categorized": self.step1.candidates_categorized,
                "jobs_checked": self.step1.jobs_checked,
                "jobs_categorized": self.step1.jobs_categorized,
                "errors": self.step1.errors,
            },
            "step2_classify": {
                "candidates_checked": self.step2.candidates_checked,
                "classified": self.step2.classified,
                "skipped_no_cv": self.step2.skipped_no_cv,
                "skipped_leadership": self.step2.skipped_leadership,
                "skipped_error": self.step2.skipped_error,
                "changed_titles": len(self.step2.changed_candidate_ids),
                "cost_usd": round(self.step2.cost_usd, 4),
            },
            "step3_stale": {
                "matches_marked_stale": self.step3.matches_marked_stale,
                "pre_scores_reset": self.step3.pre_scores_reset,
            },
            "duration_seconds": round(self.duration_seconds, 1),
        }

    def __repr__(self) -> str:
        return (
            f"Pipeline: {self.step1.candidates_categorized} kategorisiert, "
            f"{self.step2.classified} klassifiziert, "
            f"{self.step3.matches_marked_stale} stale markiert "
            f"({self.duration_seconds:.1f}s)"
        )


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════

class PipelineService:
    """Automatische Pipeline: Kategorisierung → Klassifizierung → Stale-Markierung."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def run_auto_pipeline(
        self,
        progress_callback=None,
    ) -> PipelineResult:
        """
        Fuehrt alle 3 Schritte sequentiell aus.

        Args:
            progress_callback: Optional callback(step_name, detail_dict)
        """
        start_time = datetime.now(timezone.utc)

        # Step 1: Kategorisierung (kostenlos, lokal)
        if progress_callback:
            progress_callback("categorize", {"status": "running"})
        step1 = await self._step1_categorize_changed()
        if progress_callback:
            progress_callback("categorize", {"status": "done", "result": step1})

        # Step 2: Klassifizierung (OpenAI)
        if progress_callback:
            progress_callback("classify", {"status": "running"})
        step2 = await self._step2_classify_changed_finance()
        if progress_callback:
            progress_callback("classify", {"status": "done", "result": step2})

        # Step 3: Stale-Markierung
        if progress_callback:
            progress_callback("mark_stale", {"status": "running"})
        step3 = await self._step3_mark_stale_matches(step2.changed_candidate_ids)
        if progress_callback:
            progress_callback("mark_stale", {"status": "done", "result": step3})

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()

        result = PipelineResult(
            step1=step1,
            step2=step2,
            step3=step3,
            duration_seconds=round(duration, 1),
        )

        logger.info(f"=== PIPELINE ABGESCHLOSSEN === {result}")
        return result

    # ──────────────────────────────────────────────────
    # Step 1: Kategorisierung geaenderter Kandidaten/Jobs
    # ──────────────────────────────────────────────────

    async def _step1_categorize_changed(self) -> Step1Result:
        """
        Kategorisiert Kandidaten/Jobs die seit dem letzten Mal geaendert wurden.

        Erkennung: crm_synced_at > categorized_at ODER categorized_at IS NULL
        """
        from app.services.categorization_service import CategorizationService

        result = Step1Result()
        cat_service = CategorizationService(self.db)

        # --- Kandidaten ---
        # Geaenderte Kandidaten: crm_synced_at > categorized_at oder noch nie kategorisiert
        query = select(Candidate).where(
            Candidate.deleted_at.is_(None),
            or_(
                Candidate.categorized_at.is_(None),
                and_(
                    Candidate.crm_synced_at.isnot(None),
                    Candidate.categorized_at.isnot(None),
                    Candidate.crm_synced_at > Candidate.categorized_at,
                ),
            ),
        )
        candidates = (await self.db.execute(query)).scalars().all()
        result.candidates_checked = len(candidates)

        for candidate in candidates:
            try:
                cat_result = cat_service.categorize_candidate(candidate)
                cat_service.apply_to_candidate(candidate, cat_result)
                result.candidates_categorized += 1
            except Exception as e:
                logger.error(f"Pipeline Step1: Kategorisierung Kandidat {candidate.id} fehlgeschlagen: {e}")
                result.errors += 1

        if candidates:
            await self.db.commit()

        # --- Jobs (nur unkategorisierte) ---
        job_query = select(Job).where(
            Job.deleted_at.is_(None),
            Job.categorized_at.is_(None),
        )
        jobs = (await self.db.execute(job_query)).scalars().all()
        result.jobs_checked = len(jobs)

        for job in jobs:
            try:
                cat_result = cat_service.categorize_job(job)
                cat_service.apply_to_job(job, cat_result)
                result.jobs_categorized += 1
            except Exception as e:
                logger.error(f"Pipeline Step1: Kategorisierung Job {job.id} fehlgeschlagen: {e}")
                result.errors += 1

        if jobs:
            await self.db.commit()

        logger.info(
            f"Pipeline Step1: {result.candidates_categorized}/{result.candidates_checked} Kandidaten "
            f"+ {result.jobs_categorized}/{result.jobs_checked} Jobs kategorisiert"
        )
        return result

    # ──────────────────────────────────────────────────
    # Step 2: Klassifizierung geaenderter FINANCE-Kandidaten
    # ──────────────────────────────────────────────────

    async def _step2_classify_changed_finance(self) -> Step2Result:
        """
        Klassifiziert FINANCE-Kandidaten deren Kategorisierung neuer ist als
        ihre letzte Klassifizierung.

        Erkennung: categorized_at > classification_data->>'classified_at'
                   ODER classification_data IS NULL
        """
        from app.services.finance_classifier_service import FinanceClassifierService

        result = Step2Result()

        # Alle FINANCE-Kandidaten die (re-)klassifiziert werden muessen
        # 1. classification_data ist NULL (nie klassifiziert)
        # 2. categorized_at > classification_data->>'classified_at' (Daten haben sich geaendert)
        query = select(Candidate).where(
            Candidate.deleted_at.is_(None),
            Candidate.hotlist_category == "FINANCE",
            or_(
                Candidate.classification_data.is_(None),
                and_(
                    Candidate.categorized_at.isnot(None),
                    Candidate.classification_data.isnot(None),
                    # JSONB Timestamp-Vergleich: categorized_at > classified_at
                    Candidate.categorized_at > cast(
                        Candidate.classification_data["classified_at"].astext,
                        # PostgreSQL TIMESTAMPTZ cast
                        func.to_timestamp(
                            Candidate.classification_data["classified_at"].astext,
                            "YYYY-MM-DD\"T\"HH24:MI:SS.US+00:00"
                        ).type,
                    ),
                ),
            ),
        )

        # Einfacherer Ansatz: Lade alle FINANCE-Kandidaten und filtere in Python
        # (JSONB Timestamp-Vergleich in SQL ist fragil)
        all_finance_query = select(Candidate).where(
            Candidate.deleted_at.is_(None),
            Candidate.hotlist_category == "FINANCE",
        )
        all_finance = (await self.db.execute(all_finance_query)).scalars().all()

        # In Python filtern: Wer muss (re-)klassifiziert werden?
        candidates_to_classify = []
        now = datetime.now(timezone.utc)

        for c in all_finance:
            needs_classify = False

            if c.classification_data is None:
                # Nie klassifiziert
                needs_classify = True
            elif c.categorized_at is not None:
                # Vergleiche Timestamps
                classified_at_str = None
                if isinstance(c.classification_data, dict):
                    classified_at_str = c.classification_data.get("classified_at")

                if classified_at_str is None:
                    needs_classify = True
                else:
                    try:
                        classified_at = datetime.fromisoformat(classified_at_str.replace("Z", "+00:00"))
                        # Timezone-aware machen falls noetig
                        if classified_at.tzinfo is None:
                            classified_at = classified_at.replace(tzinfo=timezone.utc)
                        cat_at = c.categorized_at
                        if cat_at.tzinfo is None:
                            cat_at = cat_at.replace(tzinfo=timezone.utc)
                        if cat_at > classified_at:
                            needs_classify = True
                    except (ValueError, TypeError):
                        needs_classify = True

            if needs_classify:
                candidates_to_classify.append(c)

        result.candidates_checked = len(candidates_to_classify)

        if not candidates_to_classify:
            logger.info("Pipeline Step2: Keine FINANCE-Kandidaten zu klassifizieren")
            return result

        logger.info(f"Pipeline Step2: {len(candidates_to_classify)} FINANCE-Kandidaten zu klassifizieren")

        # Klassifizierung mit Snapshot der alten Titel
        classifier = FinanceClassifierService(self.db)

        for i, candidate in enumerate(candidates_to_classify):
            # Snapshot: Alte Titel merken
            old_titles = set(candidate.hotlist_job_titles or [])

            try:
                classification = await classifier.classify_candidate(candidate)

                if not classification.success:
                    if classification.error == "Kein Werdegang vorhanden":
                        result.skipped_no_cv += 1
                    else:
                        result.skipped_error += 1
                    continue

                if classification.is_leadership:
                    result.skipped_leadership += 1
                    classifier.apply_to_candidate(candidate, classification)
                    continue

                if not classification.roles:
                    result.skipped_error += 1
                    classifier.apply_to_candidate(candidate, classification)
                    continue

                # Ergebnis anwenden
                classifier.apply_to_candidate(candidate, classification)
                result.classified += 1

                # Kosten tracken
                result.cost_usd += (
                    classification.input_tokens * 0.15 / 1_000_000
                    + classification.output_tokens * 0.60 / 1_000_000
                )

                # Vergleiche: Haben sich die Titel geaendert?
                new_titles = set(candidate.hotlist_job_titles or [])
                if old_titles != new_titles:
                    result.changed_candidate_ids.append(candidate.id)

                # Rate-Limiting
                if (i + 1) % 10 == 0:
                    await asyncio.sleep(0.5)

                # Zwischenspeichern alle 50 Kandidaten
                if (i + 1) % 50 == 0:
                    await self.db.commit()
                    logger.info(
                        f"Pipeline Step2: {i + 1}/{len(candidates_to_classify)} "
                        f"({result.classified} klassifiziert, ${result.cost_usd:.3f})"
                    )

            except Exception as e:
                logger.error(f"Pipeline Step2: Klassifizierung Kandidat {candidate.id} fehlgeschlagen: {e}")
                result.skipped_error += 1

        await self.db.commit()
        await classifier.close()

        logger.info(
            f"Pipeline Step2: {result.classified}/{result.candidates_checked} klassifiziert, "
            f"{len(result.changed_candidate_ids)} mit geaenderten Titeln, "
            f"${result.cost_usd:.3f}"
        )
        return result

    # ──────────────────────────────────────────────────
    # Step 3: Stale-Markierung betroffener Matches
    # ──────────────────────────────────────────────────

    async def _step3_mark_stale_matches(
        self,
        changed_candidate_ids: list,
    ) -> Step3Result:
        """
        Markiert Matches als stale fuer Kandidaten deren Titel sich geaendert haben.

        Setzt auch pre_score auf NULL zurueck (wird bei naechstem Quick-Match neu berechnet).
        """
        result = Step3Result()

        if not changed_candidate_ids:
            logger.info("Pipeline Step3: Keine geaenderten Kandidaten → keine stale Matches")
            return result

        now = datetime.now(timezone.utc)

        # Bulk-Update: Alle Matches der geaenderten Kandidaten als stale markieren
        stale_update = (
            update(Match)
            .where(
                Match.candidate_id.in_(changed_candidate_ids),
                or_(Match.stale.is_(False), Match.stale.is_(None)),
            )
            .values(
                stale=True,
                stale_reason="Kandidaten-Jobtitel geaendert (Pipeline)",
                stale_since=now,
                pre_score=None,  # Reset: muss neu berechnet werden
            )
        )
        stale_result = await self.db.execute(stale_update)
        result.matches_marked_stale = stale_result.rowcount
        result.pre_scores_reset = stale_result.rowcount

        await self.db.commit()

        logger.info(
            f"Pipeline Step3: {result.matches_marked_stale} Matches als stale markiert, "
            f"{result.pre_scores_reset} Pre-Scores zurueckgesetzt"
        )
        return result
