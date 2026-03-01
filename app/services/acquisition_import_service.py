"""AcquisitionImportService — CSV-Import von advertsdata.com.

Parst Tab-getrennte CSV, erstellt Jobs mit acquisition_source="advertsdata",
verwaltet Duplikate via anzeigen_id, berechnet Prioritaet.
"""

import csv
import hashlib
import io
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.company_contact import CompanyContact
from app.models.job import Job
from app.services.company_service import CompanyService

logger = logging.getLogger(__name__)

# Batch-Size fuer Railway (30s idle-in-transaction Timeout)
BATCH_SIZE = 50

# Re-Import-Schutz: Diese Status werden bei Duplikat NICHT zurueckgesetzt
PROTECTED_STATUSES = {
    "blacklist_hart",   # Nie wieder → absolut geschuetzt
    "blacklist_weich",  # Nur last_seen_at auffrischen
    "qualifiziert",     # In Bearbeitung → nicht zuruecksetzen
    "stelle_erstellt",  # Bereits konvertiert
}

# Status die bei Re-Import auf "neu" zurueckgesetzt werden duerfen
RESETTABLE_STATUSES = {"verloren", "followup_abgeschlossen"}

# Zeitfenster: Wenn anzeigen_id >90 Tage nicht gesehen → neuer Import
STALE_DAYS = 90

# CSV-Spalten-Mapping (advertsdata.com Tab-getrennt)
# Manche Spalten haben verschiedene Namen je nach Export-Variante
COL_MAPPING = {
    "company_name": "Unternehmen",
    "street": "Straße und Hausnummer",
    "plz": "PLZ",
    "city": "Ort",
    "domain": "Internet",
    "industry": "Branche",
    "company_size": "Mitarbeiter (MA) / Unternehmensgröße",
    "position": "Position",
    "job_url": "Anzeigenlink",
    "einsatzort": "Einsatzort",
    "job_text": "Anzeigen-Text",
    "employment_type": "Beschäftigungsart",
    "company_phone": "Telefon",
    "company_email": "E-Mail",
    "anzeigenart": "Anzeigenart",
    "ap_firma_salutation": "Anrede - AP Firma",
    "ap_firma_first_name": "Vorname - AP Firma",
    "ap_firma_last_name": "Nachname - AP Firma",
    "ap_firma_function": "Funktion - AP Firma",
    "ap_firma_phone": "Telefon - AP Firma",
    "ap_firma_email": "E-Mail - AP Firma",
    # Optionale Spalten (nur in 29-Spalten-Exporten vorhanden)
    "position_id_col": "Position-ID",
    "anzeigen_id_col": "Anzeigen-ID",
    # Optionale AP-Anzeige-Spalten (nicht in allen CSVs vorhanden)
    "ap_anzeige_salutation": "Anrede - AP Anzeige",
    "ap_anzeige_title": "Titel - AP Anzeige",
    "ap_anzeige_first_name": "Vorname - AP Anzeige",
    "ap_anzeige_last_name": "Nachname - AP Anzeige",
    "ap_anzeige_function": "Funktion - AP Anzeige",
    "ap_anzeige_phone": "Telefon - AP Anzeige",
    "ap_anzeige_email": "E-Mail - AP Anzeige",
}

# Alternative Spaltennamen (verschiedene advertsdata-Export-Varianten)
COL_ALTERNATIVES = {
    "company_phone": ["Firma Telefonnummer", "Telefon"],
}

# Branche-Keywords fuer Priority
FINANCE_KEYWORDS = [
    "steuer", "wirtschaftspruef", "buchfuehr", "buchhal", "finanz",
    "rechnungswesen", "controlling", "audit", "treuhand", "bank",
]
INDUSTRY_FIBU_KEYWORDS = [
    "industrie", "produktion", "fertigung", "maschinenbau", "automobil",
    "chemie", "pharma", "logistik", "handel",
]

# Senioritaet-Keywords fuer Priority
SENIOR_KEYWORDS = ["leiter", "head", "senior", "director", "teamleit", "abteilungsleit", "lead"]
JUNIOR_KEYWORDS = ["helfer", "praktikant", "werkstudent", "azubi", "auszubildend", "junior"]

# Geschlechts-Suffixe im Jobtitel
_GENDER_PATTERN = re.compile(r"\([mwfd/:]+\)", re.IGNORECASE)


def _extract_position_from_text(anzeigen_text: str | None) -> str | None:
    """Extrahiert den Jobtitel aus dem Anzeigen-Text.

    advertsdata CSVs haben oft keine separate "Position"-Spalte.
    Der Jobtitel steht am Anfang des Anzeigen-Texts, z.B.:
    "Senior Accountant / Buchhalter Hauptbuch (m/w/d) Einleitung Die Vp GmbH..."
    """
    if not anzeigen_text or not anzeigen_text.strip():
        return None

    text = anzeigen_text.strip()

    # LinkedIn-Format: Titel steht nach Trennlinie oft in der URL
    if text.startswith("---"):
        # Suche nach bekannten Jobtiteln im Text
        match = re.search(
            r"(?:als|position[:\s]|stelle[:\s])\s*([A-ZÄÖÜ][^\n(]{5,60}?\s*\([mwfd/:]+\))",
            text, re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()
        # Fallback: aus URL extrahieren
        url_match = re.search(r"jobs/view/([a-z0-9-]+?)(?:-at-|-\d)", text)
        if url_match:
            title = url_match.group(1).replace("-", " ").title()
            return title[:100]

    # Standard: Jobtitel bis (m/w/d) oder aehnliche Geschlechtsangabe
    gender_match = _GENDER_PATTERN.search(text[:200])
    if gender_match:
        title = text[:gender_match.end()].strip()
        # Muss kurz genug sein um ein Titel zu sein
        if len(title) <= 120:
            return title

    # XING-Format: "Bilanzbuchhalter:in Branche Stadt Vollzeit..."
    # Nimm den ersten Satz/Abschnitt bis zum ersten bekannten Keyword
    for marker in [
        "Informationsdienste", "Art der Beschäftigung", "Vollzeit",
        "Teilzeit", "Einleitung", "Top Job", "Über uns", "Ihre Aufgaben",
        "Wir suchen", "Deine Aufgaben",
    ]:
        idx = text.find(marker)
        if 5 < idx < 100:
            candidate = text[:idx].strip().rstrip(":;,. ")
            if len(candidate) >= 5:
                return candidate[:120]

    # Letzer Fallback: erste Zeile (max 120 Zeichen)
    first_line = text.split("\n")[0].strip()
    if len(first_line) <= 120:
        return first_line
    return first_line[:120].rsplit(" ", 1)[0]


def _normalize_phone(raw: str | None) -> str | None:
    """Normalisiert Telefonnummer in E.164 Format (+49...)."""
    if not raw or not raw.strip():
        return None
    phone = re.sub(r"[^0-9+]", "", raw.strip())
    if not phone:
        return None
    # Deutsche Nummern: 0xxx -> +49xxx
    if phone.startswith("0") and not phone.startswith("00"):
        phone = "+49" + phone[1:]
    elif phone.startswith("00"):
        phone = "+" + phone[2:]
    elif not phone.startswith("+"):
        phone = "+49" + phone
    # Validierung: mindestens 10 Ziffern
    digits = re.sub(r"[^0-9]", "", phone)
    if len(digits) < 10:
        return None
    return phone[:20]  # Max 20 Zeichen (DB-Feld)


def _extract_anzeigen_id(url: str | None) -> str | None:
    """Extrahiert die Anzeigen-ID aus der advertsdata URL."""
    if not url or not url.strip():
        return None
    try:
        parsed = urlparse(url.strip())
        params = parse_qs(parsed.query)
        if "id" in params:
            return params["id"][0][:50]  # Max 50 Zeichen
    except Exception:
        pass
    # Fallback: Hash der URL
    return hashlib.md5(url.strip().encode()).hexdigest()[:50]


def _parse_company_size(raw: str | None) -> int | None:
    """Extrahiert ungefaehre Mitarbeiterzahl aus String."""
    if not raw:
        return None
    raw_lower = raw.lower()
    # Versuche Zahlen zu extrahieren
    numbers = re.findall(r"\d+", raw)
    if numbers:
        # Nimm den hoechsten Wert als Obergrenze
        return max(int(n) for n in numbers)
    # Text-basiert
    if "klein" in raw_lower:
        return 25
    if "mittel" in raw_lower:
        return 250
    if "groß" in raw_lower or "gross" in raw_lower:
        return 1000
    return None


def _calculate_priority(
    row: dict[str, str],
    company_exists: bool,
    ap_has_phone: bool,
    ap_has_name: bool,
) -> int:
    """Berechnet Akquise-Prioritaet (0-10, hoeher = besser).

    P = Branche(0-2) + AP(0-2) + Groesse(0-2) + Alter(0-1) + Senioritaet(0-2) + Bekannt(0-1)
    """
    score = 0
    position = (row.get(COL_MAPPING["position"]) or "").lower()
    industry = (row.get(COL_MAPPING["industry"]) or "").lower()
    job_text = (row.get(COL_MAPPING["job_text"]) or "").lower()
    combined = position + " " + job_text

    # Branche (0-2)
    if any(kw in industry for kw in FINANCE_KEYWORDS) or any(kw in combined for kw in FINANCE_KEYWORDS):
        score += 2
    elif any(kw in industry for kw in INDUSTRY_FIBU_KEYWORDS):
        score += 1

    # AP vorhanden (0-2)
    if ap_has_phone and ap_has_name:
        score += 2
    elif ap_has_name:
        score += 1

    # Firmengroesse (0-2)
    size = _parse_company_size(row.get(COL_MAPPING["company_size"]))
    if size is not None:
        if 50 <= size <= 500:
            score += 2
        elif size > 500:
            score += 1

    # Anzeigen-Alter (0-1): Immer 1 bei neuem Import
    score += 1

    # Senioritaet (0-2)
    if any(kw in position for kw in SENIOR_KEYWORDS):
        score += 2
    elif any(kw in position for kw in JUNIOR_KEYWORDS):
        score += 0
    elif position:
        score += 1  # Normaler Buchhalter

    # Bekannte Firma (0-1)
    if company_exists:
        score += 1

    return min(score, 10)


def _get_field(row: dict[str, str], *keys: str) -> str | None:
    """Holt den ersten nicht-leeren Wert aus der Zeile."""
    for key in keys:
        if not key:
            continue
        val = row.get(key, "").strip()
        if val:
            return val
    return None


def _get_mapped_field(row: dict[str, str], field_name: str) -> str | None:
    """Holt Feld mit Fallback auf alternative Spaltennamen."""
    # Primaerer Spaltenname
    primary = COL_MAPPING.get(field_name, "")
    val = row.get(primary, "").strip() if primary else ""
    if val:
        return val
    # Alternative Spaltennamen
    for alt in COL_ALTERNATIVES.get(field_name, []):
        val = row.get(alt, "").strip()
        if val:
            return val
    return None


# Maximale Feldlaengen (aus den SQLAlchemy Models)
_FIELD_LIMITS = {
    "company_name": 255,
    "position": 255,
    "street_address": 255,
    "postal_code": 10,
    "city": 100,
    "work_location_city": 100,
    "job_url": 500,
    "employment_type": 100,
    "industry": 100,
    "company_size": 50,
    "position_id": 50,
    "anzeigen_id": 50,
    "domain": 255,
    "phone": 50,
}


def _trunc(value: str | None, field: str) -> str | None:
    """Kuerzt Wert auf die maximale DB-Feldlaenge."""
    if not value:
        return value
    limit = _FIELD_LIMITS.get(field, 255)
    return value[:limit] if len(value) > limit else value


class AcquisitionImportService:
    """Importiert Akquise-CSVs von advertsdata.com."""

    def __init__(self, db: AsyncSession, progress: dict | None = None):
        self.db = db
        self.company_service = CompanyService(db)
        self._progress = progress  # Optionales Status-Dict fuer Live-Fortschritt

    def _report(self, stats: dict, current_row: int = 0) -> None:
        """Aktualisiert das Progress-Dict fuer Live-Polling."""
        if self._progress is None:
            return
        self._progress["current_row"] = current_row
        self._progress["imported"] = stats["imported"]
        self._progress["duplicates_refreshed"] = stats["duplicates_refreshed"]
        self._progress["blacklisted_skipped"] = stats["blacklisted_skipped"]
        self._progress["errors"] = stats["errors"]

    async def import_csv(
        self,
        content: bytes,
        filename: str = "vakanzen.csv",
    ) -> dict:
        """Importiert CSV und gibt Statistiken zurueck.

        Returns:
            {
                "batch_id": UUID,
                "total_rows": int,
                "imported": int,
                "duplicates_refreshed": int,
                "blacklisted_skipped": int,
                "errors": int,
                "error_details": list[str],
            }
        """
        batch_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        # Encoding-Detection
        text_content = self._decode_csv(content)
        reader = csv.DictReader(io.StringIO(text_content), delimiter="\t")

        # Zeilen vorab zaehlen fuer Fortschrittsanzeige
        all_rows = list(reader)
        total_rows = len(all_rows)
        if self._progress is not None:
            self._progress["total_rows"] = total_rows

        # Bestehende anzeigen_ids laden (fuer Duplikat-Check)
        existing_jobs = await self._load_existing_anzeigen_ids()

        # Company-Cache fuer Performance
        company_cache: dict[str, Company | None] = {}

        stats = {
            "batch_id": str(batch_id),
            "total_rows": total_rows,
            "imported": 0,
            "duplicates_refreshed": 0,
            "blacklisted_skipped": 0,
            "protected_skipped": 0,
            "errors": 0,
            "error_details": [],
        }

        for row_num, row in enumerate(all_rows, start=2):
            self._report(stats, current_row=row_num - 1)

            try:
                result = await self._process_row(
                    row, row_num, batch_id, now, existing_jobs, company_cache,
                )

                if result == "imported":
                    stats["imported"] += 1
                elif result == "refreshed":
                    stats["duplicates_refreshed"] += 1
                elif result == "blacklisted":
                    stats["blacklisted_skipped"] += 1
                elif result == "protected":
                    stats["protected_skipped"] += 1

            except Exception as e:
                stats["errors"] += 1
                if len(stats["error_details"]) < 50:
                    stats["error_details"].append(f"Zeile {row_num}: {str(e)[:200]}")
                logger.warning(f"Import-Fehler Zeile {row_num}: {e}")
                # Session-Rollback nach DB-Fehler (z.B. Truncation)
                # damit die naechsten Zeilen nicht auch scheitern
                try:
                    await self.db.rollback()
                except Exception:
                    pass

            # Batch-Commit alle BATCH_SIZE Zeilen
            if stats["imported"] > 0 and stats["imported"] % BATCH_SIZE == 0:
                await self.db.commit()

        # Letzter Batch
        await self.db.commit()

        # Finaler Fortschritt
        self._report(stats, current_row=total_rows)

        logger.info(
            f"Akquise-Import abgeschlossen: {stats['imported']} importiert, "
            f"{stats['duplicates_refreshed']} aufgefrischt, "
            f"{stats['blacklisted_skipped']} Blacklist-Skip, "
            f"{stats['errors']} Fehler"
        )

        return stats

    async def _process_row(
        self,
        row: dict[str, str],
        row_num: int,
        batch_id: uuid.UUID,
        now: datetime,
        existing_jobs: dict[str, dict],
        company_cache: dict[str, Company | None],
    ) -> str:
        """Verarbeitet eine CSV-Zeile. Gibt Status zurueck."""
        company_name = _get_field(row, COL_MAPPING["company_name"])
        if not company_name:
            raise ValueError("Kein Firmenname")

        # Position: Erst aus "Position"-Spalte, dann aus Anzeigen-Text extrahieren
        position = _get_field(row, COL_MAPPING["position"])
        if not position:
            job_text_raw = _get_field(row, COL_MAPPING["job_text"])
            position = _extract_position_from_text(job_text_raw)
        if not position:
            raise ValueError("Keine Position")

        # Anzeigen-ID: direkt aus Spalte oder aus URL extrahieren
        job_url = _get_field(row, COL_MAPPING["job_url"])
        anzeigen_id = _get_field(row, COL_MAPPING.get("anzeigen_id_col", "")) or _extract_anzeigen_id(job_url)
        position_id = _get_field(row, COL_MAPPING.get("position_id_col", ""))

        # ── Duplikat-Check via anzeigen_id ──
        if anzeigen_id and anzeigen_id in existing_jobs:
            existing = existing_jobs[anzeigen_id]
            status = existing.get("akquise_status")

            # Blacklist-hart: komplett skippen
            if status == "blacklist_hart":
                return "blacklisted"

            # Geschuetzte Status: nur last_seen_at auffrischen
            if status in PROTECTED_STATUSES:
                await self.db.execute(
                    update(Job)
                    .where(Job.id == existing["id"])
                    .values(last_seen_at=now)
                )
                return "protected"

            # Stale-Check: >90 Tage nicht gesehen → neuer Import
            last_seen = existing.get("last_seen_at")
            if last_seen and (now - last_seen).days > STALE_DAYS:
                # Als neuen Job importieren (anzeigen_id wird neu vergeben)
                anzeigen_id = f"{anzeigen_id}_reimport_{now.strftime('%Y%m%d')}"
            else:
                # Duplikat auffrischen
                update_values = {
                    "last_seen_at": now,
                    "expires_at": now + timedelta(days=30),
                }
                # Resettable Status → auf "neu" zuruecksetzen
                if status in RESETTABLE_STATUSES:
                    update_values["akquise_status"] = "neu"
                    update_values["akquise_status_changed_at"] = now

                await self.db.execute(
                    update(Job)
                    .where(Job.id == existing["id"])
                    .values(**update_values)
                )
                return "refreshed"

        # ── Company get_or_create ──
        city = _get_field(row, COL_MAPPING["city"])
        cache_key = f"{company_name.lower()}_{(city or '').lower()}"

        if cache_key not in company_cache:
            company = await self.company_service.get_or_create_by_name(
                name=_trunc(company_name, "company_name"),
                address=_trunc(_get_field(row, COL_MAPPING["street"]), "street_address"),
                city=_trunc(city, "city"),
                phone=_trunc(_get_mapped_field(row, "company_phone"), "phone"),
                domain=_trunc(_get_field(row, COL_MAPPING["domain"]), "domain"),
                employee_count=_trunc(_get_field(row, COL_MAPPING["company_size"]), "company_size"),
                industry=_trunc(_get_field(row, COL_MAPPING["industry"]), "industry"),
            )
            company_cache[cache_key] = company
        else:
            company = company_cache[cache_key]

        if company is None:
            return "blacklisted"

        # Defense-in-Depth: Firma mit acquisition_status blacklist → skippen
        if company.acquisition_status == "blacklist":
            return "blacklisted"

        # Pruefen ob Company vorher schon Jobs hatte (fuer Priority)
        # Nicht company.jobs nutzen (Lazy-Load crasht im Async-Kontext)
        # Stattdessen: Firma war schon in DB wenn sie im Cache war
        company_has_jobs = cache_key in company_cache

        # ── Contact AP Firma ──
        ap_firma_first = _get_field(row, COL_MAPPING["ap_firma_first_name"])
        ap_firma_last = _get_field(row, COL_MAPPING["ap_firma_last_name"])
        ap_firma_phone = _get_field(row, COL_MAPPING["ap_firma_phone"])
        ap_firma_contact = None

        if ap_firma_first or ap_firma_last:
            ap_firma_contact = await self.company_service.get_or_create_contact(
                company_id=company.id,
                first_name=ap_firma_first,
                last_name=ap_firma_last,
                salutation=_get_field(row, COL_MAPPING["ap_firma_salutation"]),
                position=_get_field(row, COL_MAPPING["ap_firma_function"]),
                phone=ap_firma_phone,
                email=_get_field(row, COL_MAPPING["ap_firma_email"]),
            )
            # Phone normalisieren und auf Contact speichern
            if ap_firma_contact and ap_firma_phone:
                normalized = _normalize_phone(ap_firma_phone)
                if normalized and not ap_firma_contact.phone_normalized:
                    ap_firma_contact.phone_normalized = normalized
                    ap_firma_contact.source = "advertsdata"
                    ap_firma_contact.contact_role = "firma"

        # ── Contact AP Anzeige (optional) ──
        ap_anzeige_first = _get_field(row, COL_MAPPING.get("ap_anzeige_first_name", ""))
        ap_anzeige_last = _get_field(row, COL_MAPPING.get("ap_anzeige_last_name", ""))
        ap_anzeige_phone = _get_field(row, COL_MAPPING.get("ap_anzeige_phone", ""))

        if ap_anzeige_first or ap_anzeige_last:
            ap_anzeige_contact = await self.company_service.get_or_create_contact(
                company_id=company.id,
                first_name=ap_anzeige_first,
                last_name=ap_anzeige_last,
                salutation=_get_field(row, COL_MAPPING.get("ap_anzeige_salutation", "")),
                position=_get_field(row, COL_MAPPING.get("ap_anzeige_function", "")),
                phone=ap_anzeige_phone,
                email=_get_field(row, COL_MAPPING.get("ap_anzeige_email", "")),
            )
            if ap_anzeige_contact and ap_anzeige_phone:
                normalized = _normalize_phone(ap_anzeige_phone)
                if normalized and not ap_anzeige_contact.phone_normalized:
                    ap_anzeige_contact.phone_normalized = normalized
                    ap_anzeige_contact.source = "advertsdata"
                    ap_anzeige_contact.contact_role = "anzeige"

        # ── Priority berechnen ──
        priority = _calculate_priority(
            row,
            company_exists=company_has_jobs,
            ap_has_phone=bool(ap_firma_phone),
            ap_has_name=bool(ap_firma_first or ap_firma_last),
        )

        # ── Job erstellen ──
        einsatzort = _get_field(row, COL_MAPPING["einsatzort"])
        # Einsatzort aufteilen: "18055 Rostock Mecklenburg-Vorpommern" → PLZ + Stadt
        einsatz_plz = None
        einsatz_city = einsatzort
        if einsatzort:
            plz_match = re.match(r"^(\d{5})\s+(.+?)(?:\s+\w+-\w+)?$", einsatzort)
            if plz_match:
                einsatz_plz = plz_match.group(1)
                einsatz_city = plz_match.group(2).strip()

        job = Job(
            company_name=_trunc(company_name, "company_name"),
            company_id=company.id,
            position=_trunc(position, "position"),
            street_address=_trunc(_get_field(row, COL_MAPPING["street"]), "street_address"),
            postal_code=_trunc(einsatz_plz or _get_field(row, COL_MAPPING["plz"]), "postal_code"),
            city=_trunc(einsatz_city or city, "city"),
            work_location_city=_trunc(einsatz_city, "city"),
            job_url=_trunc(job_url, "job_url"),
            job_text=_get_field(row, COL_MAPPING["job_text"]),
            employment_type=_trunc(_get_field(row, COL_MAPPING["employment_type"]), "employment_type"),
            industry=_trunc(_get_field(row, COL_MAPPING["industry"]), "industry"),
            company_size=_trunc(_get_field(row, COL_MAPPING["company_size"]), "company_size"),
            # Akquise-Felder
            acquisition_source="advertsdata",
            position_id=_trunc(position_id, "position_id"),
            anzeigen_id=_trunc(anzeigen_id, "anzeigen_id"),
            akquise_status="neu",
            akquise_status_changed_at=now,
            akquise_priority=priority,
            first_seen_at=now,
            last_seen_at=now,
            import_batch_id=batch_id,
            # Lifecycle
            expires_at=now + timedelta(days=30),
        )
        self.db.add(job)

        # existing_jobs aktualisieren damit CSV-interne Duplikate erkannt werden
        if anzeigen_id:
            existing_jobs[anzeigen_id] = {
                "id": job.id,
                "akquise_status": "neu",
                "last_seen_at": now,
            }

        return "imported"

    async def _load_existing_anzeigen_ids(self) -> dict[str, dict]:
        """Laedt alle bestehenden Akquise-Jobs mit anzeigen_id."""
        result = await self.db.execute(
            select(
                Job.id,
                Job.anzeigen_id,
                Job.akquise_status,
                Job.last_seen_at,
            ).where(
                Job.acquisition_source.isnot(None),
                Job.anzeigen_id.isnot(None),
            )
        )
        return {
            row.anzeigen_id: {
                "id": row.id,
                "akquise_status": row.akquise_status,
                "last_seen_at": row.last_seen_at,
            }
            for row in result.all()
        }

    async def preview_csv(self, content: bytes) -> dict:
        """Analysiert CSV ohne Import (Duplikate, bekannte Firmen, Blacklist)."""
        text_content = self._decode_csv(content)
        reader = csv.DictReader(io.StringIO(text_content), delimiter="\t")

        existing_jobs = await self._load_existing_anzeigen_ids()

        stats = {
            "total_rows": 0,
            "new_leads": 0,
            "duplicates": 0,
            "blacklisted": 0,
            "known_companies": 0,
            "cities": {},
            "industries": {},
        }

        seen_companies: set[str] = set()

        for row in reader:
            stats["total_rows"] += 1
            job_url = _get_field(row, COL_MAPPING["job_url"])
            anzeigen_id = _extract_anzeigen_id(job_url)

            if anzeigen_id and anzeigen_id in existing_jobs:
                existing = existing_jobs[anzeigen_id]
                if existing.get("akquise_status") == "blacklist_hart":
                    stats["blacklisted"] += 1
                else:
                    stats["duplicates"] += 1
            else:
                stats["new_leads"] += 1

            # Stadt-Statistik
            city = _get_field(row, COL_MAPPING["city"]) or "Unbekannt"
            stats["cities"][city] = stats["cities"].get(city, 0) + 1

            # Branche-Statistik
            industry = _get_field(row, COL_MAPPING["industry"]) or "Unbekannt"
            stats["industries"][industry] = stats["industries"].get(industry, 0) + 1

            # Bekannte Firmen
            company_name = _get_field(row, COL_MAPPING["company_name"])
            if company_name and company_name.lower() not in seen_companies:
                seen_companies.add(company_name.lower())
                existing_company = await self.db.execute(
                    select(Company.id).where(
                        func.lower(Company.name) == company_name.lower()
                    ).limit(1)
                )
                if existing_company.scalar_one_or_none():
                    stats["known_companies"] += 1

        return stats

    async def rollback_batch(self, batch_id: uuid.UUID) -> dict:
        """Macht einen fehlerhaften Import rueckgaengig (nur Status='neu')."""
        # Nur Jobs loeschen die noch Status "neu" haben (nicht bearbeitet)
        result = await self.db.execute(
            select(func.count(Job.id)).where(
                Job.import_batch_id == batch_id,
                Job.akquise_status == "neu",
            )
        )
        count = result.scalar_one()

        if count == 0:
            return {"deleted": 0, "message": "Keine unbearbeiteten Jobs in diesem Batch"}

        await self.db.execute(
            update(Job)
            .where(
                Job.import_batch_id == batch_id,
                Job.akquise_status == "neu",
            )
            .values(deleted_at=datetime.now(timezone.utc))
        )
        await self.db.commit()

        return {"deleted": count, "message": f"{count} Jobs als geloescht markiert"}

    def _decode_csv(self, content: bytes) -> str:
        """Versucht verschiedene Encodings."""
        for encoding in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
            try:
                return content.decode(encoding)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return content.decode("utf-8", errors="replace")
