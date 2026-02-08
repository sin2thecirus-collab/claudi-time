"""Local Embedding Service — Embedding-Generierung fuer Matching Engine v2.

Nutzt OpenAI text-embedding-3-small (384-dim) als Standard.
Optional: sentence-transformers lokal (wenn genug RAM).

Kosten OpenAI:
- $0.02 pro 1M Tokens
- 5.000 Kandidaten + 1.500 Jobs = ~$0.05 (einmalig)
- Laufend: ~$0.01/Woche
"""

import logging
import math
from typing import Sequence

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Generiert 384-dimensionale Embeddings fuer Texte.

    Standard: OpenAI text-embedding-3-small (dimensions=384).
    Kein lokales Model noetig — spart ~1.5GB RAM auf Railway.
    """

    MODEL = "text-embedding-3-small"
    DIMENSIONS = 384
    MAX_BATCH_SIZE = 100  # OpenAI erlaubt bis 2048, aber wir bleiben konservativ

    def __init__(self):
        self.api_key = settings.openai_api_key
        self._client: httpx.AsyncClient | None = None

        if not self.api_key:
            logger.warning("OpenAI API-Key nicht konfiguriert — Embeddings deaktiviert")

    async def _get_client(self) -> httpx.AsyncClient:
        """HTTP-Client fuer OpenAI API (Singleton)."""
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

    async def close(self):
        """Schliesst den HTTP-Client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def embed(self, text: str) -> list[float] | None:
        """Erstellt ein 384-dim Embedding fuer einen einzelnen Text.

        Args:
            text: Eingabetext (max ~8000 Tokens)

        Returns:
            384-dim float-Liste oder None bei Fehler
        """
        if not self.api_key or not text or not text.strip():
            return None

        # Text kuerzen (OpenAI Limit: ~8191 Tokens ≈ 30.000 Zeichen)
        clean_text = text.strip()[:20000]

        client = await self._get_client()
        try:
            response = await client.post(
                "/embeddings",
                json={
                    "model": self.MODEL,
                    "input": clean_text,
                    "dimensions": self.DIMENSIONS,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["data"][0]["embedding"]

        except httpx.TimeoutException:
            logger.warning("Embedding Timeout")
            return None
        except httpx.HTTPStatusError as e:
            logger.error(f"Embedding API-Fehler: {e.response.status_code}")
            return None
        except Exception as e:
            logger.error(f"Embedding Fehler: {e}")
            return None

    async def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        """Erstellt Embeddings fuer mehrere Texte (Batch).

        Args:
            texts: Liste von Texten

        Returns:
            Liste von 384-dim Embeddings (None fuer fehlgeschlagene)
        """
        if not self.api_key or not texts:
            return [None] * len(texts)

        results: list[list[float] | None] = [None] * len(texts)

        # In Batches aufteilen
        for batch_start in range(0, len(texts), self.MAX_BATCH_SIZE):
            batch_end = min(batch_start + self.MAX_BATCH_SIZE, len(texts))
            batch_texts = []
            batch_indices = []

            for i in range(batch_start, batch_end):
                text = texts[i]
                if text and text.strip():
                    batch_texts.append(text.strip()[:20000])
                    batch_indices.append(i)

            if not batch_texts:
                continue

            client = await self._get_client()
            try:
                response = await client.post(
                    "/embeddings",
                    json={
                        "model": self.MODEL,
                        "input": batch_texts,
                        "dimensions": self.DIMENSIONS,
                    },
                )
                response.raise_for_status()
                data = response.json()

                for item in data["data"]:
                    idx = item["index"]
                    if idx < len(batch_indices):
                        results[batch_indices[idx]] = item["embedding"]

            except Exception as e:
                logger.error(f"Batch-Embedding Fehler: {e}")
                # Fallback: Einzeln versuchen
                for i, text in zip(batch_indices, batch_texts):
                    results[i] = await self.embed(text)

        return results

    @staticmethod
    def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
        """Berechnet die Kosinus-Aehnlichkeit zwischen zwei Vektoren.

        Args:
            a, b: Vektoren gleicher Laenge

        Returns:
            Float zwischen -1.0 und 1.0
        """
        if not a or not b or len(a) != len(b):
            return 0.0

        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)

    @staticmethod
    def cosine_similarity_batch(
        query: Sequence[float],
        candidates: list[Sequence[float]],
    ) -> list[float]:
        """Berechnet Kosinus-Aehnlichkeit von einem Query gegen viele Kandidaten.

        Optimiert fuer Batch-Vergleich (kein numpy noetig).

        Args:
            query: Query-Vektor (384-dim)
            candidates: Liste von Kandidaten-Vektoren

        Returns:
            Liste von Similarity-Scores
        """
        if not query or not candidates:
            return []

        # Query-Norm nur 1x berechnen
        query_norm = math.sqrt(sum(x * x for x in query))
        if query_norm == 0:
            return [0.0] * len(candidates)

        scores = []
        for cand in candidates:
            if not cand or len(cand) != len(query):
                scores.append(0.0)
                continue

            dot = sum(q * c for q, c in zip(query, cand))
            cand_norm = math.sqrt(sum(x * x for x in cand))

            if cand_norm == 0:
                scores.append(0.0)
            else:
                scores.append(dot / (query_norm * cand_norm))

        return scores


# Singleton-Instanz
_embedding_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """Gibt die Singleton-Instanz des EmbeddingService zurueck."""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service
