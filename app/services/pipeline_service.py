"""Pipeline Service - Automatische Reactive Pipeline nach CRM-Sync.

Fuehrt 5 Schritte sequentiell aus:
0. Geocoding neuer/geaenderter Kandidaten + Jobs (kostenlos, Nominatim)
1. Kategorisierung geaenderter Kandidaten (kostenlos, lokal)
2. Klassifizierung geaenderter FINANCE-Kandidaten (OpenAI, ~$0.001/Kandidat)
3. Stale-Markierung betroffener Matches (kostenlos, lokal)
4. Distanz-Neuberechnung fuer Matches mit neuen Koordinaten (kostenlos, PostGIS)

Erkennung "geaendert":
- Step 0: address_coords IS NULL (Kandidaten) / location_coords IS NULL (Jobs)
- Step 1: crm_synced_at > categorized_at ODER categorized_at IS NULL
- Step 2: categorized_at > classification_data->>'classified_at' ODER classification_data IS NULL
- Step 3: Vergleiche hotlist_job_titles VOR und NACH Klassifizierung
- Step 4: Matches mit distance_km IS NULL aber beide Koordinaten vorhanden
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
class Step0Result:
    """Ergebnis des Geocodings."""
    candidates_total: int = 0
    candidates_geocoded: int = 0
    candidates_skipped: int = 0
    candidates_failed: int = 0
    jobs_total: int = 0
    jobs_geocoded: int = 0
    jobs_skipped: int = 0
    jobs_failed: int = 0


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
    """Ergebnis der Loeschung veralteter Matches."""
    matches_deleted: int = 0


@dataclass
class Step4Result:
    """Ergebnis der Distanz-Neuberechnung."""
    matches_checked: int = 0
    matches_updated: int = 0
    matches_removed: int = 0  # Matches die jetzt > 25km sind


@dataclass
class Step5Result:
    """Ergebnis der Pre-Match-Generierung."""
    combos_processed: int = 0
    matches_created: int = 0
    errors_count: int = 0


@dataclass
class PipelineResult:
    """Gesamtergebnis der Pipeline."""
    step0: Step0Result
    step1: Step1Result
    step2: Step2Result
    step3: Step3Result
    step4: Step4Result
    step5: Step5Result | None = None
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        result = {
            "step0_geocoding": {
                "candidates_geocoded": self.step0.candidates_geocoded,
                "candidates_total": self.step0.candidates_total,
                "candidates_skipped": self.step0.candidates_skipped,
                "candidates_failed": self.step0.candidates_failed,
                "jobs_geocoded": self.step0.jobs_geocoded,
                "jobs_total": self.step0.jobs_total,
                "jobs_skipped": self.step0.jobs_skipped,
                "jobs_failed": self.step0.jobs_failed,
            },
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
            "step3_cleanup": {
                "matches_deleted": self.step3.matches_deleted,
            },
            "step4_distances": {
                "matches_checked": self.step4.matches_checked,
                "matches_updated": self.step4.matches_updated,
                "matches_removed": self.step4.matches_removed,
            },
            "duration_seconds": round(self.duration_seconds, 1),
        }
        if self.step5:
            result["step5_pre_match"] = {
                "combos_processed": self.step5.combos_processed,
                "matches_created": self.step5.matches_created,
                "errors_count": self.step5.errors_count,
            }
        return result

    def __repr__(self) -> str:
        pre_match_info = ""
        if self.step5:
            pre_match_info = f", {self.step5.matches_created} Pre-Matches erstellt"
        return (
            f"Pipeline: "
            f"{self.step0.candidates_geocoded}+{self.step0.jobs_geocoded} geocodiert, "
            f"{self.step1.candidates_categorized} kategorisiert, "
            f"{self.step2.classified} klassifiziert, "
            f"{self.step3.matches_deleted} Matches geloescht, "
            f"{self.step4.matches_updated} Distanzen aktualisiert"
            f"{pre_match_info} "
            f"({self.duration_seconds:.1f}s)"
        )


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════

class PipelineService:
    """Automatische Pipeline: Geocoding → Kategorisierung → Klassifizierung → Stale → Distanzen."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def run_auto_pipeline(
        self,
        progress_callback=None,
    ) -> PipelineResult:
        """
        Fuehrt alle 5 Schritte sequentiell aus.

        Args:
            progress_callback: Optional callback(step_name, detail_dict)
        """
        start_time = datetime.now(timezone.utc)

        # Step 0: Geocoding (kostenlos, Nominatim API)
        if progress_callback:
            progress_callback("geocoding", {"status": "running"})
        step0 = await self._step0_geocode_pending()
        if progress_callback:
            progress_callback("geocoding", {"status": "done", "result": step0})

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

        # Step 3: Veraltete Matches loeschen
        if progress_callback:
            progress_callback("delete_stale", {"status": "running"})
        step3 = await self._step3_delete_stale_matches(step2.changed_candidate_ids)
        if progress_callback:
            progress_callback("delete_stale", {"status": "done", "result": step3})

        # Step 4: Distanzen neu berechnen fuer Matches ohne distance_km
        if progress_callback:
            progress_callback("update_distances", {"status": "running"})
        step4 = await self._step4_update_match_distances()
        if progress_callback:
            progress_callback("update_distances", {"status": "done", "result": step4})

        # Step 5: Pre-Matches generieren (FINANCE)
        step5 = None
        try:
            if progress_callback:
                progress_callback("pre_match", {"status": "running"})
            step5 = await self._step5_generate_pre_matches(progress_callback)
            if progress_callback:
                progress_callback("pre_match", {"status": "done", "result": step5})
        except Exception as e:
            logger.error(f"Step 5 (Pre-Match) fehlgeschlagen: {e}")
            step5 = Step5Result(errors_count=1)

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()

        result = PipelineResult(
            step0=step0,
            step1=step1,
            step2=step2,
            step3=step3,
            step4=step4,
            step5=step5,
            duration_seconds=round(duration, 1),
        )

        logger.info(f"=== PIPELINE ABGESCHLOSSEN === {result}")
        return result

    # ──────────────────────────────────────────────────
    # Step 0: Geocoding neuer Kandidaten/Jobs
    # ──────────────────────────────────────────────────

    async def _step0_geocode_pending(self) -> Step0Result:
        """
        Geocodiert Kandidaten und Jobs die noch keine Koordinaten haben.

        Verwendet OpenStreetMap Nominatim (kostenlos, 1 Request/Sekunde).
        Batch-Commits alle 25 Eintraege damit Fortschritt sofort in DB sichtbar.
        """
        from app.services.geocoding_service import GeocodingService

        result = Step0Result()
        BATCH = 25  # Commit-Intervall

        # --- Kandidaten ohne Koordinaten ---
        cand_query = select(Candidate).where(
            Candidate.address_coords.is_(None),
            Candidate.deleted_at.is_(None),
            Candidate.city.isnot(None),
            Candidate.city != "",
        )
        candidates = (await self.db.execute(cand_query)).scalars().all()
        result.candidates_total = len(candidates)
        logger.info(f"Pipeline Step0: {len(candidates)} Kandidaten zu geocodieren")

        if candidates:
            geo = GeocodingService(self.db)
            try:
                for i, candidate in enumerate(candidates):
                    try:
                        ok = await geo.geocode_candidate(candidate)
                        if ok:
                            result.candidates_geocoded += 1
                        else:
                            result.candidates_skipped += 1
                    except Exception as e:
                        result.candidates_failed += 1
                        logger.error(f"Step0 Kandidat {candidate.id}: {e}")

                    # Batch-Commit + Log
                    if (i + 1) % BATCH == 0:
                        await self.db.commit()
                        logger.info(
                            f"Step0 Kandidaten: {i+1}/{len(candidates)} "
                            f"({result.candidates_geocoded} OK, {result.candidates_skipped} skip, "
                            f"{result.candidates_failed} fail)"
                        )

                await self.db.commit()
            finally:
                await geo.close()

        # --- Jobs ohne Koordinaten ---
        job_query = select(Job).where(
            Job.location_coords.is_(None),
            Job.deleted_at.is_(None),
            Job.city.isnot(None),
            Job.city != "",
        )
        jobs = (await self.db.execute(job_query)).scalars().all()
        result.jobs_total = len(jobs)
        logger.info(f"Pipeline Step0: {len(jobs)} Jobs zu geocodieren")

        if jobs:
            geo = GeocodingService(self.db)
            try:
                for i, job in enumerate(jobs):
                    try:
                        ok = await geo.geocode_job(job)
                        if ok:
                            result.jobs_geocoded += 1
                        else:
                            result.jobs_skipped += 1
                    except Exception as e:
                        result.jobs_failed += 1
                        logger.error(f"Step0 Job {job.id}: {e}")

                    if (i + 1) % BATCH == 0:
                        await self.db.commit()
                        logger.info(
                            f"Step0 Jobs: {i+1}/{len(jobs)} "
                            f"({result.jobs_geocoded} OK)"
                        )

                await self.db.commit()
            finally:
                await geo.close()

        logger.info(
            f"Pipeline Step0 FERTIG: {result.candidates_geocoded}/{result.candidates_total} Kandidaten "
            f"+ {result.jobs_geocoded}/{result.jobs_total} Jobs geocodiert"
        )
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
    # Step 3: Veraltete Matches loeschen
    # ──────────────────────────────────────────────────

    async def _step3_delete_stale_matches(
        self,
        changed_candidate_ids: list,
    ) -> Step3Result:
        """
        Loescht Matches fuer Kandidaten deren Jobtitel sich geaendert haben.

        Wenn sich der Jobtitel aendert sind die alten Matches wertlos
        und werden direkt geloescht statt nur markiert.
        """
        from sqlalchemy import delete as sa_delete

        result = Step3Result()

        if not changed_candidate_ids:
            logger.info("Pipeline Step3: Keine geaenderten Kandidaten → nichts zu loeschen")
            return result

        # Alle Matches der geaenderten Kandidaten loeschen
        delete_stmt = (
            sa_delete(Match)
            .where(Match.candidate_id.in_(changed_candidate_ids))
        )
        del_result = await self.db.execute(delete_stmt)
        result.matches_deleted = del_result.rowcount

        await self.db.commit()

        logger.info(
            f"Pipeline Step3: {result.matches_deleted} veraltete Matches geloescht"
        )
        return result

    # ──────────────────────────────────────────────────
    # Step 4: Distanzen fuer Matches neu berechnen
    # ──────────────────────────────────────────────────

    async def _step4_update_match_distances(self) -> Step4Result:
        """
        Berechnet distance_km neu fuer alle Matches wo:
        - distance_km IS NULL (noch nie berechnet)
        - ABER beide Seiten Koordinaten haben

        Verwendet PostGIS ST_Distance fuer genaue Erdkugel-Berechnung.
        Entfernt Matches die jetzt > 25km sind (nach Geocoding-Korrektur).
        """
        from geoalchemy2.functions import ST_Distance

        result = Step4Result()

        MAX_DISTANCE_KM = 25
        METERS_PER_KM = 1000

        # Alle Matches wo distance_km NULL ist, aber beide Koordinaten vorhanden
        query = (
            select(Match, Candidate, Job)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .join(Job, Match.job_id == Job.id)
            .where(
                Match.distance_km.is_(None),
                Candidate.address_coords.isnot(None),
                Job.location_coords.isnot(None),
            )
        )
        rows = (await self.db.execute(query)).all()
        result.matches_checked = len(rows)

        if not rows:
            logger.info("Pipeline Step4: Keine Matches ohne Distanz gefunden")
            return result

        # Distanz per SQL berechnen (effizienter als einzeln)
        # Wir machen es in Batches per Match-ID
        match_ids = [row[0].id for row in rows]

        # Bulk-Distanzberechnung via SQL
        dist_query = (
            select(
                Match.id,
                ST_Distance(
                    Candidate.address_coords,
                    Job.location_coords,
                    True,  # use_spheroid fuer genaue Berechnung
                ).label("distance_m"),
            )
            .join(Candidate, Match.candidate_id == Candidate.id)
            .join(Job, Match.job_id == Job.id)
            .where(Match.id.in_(match_ids))
        )
        dist_rows = (await self.db.execute(dist_query)).all()

        # Distanzen als Dict: match_id → distance_km
        distances = {row[0]: row[1] / METERS_PER_KM for row in dist_rows}

        # Matches aktualisieren
        matches_to_remove = []
        for match_obj, candidate, job in rows:
            dist_km = distances.get(match_obj.id)
            if dist_km is None:
                continue

            if dist_km > MAX_DISTANCE_KM:
                # Match ist zu weit weg → entfernen
                matches_to_remove.append(match_obj.id)
                result.matches_removed += 1
            else:
                match_obj.distance_km = round(dist_km, 2)
                result.matches_updated += 1

        # Zu weit entfernte Matches loeschen (nur wenn keine KI-Bewertung)
        if matches_to_remove:
            from sqlalchemy import delete as sa_delete
            delete_stmt = (
                sa_delete(Match)
                .where(
                    Match.id.in_(matches_to_remove),
                    Match.ai_score.is_(None),  # Nur ohne KI-Bewertung loeschen
                )
            )
            del_result = await self.db.execute(delete_stmt)
            # Matches mit KI-Bewertung behalten aber distance setzen
            kept_count = len(matches_to_remove) - del_result.rowcount
            if kept_count > 0:
                logger.info(f"Pipeline Step4: {kept_count} Matches > 25km behalten (haben KI-Score)")

        await self.db.commit()

        logger.info(
            f"Pipeline Step4: {result.matches_updated} Distanzen aktualisiert, "
            f"{result.matches_removed} Matches > 25km"
        )
        return result

    # ──────────────────────────────────────────────────
    # Step 5: Pre-Matches automatisch generieren
    # ──────────────────────────────────────────────────

    async def _step5_generate_pre_matches(self, progress_callback=None) -> Step5Result:
        """
        Generiert Pre-Matches fuer FINANCE-Kategorie.

        Findet alle Job-Kandidat-Paare im Umkreis von 30km
        mit passendem Berufstitel und erstellt Match-Eintraege.
        """
        from app.services.pre_match_service import PreMatchService

        result = Step5Result()

        service = PreMatchService(self.db)
        gen_result = await service.generate_all(
            category="FINANCE",
            progress_callback=progress_callback,
        )

        result.combos_processed = gen_result.combos_processed
        result.matches_created = gen_result.matches_created
        result.errors_count = len(gen_result.errors)

        logger.info(
            f"Pipeline Step5: {result.matches_created} Pre-Matches erstellt "
            f"({result.combos_processed} Jobs verarbeitet)"
        )
        return result
