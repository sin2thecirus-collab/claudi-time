"""CSV-Import Service für das Matching-Tool."""

import csv
import io
import logging
from datetime import datetime, timezone
from typing import BinaryIO
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ImportJob, Job
from app.models.import_job import ImportStatus
from app.services.categorization_service import CategorizationService
from app.services.company_service import CompanyService
from app.services.csv_validator import (
    CSVValidator,
    ValidationResult,
    calculate_content_hash,
)

logger = logging.getLogger(__name__)


class CSVImportService:
    """
    Service für den CSV-Import von Jobs.

    Workflow:
    1. Datei validieren (csv_validator)
    2. ImportJob erstellen (Tracking)
    3. Zeilen verarbeiten (Batch-Insert)
    4. Duplikate erkennen (content_hash)
    5. Status aktualisieren
    """

    def __init__(self, db: AsyncSession):
        """
        Initialisiert den Import-Service.

        Args:
            db: AsyncSession für Datenbankzugriff
        """
        self.db = db
        self.validator = CSVValidator()

    async def create_import_job(
        self,
        filename: str,
        content: bytes,
    ) -> ImportJob:
        """
        Erstellt einen Import-Job aus Bytes-Content.

        Schnelle Header-Pruefung (nur erste Zeile) und Zeilenzaehlung
        auf Byte-Ebene (kein volles Decode der Datei).

        Args:
            filename: Original-Dateiname
            content: CSV-Inhalt als Bytes

        Returns:
            ImportJob-Objekt (Status PENDING oder FAILED)
        """
        errors: list[dict] = []

        # Zeilenanzahl auf Byte-Ebene zaehlen (schnell, kein Decode)
        total_rows = content.count(b"\n")
        if total_rows > 0:
            total_rows -= 1  # Minus Header-Zeile
        if total_rows < 0:
            total_rows = 0

        # Nur die ersten 5000 Bytes dekodieren fuer Header-Check
        sample = content[:5000]
        encoding = self.validator.detect_encoding(sample)
        try:
            text_sample = sample.decode(encoding)
        except UnicodeDecodeError:
            encoding = "iso-8859-1"
            text_sample = sample.decode(encoding, errors="replace")

        delimiter = self.validator.detect_delimiter(text_sample)

        # Header pruefen (nur erste Zeile)
        first_line = text_sample.split("\n")[0] if text_sample else ""
        headers = [h.strip() for h in first_line.split(delimiter)]
        if not headers or not headers[0]:
            errors.append({
                "row": None, "column": None,
                "message": "CSV-Header fehlt oder ist leer", "value": None,
            })
        elif "Unternehmen" not in headers:
            errors.append({
                "row": None, "column": "Unternehmen",
                "message": "Pflicht-Spalte 'Unternehmen' fehlt im Header",
                "value": None,
            })

        # ImportJob erstellen
        import_job = ImportJob(
            filename=filename,
            total_rows=total_rows,
            status=ImportStatus.PENDING,
        )

        if errors:
            import_job.status = ImportStatus.FAILED
            import_job.error_message = "Validierung fehlgeschlagen"
            import_job.errors_detail = {
                "validation_errors": errors,
                "warnings": [],
            }

        self.db.add(import_job)
        await self.db.commit()
        await self.db.refresh(import_job)

        logger.info(
            f"Import-Job erstellt: {import_job.id}, "
            f"Datei: {filename}, "
            f"Zeilen: ~{total_rows}, "
            f"Status: {import_job.status}"
        )

        return import_job

    async def get_import_job(self, import_job_id: UUID) -> ImportJob | None:
        """Holt einen Import-Job aus der Datenbank."""
        return await self.db.get(ImportJob, import_job_id)

    async def validate_file(self, file: BinaryIO) -> ValidationResult:
        """
        Validiert eine CSV-Datei ohne Import.

        Args:
            file: Datei-Objekt (binär)

        Returns:
            ValidationResult mit allen Fehlern
        """
        return self.validator.validate(file)

    async def start_import(
        self,
        file: BinaryIO,
        filename: str,
    ) -> ImportJob:
        """
        Startet einen neuen Import-Job.

        Args:
            file: Datei-Objekt (binär)
            filename: Original-Dateiname

        Returns:
            ImportJob-Objekt
        """
        # Validieren
        validation = self.validator.validate(file)

        # ImportJob erstellen
        import_job = ImportJob(
            filename=filename,
            total_rows=validation.total_rows,
            status=ImportStatus.PENDING,
        )

        if not validation.is_valid:
            import_job.status = ImportStatus.FAILED
            import_job.error_message = "Validierung fehlgeschlagen"
            import_job.errors_detail = {
                "validation_errors": [
                    {
                        "row": e.row,
                        "column": e.column,
                        "message": e.message,
                        "value": e.value,
                    }
                    for e in validation.errors[:50]  # Max 50 Fehler speichern
                ],
                "warnings": validation.warnings,
            }

        self.db.add(import_job)
        await self.db.commit()
        await self.db.refresh(import_job)

        logger.info(
            f"Import-Job erstellt: {import_job.id}, "
            f"Datei: {filename}, "
            f"Zeilen: {validation.total_rows}, "
            f"Status: {import_job.status}"
        )

        return import_job

    async def process_import(self, import_job_id: UUID, content: bytes | None = None) -> ImportJob:
        """
        Verarbeitet einen Import-Job.

        Wenn content uebergeben wird, wird die CSV direkt verarbeitet.
        Ohne content wird nur der Status auf PROCESSING gesetzt.

        Args:
            import_job_id: ID des Import-Jobs
            content: Optional - CSV-Inhalt als Bytes

        Returns:
            Aktualisierter ImportJob
        """
        # ImportJob laden
        import_job = await self.db.get(ImportJob, import_job_id)
        if not import_job:
            raise ValueError(f"ImportJob {import_job_id} nicht gefunden")

        if import_job.status != ImportStatus.PENDING:
            logger.warning(f"ImportJob {import_job_id} ist nicht im Status PENDING")
            return import_job

        # Status auf PROCESSING setzen
        import_job.status = ImportStatus.PROCESSING
        import_job.started_at = datetime.now(timezone.utc)
        await self.db.commit()

        logger.info(f"Starte Import-Verarbeitung: {import_job_id}")

        # Wenn Content vorhanden, direkt verarbeiten
        if content:
            # Encoding und Delimiter schnell erkennen (ohne volle Validierung)
            encoding = self.validator.detect_encoding(content[:10000])  # Nur Sample
            try:
                text_sample = content[:5000].decode(encoding)
            except UnicodeDecodeError:
                encoding = "iso-8859-1"
                text_sample = content[:5000].decode(encoding, errors="replace")
            delimiter = self.validator.detect_delimiter(text_sample)

            logger.info(
                f"Import {import_job_id}: Encoding={encoding}, Delimiter='{delimiter}'"
            )

            import_job = await self.process_csv_content(
                import_job=import_job,
                content=content,
                encoding=encoding,
                delimiter=delimiter,
            )

        return import_job

    async def process_csv_content(
        self,
        import_job: ImportJob,
        content: bytes,
        encoding: str = "utf-8",
        delimiter: str = "\t",
    ) -> ImportJob:
        """
        Verarbeitet den CSV-Inhalt und importiert Jobs.

        Args:
            import_job: ImportJob-Objekt
            content: CSV-Inhalt als Bytes
            encoding: Datei-Encoding
            delimiter: Spalten-Trennzeichen

        Returns:
            Aktualisierter ImportJob
        """
        try:
            # Dekodieren
            logger.info(f"Dekodiere Content ({len(content)} bytes) mit {encoding}")
            text_content = content.decode(encoding)
            reader = csv.DictReader(io.StringIO(text_content), delimiter=delimiter)

            logger.info(f"CSV-Header: {reader.fieldnames}")

            processed = 0
            successful = 0
            failed = 0
            duplicates = 0
            blacklisted = 0
            errors_detail: list[dict] = []
            batch: list[Job] = []
            BATCH_SIZE = 500

            # Company Service + Cache fuer Performance
            company_service = CompanyService(self.db)
            company_cache: dict[str, object] = {}  # name -> Company | None

            # Bestehende content_hashes laden (für Duplikaterkennung)
            logger.info("Lade bestehende Content-Hashes...")
            existing_hashes = await self._get_existing_hashes()
            logger.info(f"{len(existing_hashes)} bestehende Hashes geladen")

            for row_num, row in enumerate(reader, start=2):
                processed += 1

                try:
                    # Pflichtfeld: Unternehmen muss vorhanden sein
                    company_name = row.get("Unternehmen", "").strip()
                    if not company_name:
                        failed += 1
                        if len(errors_detail) < 50:
                            errors_detail.append({
                                "row": row_num,
                                "message": "Pflichtfeld 'Unternehmen' ist leer",
                            })
                        continue

                    # Company lookup/create (mit Cache)
                    cache_key = company_name.lower()
                    if cache_key not in company_cache:
                        company = await company_service.get_or_create_by_name(
                            name=company_name,
                            street=self._get_field(row, "Straße", "Straße und Hausnummer"),
                            postal_code=self._get_field(row, "PLZ"),
                            city=self._get_field(row, "Stadt", "Ort"),
                            domain=self._get_field(row, "Internet", "Domain", "Website"),
                            employee_count=self._get_field(
                                row, "Unternehmensgröße", "Unternehmensgroesse",
                                "Mitarbeiter (MA) / Unternehmensgröße", "Mitarbeiter",
                            ),
                        )
                        company_cache[cache_key] = company
                    else:
                        company = company_cache[cache_key]

                    # Blacklisted → skip
                    if company is None:
                        blacklisted += 1
                        duplicates += 1  # Zaehlt als uebersprungen
                        continue

                    # Kontaktperson extrahieren (falls vorhanden)
                    ap_vorname = self._get_field(row, "Vorname - AP Firma")
                    ap_nachname = self._get_field(row, "Nachname - AP Firma")
                    if ap_vorname or ap_nachname:
                        try:
                            await company_service.get_or_create_contact(
                                company_id=company.id,
                                first_name=ap_vorname,
                                last_name=ap_nachname,
                                salutation=self._get_field(row, "Anrede - AP Firma"),
                                position=self._get_field(row, "Funktion - AP Firma"),
                                phone=self._get_field(row, "Telefon - AP Firma"),
                                email=self._get_field(row, "E-Mail - AP Firma"),
                            )
                        except Exception as e:
                            logger.debug(f"Kontakt-Extraktion fehlgeschlagen: {e}")

                    # Content-Hash berechnen
                    content_hash = calculate_content_hash(row)

                    # Duplikat prüfen
                    if content_hash in existing_hashes:
                        duplicates += 1
                        continue

                    # Job erstellen (mit company_id)
                    job = self._row_to_job(row, content_hash, company_id=company.id)

                    # Hotlist-Kategorisierung direkt nach Erstellung
                    self._categorize_job(job)

                    batch.append(job)
                    existing_hashes.add(content_hash)
                    successful += 1

                except Exception as e:
                    failed += 1
                    if len(errors_detail) < 50:
                        errors_detail.append({
                            "row": row_num,
                            "message": str(e),
                        })

                # Batch in DB schreiben alle BATCH_SIZE Zeilen
                if len(batch) >= BATCH_SIZE:
                    batch_ok = await self._flush_batch(batch, import_job, processed, successful, failed, duplicates, errors_detail)
                    if not batch_ok:
                        failed += len(batch)
                        successful -= len(batch)
                    batch = []
                    logger.info(
                        f"Import-Fortschritt: {processed}/{import_job.total_rows} "
                        f"({successful} OK, {duplicates} Duplikate, {failed} Fehler)"
                    )

            # Restliche Jobs in DB schreiben
            if batch:
                batch_ok = await self._flush_batch(batch, import_job, processed, successful, failed, duplicates, errors_detail)
                if not batch_ok:
                    failed += len(batch)
                    successful -= len(batch)

            # Finale Werte setzen
            import_job.processed_rows = processed
            import_job.successful_rows = successful
            import_job.failed_rows = failed + duplicates
            import_job.status = ImportStatus.COMPLETED
            import_job.completed_at = datetime.now(timezone.utc)

            if errors_detail:
                import_job.errors_detail = {"import_errors": errors_detail}
            if duplicates > 0:
                if not import_job.errors_detail:
                    import_job.errors_detail = {}
                import_job.errors_detail["duplicates_skipped"] = duplicates
            if blacklisted > 0:
                if not import_job.errors_detail:
                    import_job.errors_detail = {}
                import_job.errors_detail["blacklisted_skipped"] = blacklisted

            await self.db.commit()

            logger.info(
                f"Import abgeschlossen: {import_job.id}, "
                f"Verarbeitet: {processed}, "
                f"Erfolgreich: {successful}, "
                f"Fehlgeschlagen: {failed}"
            )

        except Exception as e:
            logger.error(f"Import fehlgeschlagen: {e}", exc_info=True)
            import_job.status = ImportStatus.FAILED
            import_job.error_message = str(e)
            import_job.completed_at = datetime.now(timezone.utc)
            await self.db.commit()

        return import_job

    async def cancel_import(self, import_job_id: UUID) -> ImportJob:
        """
        Bricht einen laufenden Import ab.

        Args:
            import_job_id: ID des Import-Jobs

        Returns:
            Aktualisierter ImportJob
        """
        import_job = await self.db.get(ImportJob, import_job_id)
        if not import_job:
            raise ValueError(f"ImportJob {import_job_id} nicht gefunden")

        if import_job.is_complete:
            logger.warning(f"ImportJob {import_job_id} ist bereits abgeschlossen")
            return import_job

        import_job.status = ImportStatus.CANCELLED
        import_job.completed_at = datetime.now(timezone.utc)
        await self.db.commit()

        logger.info(f"Import abgebrochen: {import_job_id}")
        return import_job

    async def get_import_status(self, import_job_id: UUID) -> ImportJob | None:
        """
        Holt den Status eines Import-Jobs.

        Args:
            import_job_id: ID des Import-Jobs

        Returns:
            ImportJob oder None
        """
        return await self.db.get(ImportJob, import_job_id)

    async def _flush_batch(
        self,
        batch: list[Job],
        import_job: ImportJob,
        processed: int,
        successful: int,
        failed: int,
        duplicates: int,
        errors_detail: list[dict],
    ) -> bool:
        """Schreibt einen Batch in die DB. Bei Fehler: Rollback + Warnung.

        Returns:
            True wenn Batch erfolgreich, False bei Fehler
        """
        try:
            self.db.add_all(batch)
            import_job.processed_rows = processed
            import_job.successful_rows = successful
            import_job.failed_rows = failed + duplicates
            await self.db.commit()
            return True
        except Exception as e:
            logger.error(f"Batch-Insert fehlgeschlagen ({len(batch)} Jobs): {e}")
            await self.db.rollback()
            # ImportJob neu laden nach Rollback (Session-State ist verloren)
            await self.db.refresh(import_job)
            if len(errors_detail) < 50:
                errors_detail.append({
                    "row": None,
                    "message": f"Batch mit {len(batch)} Jobs fehlgeschlagen: {str(e)[:200]}",
                })
            return False

    async def _get_existing_hashes(self) -> set[str]:
        """
        Lädt alle bestehenden content_hashes aus der Datenbank.

        Returns:
            Set der bestehenden Hashes
        """
        result = await self.db.execute(
            select(Job.content_hash).where(Job.content_hash.isnot(None))
        )
        return {row[0] for row in result.all()}

    def _categorize_job(self, job: Job) -> None:
        """Kategorisiert einen Job für die Hotlist (synchron, kein DB-Commit)."""
        try:
            cat_service = CategorizationService(self.db)
            result = cat_service.categorize_job(job)
            cat_service.apply_to_job(job, result)
            logger.debug(
                f"Job '{job.position}' kategorisiert: "
                f"{result.category} ({result.job_title}, {result.city})"
            )
        except Exception as e:
            logger.warning(f"Kategorisierung fehlgeschlagen für Job '{job.position}': {e}")

    def _get_field(self, row: dict[str, str], *keys: str, max_len: int = 0) -> str | None:
        """Liest ein Feld mit mehreren moeglichen Spaltennamen (Alias-Support).

        Args:
            row: CSV-Zeile als Dictionary
            *keys: Moegliche Spaltennamen (erster Treffer gewinnt)
            max_len: Maximale Laenge (0 = kein Limit)
        """
        for key in keys:
            value = row.get(key, "").strip()
            if value:
                if max_len and len(value) > max_len:
                    value = value[:max_len]
                return value
        return None

    def _row_to_job(self, row: dict[str, str], content_hash: str, company_id=None) -> Job:
        """
        Konvertiert eine CSV-Zeile in ein Job-Objekt.

        Unterstuetzt alternative Spaltennamen (z.B. 'Ort' statt 'Stadt',
        'Anzeigenlink' statt 'URL', etc.)

        Args:
            row: CSV-Zeile als Dictionary
            content_hash: Berechneter Content-Hash
            company_id: Optional - UUID der verknuepften Company

        Returns:
            Job-Objekt
        """
        # DB-Limits: company_name/position=VARCHAR(255), city/work_location_city/
        # employment_type/industry=VARCHAR(100), company_size=VARCHAR(100)
        return Job(
            company_name=row.get("Unternehmen", "").strip()[:255],
            company_id=company_id,
            position=(self._get_field(row, "Position", "Funktion - AP Firma")
                      or "Keine Position angegeben")[:255],
            street_address=self._get_field(row, "Straße", "Straße und Hausnummer", max_len=255),
            postal_code=self._get_field(row, "PLZ", max_len=10),
            city=self._get_field(row, "Stadt", "Ort", max_len=100),
            work_location_city=self._get_field(
                row, "Arbeitsort", "Einsatzort", "Ort", "Stadt", max_len=100,
            ),
            job_url=self._get_field(row, "URL", "Anzeigenlink", "Link", max_len=500),
            job_text=self._get_field(
                row, "Beschreibung", "Stellenbeschreibung", "Anzeigen-Text",
            ),
            employment_type=self._get_field(row, "Beschäftigungsart", "Art", max_len=100),
            industry=self._get_field(row, "Branche", max_len=100),
            company_size=self._get_field(
                row, "Unternehmensgröße", "Unternehmensgroesse",
                "Mitarbeiter (MA) / Unternehmensgröße", "Mitarbeiter",
                max_len=50,
            ),
            content_hash=content_hash,
        )


async def run_csv_import(
    db: AsyncSession,
    file: BinaryIO,
    filename: str,
) -> ImportJob:
    """
    Convenience-Funktion für den vollständigen Import.

    Args:
        db: AsyncSession
        file: Datei-Objekt
        filename: Dateiname

    Returns:
        ImportJob nach Abschluss
    """
    service = CSVImportService(db)

    # Validieren und ImportJob erstellen
    import_job = await service.start_import(file, filename)

    if import_job.status == ImportStatus.FAILED:
        return import_job

    # Import starten
    import_job = await service.process_import(import_job.id)

    # Datei zurücksetzen und Inhalt verarbeiten
    file.seek(0)
    content = file.read()

    # Encoding und Delimiter aus Validierung verwenden
    validation = service.validator.validate(file)
    file.seek(0)

    import_job = await service.process_csv_content(
        import_job=import_job,
        content=content,
        encoding=validation.encoding,
        delimiter=validation.delimiter,
    )

    return import_job
