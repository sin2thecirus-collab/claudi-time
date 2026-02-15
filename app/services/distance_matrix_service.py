"""Google Maps Distance Matrix Service — Phase 10.

Berechnet echte Fahrzeit (Auto + ÖPNV) zwischen Job und Kandidaten
über die Google Maps Distance Matrix API.

Caching-Strategie: PLZ→PLZ Paare werden gecacht, um API-Kosten
zu minimieren (~$5/1000 Elements).

Ablauf im Matching:
1. Hard Filter (PostGIS Luftlinie ≤30km) → ~200 Kandidaten
2. Distance Matrix (Google Maps) → echte Fahrzeit für diese ~200
3. Fahrzeit wird auf Match gespeichert (drive_time_car_min, drive_time_transit_min)
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Google Maps API ──────────────────────────────────────────
DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
# Google erlaubt max 25 origins ODER 25 destinations pro Request
BATCH_SIZE = 25
# Rate-Limit Pause zwischen Batch-Requests (Sekunden)
RATE_LIMIT_DELAY = 0.1


@dataclass
class DriveTimeResult:
    """Fahrzeit-Ergebnis für ein Origin-Destination-Paar."""

    car_min: int | None = None      # Fahrzeit Auto in Minuten
    transit_min: int | None = None   # Fahrzeit ÖPNV in Minuten
    car_km: float | None = None      # Strecke Auto in km
    status: str = "ok"               # ok / not_found / api_error / no_api_key


class DistanceMatrixService:
    """Google Maps Distance Matrix API für echte Fahrzeit.

    Features:
    - PLZ→PLZ Caching (In-Memory) — reduziert API-Calls massiv
    - Batch-Requests (25 Destinations pro Call)
    - Separate Auto + ÖPNV Abfragen
    - Graceful Fallback wenn kein API-Key konfiguriert
    """

    # ── Class-Level Cache: (origin_plz, dest_plz) → DriveTimeResult ──
    _cache: dict[tuple[str, str], DriveTimeResult] = {}
    _cache_hits: int = 0
    _cache_misses: int = 0
    _api_calls: int = 0
    _api_elements: int = 0

    def __init__(self) -> None:
        self._api_key = settings.google_maps_api_key

    @property
    def has_api_key(self) -> bool:
        """Prüft ob ein Google Maps API Key konfiguriert ist."""
        return bool(self._api_key)

    # ── Public API ───────────────────────────────────────────

    async def get_drive_time(
        self,
        origin_lat: float,
        origin_lng: float,
        origin_plz: str | None,
        dest_lat: float,
        dest_lng: float,
        dest_plz: str | None,
    ) -> DriveTimeResult:
        """Fahrzeit Auto + ÖPNV für ein einzelnes Paar.

        Args:
            origin_lat/lng: Koordinaten des Startpunkts (Kandidat)
            origin_plz: PLZ des Startpunkts (für Caching)
            dest_lat/lng: Koordinaten des Ziels (Job)
            dest_plz: PLZ des Ziels (für Caching)

        Returns:
            DriveTimeResult mit car_min, transit_min, car_km
        """
        if not self.has_api_key:
            return DriveTimeResult(status="no_api_key")

        # Gleiche PLZ → ~0 Fahrzeit (Nachbar-PLZ)
        if origin_plz and dest_plz and origin_plz == dest_plz:
            return DriveTimeResult(car_min=5, transit_min=10, car_km=2.0, status="same_plz")

        # Cache-Check
        cache_key = self._make_cache_key(origin_plz, dest_plz)
        if cache_key and cache_key in self._cache:
            self._cache_hits += 1
            return self._cache[cache_key]

        self._cache_misses += 1

        # API-Calls: Auto + ÖPNV
        origin = f"{origin_lat},{origin_lng}"
        dest = f"{dest_lat},{dest_lng}"

        car_result = await self._call_api(origin, dest, mode="driving")
        transit_result = await self._call_api(origin, dest, mode="transit")

        result = DriveTimeResult(
            car_min=car_result.get("duration_min"),
            transit_min=transit_result.get("duration_min"),
            car_km=car_result.get("distance_km"),
            status="ok" if car_result.get("status") == "OK" else "api_error",
        )

        # Cache speichern
        if cache_key and result.status == "ok":
            self._cache[cache_key] = result

        return result

    async def batch_drive_times(
        self,
        job_lat: float,
        job_lng: float,
        job_plz: str | None,
        candidates: list[dict],
    ) -> dict[str, DriveTimeResult]:
        """Batch-Fahrzeit für alle Kandidaten zu einem Job.

        Nutzt Google Maps Batch-API: 25 destinations pro Request.
        Cached PLZ→PLZ Paare um API-Calls zu reduzieren.

        Args:
            job_lat/lng: Koordinaten des Jobs
            job_plz: PLZ des Jobs
            candidates: Liste von dicts mit keys:
                - candidate_id (str)
                - lat (float)
                - lng (float)
                - plz (str | None)

        Returns:
            dict[candidate_id → DriveTimeResult]
        """
        if not self.has_api_key:
            logger.info("Kein Google Maps API Key — überspringe Fahrzeit-Berechnung")
            return {}

        if not candidates:
            return {}

        results: dict[str, DriveTimeResult] = {}
        uncached: list[dict] = []

        # ── Phase 1: Cache-Prüfung ──
        for cand in candidates:
            cand_id = cand["candidate_id"]
            cand_plz = cand.get("plz")

            # Gleiche PLZ
            if cand_plz and job_plz and cand_plz == job_plz:
                results[cand_id] = DriveTimeResult(
                    car_min=5, transit_min=10, car_km=2.0, status="same_plz"
                )
                continue

            # Cache-Check
            cache_key = self._make_cache_key(cand_plz, job_plz)
            if cache_key and cache_key in self._cache:
                self._cache_hits += 1
                results[cand_id] = self._cache[cache_key]
                continue

            self._cache_misses += 1
            uncached.append(cand)

        if not uncached:
            logger.info(
                f"Alle {len(candidates)} Kandidaten aus Cache bedient "
                f"(hits={self._cache_hits})"
            )
            return results

        logger.info(
            f"Fahrzeit-Berechnung: {len(uncached)} von {len(candidates)} "
            f"Kandidaten per Google Maps API ({len(candidates) - len(uncached)} aus Cache)"
        )

        # ── Phase 2: Batch-API-Calls ──
        job_origin = f"{job_lat},{job_lng}"

        # Aufteilen in Batches von max 25
        for batch_start in range(0, len(uncached), BATCH_SIZE):
            batch = uncached[batch_start : batch_start + BATCH_SIZE]
            destinations = [f"{c['lat']},{c['lng']}" for c in batch]
            dest_str = "|".join(destinations)

            # Auto-Fahrzeit
            car_results = await self._call_batch_api(job_origin, dest_str, mode="driving")

            # ÖPNV-Fahrzeit
            transit_results = await self._call_batch_api(job_origin, dest_str, mode="transit")

            # Ergebnisse zuordnen
            for i, cand in enumerate(batch):
                cand_id = cand["candidate_id"]
                cand_plz = cand.get("plz")

                car_data = car_results[i] if i < len(car_results) else {}
                transit_data = transit_results[i] if i < len(transit_results) else {}

                result = DriveTimeResult(
                    car_min=car_data.get("duration_min"),
                    transit_min=transit_data.get("duration_min"),
                    car_km=car_data.get("distance_km"),
                    status="ok" if car_data.get("status") == "OK" else "api_error",
                )

                results[cand_id] = result

                # Cache speichern
                cache_key = self._make_cache_key(cand_plz, job_plz)
                if cache_key and result.status == "ok":
                    self._cache[cache_key] = result

            # Rate-Limit Pause
            if batch_start + BATCH_SIZE < len(uncached):
                await asyncio.sleep(RATE_LIMIT_DELAY)

        logger.info(
            f"Fahrzeit fertig: {len(results)} Ergebnisse, "
            f"API-Calls: {self._api_calls}, Elements: {self._api_elements}, "
            f"Cache: {len(self._cache)} Einträge"
        )

        return results

    def get_cache_stats(self) -> dict:
        """Cache-Statistiken für Debug-Endpoint."""
        return {
            "cache_size": len(self._cache),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate": (
                round(self._cache_hits / (self._cache_hits + self._cache_misses) * 100, 1)
                if (self._cache_hits + self._cache_misses) > 0
                else 0
            ),
            "api_calls_total": self._api_calls,
            "api_elements_total": self._api_elements,
            "estimated_cost_usd": round(self._api_elements * 0.005, 2),
            "has_api_key": self.has_api_key,
        }

    def clear_cache(self) -> int:
        """Cache leeren. Gibt Anzahl gelöschter Einträge zurück."""
        count = len(self._cache)
        self._cache.clear()
        return count

    # ── Private Methods ──────────────────────────────────────

    def _make_cache_key(
        self, plz_a: str | None, plz_b: str | None
    ) -> tuple[str, str] | None:
        """Erstellt einen sortierten Cache-Key aus zwei PLZ.

        Sortiert, damit A→B und B→A den gleichen Key haben.
        Returns None wenn eine PLZ fehlt.
        """
        if not plz_a or not plz_b:
            return None
        # Sortieren: kleinere PLZ zuerst
        return tuple(sorted([plz_a.strip(), plz_b.strip()]))

    async def _call_api(
        self, origin: str, destination: str, mode: str = "driving"
    ) -> dict:
        """Einzelner API-Call für ein Origin-Destination-Paar.

        Returns: {"duration_min": int, "distance_km": float, "status": str}
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                params = {
                    "origins": origin,
                    "destinations": destination,
                    "mode": mode,
                    "language": "de",
                    "key": self._api_key,
                }

                # ÖPNV braucht departure_time
                if mode == "transit":
                    import time
                    # Nächsten Montag 8:00 Uhr als Referenz
                    now = time.time()
                    # Einfach: jetzt + 1 Tag als Annäherung
                    params["departure_time"] = str(int(now + 86400))

                self._api_calls += 1
                self._api_elements += 1

                resp = await client.get(DISTANCE_MATRIX_URL, params=params)
                resp.raise_for_status()
                data = resp.json()

                if data.get("status") != "OK":
                    logger.warning(f"Distance Matrix API Fehler: {data.get('status')}")
                    return {"status": data.get("status", "ERROR")}

                element = data["rows"][0]["elements"][0]
                if element["status"] != "OK":
                    return {"status": element["status"]}

                duration_sec = element["duration"]["value"]
                distance_m = element.get("distance", {}).get("value", 0)

                return {
                    "duration_min": math.ceil(duration_sec / 60),
                    "distance_km": round(distance_m / 1000, 1),
                    "status": "OK",
                }

        except Exception as e:
            logger.error(f"Distance Matrix API Fehler ({mode}): {e}")
            return {"status": "ERROR", "error": str(e)}

    async def _call_batch_api(
        self, origin: str, destinations: str, mode: str = "driving"
    ) -> list[dict]:
        """Batch API-Call: 1 Origin → N Destinations (max 25).

        Args:
            origin: "lat,lng"
            destinations: "lat1,lng1|lat2,lng2|..." (pipe-getrennt)
            mode: "driving" oder "transit"

        Returns: Liste von dicts, ein Element pro Destination
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                params = {
                    "origins": origin,
                    "destinations": destinations,
                    "mode": mode,
                    "language": "de",
                    "key": self._api_key,
                }

                if mode == "transit":
                    import time
                    params["departure_time"] = str(int(time.time() + 86400))

                dest_count = len(destinations.split("|"))
                self._api_calls += 1
                self._api_elements += dest_count

                resp = await client.get(DISTANCE_MATRIX_URL, params=params)
                resp.raise_for_status()
                data = resp.json()

                if data.get("status") != "OK":
                    logger.warning(
                        f"Batch Distance Matrix API Fehler: {data.get('status')} "
                        f"({dest_count} destinations, mode={mode})"
                    )
                    return [{"status": "ERROR"}] * dest_count

                results = []
                for element in data["rows"][0]["elements"]:
                    if element["status"] != "OK":
                        results.append({"status": element["status"]})
                        continue

                    duration_sec = element["duration"]["value"]
                    distance_m = element.get("distance", {}).get("value", 0)

                    results.append({
                        "duration_min": math.ceil(duration_sec / 60),
                        "distance_km": round(distance_m / 1000, 1),
                        "status": "OK",
                    })

                return results

        except Exception as e:
            logger.error(f"Batch Distance Matrix API Fehler ({mode}): {e}")
            dest_count = len(destinations.split("|"))
            return [{"status": "ERROR", "error": str(e)}] * dest_count


# ── Singleton-Instanz ────────────────────────────────────────
distance_matrix_service = DistanceMatrixService()
