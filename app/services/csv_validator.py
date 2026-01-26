"""CSV-Validator für das Matching-Tool."""

import csv
import hashlib
import io
import logging
from dataclasses import dataclass, field
from typing import BinaryIO

import chardet

from app.config import Limits

logger = logging.getLogger(__name__)


# Pflicht-Spalten im CSV (deutsche Bezeichnungen)
REQUIRED_COLUMNS = {"Unternehmen", "Position"}

# Alle erwarteten Spalten (für Validierung)
EXPECTED_COLUMNS = {
    "Unternehmen",
    "Position",
    "Straße",
    "PLZ",
    "Stadt",
    "Arbeitsort",
    "URL",
    "Beschreibung",
    "Beschäftigungsart",
    "Branche",
    "Unternehmensgröße",
}


@dataclass
class ValidationError:
    """Ein einzelner Validierungsfehler."""

    row: int | None  # Zeilennummer (None für Header-Fehler)
    column: str | None  # Spaltenname
    message: str  # Fehlermeldung
    value: str | None = None  # Ungültiger Wert


@dataclass
class ValidationResult:
    """Ergebnis der CSV-Validierung."""

    is_valid: bool
    total_rows: int
    encoding: str
    delimiter: str
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        """Anzahl der Fehler."""
        return len(self.errors)


class CSVValidator:
    """
    Validator für CSV-Dateien (Job-Import).

    Prüft:
    - Encoding (UTF-8, ISO-8859-1, etc.)
    - Delimiter (Tab-getrennt)
    - Pflicht-Spalten
    - Zeilenanzahl (max. 10.000)
    - Pflichtfelder pro Zeile
    """

    def __init__(self, max_errors: int = 100):
        """
        Initialisiert den Validator.

        Args:
            max_errors: Maximale Anzahl gesammelter Fehler
        """
        self.max_errors = max_errors

    def detect_encoding(self, content: bytes) -> str:
        """
        Erkennt das Encoding der Datei.

        Args:
            content: Dateiinhalt als Bytes

        Returns:
            Erkanntes Encoding (z.B. 'utf-8', 'iso-8859-1')
        """
        result = chardet.detect(content)
        encoding = result.get("encoding", "utf-8")

        # Fallback für unbekannte Encodings
        if not encoding:
            encoding = "utf-8"

        logger.debug(f"Erkanntes Encoding: {encoding} (Konfidenz: {result.get('confidence', 0):.2%})")
        return encoding

    def detect_delimiter(self, content: str, sample_lines: int = 5) -> str:
        """
        Erkennt den Delimiter.

        Args:
            content: Dateiinhalt als String
            sample_lines: Anzahl Zeilen für Analyse

        Returns:
            Erkannter Delimiter (Standard: Tab)
        """
        lines = content.split("\n")[:sample_lines]
        sample = "\n".join(lines)

        try:
            sniffer = csv.Sniffer()
            dialect = sniffer.sniff(sample, delimiters="\t,;|")
            return dialect.delimiter
        except csv.Error:
            # Fallback: Tab ist erwartet
            return "\t"

    def validate_header(
        self,
        header: list[str],
        errors: list[ValidationError],
    ) -> set[str]:
        """
        Validiert den Header.

        Args:
            header: Liste der Spaltennamen
            errors: Liste für Fehler

        Returns:
            Set der gefundenen Pflicht-Spalten
        """
        # Header-Spalten normalisieren (Leerzeichen entfernen)
        normalized_header = {col.strip() for col in header}

        # Pflicht-Spalten prüfen
        missing_required = REQUIRED_COLUMNS - normalized_header
        for col in missing_required:
            errors.append(
                ValidationError(
                    row=None,
                    column=col,
                    message=f"Pflicht-Spalte '{col}' fehlt im Header",
                )
            )

        # Unbekannte Spalten als Warnung (nicht als Fehler)
        unknown_columns = normalized_header - EXPECTED_COLUMNS
        if unknown_columns:
            logger.warning(f"Unbekannte Spalten im CSV: {unknown_columns}")

        return normalized_header & REQUIRED_COLUMNS

    def validate_row(
        self,
        row: dict[str, str],
        row_num: int,
        errors: list[ValidationError],
    ) -> bool:
        """
        Validiert eine einzelne Zeile.

        Args:
            row: Zeile als Dictionary
            row_num: Zeilennummer (1-basiert)
            errors: Liste für Fehler

        Returns:
            True wenn Zeile gültig
        """
        is_valid = True

        # Pflichtfelder prüfen
        for col in REQUIRED_COLUMNS:
            value = row.get(col, "").strip()
            if not value:
                if len(errors) < self.max_errors:
                    errors.append(
                        ValidationError(
                            row=row_num,
                            column=col,
                            message=f"Pflichtfeld '{col}' ist leer",
                            value=value,
                        )
                    )
                is_valid = False

        # PLZ validieren (falls vorhanden)
        plz = row.get("PLZ", "").strip()
        if plz and not self._is_valid_plz(plz):
            if len(errors) < self.max_errors:
                errors.append(
                    ValidationError(
                        row=row_num,
                        column="PLZ",
                        message="PLZ muss 5 Ziffern haben",
                        value=plz,
                    )
                )
            # Keine Blockierung - nur Warnung

        return is_valid

    def _is_valid_plz(self, plz: str) -> bool:
        """Prüft, ob eine PLZ gültig ist (5 Ziffern)."""
        return len(plz) == 5 and plz.isdigit()

    def validate(self, file: BinaryIO) -> ValidationResult:
        """
        Validiert eine CSV-Datei vollständig.

        Args:
            file: Datei-Objekt (binär)

        Returns:
            ValidationResult mit allen Fehlern und Warnungen
        """
        errors: list[ValidationError] = []
        warnings: list[str] = []

        # Datei lesen
        content_bytes = file.read()
        file.seek(0)  # Zurücksetzen für spätere Verwendung

        # Dateigröße prüfen
        file_size_mb = len(content_bytes) / (1024 * 1024)
        if file_size_mb > Limits.CSV_MAX_FILE_SIZE_MB:
            errors.append(
                ValidationError(
                    row=None,
                    column=None,
                    message=f"Datei zu groß: {file_size_mb:.1f} MB (max. {Limits.CSV_MAX_FILE_SIZE_MB} MB)",
                )
            )
            return ValidationResult(
                is_valid=False,
                total_rows=0,
                encoding="unknown",
                delimiter="unknown",
                errors=errors,
                warnings=warnings,
            )

        # Encoding erkennen
        encoding = self.detect_encoding(content_bytes)

        try:
            content = content_bytes.decode(encoding)
        except UnicodeDecodeError:
            # Fallback auf ISO-8859-1 (Latin-1)
            encoding = "iso-8859-1"
            content = content_bytes.decode(encoding, errors="replace")
            warnings.append(f"Encoding-Probleme erkannt, verwende {encoding}")

        # Delimiter erkennen
        delimiter = self.detect_delimiter(content)

        if delimiter != "\t":
            warnings.append(f"Erwarteter Delimiter: Tab, gefunden: '{delimiter}'")

        # CSV parsen
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)

        # Header validieren
        if not reader.fieldnames:
            errors.append(
                ValidationError(
                    row=None,
                    column=None,
                    message="CSV-Header fehlt oder ist leer",
                )
            )
            return ValidationResult(
                is_valid=False,
                total_rows=0,
                encoding=encoding,
                delimiter=delimiter,
                errors=errors,
                warnings=warnings,
            )

        found_required = self.validate_header(list(reader.fieldnames), errors)

        # Wenn Pflicht-Spalten fehlen, abbrechen
        if found_required != REQUIRED_COLUMNS:
            return ValidationResult(
                is_valid=False,
                total_rows=0,
                encoding=encoding,
                delimiter=delimiter,
                errors=errors,
                warnings=warnings,
            )

        # Zeilen validieren
        total_rows = 0
        valid_rows = 0

        for row_num, row in enumerate(reader, start=2):  # Header ist Zeile 1
            total_rows += 1

            # Zeilenlimit prüfen
            if total_rows > Limits.CSV_MAX_ROWS:
                errors.append(
                    ValidationError(
                        row=row_num,
                        column=None,
                        message=f"Zeilenlimit überschritten (max. {Limits.CSV_MAX_ROWS:,} Zeilen)",
                    )
                )
                break

            if self.validate_row(row, row_num, errors):
                valid_rows += 1

            # Fehler-Limit erreicht?
            if len(errors) >= self.max_errors:
                warnings.append(
                    f"Validierung nach {self.max_errors} Fehlern abgebrochen"
                )
                break

        is_valid = len(errors) == 0

        logger.info(
            f"CSV-Validierung: {total_rows} Zeilen, {valid_rows} gültig, "
            f"{len(errors)} Fehler, {len(warnings)} Warnungen"
        )

        return ValidationResult(
            is_valid=is_valid,
            total_rows=total_rows,
            encoding=encoding,
            delimiter=delimiter,
            errors=errors,
            warnings=warnings,
        )


def calculate_content_hash(row: dict[str, str]) -> str:
    """
    Berechnet einen Hash für Duplikaterkennung.

    Basiert auf: Unternehmen + Position + Stadt + PLZ

    Args:
        row: Zeile als Dictionary

    Returns:
        SHA-256 Hash (64 Zeichen)
    """
    components = [
        row.get("Unternehmen", "").strip().lower(),
        row.get("Position", "").strip().lower(),
        row.get("Stadt", "").strip().lower(),
        row.get("PLZ", "").strip(),
    ]
    content = "|".join(components)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
