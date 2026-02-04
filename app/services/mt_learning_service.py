"""MT Learning Service — Lern-Logik fuer manuelle Titel-Zuweisungen.

Speichert jede manuelle Zuweisung als Trainingsdaten und kann
aehnliche CVs finden um Vorschlaege zu machen.

Phase 1: Lookup-Tabelle + GPT-4o-mini (primitiv, aber sammelt Daten)
Phase 2 (spaeter): SetFit ML-Modell auf gesammelten Daten
Phase 3 (spaeter): Eigenes deutsches LLM
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.mt_training import MTTrainingData

logger = logging.getLogger(__name__)


# Haeufige Buchhaltungs-Jobtitel (fuer Chips/Checkboxen in der UI)
COMMON_JOB_TITLES = [
    "Bilanzbuchhalter/in",
    "Finanzbuchhalter/in",
    "Kreditorenbuchhalter/in",
    "Debitorenbuchhalter/in",
    "Lohnbuchhalter/in",
    "Lohn- und Gehaltsbuchhalter/in",
    "Steuerfachangestellte/r",
    "Controller/in",
    "Buchhalter/in",
    "Hauptbuchhalter/in",
    "Anlagenbuchhalter/in",
    "Kontokorrentbuchhalter/in",
    "Finanzbuchhalter/in mit Schwerpunkt Kreditoren",
    "Finanzbuchhalter/in mit Schwerpunkt Debitoren",
    "Kaufmaennische/r Mitarbeiter/in Buchhaltung",
    "Sachbearbeiter/in Buchhaltung",
]


class MTLearningService:
    """Service fuer das Lern-System (Training Data + Vorschlaege)."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def save_title_assignment(
        self,
        candidate: Candidate,
        assigned_titles: list[str],
        predicted_titles: list[str] | None = None,
    ) -> MTTrainingData:
        """Speichert eine manuelle Titel-Zuweisung als Trainingsdaten.

        Args:
            candidate: Der Kandidat
            assigned_titles: Die manuell zugewiesenen Titel
            predicted_titles: Was MT vorgeschlagen hatte (optional)

        Returns:
            Der erstellte MTTrainingData-Eintrag
        """
        # CV-Zusammenfassung erstellen
        input_text = self._build_cv_summary(candidate)

        # Pruefen ob MT richtig lag
        was_correct = None
        if predicted_titles:
            was_correct = set(predicted_titles) == set(assigned_titles)

        # Reasoning generieren
        reasoning = None
        if predicted_titles and not was_correct:
            reasoning = (
                f"MT hat {predicted_titles} vorgeschlagen, "
                f"User hat {assigned_titles} zugewiesen."
            )

        # Embedding vom Kandidaten uebernehmen (falls vorhanden)
        embedding = None
        if hasattr(candidate, "embedding") and candidate.embedding:
            embedding = candidate.embedding

        training_entry = MTTrainingData(
            entity_type="candidate",
            entity_id=candidate.id,
            input_text=input_text,
            predicted_titles=predicted_titles,
            assigned_titles=assigned_titles,
            was_correct=was_correct,
            reasoning=reasoning,
            embedding=embedding,
        )

        self.db.add(training_entry)
        await self.db.flush()

        logger.info(
            f"Training-Daten gespeichert: Kandidat {candidate.full_name} → "
            f"{assigned_titles} (korrekt: {was_correct})"
        )
        return training_entry

    async def get_suggestion_for_candidate(
        self, candidate: Candidate
    ) -> dict:
        """Generiert einen Titel-Vorschlag fuer einen Kandidaten.

        Prioritaet:
        1. Aehnliche CVs in mt_training_data (falls genug Daten)
        2. classification_data (OpenAI) falls vorhanden
        3. hotlist_job_title (Keyword-System)
        4. Kein Vorschlag

        Returns:
            {
                "suggested_titles": [...],
                "source": "training_data" | "classification" | "keyword" | "none",
                "confidence": float (0-1),
                "reasoning": str,
            }
        """
        # Methode 1: Aehnliche CVs aus Training-Daten
        if hasattr(candidate, "embedding") and candidate.embedding:
            similar = await self._find_similar_training_entries(candidate)
            if similar:
                return similar

        # Methode 2: OpenAI classification_data
        if candidate.classification_data and isinstance(candidate.classification_data, dict):
            roles = candidate.classification_data.get("roles", [])
            if roles:
                return {
                    "suggested_titles": roles,
                    "source": "classification",
                    "confidence": 0.6,
                    "reasoning": (
                        f"Basierend auf OpenAI-Klassifizierung: "
                        f"{candidate.classification_data.get('reasoning', '')}"
                    ),
                }

        # Methode 3: Keyword-basierter Titel
        if candidate.hotlist_job_titles:
            return {
                "suggested_titles": list(candidate.hotlist_job_titles),
                "source": "keyword",
                "confidence": 0.4,
                "reasoning": "Basierend auf Keyword-Kategorisierung",
            }
        elif candidate.hotlist_job_title:
            return {
                "suggested_titles": [candidate.hotlist_job_title],
                "source": "keyword",
                "confidence": 0.4,
                "reasoning": "Basierend auf Keyword-Kategorisierung",
            }

        # Methode 4: Kein Vorschlag
        return {
            "suggested_titles": [],
            "source": "none",
            "confidence": 0.0,
            "reasoning": "Kein Vorschlag moeglich — bitte manuell zuweisen",
        }

    async def _find_similar_training_entries(
        self, candidate: Candidate
    ) -> dict | None:
        """Sucht aehnliche CVs in mt_training_data per Embedding-Vergleich.

        Primitive Implementierung: Cosinus-Aehnlichkeit in Python.
        Spaeter: pgvector fuer SQL-basierte Suche.
        """
        # Mindestens 5 Training-Eintraege benoetigt
        count_q = await self.db.execute(
            select(func.count(MTTrainingData.id)).where(
                MTTrainingData.entity_type == "candidate",
                MTTrainingData.embedding.isnot(None),
            )
        )
        total_entries = count_q.scalar() or 0
        if total_entries < 5:
            return None

        # Alle Eintraege mit Embeddings laden
        entries_q = await self.db.execute(
            select(MTTrainingData).where(
                MTTrainingData.entity_type == "candidate",
                MTTrainingData.embedding.isnot(None),
            ).order_by(MTTrainingData.created_at.desc()).limit(200)
        )
        entries = entries_q.scalars().all()

        if not entries:
            return None

        # Cosinus-Aehnlichkeit berechnen
        cand_emb = candidate.embedding
        if not cand_emb or not isinstance(cand_emb, list):
            return None

        best_score = -1.0
        best_entry = None
        title_votes: dict[str, float] = {}

        for entry in entries:
            if not entry.embedding or not isinstance(entry.embedding, list):
                continue

            similarity = self._cosine_similarity(cand_emb, entry.embedding)

            if similarity > best_score:
                best_score = similarity
                best_entry = entry

            # Titel-Voting: Aehnlichkeit als Gewicht
            if similarity > 0.75 and entry.assigned_titles:
                for title in entry.assigned_titles:
                    title_votes[title] = title_votes.get(title, 0) + similarity

        if not title_votes or best_score < 0.75:
            return None

        # Top-Titel nach Gewicht sortieren
        sorted_titles = sorted(
            title_votes.items(), key=lambda x: x[1], reverse=True
        )
        suggested = [t[0] for t in sorted_titles[:3]]

        return {
            "suggested_titles": suggested,
            "source": "training_data",
            "confidence": min(best_score, 0.95),
            "reasoning": (
                f"Basierend auf {len(title_votes)} aehnlichen CVs "
                f"(beste Aehnlichkeit: {best_score:.0%})"
            ),
        }

    async def get_training_stats(self) -> dict:
        """Gibt Statistiken ueber die gesammelten Trainingsdaten zurueck."""
        # Gesamtanzahl
        total_q = await self.db.execute(
            select(func.count(MTTrainingData.id))
        )
        total = total_q.scalar() or 0

        # Wie oft war MT korrekt?
        correct_q = await self.db.execute(
            select(func.count(MTTrainingData.id)).where(
                MTTrainingData.was_correct.is_(True)
            )
        )
        correct = correct_q.scalar() or 0

        incorrect_q = await self.db.execute(
            select(func.count(MTTrainingData.id)).where(
                MTTrainingData.was_correct.is_(False)
            )
        )
        incorrect = incorrect_q.scalar() or 0

        # Titel-Verteilung
        # raw SQL weil wir JSONB-Arrays auflösen muessen
        try:
            title_dist_q = await self.db.execute(text("""
                SELECT title, COUNT(*) as cnt
                FROM mt_training_data,
                     LATERAL jsonb_array_elements_text(assigned_titles) AS title
                WHERE entity_type = 'candidate'
                GROUP BY title
                ORDER BY cnt DESC
                LIMIT 20
            """))
            title_distribution = [
                {"title": row[0], "count": row[1]}
                for row in title_dist_q.all()
            ]
        except Exception:
            title_distribution = []

        evaluated = correct + incorrect
        accuracy = round(correct / evaluated * 100, 1) if evaluated > 0 else 0

        return {
            "total_entries": total,
            "correct_predictions": correct,
            "incorrect_predictions": incorrect,
            "accuracy_percent": accuracy,
            "title_distribution": title_distribution,
        }

    def _build_cv_summary(self, candidate: Candidate) -> str:
        """Erstellt eine Text-Zusammenfassung des Kandidaten-CVs."""
        parts = []

        if candidate.current_position:
            parts.append(f"Position: {candidate.current_position}")
        if candidate.current_company:
            parts.append(f"Unternehmen: {candidate.current_company}")
        if candidate.city:
            parts.append(f"Stadt: {candidate.city}")

        # Skills
        if candidate.skills:
            parts.append(f"Skills: {', '.join(candidate.skills[:20])}")

        # IT-Skills
        if candidate.it_skills:
            parts.append(f"IT-Skills: {', '.join(candidate.it_skills[:15])}")

        # Berufserfahrung (erste 3 Stationen)
        if candidate.work_history and isinstance(candidate.work_history, list):
            wh_lines = []
            for entry in candidate.work_history[:3]:
                if isinstance(entry, dict):
                    position = entry.get("position", entry.get("titel", ""))
                    company = entry.get("company", entry.get("unternehmen", ""))
                    if position or company:
                        wh_lines.append(f"  - {position} bei {company}".strip())
            if wh_lines:
                parts.append("Berufserfahrung:\n" + "\n".join(wh_lines))

        # Ausbildung (erste 2)
        if candidate.education and isinstance(candidate.education, list):
            edu_lines = []
            for entry in candidate.education[:2]:
                if isinstance(entry, dict):
                    degree = entry.get("degree", entry.get("abschluss", ""))
                    institution = entry.get("institution", entry.get("schule", ""))
                    if degree or institution:
                        edu_lines.append(f"  - {degree} ({institution})".strip())
            if edu_lines:
                parts.append("Ausbildung:\n" + "\n".join(edu_lines))

        # Weiterbildungen
        if candidate.further_education and isinstance(candidate.further_education, list):
            fe_lines = []
            for entry in candidate.further_education[:3]:
                if isinstance(entry, dict):
                    name = entry.get("name", entry.get("bezeichnung", ""))
                    if name:
                        fe_lines.append(f"  - {name}")
                elif isinstance(entry, str):
                    fe_lines.append(f"  - {entry}")
            if fe_lines:
                parts.append("Weiterbildungen:\n" + "\n".join(fe_lines))

        # Sprachen
        if candidate.languages and isinstance(candidate.languages, list):
            lang_strs = []
            for lang in candidate.languages[:5]:
                if isinstance(lang, dict):
                    name = lang.get("language", lang.get("sprache", ""))
                    level = lang.get("level", lang.get("niveau", ""))
                    if name:
                        lang_strs.append(f"{name} ({level})" if level else name)
                elif isinstance(lang, str):
                    lang_strs.append(lang)
            if lang_strs:
                parts.append(f"Sprachen: {', '.join(lang_strs)}")

        return "\n".join(parts) if parts else "Keine CV-Daten verfuegbar"

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Berechnet Cosinus-Aehnlichkeit zwischen zwei Vektoren."""
        if len(a) != len(b) or not a:
            return 0.0

        dot_product = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot_product / (norm_a * norm_b)
