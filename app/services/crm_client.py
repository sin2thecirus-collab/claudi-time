"""Recruit CRM API Client.

Dieser Client kommuniziert mit der Recruit CRM API, um Kandidaten,
Unternehmen und Kontakte abzurufen.
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
        # HINWEIS: Recruit CRM API akzeptiert keine sort_by/sort_order Parameter!
        # Diese wurden entfernt, da sie HTTP 422 verursachen.
        params: dict[str, Any] = {}

        # Pagination - nur wenn explizit gesetzt
        if page > 1:
            params["page"] = page

        if updated_since:
            # Format: ISO 8601
            params["updated_on_start"] = updated_since.isoformat()

        logger.info(f"CRM get_candidates aufgerufen: page={page}, params={params}")
        response = await self._request("GET", "/candidates", params=params if params else None)
        logger.info(f"CRM Response erhalten: {len(response.get('data', []))} Kandidaten, next_page={response.get('next_page_url')}")

        # Recruit CRM gibt Pagination-Felder direkt in der Response zurück (nicht in "meta")
        # Felder: current_page, data, first_page_url, from, next_page_url, path, per_page, prev_page_url, to
        data = response.get("data", [])

        # Berechne total und last_page aus den verfügbaren Feldern
        # "to" ist der Index des letzten Elements auf dieser Seite
        # "from" ist der Index des ersten Elements auf dieser Seite
        current_page = response.get("current_page", page)
        items_per_page = response.get("per_page", per_page)
        to_index = response.get("to", len(data))

        # Wenn next_page_url null ist, sind wir auf der letzten Seite
        has_next = response.get("next_page_url") is not None

        # Schätze total basierend auf aktueller Seite und ob es weitere gibt
        if has_next:
            # Es gibt mehr Seiten - schätze total als mindestens aktuelle Position + 1 Seite
            estimated_total = to_index + items_per_page
            estimated_last_page = current_page + 1
        else:
            # Letzte Seite
            estimated_total = to_index if to_index else len(data)
            estimated_last_page = current_page

        return {
            "data": data,
            "meta": {
                "current_page": current_page,
                "per_page": items_per_page,
                "total": estimated_total,
                "last_page": estimated_last_page,
                "has_next": has_next,
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
        has_more = True
        estimated_total = 0

        while has_more:
            if max_pages and page > max_pages:
                break

            result = await self.get_candidates(
                page=page,
                per_page=per_page,
                updated_since=updated_since,
            )

            meta = result["meta"]
            has_more = meta.get("has_next", False)
            estimated_total = meta.get("total", estimated_total)
            candidates = result["data"]

            # Keine Daten? Abbrechen
            if not candidates:
                break

            yield page, candidates, estimated_total

            page += 1

    def map_to_candidate_data(self, crm_data: dict[str, Any]) -> dict[str, Any]:
        """Mappt CRM-Daten auf das interne Kandidaten-Format.

        Basierend auf der tatsächlichen Recruit CRM API Response-Struktur:
        - id, first_name, last_name, email, contact_number
        - city, postal_code, address (direkte Felder)
        - position, current_organization
        - skill (Array von Skill-Objekten)
        - resume (Objekt mit file_link)

        Args:
            crm_data: Rohdaten aus der CRM API

        Returns:
            Dictionary mit gemappten Feldern für CandidateCreate
        """
        # Adresse - Recruit CRM liefert "address" als Volltext
        # plus separate Felder "postal_code" und "city"/"locality".
        # Das address-Feld enthält oft die komplette Adresse inkl. Stadt, PLZ, Land
        # → Wir extrahieren nur die Straße daraus.
        raw_address = crm_data.get("address") or ""
        postal_code = crm_data.get("postal_code") or None
        city = crm_data.get("city") or crm_data.get("locality") or None

        # Straße aus dem address-Feld extrahieren (Land/Stadt/PLZ entfernen)
        street_address = self._extract_street_from_address(
            raw_address, postal_code, city
        )

        # Skills aus CRM - API liefert "skill" als Array von Objekten oder String
        skills_data = crm_data.get("skill", [])
        skills = []
        if isinstance(skills_data, list):
            for s in skills_data:
                if isinstance(s, dict):
                    skills.append(s.get("name", str(s)))
                elif isinstance(s, str):
                    skills.append(s)
        elif isinstance(skills_data, str) and skills_data:
            skills = [s.strip() for s in skills_data.split(",") if s.strip()]

        # CV/Resume URL - Recruit CRM verwendet "resume.file_link"
        resume_data = crm_data.get("resume")
        cv_url = None
        if isinstance(resume_data, dict):
            cv_url = resume_data.get("file_link") or resume_data.get("url")
        elif isinstance(resume_data, str):
            cv_url = resume_data

        # Position und Firma direkt aus CRM (falls vorhanden)
        current_position = crm_data.get("position") or None
        current_company = crm_data.get("current_organization") or None

        # CRM-ID: Verwende "slug" als eindeutige ID (z.B. "17694407523800116635EqN")
        crm_id = crm_data.get("slug") or str(crm_data.get("id", ""))

        return {
            "crm_id": crm_id,
            "first_name": crm_data.get("first_name"),
            "last_name": crm_data.get("last_name"),
            "email": crm_data.get("email"),
            "phone": crm_data.get("contact_number") or crm_data.get("phone") or crm_data.get("mobile"),
            # Adresse direkt aus CRM-Feldern
            "street_address": street_address,
            "postal_code": postal_code,
            "city": city,
            # Position und Firma aus CRM (kann durch CV-Parsing überschrieben werden)
            "current_position": current_position,
            "current_company": current_company,
            # Skills aus CRM
            "skills": skills if skills else None,
            # CV URL für optionales OpenAI Parsing
            "cv_url": cv_url,
        }

    # Länder-Keywords zum Rausfiltern aus Adressen
    _COUNTRY_KEYWORDS = {
        "germany", "deutschland", "austria", "österreich", "switzerland",
        "schweiz", "france", "frankreich", "netherlands", "niederlande",
        "belgium", "belgien", "italy", "italien", "spain", "spanien",
        "poland", "polen", "czech republic", "tschechien",
    }

    def _extract_street_from_address(
        self,
        full_address: str,
        known_postal_code: str | None,
        known_city: str | None,
    ) -> str | None:
        """Extrahiert nur die Straße+Hausnummer aus einem vollen Adress-String.

        Entfernt PLZ, Stadt, Land aus dem address-Feld, damit nur
        "Straße Hausnummer" übrig bleibt.

        Args:
            full_address: Vollständige Adresse aus CRM (z.B. "Musterstr. 5, München, Germany, 80331")
            known_postal_code: Separat gelieferter PLZ-Wert aus CRM
            known_city: Separat gelieferter Stadt-Wert aus CRM

        Returns:
            Nur der Straßen-Teil oder None
        """
        if not full_address or not full_address.strip():
            return None

        parts = [p.strip() for p in full_address.split(",")]
        street_parts = []

        for part in parts:
            part_lower = part.lower().strip()

            # Land rausfiltern
            if part_lower in self._COUNTRY_KEYWORDS:
                continue

            # Bekannte Stadt rausfiltern (case-insensitive)
            if known_city and part_lower == known_city.lower().strip():
                continue

            # Bekannte PLZ rausfiltern
            if known_postal_code and part.strip() == known_postal_code.strip():
                continue

            # 5-stellige PLZ erkennen und rausfiltern (auch wenn nicht separat geliefert)
            stripped = part.strip()
            if stripped.isdigit() and len(stripped) == 5:
                continue

            # Kombination "PLZ Stadt" erkennen (z.B. "85662 Hohenbrunn")
            words = stripped.split()
            if len(words) >= 2 and words[0].isdigit() and len(words[0]) == 5:
                continue

            # Teil behalten (gehört zur Straße)
            if part.strip():
                street_parts.append(part.strip())

        result = ", ".join(street_parts) if street_parts else None

        # Falls Ergebnis identisch mit der vollen Adresse ist und wir
        # keine PLZ/Stadt separat haben, die _parse_address Methode nutzen
        if result and not known_postal_code and not known_city:
            parsed = self._parse_address(full_address)
            if parsed.get("street"):
                return parsed["street"]

        return result

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

    # ── Companies ────────────────────────────────────────

    async def get_companies(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """Ruft Unternehmen paginiert ab.

        Args:
            page: Seitennummer (1-basiert)
            per_page: Eintraege pro Seite (max. 100)

        Returns:
            Dictionary mit 'data' (Liste) und 'meta' (Pagination)
        """
        params: dict[str, Any] = {}
        if page > 1:
            params["page"] = page

        logger.info(f"CRM get_companies: page={page}")
        response = await self._request("GET", "/companies", params=params if params else None)

        data = response.get("data", [])
        current_page = response.get("current_page", page)
        items_per_page = response.get("per_page", per_page)
        to_index = response.get("to", len(data))
        has_next = response.get("next_page_url") is not None

        if has_next:
            estimated_total = to_index + items_per_page
            estimated_last_page = current_page + 1
        else:
            estimated_total = to_index if to_index else len(data)
            estimated_last_page = current_page

        return {
            "data": data,
            "meta": {
                "current_page": current_page,
                "per_page": items_per_page,
                "total": estimated_total,
                "last_page": estimated_last_page,
                "has_next": has_next,
            },
        }

    async def get_all_companies_paginated(
        self,
        per_page: int = 100,
        max_pages: int | None = None,
    ):
        """Generator, der alle Unternehmen seitenweise abruft.

        Yields:
            Tuple (page_number, companies_list, total_count)
        """
        page = 1
        has_more = True
        estimated_total = 0

        while has_more:
            if max_pages and page > max_pages:
                break

            # Rate-Limit Retry (max 3 Versuche)
            for attempt in range(3):
                try:
                    result = await self.get_companies(page=page, per_page=per_page)
                    break
                except CRMRateLimitError as e:
                    wait = e.retry_after if attempt < 2 else 120
                    logger.warning(f"Rate-Limit bei Seite {page}, warte {wait}s (Versuch {attempt+1}/3)")
                    await asyncio.sleep(wait)
            else:
                logger.error(f"Rate-Limit: 3 Versuche fehlgeschlagen bei Seite {page}")
                break

            meta = result["meta"]
            has_more = meta.get("has_next", False)
            estimated_total = meta.get("total", estimated_total)
            companies = result["data"]

            if not companies:
                break

            yield page, companies, estimated_total
            page += 1

    def map_to_company_data(self, crm_data: dict[str, Any]) -> dict[str, Any]:
        """Mappt CRM-Unternehmensdaten auf das interne Format.

        Recruit CRM Company-Felder:
        - company_name, website, phone, city, postal_code, address
        - no_of_employees (String oder Zahl)

        Returns:
            Dictionary fuer Company-Erstellung (OHNE crm_id!)
        """
        # Name (Pflichtfeld)
        name = (crm_data.get("company_name") or "").strip()
        if not name:
            return {}

        # Domain bereinigen (kein https://, kein www.)
        raw_domain = crm_data.get("website") or ""
        domain = raw_domain.strip()
        if domain:
            domain = domain.replace("https://", "").replace("http://", "").rstrip("/")
            if not domain.startswith("www."):
                domain = domain  # Behalten wie es ist

        # Adresse zusammenbauen (EIN Feld)
        raw_address = (crm_data.get("address") or "").strip()
        postal_code = (crm_data.get("postal_code") or "").strip()
        city_raw = (crm_data.get("city") or crm_data.get("locality") or "").strip()

        # Adresse zusammensetzen: street, PLZ Stadt
        address_parts = []
        # Strassenteil aus raw_address extrahieren (ohne Stadt/PLZ/Land)
        street = self._extract_street_from_address(raw_address, postal_code or None, city_raw or None)
        if street:
            address_parts.append(street)
        plz_city = " ".join(p for p in [postal_code, city_raw] if p)
        if plz_city:
            address_parts.append(plz_city)
        address = ", ".join(address_parts) if address_parts else None

        # Telefon
        phone = (crm_data.get("phone") or "").strip() or None

        # Mitarbeiteranzahl
        emp = crm_data.get("no_of_employees")
        employee_count = str(emp).strip() if emp else None

        return {
            "name": name,
            "domain": domain or None,
            "address": address,
            "city": city_raw or None,
            "phone": phone,
            "employee_count": employee_count,
        }

    # ── Contacts ──────────────────────────────────────────

    async def get_contacts(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """Ruft Kontakte paginiert ab.

        Args:
            page: Seitennummer (1-basiert)
            per_page: Eintraege pro Seite (max. 100)

        Returns:
            Dictionary mit 'data' (Liste) und 'meta' (Pagination)
        """
        params: dict[str, Any] = {}
        if page > 1:
            params["page"] = page

        logger.info(f"CRM get_contacts: page={page}")
        response = await self._request("GET", "/contacts", params=params if params else None)

        data = response.get("data", [])
        current_page = response.get("current_page", page)
        items_per_page = response.get("per_page", per_page)
        to_index = response.get("to", len(data))
        has_next = response.get("next_page_url") is not None

        if has_next:
            estimated_total = to_index + items_per_page
            estimated_last_page = current_page + 1
        else:
            estimated_total = to_index if to_index else len(data)
            estimated_last_page = current_page

        return {
            "data": data,
            "meta": {
                "current_page": current_page,
                "per_page": items_per_page,
                "total": estimated_total,
                "last_page": estimated_last_page,
                "has_next": has_next,
            },
        }

    async def get_all_contacts_paginated(
        self,
        per_page: int = 100,
        max_pages: int | None = None,
    ):
        """Generator, der alle Kontakte seitenweise abruft.

        Yields:
            Tuple (page_number, contacts_list, total_count)
        """
        page = 1
        has_more = True
        estimated_total = 0

        while has_more:
            if max_pages and page > max_pages:
                break

            # Rate-Limit Retry (max 3 Versuche)
            for attempt in range(3):
                try:
                    result = await self.get_contacts(page=page, per_page=per_page)
                    break
                except CRMRateLimitError as e:
                    wait = e.retry_after if attempt < 2 else 120
                    logger.warning(f"Rate-Limit bei Seite {page}, warte {wait}s (Versuch {attempt+1}/3)")
                    await asyncio.sleep(wait)
            else:
                logger.error(f"Rate-Limit: 3 Versuche fehlgeschlagen bei Seite {page}")
                break

            meta = result["meta"]
            has_more = meta.get("has_next", False)
            estimated_total = meta.get("total", estimated_total)
            contacts = result["data"]

            if not contacts:
                break

            yield page, contacts, estimated_total
            page += 1

    def map_to_contact_data(self, crm_data: dict[str, Any]) -> dict[str, Any]:
        """Mappt CRM-Kontaktdaten auf das interne Format.

        Recruit CRM Contact-Felder:
        - first_name, last_name, email, phone, mobile_number
        - designation (Position), city/locality
        - company_name (Name des zugehoerigen Unternehmens)

        Returns:
            Dictionary fuer CompanyContact-Erstellung (OHNE crm_id!)
        """
        first_name = (crm_data.get("first_name") or "").strip() or None
        last_name = (crm_data.get("last_name") or "").strip() or None

        if not first_name and not last_name:
            return {}

        # Anrede aus "title" oder "salutation" Feld
        salutation = (crm_data.get("title") or crm_data.get("salutation") or "").strip() or None

        # Position
        position = (crm_data.get("designation") or crm_data.get("position") or "").strip() or None

        # Kontaktdaten
        email = (crm_data.get("email") or "").strip() or None
        phone = (crm_data.get("phone") or "").strip() or None
        mobile = (crm_data.get("mobile_number") or crm_data.get("mobile") or "").strip() or None

        # Stadt/Arbeitsort
        city = (crm_data.get("city") or crm_data.get("locality") or "").strip() or None

        # Firmenname fuer Zuordnung (wird beim Import verwendet)
        company_name = (crm_data.get("company_name") or "").strip() or None

        return {
            "salutation": salutation,
            "first_name": first_name,
            "last_name": last_name,
            "position": position,
            "email": email,
            "phone": phone,
            "mobile": mobile,
            "city": city,
            "_company_name": company_name,  # Intern, fuer Zuordnung beim Import
        }

    async def __aenter__(self) -> "RecruitCRMClient":
        """Context-Manager Entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context-Manager Exit."""
        await self.close()
