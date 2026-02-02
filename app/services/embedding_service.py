"""Embedding Service - Generiert und verwaltet Vektoren fuer semantisches Matching.

Nutzt OpenAI text-embedding-3-small (1536 Dimensionen):
- $0.02 / 1M Tokens → ~$0.05 fuer alle Finance-Kandidaten + Jobs
- Generiert Embeddings aus strukturierten Texten (CV, Job-Beschreibung)
- Speichert Vektoren in PostgreSQL als JSONB-Array (kein pgvector noetig!)
- Bietet Cosine-Similarity-Suche in Python + PostGIS-Distanzfilter in SQL
"""

import logging
import math
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, select, text, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.candidate import Candidate
from app.models.job import Job

logger = logging.getLogger(__name__)

# OpenAI Embedding Model
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536

# Preise (Stand: 2026)
PRICE_EMBEDDING_PER_1M = 0.02  # $0.02 / 1M Tokens


class EmbeddingService:
    """Service fuer Embedding-Generierung und Similarity-Suche.

    Verantwortlich fuer:
    - Text-Aufbereitung: Baut aus strukturierten Daten einen optimalen Embedding-Text
    - Embedding-Generierung: OpenAI API Call (text-embedding-3-small)
    - Speicherung: Vektoren in candidates.embedding / jobs.embedding
    - Similarity-Suche: Python Cosine-Similarity + PostGIS-Distanzfilter
    """

    def __init__(self, db: AsyncSession, api_key: str | None = None):
        self.db = db
        self.api_key = api_key or settings.openai_api_key
        self._client: httpx.AsyncClient | None = None
        self._total_tokens = 0

    async def _get_client(self) -> httpx.AsyncClient:
        """HTTP-Client fuer OpenAI API."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://api.openai.com/v1",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(60.0),
            )
        return self._client

    async def close(self) -> None:
        """Schliesst den HTTP-Client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @property
    def total_tokens(self) -> int:
        """Gesamtverbrauch dieser Session."""
        return self._total_tokens

    @property
    def total_cost_usd(self) -> float:
        """Geschaetzte Kosten dieser Session."""
        return round((self._total_tokens / 1_000_000) * PRICE_EMBEDDING_PER_1M, 6)

    # ═══════════════════════════════════════════════════════════════
    # TEXT-AUFBEREITUNG
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def build_candidate_text(candidate: Candidate) -> str:
        """Baut den Embedding-Text fuer einen Kandidaten.

        STRUKTUR (Reihenfolge ist wichtig — Anfang wird staerker gewichtet):
        1. Kernprofil: Titel + Kerntaetigkeiten (Zusammenfassung)
        2. Kompletter Werdegang MIT Taetigkeitsbeschreibungen
        3. Weiterbildungen (IHK Bilanzbuchhalter etc.)
        4. Skills, IT-Kenntnisse, Sprachen
        5. Ausbildung

        Warum Titel + Taetigkeiten als Zusammenfassung am Anfang?
        → "Bilanzbuchhalter" kann bedeuten: Eigenstaendige JA-Erstellung ODER
          nur Mitwirkung bei Abschluessen. Erst die Taetigkeiten zeigen das Level.
        → Embeddings gewichten den Anfang eines Textes staerker.
        → Die Zusammenfassung gibt dem Vektor die richtige "Richtung".

        KEIN Name, keine Kontaktdaten (Datenschutz + irrelevant fuer Matching).
        """
        parts = []

        # ── 1. KERNPROFIL (Titel + Kerntaetigkeiten als Zusammenfassung) ──
        # Das ist das Wichtigste: WAS macht die Person KONKRET?
        # Steht bewusst am Anfang, weil Embeddings den Textanfang staerker gewichten.
        profile_parts = []

        if candidate.current_position:
            profile_parts.append(candidate.current_position)
        elif candidate.hotlist_job_titles:
            profile_parts.append(candidate.hotlist_job_titles[0])

        # Kerntaetigkeiten aus der aktuellsten Position extrahieren
        work_history = candidate.work_history or []
        latest_desc = ""
        if work_history:
            for entry in work_history:
                if isinstance(entry, dict) and entry.get("description"):
                    latest_desc = entry["description"]
                    break  # Nur die aktuellste (erste) Position

        if profile_parts:
            summary = f"Kernprofil: {' / '.join(profile_parts)}"
            if latest_desc:
                # Erste 300 Zeichen der Kerntaetigkeiten — genuegt fuer die Embedding-Richtung
                summary += f" — Kerntaetigkeiten: {latest_desc[:300]}"
            parts.append(summary)

        # Klassifizierte Rollen (alle, fuer Breite)
        if candidate.hotlist_job_titles:
            parts.append(f"Rollen: {', '.join(candidate.hotlist_job_titles)}")

        if candidate.current_company:
            parts.append(f"Aktuelles Unternehmen: {candidate.current_company}")

        # ── 2. KOMPLETTER WERDEGANG (mit allen Taetigkeiten) ──
        if work_history:
            work_lines = ["Berufserfahrung:"]
            for entry in work_history:
                if not isinstance(entry, dict):
                    continue
                position = entry.get("position", "")
                company = entry.get("company", "")
                start = entry.get("start_date", "")
                end = entry.get("end_date", "aktuell")
                desc = entry.get("description", "")

                line = f"- {position}"
                if company:
                    line += f" bei {company}"
                if start:
                    line += f" ({start} bis {end})"
                work_lines.append(line)

                # Taetigkeitsbeschreibung KOMPLETT (nicht abschneiden!)
                if desc:
                    work_lines.append(f"  Taetigkeiten: {desc}")

            parts.append("\n".join(work_lines))

        # Ausbildung
        education = candidate.education or []
        if education:
            edu_lines = ["Ausbildung:"]
            for entry in education:
                if isinstance(entry, dict):
                    degree = entry.get("degree", "")
                    institution = entry.get("institution", "")
                    field_of_study = entry.get("field_of_study", "")
                    edu_parts = [p for p in [degree, field_of_study, institution] if p]
                    if edu_parts:
                        edu_lines.append(f"- {', '.join(edu_parts)}")
            parts.append("\n".join(edu_lines))

        # Weiterbildungen (Bilanzbuchhalter IHK, Steuerfachwirt etc.)
        further_edu = candidate.further_education or []
        if further_edu:
            fe_lines = ["Weiterbildungen:"]
            for entry in further_edu:
                if isinstance(entry, dict):
                    title = entry.get("title", entry.get("name", ""))
                    institution = entry.get("institution", entry.get("provider", ""))
                    fe_parts = [p for p in [title, institution] if p]
                    if fe_parts:
                        fe_lines.append(f"- {', '.join(fe_parts)}")
                elif isinstance(entry, str) and entry.strip():
                    fe_lines.append(f"- {entry}")
            parts.append("\n".join(fe_lines))

        # Skills
        if candidate.skills:
            parts.append(f"Skills: {', '.join(candidate.skills)}")

        # IT-Kenntnisse (DATEV, SAP, Lexware etc.)
        if candidate.it_skills:
            parts.append(f"IT-Kenntnisse: {', '.join(candidate.it_skills)}")

        # Sprachen
        languages = candidate.languages or []
        if languages:
            lang_parts = []
            for entry in languages:
                if isinstance(entry, dict):
                    lang_parts.append(
                        f"{entry.get('language', '?')} ({entry.get('level', '?')})"
                    )
                elif isinstance(entry, str):
                    lang_parts.append(entry)
            if lang_parts:
                parts.append(f"Sprachen: {', '.join(lang_parts)}")

        # Fallback: CV-Text wenn kein strukturierter Werdegang
        if not work_history and candidate.cv_text:
            # CV-Text maximal 2000 Zeichen (fuer Embedding reicht das)
            cv_text = candidate.cv_text[:2000]
            parts.append(f"Lebenslauf:\n{cv_text}")

        return "\n\n".join(parts)

    @staticmethod
    def build_job_text(job: Job) -> str:
        """Baut den Embedding-Text fuer einen Job.

        STRUKTUR (Reihenfolge ist wichtig — Anfang wird staerker gewichtet):
        1. Kernprofil: Gesuchte Rolle + Kern-Anforderungen (Zusammenfassung)
        2. Klassifizierte Rollen + Metadaten
        3. Vollstaendiger Stellentext (NICHT abgeschnitten!)

        Warum Zusammenfassung am Anfang?
        → Jobtitel allein ist mehrdeutig ("Buchhalter" kann Junior oder Senior sein)
        → Erst Position + Kerntaetigkeiten bestimmen, wen wir wirklich suchen
        → Stellentexte sind oft lang mit HR-Bla — das Wesentliche muss vorne stehen
        """
        parts = []

        # ── 1. KERNPROFIL (Position + erste 300 Zeichen des Stellentexts) ──
        # Gibt dem Embedding die richtige "Richtung" bevor der lange Text kommt
        if job.position:
            summary = f"Gesucht: {job.position}"
            if job.job_text:
                # Erste 300 Zeichen enthalten meist die Kern-Anforderungen
                summary += f" — Anforderungen: {job.job_text[:300]}"
            parts.append(summary)

        # ── 2. METADATEN + KLASSIFIZIERUNG ──
        if job.hotlist_job_titles:
            parts.append(f"Rollen: {', '.join(job.hotlist_job_titles)}")
        if job.industry:
            parts.append(f"Branche: {job.industry}")
        if job.employment_type:
            parts.append(f"Beschaeftigungsart: {job.employment_type}")

        # ── 3. VOLLSTAENDIGER STELLENTEXT ──
        # NICHT abschneiden — Deep-AI bekommt ihn sowieso,
        # aber fuer das Embedding ist die volle Tiefe wichtig
        if job.job_text:
            parts.append(f"Stellenbeschreibung:\n{job.job_text}")

        return "\n\n".join(parts)

    # ═══════════════════════════════════════════════════════════════
    # EMBEDDING-GENERIERUNG
    # ═══════════════════════════════════════════════════════════════

    async def generate_embedding(self, text_input: str) -> list[float] | None:
        """Generiert ein Embedding fuer einen Text via OpenAI API.

        Args:
            text_input: Der zu embeddierende Text

        Returns:
            Liste von 1536 Floats oder None bei Fehler
        """
        if not self.api_key:
            logger.warning("OpenAI API-Key nicht konfiguriert — Embedding nicht moeglich")
            return None

        if not text_input or not text_input.strip():
            logger.warning("Leerer Text — kein Embedding moeglich")
            return None

        try:
            client = await self._get_client()

            response = await client.post(
                "/embeddings",
                json={
                    "model": EMBEDDING_MODEL,
                    "input": text_input,
                    "dimensions": EMBEDDING_DIMENSIONS,
                },
            )
            response.raise_for_status()
            result = response.json()

            # Token-Tracking
            usage = result.get("usage", {})
            tokens_used = usage.get("total_tokens", 0)
            self._total_tokens += tokens_used

            embedding = result["data"][0]["embedding"]
            return embedding

        except httpx.HTTPStatusError as e:
            logger.error(f"OpenAI Embedding API-Fehler: {e.response.status_code}")
            return None
        except Exception as e:
            logger.error(f"Embedding-Generierung fehlgeschlagen: {e}")
            return None

    # ═══════════════════════════════════════════════════════════════
    # EINZELNE EMBEDDINGS GENERIEREN + SPEICHERN
    # ═══════════════════════════════════════════════════════════════

    async def embed_candidate(self, candidate_id: UUID) -> bool:
        """Generiert und speichert Embedding fuer einen Kandidaten.

        Returns:
            True bei Erfolg, False bei Fehler
        """
        result = await self.db.execute(
            select(Candidate).where(Candidate.id == candidate_id)
        )
        candidate = result.scalar_one_or_none()

        if not candidate:
            logger.warning(f"Kandidat {candidate_id} nicht gefunden")
            return False

        text_input = self.build_candidate_text(candidate)
        if not text_input.strip():
            logger.warning(f"Kandidat {candidate_id}: Kein Text fuer Embedding")
            return False

        embedding = await self.generate_embedding(text_input)
        if embedding is None:
            return False

        candidate.embedding = embedding
        await self.db.flush()

        logger.debug(f"Embedding fuer Kandidat {candidate_id} generiert ({len(text_input)} Zeichen)")
        return True

    async def embed_job(self, job_id: UUID) -> bool:
        """Generiert und speichert Embedding fuer einen Job.

        Returns:
            True bei Erfolg, False bei Fehler
        """
        result = await self.db.execute(
            select(Job).where(Job.id == job_id)
        )
        job = result.scalar_one_or_none()

        if not job:
            logger.warning(f"Job {job_id} nicht gefunden")
            return False

        text_input = self.build_job_text(job)
        if not text_input.strip():
            logger.warning(f"Job {job_id}: Kein Text fuer Embedding")
            return False

        embedding = await self.generate_embedding(text_input)
        if embedding is None:
            return False

        job.embedding = embedding
        await self.db.flush()

        logger.debug(f"Embedding fuer Job {job_id} generiert ({len(text_input)} Zeichen)")
        return True

    # ═══════════════════════════════════════════════════════════════
    # BATCH: ALLE FINANCE-EMBEDDINGS GENERIEREN
    # ═══════════════════════════════════════════════════════════════

    async def embed_all_finance_candidates(
        self,
        progress_callback: Any = None,
    ) -> dict:
        """Generiert Embeddings fuer alle Finance-Kandidaten ohne Embedding.

        Returns:
            Dict mit Statistiken: total, embedded, skipped, errors
        """
        # Alle Finance-Kandidaten ohne Embedding laden
        query = (
            select(Candidate.id)
            .where(
                and_(
                    Candidate.hotlist_category == "FINANCE",
                    Candidate.hidden == False,  # noqa: E712
                    Candidate.deleted_at.is_(None),
                    Candidate.embedding.is_(None),
                )
            )
            .order_by(Candidate.created_at.desc())
        )

        result = await self.db.execute(query)
        candidate_ids = [row[0] for row in result.all()]

        stats = {"total": len(candidate_ids), "embedded": 0, "skipped": 0, "errors": 0}

        if not candidate_ids:
            logger.info("Keine Finance-Kandidaten ohne Embedding gefunden")
            return stats

        logger.info(f"Embedding-Generierung fuer {len(candidate_ids)} Finance-Kandidaten gestartet")

        for i, cid in enumerate(candidate_ids):
            try:
                success = await self.embed_candidate(cid)
                if success:
                    stats["embedded"] += 1
                else:
                    stats["skipped"] += 1

                # Alle 20 Kandidaten committen (Batch-Commit)
                if (i + 1) % 20 == 0:
                    await self.db.commit()
                    if progress_callback:
                        progress_callback(
                            "embedding_candidates",
                            f"{i + 1}/{len(candidate_ids)} Kandidaten | "
                            f"{stats['embedded']} embedded | "
                            f"~${self.total_cost_usd:.4f}",
                        )

            except Exception as e:
                logger.error(f"Embedding-Fehler fuer Kandidat {cid}: {e}")
                stats["errors"] += 1
                # Rollback damit die Session nicht im Fehlerzustand haengt
                try:
                    await self.db.rollback()
                except Exception:
                    pass

        # Finaler Commit
        try:
            await self.db.commit()
        except Exception as e:
            logger.error(f"Finaler Commit fehlgeschlagen: {e}")
            await self.db.rollback()

        logger.info(
            f"Finance-Kandidaten Embedding fertig: "
            f"{stats['embedded']}/{stats['total']} embedded, "
            f"{stats['skipped']} uebersprungen, {stats['errors']} Fehler, "
            f"Kosten: ~${self.total_cost_usd:.4f}"
        )
        return stats

    async def embed_all_finance_jobs(
        self,
        progress_callback: Any = None,
    ) -> dict:
        """Generiert Embeddings fuer alle Finance-Jobs ohne Embedding.

        Returns:
            Dict mit Statistiken: total, embedded, skipped, errors
        """
        query = (
            select(Job.id)
            .where(
                and_(
                    Job.hotlist_category == "FINANCE",
                    Job.deleted_at.is_(None),
                    Job.embedding.is_(None),
                )
            )
            .order_by(Job.created_at.desc())
        )

        result = await self.db.execute(query)
        job_ids = [row[0] for row in result.all()]

        stats = {"total": len(job_ids), "embedded": 0, "skipped": 0, "errors": 0}

        if not job_ids:
            logger.info("Keine Finance-Jobs ohne Embedding gefunden")
            return stats

        logger.info(f"Embedding-Generierung fuer {len(job_ids)} Finance-Jobs gestartet")

        for i, jid in enumerate(job_ids):
            try:
                success = await self.embed_job(jid)
                if success:
                    stats["embedded"] += 1
                else:
                    stats["skipped"] += 1

                if (i + 1) % 20 == 0:
                    await self.db.commit()
                    if progress_callback:
                        progress_callback(
                            "embedding_jobs",
                            f"{i + 1}/{len(job_ids)} Jobs | "
                            f"{stats['embedded']} embedded | "
                            f"~${self.total_cost_usd:.4f}",
                        )

            except Exception as e:
                logger.error(f"Embedding-Fehler fuer Job {jid}: {e}")
                stats["errors"] += 1
                # Rollback damit die Session nicht im Fehlerzustand haengt
                try:
                    await self.db.rollback()
                except Exception:
                    pass

        # Finaler Commit
        try:
            await self.db.commit()
        except Exception as e:
            logger.error(f"Finaler Commit fehlgeschlagen: {e}")
            await self.db.rollback()

        logger.info(
            f"Finance-Jobs Embedding fertig: "
            f"{stats['embedded']}/{stats['total']} embedded, "
            f"{stats['skipped']} uebersprungen, {stats['errors']} Fehler, "
            f"Kosten: ~${self.total_cost_usd:.4f}"
        )
        return stats

    # ═══════════════════════════════════════════════════════════════
    # SIMILARITY-SUCHE (Python Cosine-Similarity + PostGIS Distanz)
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
        """Berechnet die Cosine-Similarity zwischen zwei Vektoren.

        Rein Python — kein numpy noetig. Performant genug fuer ~2000 Kandidaten.
        """
        dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot_product / (norm_a * norm_b)

    async def find_similar_candidates(
        self,
        job_id: UUID,
        limit: int = 10,
        max_distance_km: float = 30.0,
    ) -> list[dict]:
        """Findet die aehnlichsten Kandidaten fuer einen Job via Embedding-Similarity.

        Ablauf:
        1. Job-Embedding laden
        2. Alle Finance-Kandidaten mit Embedding + Distanz laden (PostGIS-Filter in SQL)
        3. Cosine-Similarity in Python berechnen
        4. Top N zurueckgeben

        Performant genug fuer ~2000 Finance-Kandidaten (< 100ms in Python).

        Args:
            job_id: Job-ID
            limit: Maximale Anzahl Ergebnisse (default: 10)
            max_distance_km: Maximale Entfernung in km (default: 30)

        Returns:
            Liste von Dicts: [{"candidate_id", "similarity", "distance_km"}, ...]
            Sortiert nach Similarity DESC
        """
        # Job laden
        result = await self.db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()

        if not job:
            logger.warning(f"Job {job_id} nicht gefunden")
            return []

        if job.embedding is None:
            logger.warning(f"Job {job_id} hat kein Embedding — erst generieren!")
            return []

        job_embedding = job.embedding  # JSONB → Python list[float]
        if not isinstance(job_embedding, list) or len(job_embedding) == 0:
            logger.warning(f"Job {job_id}: Ungültiges Embedding-Format")
            return []

        # ── Schritt 1: Alle Finance-Kandidaten mit Embedding + PostGIS-Distanz laden ──
        # Distanz wird in SQL berechnet (PostGIS), Similarity in Python.
        query = text("""
            SELECT
                c.id AS candidate_id,
                c.embedding AS candidate_embedding,
                CASE
                    WHEN c.address_coords IS NOT NULL AND j.location_coords IS NOT NULL
                    THEN ST_Distance(
                        c.address_coords::geography,
                        j.location_coords::geography
                    ) / 1000.0
                    ELSE NULL
                END AS distance_km
            FROM candidates c
            CROSS JOIN (SELECT location_coords FROM jobs WHERE id = :job_id) j
            WHERE
                c.hotlist_category = 'FINANCE'
                AND c.hidden = false
                AND c.deleted_at IS NULL
                AND c.embedding IS NOT NULL
                AND (
                    -- Distanz-Filter: Entweder innerhalb max_distance_km ODER keine Koordinaten
                    c.address_coords IS NULL
                    OR j.location_coords IS NULL
                    OR ST_DWithin(
                        c.address_coords::geography,
                        j.location_coords::geography,
                        :max_distance_m
                    )
                )
        """)

        result = await self.db.execute(
            query,
            {
                "job_id": str(job_id),
                "max_distance_m": max_distance_km * 1000,  # km → Meter
            },
        )

        # ── Schritt 2: Cosine-Similarity in Python berechnen ──
        scored_candidates = []
        for row in result.all():
            cand_id = row[0]
            cand_embedding = row[1]  # JSONB → Python list
            distance_km = row[2]

            if not isinstance(cand_embedding, list) or len(cand_embedding) == 0:
                continue

            similarity = self._cosine_similarity(job_embedding, cand_embedding)
            scored_candidates.append({
                "candidate_id": cand_id,
                "similarity": round(similarity, 4),
                "distance_km": round(float(distance_km), 1) if distance_km is not None else None,
            })

        # ── Schritt 3: Top N sortiert nach Similarity DESC ──
        scored_candidates.sort(key=lambda c: c["similarity"], reverse=True)
        top_candidates = scored_candidates[:limit]

        logger.info(
            f"Similarity-Suche fuer Job {job_id}: "
            f"{len(top_candidates)} von {len(scored_candidates)} Kandidaten zurueckgegeben "
            f"(max {max_distance_km}km, Top {limit})"
        )

        return top_candidates

    # ═══════════════════════════════════════════════════════════════
    # STATUS / STATISTIKEN
    # ═══════════════════════════════════════════════════════════════

    async def get_embedding_stats(self) -> dict:
        """Gibt Statistiken ueber den Embedding-Status zurueck.

        Returns:
            Dict mit Anzahl Kandidaten/Jobs mit/ohne Embedding
        """
        # Kandidaten
        cand_total = await self.db.execute(
            select(func.count(Candidate.id)).where(
                and_(
                    Candidate.hotlist_category == "FINANCE",
                    Candidate.hidden == False,  # noqa: E712
                    Candidate.deleted_at.is_(None),
                )
            )
        )
        cand_with_emb = await self.db.execute(
            select(func.count(Candidate.id)).where(
                and_(
                    Candidate.hotlist_category == "FINANCE",
                    Candidate.hidden == False,  # noqa: E712
                    Candidate.deleted_at.is_(None),
                    Candidate.embedding.is_not(None),
                )
            )
        )

        # Jobs
        job_total = await self.db.execute(
            select(func.count(Job.id)).where(
                and_(
                    Job.hotlist_category == "FINANCE",
                    Job.deleted_at.is_(None),
                )
            )
        )
        job_with_emb = await self.db.execute(
            select(func.count(Job.id)).where(
                and_(
                    Job.hotlist_category == "FINANCE",
                    Job.deleted_at.is_(None),
                    Job.embedding.is_not(None),
                )
            )
        )

        ct = cand_total.scalar() or 0
        ce = cand_with_emb.scalar() or 0
        jt = job_total.scalar() or 0
        je = job_with_emb.scalar() or 0

        return {
            "candidates": {"total": ct, "with_embedding": ce, "without_embedding": ct - ce},
            "jobs": {"total": jt, "with_embedding": je, "without_embedding": jt - je},
            "session_tokens": self._total_tokens,
            "session_cost_usd": self.total_cost_usd,
        }

    # ═══════════════════════════════════════════════════════════════
    # CONTEXT MANAGER
    # ═══════════════════════════════════════════════════════════════

    async def __aenter__(self) -> "EmbeddingService":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
