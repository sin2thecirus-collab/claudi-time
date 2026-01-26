"""Recruit CRM API Client.

Dieser Client kommuniziert mit der Recruit CRM API, um Kandidaten abzurufen.
Dokumentation: https://docs.recruitcrm.io/
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx

from app.config import limits, settings

logger = logging.getLogger(__name__)


class CRMError(Exception):
    """Basis-Exception für CRM-Fehler."""

    def __init__(self, message: str, status_code: int | None = None):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class CRMRateLimitError(CRMError):
    """Rate-Limit erreicht."""

    def __init__(self, retry_after: int | None = None):
        self.retry_after = retry_after or 60
        super().__init__(
            f"Rate-Limit erreicht. Erneuter Versuch in {self.retry_after} Sekunden.",
            status_code=429,
        )


class CRMAuthenticationError(CRMError):
    """Authentifizierungsfehler."""

    def __init__(self):
        super().__init__("CRM-Authentifizierung fehlgeschlagen. API-Key prüfen.", status_code=401)


class CRMNotFoundError(CRMError):
    """Ressource nicht gefunden."""

    def __init__(self, resource: str):
        super().__init__(f"Ressource nicht gefunden: {resource}", status_code=404)


class RecruitCRMClient:
    """Client für die Recruit CRM API.

    Unterstützt:
    - Kandidaten-Abruf (paginiert und einzeln)
    - CV-URL-Abruf
    - Rate-Limiting (60 Requests/Minute)
    - Retry bei Timeouts
    """

    # Rate-Limiting: 60 Requests pro Minute
    REQUESTS_PER_MINUTE = 60
    REQUEST_INTERVAL = 1.0  # 1 Sekunde zwischen Requests

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
    ):
        """Initialisiert den CRM-Client.

        Args:
            api_key: API-Schlüssel (Standard: aus Settings)
            base_url: Basis-URL der API (Standard: aus Settings)
            timeout: Timeout in Sekunden (Standard: aus Limits)
        """
        self.api_key = api_key or settings.recruit_crm_api_key
        self.base_url = (base_url or settings.recruit_crm_base_url).rstrip("/")
        self.timeout = timeout or limits.TIMEOUT_CRM

        if not self.api_key:
            raise ValueError("CRM API-Key nicht konfiguriert")

        self._last_request_time: float = 0
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Gibt den HTTP-Client zurück (lazy initialization)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def close(self) -> None:
        """Schließt den HTTP-Client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _rate_limit_wait(self) -> None:
        """Wartet, um das Rate-Limit einzuhalten."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        if elapsed < self.REQUEST_INTERVAL:
            await asyncio.sleep(self.REQUEST_INTERVAL - elapsed)
        self._last_request_time = asyncio.get_event_loop().time()

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        retry_count: int = 2,
    ) -> dict[str, Any]:
        """Führt einen API-Request durch.

        Args:
            method: HTTP-Methode (GET, POST, etc.)
            endpoint: API-Endpunkt (ohne Basis-URL)
            params: Query-Parameter
            retry_count: Anzahl Retry-Versuche bei Timeout

        Returns:
            JSON-Response als Dictionary

        Raises:
            CRMError: Bei API-Fehlern
            CRMRateLimitError: Bei Rate-Limit
            CRMAuthenticationError: Bei Authentifizierungsfehler
        """
        await self._rate_limit_wait()

        client = await self._get_client()
        url = f"{endpoint.lstrip('/')}"

        for attempt in range(retry_count + 1):
            try:
                logger.debug(f"CRM Request: {method} {url} (Versuch {attempt + 1})")

                response = await client.request(method, url, params=params)

                # Erfolg
                if response.status_code == 200:
                    return response.json()

                # Rate-Limit
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    raise CRMRateLimitError(
                        retry_after=int(retry_after) if retry_after else None
                    )

                # Authentifizierung
                if response.status_code == 401:
                    raise CRMAuthenticationError()

                # Nicht gefunden
                if response.status_code == 404:
                    raise CRMNotFoundError(url)

                # Andere Fehler
                error_msg = f"CRM API Fehler: {response.status_code}"
                try:
                    error_data = response.json()
                    if "message" in error_data:
                        error_msg = f"{error_msg} - {error_data['message']}"
                except Exception:
                    error_msg = f"{error_msg} - {response.text[:200]}"

                raise CRMError(error_msg, status_code=response.status_code)

            except httpx.TimeoutException:
                if attempt < retry_count:
                    logger.warning(f"CRM Timeout, Versuch {attempt + 2}/{retry_count + 1}")
                    await asyncio.sleep(1)
                    continue
                raise CRMError("CRM API Timeout nach mehreren Versuchen")

            except httpx.RequestError as e:
                if attempt < retry_count:
                    logger.warning(f"CRM Verbindungsfehler: {e}, Versuch {attempt + 2}/{retry_count + 1}")
                    await asyncio.sleep(1)
                    continue
                raise CRMError(f"CRM Verbindungsfehler: {e}")

    async def get_candidates(
        self,
        page: int = 1,
        per_page: int = 25,
        updated_since: datetime | None = None,
    ) -> dict[str, Any]:
        """Ruft Kandidaten paginiert ab.

        Args:
            page: Seitennummer (1-basiert)
            per_page: Einträge pro Seite (max. 100)
            updated_since: Nur Kandidaten, die nach diesem Zeitpunkt geändert wurden

        Returns:
            Dictionary mit 'data' (Liste der Kandidaten) und 'meta' (Pagination-Info)
        """
        params = {
            "page": page,
            "per_page": min(per_page, 100),  # API-Limit
            "sort_by": "updated_on",
            "sort_order": "desc",
        }

        if updated_since:
            # Format: ISO 8601
            params["updated_on_start"] = updated_since.isoformat()

        response = await self._request("GET", "/candidates", params=params)

        return {
            "data": response.get("data", []),
            "meta": {
                "current_page": response.get("current_page", page),
                "per_page": response.get("per_page", per_page),
                "total": response.get("total", 0),
                "last_page": response.get("last_page", 1),
            },
        }

    async def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Ruft einen einzelnen Kandidaten ab.

        Args:
            candidate_id: CRM-ID des Kandidaten

        Returns:
            Kandidaten-Daten als Dictionary

        Raises:
            CRMNotFoundError: Wenn Kandidat nicht gefunden
        """
        response = await self._request("GET", f"/candidates/{candidate_id}")
        return response.get("data", response)

    async def get_candidate_cv_url(self, candidate_id: str) -> str | None:
        """Ruft die CV-URL eines Kandidaten ab.

        Args:
            candidate_id: CRM-ID des Kandidaten

        Returns:
            CV-URL oder None, wenn kein CV vorhanden
        """
        try:
            candidate = await self.get_candidate(candidate_id)
            return candidate.get("resume_url") or candidate.get("resume", {}).get("url")
        except CRMNotFoundError:
            return None

    async def get_all_candidates_paginated(
        self,
        per_page: int = 100,
        updated_since: datetime | None = None,
        max_pages: int | None = None,
    ):
        """Generator, der alle Kandidaten seitenweise abruft.

        Args:
            per_page: Einträge pro Seite
            updated_since: Nur Kandidaten seit diesem Zeitpunkt
            max_pages: Maximale Anzahl Seiten (None = alle)

        Yields:
            Tuple (page_number, candidates_list, total_count)
        """
        page = 1
        total_pages = 1

        while page <= total_pages:
            if max_pages and page > max_pages:
                break

            result = await self.get_candidates(
                page=page,
                per_page=per_page,
                updated_since=updated_since,
            )

            meta = result["meta"]
            total_pages = meta["last_page"]
            total = meta["total"]

            yield page, result["data"], total

            page += 1

    def map_to_candidate_data(self, crm_data: dict[str, Any]) -> dict[str, Any]:
        """Mappt CRM-Daten auf das interne Kandidaten-Format.

        WICHTIG: Aus CRM werden nur Basisdaten + Adresse geholt!
        - Position, Werdegang (work_history) und Ausbildung (education)
          werden aus dem CV/Resume via OpenAI extrahiert.

        Args:
            crm_data: Rohdaten aus der CRM API

        Returns:
            Dictionary mit gemappten Feldern für CandidateCreate
        """
        # Adresse aus CRM-Feld "full_address" extrahieren
        full_address = (
            crm_data.get("full_address") or
            crm_data.get("address") or
            crm_data.get("location") or
            ""
        )
        address_parts = self._parse_address(full_address)

        # Skills aus CRM (kann String oder Liste sein)
        skills = crm_data.get("skills", [])
        if isinstance(skills, str):
            skills = [s.strip() for s in skills.split(",") if s.strip()]

        # CV/Resume URL - für späteres Parsing mit OpenAI
        resume_data = crm_data.get("resume")
        cv_url = None
        if isinstance(resume_data, dict):
            cv_url = resume_data.get("url")
        elif isinstance(resume_data, str):
            cv_url = resume_data
        if not cv_url:
            cv_url = crm_data.get("resume_url") or crm_data.get("cv_url")

        return {
            "crm_id": str(crm_data.get("id", crm_data.get("slug", ""))),
            "first_name": crm_data.get("first_name"),
            "last_name": crm_data.get("last_name"),
            "email": crm_data.get("email"),
            "phone": crm_data.get("phone") or crm_data.get("mobile") or crm_data.get("contact_number"),
            # Adresse aus CRM "full_address" Feld
            "street_address": address_parts.get("street"),
            "postal_code": address_parts.get("postal_code"),
            "city": crm_data.get("locality") or crm_data.get("city") or address_parts.get("city"),
            # Skills aus CRM
            "skills": skills if skills else None,
            # CV URL für OpenAI Parsing (Position, Werdegang, Ausbildung)
            "cv_url": cv_url,
            # NICHT aus CRM - wird aus CV via OpenAI extrahiert:
            # - current_position (aus CV)
            # - current_company (aus CV)
            # - work_history (aus CV)
            # - education (aus CV)
        }

    def _parse_address(self, full_address: str) -> dict[str, str | None]:
        """Parst eine vollständige Adresse in Komponenten.

        Args:
            full_address: Vollständige Adresse als String

        Returns:
            Dictionary mit 'street', 'postal_code', 'city'
        """
        if not full_address:
            return {"street": None, "postal_code": None, "city": None}

        # Versuche, deutsche Adressformate zu parsen
        # Format: "Straße 123, 12345 Stadt" oder "Straße 123, Stadt, 12345"
        parts = [p.strip() for p in full_address.split(",")]

        result = {"street": None, "postal_code": None, "city": None}

        if len(parts) >= 1:
            result["street"] = parts[0]

        # PLZ und Stadt suchen
        for part in parts[1:]:
            words = part.split()
            for i, word in enumerate(words):
                # Deutsche PLZ: 5 Ziffern
                if word.isdigit() and len(word) == 5:
                    result["postal_code"] = word
                    # Stadt ist der Rest
                    city_parts = words[i + 1:]
                    if city_parts:
                        result["city"] = " ".join(city_parts)
                    break
            else:
                # Keine PLZ gefunden, könnte Stadt sein
                if not result["city"] and part.strip():
                    result["city"] = part.strip()

        return result

    async def __aenter__(self) -> "RecruitCRMClient":
        """Context-Manager Entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context-Manager Exit."""
        await self.close()
