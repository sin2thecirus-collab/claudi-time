"""CV Parser Service - Extrahiert strukturierte Daten aus CVs.

Dieser Service:
- Lädt PDFs von URLs herunter
- Extrahiert Text mit PyMuPDF
- Nutzt OpenAI für strukturierte Extraktion
- Aktualisiert Kandidaten in der DB
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import UUID

import httpx

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import limits, settings
from app.models.candidate import Candidate
from app.schemas.candidate import CVParseResult, EducationEntry, LanguageEntry, WorkHistoryEntry

logger = logging.getLogger(__name__)


# System-Prompt für CV-Parsing (optimiert für Geschwindigkeit)
CV_PARSING_SYSTEM_PROMPT = """Extrahiere strukturierte Daten aus dem CV. Texte 1:1 uebernehmen, nicht umformulieren!

WICHTIGSTE REGEL — NIEMALS DATEN ERFINDEN:
- Extrahiere NUR Informationen die WORTWÖRTLICH im CV-Text stehen!
- Wenn bei einer Position KEINE Taetigkeiten/Aufgaben beschrieben sind → description: null
- Wenn KEIN Firmenname bei einer Position steht → company: null
- NIEMALS Aufgaben, Taetigkeiten, Firmen oder andere Daten ERFINDEN oder VERMUTEN!
- Lieber null zurueckgeben als etwas zu halluzinieren!

REGELN:
- Fehlende Info = null (JSON null, nicht String "null")
- Datum: "MM/YYYY" oder "YYYY", aktuell = "heute"
- Bulletpoints: JEDE Taetigkeit mit "• " beginnen, mit "\\n" trennen
- institution und degree bei education/further_education NIEMALS null wenn Info im CV vorhanden!
- NAMEN UND POSITIONEN: IMMER in korrekter Gross-/Kleinschreibung! "LARA HARTMANN" -> "Lara Hartmann", "STEUERFACHANGESTELLTE" -> "Steuerfachangestellte". NIEMALS komplett in Grossbuchstaben!

KATEGORIEN:

1. current_position: Aktuelle/letzte Berufsbezeichnung (korrekte Schreibweise!)
2. current_company: Aktueller/letzter Arbeitgeber aus dem Werdegang. Wenn kein Firmenname im CV steht → null!

3. work_history: ALLE Jobs (auch Praktika, Nebenjobs), neueste zuerst
   - {company, position, start_date, end_date, description}
   - description: NUR Taetigkeiten die EXPLIZIT im CV stehen als Bullets. Wenn KEINE Aufgaben beschrieben → null! NIEMALS Aufgaben erfinden!
   - company: NUR wenn ein Firmenname im CV steht. Wenn kein Firmenname → null!
   - NICHT hier: Ausbildungen, Weiterbildungen, IHK, Zertifikate -> education/further_education

4. education: Schulen, Berufsausbildungen (NICHT IHK!), Studium
   - {institution, degree, field_of_study, start_date, end_date}
   - institution: PFLICHT! Name der Schule/Uni/Bildungseinrichtung. NIEMALS null wenn im CV erkennbar!
   - degree: PFLICHT! Abschluss/Ausbildungsberuf. NIEMALS null wenn im CV erkennbar!
   - degree extrahieren aus "Abschluss:" oder Schultyp:
     * Berufsoberschule/FOS = "Hochschulreife", Gymnasium = "Abitur"
     * Wirtschaftsschule = "Wirtschaftsschulabschluss", Realschule = "Mittlere Reife"
   - "Ausbildung zum/zur X" -> degree = Beruf, ABER: IHK-Pruefungen (Bilanzbuchhalter, Fachwirt, etc.) -> further_education!
   - Studium ohne Abschluss: degree = null

5. further_education: IHK-Pruefungen (Bilanzbuchhalter, Fachwirt, Betriebswirt IHK), Meister, Seminare, Zertifikate
   - {institution, degree, field_of_study, start_date, end_date}
   - institution: PFLICHT! Name der IHK/Bildungstraeger/Akademie. NIEMALS null wenn erkennbar!
   - degree: PFLICHT! Bezeichnung der Weiterbildung/Pruefung. NIEMALS null!
   - Auch Teilinfos extrahieren (z.B. nur "IHK" statt vollem Namen)
   - WICHTIG: Auch wenn im CV unter "Ausbildung" gelistet - IHK = immer further_education!
   - Gesamten CV durchsuchen!

6. it_skills: Software/Tools (SAP, DATEV, Excel, ERP, CRM, Programmiersprachen)

7. languages: [{language, level}] - Level wie im CV (B2, Muttersprache, fliessend, etc.)

8. skills: Fachkenntnisse (NICHT IT, NICHT Sprachen), Fuehrerschein

9. Persoenlich: first_name, last_name, email, phone, birth_date (DD.MM.YYYY)
   - NAMEN: Korrekte Gross-/Kleinschreibung! "LARA" -> "Lara", "SOPHIE" -> "Sophie"
   - email: E-Mail-Adresse aus dem CV extrahieren! Steht oft in der Kopfzeile/Kontaktdaten.
   - phone: Telefonnummer (Festnetz oder Mobil) extrahieren!
   - birth_date IMMER suchen! Haeufige Formate:
     "geboren am 15.03.1985", "geb. 15.03.85", "Geburtsdatum: 15.03.1985"
     "* 15.03.1985", "Jahrgang 1985", "15. Maerz 1985"
     Wenn nur Jahr: "01.01.YYYY". Steht oft am Anfang oder Ende des CVs.
   - Wenn KEIN Geburtsdatum auffindbar: estimated_age anhand des Werdegangs schaetzen!
     Berufsstart-Jahr abziehen von aktuellem Jahr + ca. 20 Jahre Lebenserfahrung.
     Beispiel: Berufsstart 2010 → geschaetzt 2026-2010+20 = 36 Jahre → estimated_age: 36

10. Adresse: street_address, postal_code, city
    - Steht meistens in der Kopfzeile des CVs bei den Kontaktdaten!
    - "Musterstr. 12, 60311 Frankfurt" -> street_address: "Musterstr. 12", postal_code: "60311", city: "Frankfurt"

JSON-Ausgabe:
{"current_position":"...","current_company":"...","work_history":[{"company":"...","position":"...","start_date":"...","end_date":"...","description":"• Task1\\n• Task2"}],"education":[{"institution":"...","degree":"...","field_of_study":"...","start_date":"...","end_date":"..."}],"further_education":[...],"it_skills":["..."],"languages":[{"language":"...","level":"..."}],"skills":["..."],"first_name":"...","last_name":"...","email":"...","phone":"...","birth_date":"...","estimated_age":null,"street_address":"...","postal_code":"...","city":"..."}"""


@dataclass
class ParseResult:
    """Ergebnis des CV-Parsings."""

    success: bool
    data: CVParseResult | None = None
    error: str | None = None
    raw_text: str | None = None
    tokens_used: int = 0


class CVParserService:
    """Service zum Parsen von CVs mit OpenAI.

    Workflow:
    1. PDF von URL herunterladen
    2. Text mit PyMuPDF extrahieren
    3. Text an OpenAI senden
    4. Strukturierte Daten zurückgeben
    """

    def __init__(self, db: AsyncSession, openai_api_key: str | None = None):
        """Initialisiert den CV-Parser.

        Args:
            db: Datenbank-Session
            openai_api_key: Optional API-Key (Standard: aus Settings)
        """
        self.db = db
        self.api_key = openai_api_key or settings.openai_api_key
        self._http_client: httpx.AsyncClient | None = None

        if not self.api_key:
            logger.warning("OpenAI API-Key nicht konfiguriert - CV-Parsing deaktiviert")

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Gibt den HTTP-Client zurück."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(float(limits.TIMEOUT_OPENAI)),
                follow_redirects=True,
            )
        return self._http_client

    async def close(self) -> None:
        """Schließt Ressourcen."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def download_cv(self, cv_url: str) -> bytes:
        """Lädt ein CV-PDF von einer URL herunter.

        Args:
            cv_url: URL zum PDF

        Returns:
            PDF als Bytes

        Raises:
            ValueError: Bei Download-Fehler
        """
        if not cv_url:
            raise ValueError("Keine CV-URL angegeben")

        client = await self._get_http_client()

        try:
            response = await client.get(cv_url)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "pdf" not in content_type.lower() and not cv_url.lower().endswith(".pdf"):
                logger.warning(f"CV ist möglicherweise kein PDF: {content_type}")

            return response.content

        except httpx.HTTPStatusError as e:
            raise ValueError(f"CV-Download fehlgeschlagen: HTTP {e.response.status_code}")
        except httpx.RequestError as e:
            raise ValueError(f"CV-Download fehlgeschlagen: {e}")

    def extract_text_from_pdf(self, pdf_bytes: bytes) -> str:
        """Extrahiert Text aus einem PDF.

        Args:
            pdf_bytes: PDF als Bytes

        Returns:
            Extrahierter Text

        Raises:
            ValueError: Bei Extraktions-Fehler
        """
        try:
            import fitz  # PyMuPDF
        except ImportError:
            raise ValueError("PyMuPDF (fitz) nicht installiert")

        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            text_parts = []

            for page in doc:
                text = page.get_text()
                if text:
                    text_parts.append(text)

            doc.close()

            full_text = "\n\n".join(text_parts)

            # Null-Bytes und andere Kontrollzeichen entfernen (PostgreSQL-kompatibel)
            import re
            full_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", full_text)

            if not full_text.strip():
                raise ValueError("PDF enthält keinen extrahierbaren Text")

            return full_text

        except Exception as e:
            if "PDF" in str(e) or "fitz" in str(e):
                raise ValueError(f"PDF-Verarbeitung fehlgeschlagen: {e}")
            raise

    def extract_images_from_pdf(self, pdf_bytes: bytes, max_pages: int = 3) -> list[bytes]:
        """Rendert PDF-Seiten als PNG-Bilder (für Vision-Fallback).

        Args:
            pdf_bytes: PDF als Bytes
            max_pages: Maximale Anzahl Seiten

        Returns:
            Liste von PNG-Bytes
        """
        try:
            import fitz  # PyMuPDF
        except ImportError:
            raise ValueError("PyMuPDF (fitz) nicht installiert")

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        images = []

        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            # Seite als Bild rendern (150 DPI für gute Qualität bei kleiner Größe)
            pix = page.get_pixmap(dpi=150)
            images.append(pix.tobytes("png"))

        doc.close()
        return images

    async def parse_cv_with_vision(self, pdf_images: list[bytes]) -> ParseResult:
        """Parst CV-Bilder mit OpenAI Vision (GPT-4o).

        Für PDFs die keinen extrahierbaren Text haben (Bild-PDFs, Canva, etc.)

        Args:
            pdf_images: Liste von PNG-Bytes (je Seite ein Bild)

        Returns:
            ParseResult mit strukturierten Daten
        """
        import base64

        if not self.api_key:
            return ParseResult(success=False, error="OpenAI API-Key nicht konfiguriert")

        if not pdf_images:
            return ParseResult(success=False, error="Keine Bilder zum Analysieren")

        # Bilder als base64 für OpenAI Vision
        image_contents = []
        for img_bytes in pdf_images:
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            image_contents.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{b64}",
                    "detail": "high",
                },
            })

        client = await self._get_http_client()

        try:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "system", "content": CV_PARSING_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Analysiere diesen Lebenslauf (als Bild) und extrahiere den VOLLSTÄNDIGEN beruflichen Werdegang mit ALLEN Stationen. WICHTIG: Nur Informationen extrahieren die im Bild sichtbar sind! NIEMALS Aufgaben oder Firmen erfinden!"},
                                *image_contents,
                            ],
                        },
                    ],
                    "temperature": 0.1,
                    "max_tokens": 4000,
                    "response_format": {"type": "json_object"},
                },
                timeout=90.0,  # Vision braucht länger
            )

            response.raise_for_status()
            result = response.json()

            tokens = result.get("usage", {}).get("total_tokens", 0)
            content = result["choices"][0]["message"]["content"]
            parsed_data = json.loads(content)
            cv_result = self._map_to_cv_result(parsed_data)

            logger.info(f"Vision CV-Parse erfolgreich: {tokens} Tokens")

            return ParseResult(
                success=True,
                data=cv_result,
                raw_text=f"[Vision-Parse: {len(pdf_images)} Seite(n)]",
                tokens_used=tokens,
            )

        except Exception as e:
            logger.error(f"Vision CV-Parse Fehler: {e}")
            return ParseResult(
                success=False,
                error=f"Vision-Parsing fehlgeschlagen: {str(e)}",
            )

    async def parse_cv_text(self, cv_text: str) -> ParseResult:
        """Parst CV-Text mit OpenAI.

        Args:
            cv_text: Extrahierter Text aus dem CV

        Returns:
            ParseResult mit strukturierten Daten
        """
        if not self.api_key:
            return ParseResult(
                success=False,
                error="OpenAI API-Key nicht konfiguriert",
                raw_text=cv_text,
            )

        if not cv_text or len(cv_text.strip()) < 50:
            return ParseResult(
                success=False,
                error="CV-Text zu kurz für Analyse",
                raw_text=cv_text,
            )

        # Text kürzen falls zu lang (ca. 15.000 Zeichen max)
        max_chars = 15000
        if len(cv_text) > max_chars:
            cv_text = cv_text[:max_chars] + "\n\n[... Text gekürzt ...]"

        client = await self._get_http_client()

        max_attempts = 3
        last_error = None

        for attempt in range(max_attempts):
            try:
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [
                            {"role": "system", "content": CV_PARSING_SYSTEM_PROMPT},
                            {"role": "user", "content": f"Analysiere diesen Lebenslauf und extrahiere den VOLLSTÄNDIGEN beruflichen Werdegang mit ALLEN Stationen. WICHTIG: Nur Informationen extrahieren die im Text stehen! NIEMALS Aufgaben oder Firmen erfinden!\n\n{cv_text}"},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 4000,  # Erhöht für vollständigen Werdegang
                        "response_format": {"type": "json_object"},
                    },
                    timeout=limits.TIMEOUT_OPENAI,
                )

                response.raise_for_status()
                result = response.json()

                # Token-Verbrauch
                tokens = result.get("usage", {}).get("total_tokens", 0)

                # Response parsen
                content = result["choices"][0]["message"]["content"]
                parsed_data = json.loads(content)

                # In Pydantic-Schema konvertieren
                cv_result = self._map_to_cv_result(parsed_data)

                return ParseResult(
                    success=True,
                    data=cv_result,
                    raw_text=cv_text,
                    tokens_used=tokens,
                )

            except httpx.TimeoutException:
                last_error = "OpenAI Timeout - bitte später erneut versuchen"
                if attempt < max_attempts - 1:
                    wait_time = 5 * (attempt + 1)  # 5s, 10s
                    logger.warning(
                        f"OpenAI Timeout (Versuch {attempt + 1}/{max_attempts}), "
                        f"Retry in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                    continue
                # Letzter Versuch fehlgeschlagen
                return ParseResult(
                    success=False,
                    error=last_error,
                    raw_text=cv_text,
                )

            except json.JSONDecodeError as e:
                logger.error(f"JSON-Parse-Fehler: {e}")
                return ParseResult(
                    success=False,
                    error="Ungültige Antwort von OpenAI",
                    raw_text=cv_text,
                )
            except Exception as e:
                logger.exception(f"CV-Parsing-Fehler: {e}")
                return ParseResult(
                    success=False,
                    error=f"Parsing-Fehler: {str(e)}",
                    raw_text=cv_text,
                )

        # Sollte nicht erreicht werden, aber sicherheitshalber
        return ParseResult(
            success=False,
            error=last_error or "Unbekannter Fehler nach allen Versuchen",
            raw_text=cv_text,
        )

    @staticmethod
    def _clean_null(value: str | None) -> str | None:
        """Konvertiert den String 'null' zu echtem None."""
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() == "null":
            return None
        return value

    @staticmethod
    def _normalize_case(value: str | None) -> str | None:
        """Normalisiert ALL CAPS zu Title Case. Laesst gemischte Schreibweise in Ruhe."""
        if not value or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return None
        # Nur konvertieren wenn KOMPLETT Grossbuchstaben (mindestens 2 Buchstaben)
        alpha_chars = [c for c in stripped if c.isalpha()]
        if len(alpha_chars) >= 2 and all(c.isupper() for c in alpha_chars):
            return stripped.title()
        return stripped

    def _map_to_cv_result(self, data: dict) -> CVParseResult:
        """Mappt OpenAI-Response auf CVParseResult."""
        work_history = None
        if data.get("work_history"):
            work_history = [
                WorkHistoryEntry(
                    company=self._clean_null(entry.get("company")),
                    position=self._normalize_case(self._clean_null(entry.get("position"))),
                    start_date=self._clean_null(entry.get("start_date")),
                    end_date=self._clean_null(entry.get("end_date")),
                    description=self._clean_null(entry.get("description")),
                )
                for entry in data["work_history"]
                if isinstance(entry, dict)
            ]

        education = None
        if data.get("education"):
            education = [
                EducationEntry(
                    institution=entry.get("institution"),
                    degree=entry.get("degree"),
                    field_of_study=entry.get("field_of_study"),
                    start_date=entry.get("start_date"),
                    end_date=entry.get("end_date"),
                )
                for entry in data["education"]
                if isinstance(entry, dict)
            ]

        further_education = None
        if data.get("further_education"):
            further_education = [
                EducationEntry(
                    institution=entry.get("institution"),
                    degree=entry.get("degree"),
                    field_of_study=entry.get("field_of_study"),
                    start_date=entry.get("start_date"),
                    end_date=entry.get("end_date"),
                )
                for entry in data["further_education"]
                if isinstance(entry, dict)
            ]

        # Sprachen parsen
        languages = None
        if data.get("languages"):
            languages = [
                LanguageEntry(
                    language=entry.get("language", ""),
                    level=entry.get("level"),
                )
                for entry in data["languages"]
                if isinstance(entry, dict) and entry.get("language")
            ]

        # estimated_age: int oder None
        estimated_age = data.get("estimated_age")
        if estimated_age is not None:
            try:
                estimated_age = int(estimated_age)
            except (ValueError, TypeError):
                estimated_age = None

        return CVParseResult(
            first_name=self._normalize_case(data.get("first_name")),
            last_name=self._normalize_case(data.get("last_name")),
            email=self._clean_null(data.get("email")),
            phone=self._clean_null(data.get("phone")),
            birth_date=data.get("birth_date"),
            estimated_age=estimated_age,
            street_address=self._clean_null(data.get("street_address")),
            postal_code=self._clean_null(data.get("postal_code")),
            city=self._clean_null(data.get("city")),
            current_position=self._normalize_case(data.get("current_position")),
            current_company=self._clean_null(data.get("current_company")),
            skills=data.get("skills"),
            languages=languages,
            it_skills=data.get("it_skills"),
            work_history=work_history,
            education=education,
            further_education=further_education,
        )

    async def parse_candidate_cv(self, candidate_id: UUID) -> tuple[Candidate, ParseResult]:
        """Parst das CV eines Kandidaten und aktualisiert die DB.

        Args:
            candidate_id: ID des Kandidaten

        Returns:
            Tuple (aktualisierter Kandidat, ParseResult)

        Raises:
            ValueError: Wenn Kandidat nicht gefunden oder kein CV vorhanden
        """
        # Kandidat laden
        result = await self.db.execute(
            select(Candidate).where(Candidate.id == candidate_id)
        )
        candidate = result.scalar_one_or_none()

        if not candidate:
            raise ValueError(f"Kandidat nicht gefunden: {candidate_id}")

        if not candidate.cv_url:
            raise ValueError(f"Kandidat hat keine CV-URL: {candidate_id}")

        # CV herunterladen
        try:
            pdf_bytes = await self.download_cv(candidate.cv_url)
        except ValueError as e:
            return candidate, ParseResult(success=False, error=str(e))

        # Text extrahieren
        cv_text = None
        try:
            cv_text = self.extract_text_from_pdf(pdf_bytes)
        except ValueError:
            pass  # Kein Text → Vision-Fallback

        # Parsen: Text oder Vision
        if cv_text and len(cv_text.strip()) >= 50:
            # Standard: Text-basiertes Parsing
            parse_result = await self.parse_cv_text(cv_text)
        else:
            # Fallback: Vision-Parsing (Bild-PDFs)
            logger.info(f"Vision-Fallback für Kandidat {candidate_id} (kein extrahierbarer Text)")
            try:
                pdf_images = self.extract_images_from_pdf(pdf_bytes)
                parse_result = await self.parse_cv_with_vision(pdf_images)
                cv_text = parse_result.raw_text or "[Vision-Parse]"
            except ValueError as e:
                return candidate, ParseResult(success=False, error=str(e))

        if parse_result.success and parse_result.data:
            # Kandidat aktualisieren
            await self._update_candidate_from_cv(candidate, parse_result.data, cv_text)
            await self.db.commit()

        return candidate, parse_result

    async def _update_candidate_from_cv(
        self,
        candidate: Candidate,
        cv_data: CVParseResult,
        cv_text: str,
    ) -> None:
        """Aktualisiert einen Kandidaten mit CV-Daten.

        Überschreibt nur, wenn CRM-Daten leer sind.
        """
        now = datetime.now(timezone.utc)

        # Persönliche Daten (überschreiben wenn leer ODER "Not Available")
        # CRM speichert oft first_name="Not", last_name="Available"
        _bad_names = {"not available", "unbekannt", "n/a", "na", "", "not", "available", "unknown", "none"}
        _full_name = f"{candidate.first_name or ''} {candidate.last_name or ''}".strip().lower()
        _is_bad_name = (
            _full_name in _bad_names
            or (candidate.first_name or "").strip().lower() in _bad_names
            or (candidate.last_name or "").strip().lower() in _bad_names
        )
        if cv_data.first_name and (not candidate.first_name or _is_bad_name):
            candidate.first_name = cv_data.first_name
        if cv_data.last_name and (not candidate.last_name or _is_bad_name):
            candidate.last_name = cv_data.last_name

        # Geburtsdatum parsen (immer aus CV übernehmen wenn vorhanden)
        if cv_data.birth_date:
            candidate.birth_date = self._parse_birth_date(cv_data.birth_date)

        # E-Mail und Telefon (aus CV uebernehmen wenn leer)
        if cv_data.email and not candidate.email:
            candidate.email = cv_data.email
        if cv_data.phone and not candidate.phone:
            candidate.phone = cv_data.phone

        # Adresse aus CV uebernehmen wenn leer
        if cv_data.street_address and not candidate.street_address:
            candidate.street_address = cv_data.street_address
        if cv_data.postal_code and not candidate.postal_code:
            candidate.postal_code = cv_data.postal_code
        if cv_data.city and not candidate.city:
            candidate.city = cv_data.city

        # Position (immer aus CV uebernehmen, ausser manuell bearbeitet)
        # Rohwert aus CV — wird spaeter durch verifizierte Rolle ueberschrieben (Schritt 4.5)
        manual = candidate.manual_overrides or {}
        if "current_position" not in manual and cv_data.current_position:
            candidate.current_position = cv_data.current_position

        # Aktuelles Unternehmen (IMMER aus CV uebernehmen — auch null/leer,
        # damit halluzinierte Werte beim Re-Parse korrigiert werden)
        if "current_company" not in manual:
            candidate.current_company = cv_data.current_company

        # Skills (erweitern, nicht überschreiben)
        if cv_data.skills:
            existing_skills = set(candidate.skills or [])
            new_skills = set(cv_data.skills)
            candidate.skills = list(existing_skills | new_skills)

        # Sprachen (immer ueberschreiben aus CV)
        if cv_data.languages:
            candidate.languages = [
                entry.model_dump() for entry in cv_data.languages
            ]

        # IT-Kenntnisse (erweitern, nicht ueberschreiben)
        if cv_data.it_skills:
            existing_it = set(candidate.it_skills or [])
            new_it = set(cv_data.it_skills)
            candidate.it_skills = list(existing_it | new_it)

        # Work History und Education (immer ueberschreiben aus CV)
        if cv_data.work_history:
            candidate.work_history = [
                entry.model_dump() for entry in cv_data.work_history
            ]
        if cv_data.education:
            candidate.education = [
                entry.model_dump() for entry in cv_data.education
            ]
        if cv_data.further_education:
            candidate.further_education = [
                entry.model_dump() for entry in cv_data.further_education
            ]

        # CV-Text und Timestamp (Null-Bytes entfernen für PostgreSQL)
        candidate.cv_text = cv_text.replace("\x00", "") if cv_text else cv_text
        candidate.cv_parsed_at = now
        candidate.updated_at = now

    def _parse_birth_date(self, date_str: str) -> date | None:
        """Parst ein Geburtsdatum aus verschiedenen Formaten."""
        if not date_str:
            return None

        # Verschiedene Formate versuchen
        formats = [
            "%d.%m.%Y",  # 15.03.1985
            "%d/%m/%Y",  # 15/03/1985
            "%Y-%m-%d",  # 1985-03-15
            "%d-%m-%Y",  # 15-03-1985
            "%Y",        # 1985 (nur Jahr)
        ]

        for fmt in formats:
            try:
                parsed = datetime.strptime(date_str.strip(), fmt)
                return parsed.date()
            except ValueError:
                continue

        # Regex für flexibleres Parsing
        match = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", date_str)
        if match:
            try:
                day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
                return date(year, month, day)
            except ValueError:
                pass

        logger.warning(f"Konnte Geburtsdatum nicht parsen: {date_str}")
        return None

    async def __aenter__(self) -> "CVParserService":
        """Context-Manager Entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context-Manager Exit."""
        await self.close()
