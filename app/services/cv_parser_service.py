"""CV Parser Service - Extrahiert strukturierte Daten aus CVs.

Dieser Service:
- Lädt PDFs von URLs herunter
- Extrahiert Text mit PyMuPDF
- Nutzt OpenAI für strukturierte Extraktion
- Aktualisiert Kandidaten in der DB
"""

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


# System-Prompt für CV-Parsing
CV_PARSING_SYSTEM_PROMPT = """Du bist ein erfahrener HR-Analyst, der Lebensläufe (CVs) analysiert.

Deine Aufgabe ist es, strukturierte Informationen aus dem CV-Text zu extrahieren.

WICHTIG:
- Extrahiere NUR Informationen, die EXPLIZIT im Text stehen
- Uebernimm Texte GENAU SO wie sie im Lebenslauf stehen (nicht umformulieren!)
- Wenn eine Information nicht vorhanden ist, setze den Wert auf null (JSON null, NICHT den String "null"!)
- Alle Texte auf DEUTSCH
- Datumsformate: "MM/YYYY" oder "YYYY" fuer Start/Ende
- Bei "heute" oder "aktuell" als Enddatum: "heute" verwenden

Extrahiere folgende Informationen:

1. Aktuelle Position (current_position):
   - Die AKTUELLE/LETZTE Berufsbezeichnung des Kandidaten
   - GENAU so uebernehmen wie im CV geschrieben

2. VOLLSTAENDIGER Beruflicher Werdegang (work_history):
   - Extrahiere ALLE beruflichen Stationen - KEINE auslassen!
   - Fuer JEDE Station: company, position, start_date, end_date, description
   - description: Welche Taetigkeiten/Aufgaben wurden ausgeubt? GENAU aus dem CV uebernehmen!
   - Wenn Aufzaehlungspunkte/Bulletpoints vorhanden: als kommaseparierten Text zusammenfassen
   - Wenn keine Taetigkeiten beschrieben: null (nicht den String "null"!)
   - Chronologisch sortiert (neueste zuerst)
   - Auch Praktika, Werkstudentenjobs, Nebenjobs erfassen
   - ACHTUNG: Folgendes gehoert NICHT in work_history sondern in education:
     * IHK-Pruefungen, IHK-Weiterbildungen (z.B. "Bilanzbuchhalter IHK")
     * Meisterschulen, Meisterbrief
     * Weiterbildungsinstitute (z.B. Steuerfachschule Endriss, DAA, TA Bildungszentrum)
     * Zertifizierungen, Lehrgaenge, Umschulungen
     * Alles was eine AUSBILDUNG/WEITERBILDUNG ist, auch wenn es im CV unter "Berufserfahrung" steht

3. VOLLSTAENDIGE Ausbildung & Qualifikationen (education):
   - ALLE Abschluesse: Schule, Ausbildung, Studium, Weiterbildungen, Zertifikate
   - Fuer JEDEN Eintrag: institution, degree, field_of_study, start_date, end_date
   - IHK-Pruefungen (z.B. "Bilanzbuchhalter IHK", "Fachwirt IHK") gehoeren IMMER hierher!
   - Meisterbrief, Fortbildungen, Schulungen, Lehrgaenge gehoeren IMMER hierher!
   - Auch wenn diese im CV unter "Berufserfahrung" stehen - sie gehoeren in education!
   - WICHTIG: Viele CVs haben einen SEPARATEN Abschnitt "Weiterbildungen" oder "Fortbildungen"
     oder "Zertifikate" oder "Seminare" - diese Eintraege gehoeren ALLE in education!
     Beispiele: Leadership-Seminare, SAP-Schulungen, IHK-Sachkundepruefungen,
     AdA-Schein (Ausbildung der Ausbilder), Sprachkurse, Softwareschulungen
   - Durchsuche den GESAMTEN CV nach Bildungs-/Weiterbildungseintraegen, nicht nur den Abschnitt "Bildung"!

4. IT-Kenntnisse (it_skills) - SEPARATES Feld:
   - NUR Software, Tools und technische Systeme
   - z.B. SAP, DATEV, Lexware, Microsoft Office, Excel, ERP-Systeme, CRM-Systeme
   - Programmiersprachen, Datenbanken, Betriebssysteme
   - KEINE allgemeinen Fachkenntnisse hier (die gehoeren in skills)

5. Sprachkenntnisse (languages) - SEPARATES Feld:
   - JEDE Sprache als eigenes Objekt mit language und level
   - Level GENAU so uebernehmen wie im CV (z.B. "B2", "Muttersprache", "Grundkenntnisse", "fliessend", "verhandlungssicher")
   - Wenn kein Level angegeben: level auf null setzen

6. Sonstige Skills/Kenntnisse (skills):
   - Fachliche Kenntnisse: Buchhaltung, Bilanzierung, Schweissen, etc.
   - KEINE IT-Kenntnisse hier (die gehoeren in it_skills)
   - KEINE Sprachen hier (die gehoeren in languages)
   - Fuehrerschein, Zertifikate etc.

7. Persoenliche Daten (falls im CV vorhanden):
   - first_name, last_name
   - birth_date (Format: "DD.MM.YYYY")
   - street_address, postal_code, city

Antworte NUR mit einem validen JSON-Objekt:
{
  "current_position": "Finanzbuchhalter",
  "work_history": [
    {
      "company": "ABC GmbH",
      "position": "Finanzbuchhalter",
      "start_date": "03/2020",
      "end_date": "heute",
      "description": "Debitoren-/Kreditorenbuchhaltung, Monatsabschluesse, Mahnwesen"
    }
  ],
  "education": [
    {
      "institution": "IHK Muenchen",
      "degree": "Bilanzbuchhalter (IHK)",
      "field_of_study": "Finanz- und Rechnungswesen",
      "start_date": "2018",
      "end_date": "2019"
    }
  ],
  "it_skills": ["SAP FI", "DATEV", "Microsoft Excel", "Lexware"],
  "languages": [
    {"language": "Deutsch", "level": "Muttersprache"},
    {"language": "Englisch", "level": "B2"},
    {"language": "Franzoesisch", "level": "Grundkenntnisse"}
  ],
  "skills": ["Buchhaltung", "Bilanzierung", "Steuererklarung", "Fuehrerschein Klasse B"],
  "first_name": "Max",
  "last_name": "Mustermann",
  "birth_date": "15.03.1990",
  "street_address": "Musterstrasse 123",
  "postal_code": "80331",
  "city": "Muenchen"
}"""


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
                timeout=httpx.Timeout(30.0),
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
                        {"role": "user", "content": f"Analysiere diesen Lebenslauf und extrahiere den VOLLSTÄNDIGEN beruflichen Werdegang mit ALLEN Stationen:\n\n{cv_text}"},
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
            return ParseResult(
                success=False,
                error="OpenAI Timeout - bitte später erneut versuchen",
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

    @staticmethod
    def _clean_null(value: str | None) -> str | None:
        """Konvertiert den String 'null' zu echtem None."""
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() == "null":
            return None
        return value

    def _map_to_cv_result(self, data: dict) -> CVParseResult:
        """Mappt OpenAI-Response auf CVParseResult."""
        work_history = None
        if data.get("work_history"):
            work_history = [
                WorkHistoryEntry(
                    company=self._clean_null(entry.get("company")),
                    position=self._clean_null(entry.get("position")),
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

        return CVParseResult(
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            birth_date=data.get("birth_date"),
            street_address=data.get("street_address"),
            postal_code=data.get("postal_code"),
            city=data.get("city"),
            current_position=data.get("current_position"),
            skills=data.get("skills"),
            languages=languages,
            it_skills=data.get("it_skills"),
            work_history=work_history,
            education=education,
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
        try:
            cv_text = self.extract_text_from_pdf(pdf_bytes)
        except ValueError as e:
            return candidate, ParseResult(success=False, error=str(e))

        # Mit OpenAI parsen
        parse_result = await self.parse_cv_text(cv_text)

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

        # Persönliche Daten (nur wenn nicht bereits vorhanden)
        if not candidate.first_name and cv_data.first_name:
            candidate.first_name = cv_data.first_name
        if not candidate.last_name and cv_data.last_name:
            candidate.last_name = cv_data.last_name

        # Geburtsdatum parsen
        if not candidate.birth_date and cv_data.birth_date:
            candidate.birth_date = self._parse_birth_date(cv_data.birth_date)

        # Adresse (nur wenn nicht bereits vorhanden)
        if not candidate.street_address and cv_data.street_address:
            candidate.street_address = cv_data.street_address
        if not candidate.postal_code and cv_data.postal_code:
            candidate.postal_code = cv_data.postal_code
        if not candidate.city and cv_data.city:
            candidate.city = cv_data.city

        # Position
        if not candidate.current_position and cv_data.current_position:
            candidate.current_position = cv_data.current_position

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
