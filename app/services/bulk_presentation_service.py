"""BulkPresentationService — CSV-Bulk-Upload fuer Kandidaten-Vorstellungen.

Verarbeitet CSV-Dateien (advertsdata-Format, Tab-getrennt) und erstellt
fuer jede Zeile eine individuelle Presentation mit:
- Spam-Check
- Company auto-create
- Drive-Time Berechnung
- Skills-Match per GPT-4o
- E-Mail-Generierung per GPT-4o
- Row-Level-Tracking fuer Absturz-Recovery
"""

import asyncio
import csv
import io
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from app.services.presentation_service import MAILBOXES

logger = logging.getLogger(__name__)

# ── CSV Column-Mapping (advertsdata-Format) ──
# Jeder interne Feldname hat eine Liste moeglicher CSV-Spaltennamen (case-insensitive)
# WICHTIG: advertsdata.com hat AP Firma + AP Anzeige Kontakte — wir bevorzugen AP Anzeige
COL_ALIASES = {
    "company_name": ["Unternehmen", "Firma", "Company", "Firmenname", "company_name", "company"],
    "position": ["Position", "Stelle", "Jobtitel", "Job", "Titel", "position", "job_title"],
    "job_text": ["Anzeigen-Text", "Anzeigentext", "Stellentext", "Beschreibung", "Job-Text", "job_text", "description", "text"],
    "contact_salutation": ["Anrede - AP Anzeige", "Kontakt: Anrede", "Anrede", "salutation"],
    "contact_firstname": ["Vorname - AP Anzeige", "Kontakt: Vorname", "Vorname", "firstname", "first_name"],
    "contact_lastname": ["Nachname - AP Anzeige", "Kontakt: Nachname", "Nachname", "lastname", "last_name"],
    "contact_email": ["E-Mail - AP Anzeige", "E-Mail - AP Firma", "E-Mail", "Email", "Mail", "email", "e-mail", "contact_email"],
    "contact_phone": ["Telefon - AP Anzeige", "Telefon - AP Firma", "Telefon", "Firma Telefonnummer", "Tel", "Phone", "phone", "telefon"],
    "plz": ["PLZ", "Postleitzahl", "plz", "zip", "postal_code"],
    "city": ["Einsatzort", "Ort", "Stadt", "City", "city", "ort"],
    "address": ["Straße und Hausnummer", "Strasse", "Straße", "Adresse", "address", "street"],
    "domain": ["Internet", "Website", "Domain", "URL", "domain", "website", "url"],
    # Zusaetzliche advertsdata-Felder (optional)
    "branche": ["Branche"],
    "company_size": ["Mitarbeiter (MA) / Unternehmensgröße"],
    "anzeigenlink": ["Anzeigenlink"],
    "anzeigenart": ["Anzeigenart"],
    "beschaeftigungsart": ["Beschäftigungsart"],
    "contact_function": ["Funktion - AP Anzeige", "Funktion - AP Firma"],
}

# Legacy exact-match mapping (fuer Rueckwaertskompatibilitaet)
COL_MAPPING = {
    "Unternehmen": "company_name",
    "Position": "position",
    "Anzeigen-Text": "job_text",
    "Kontakt: Anrede": "contact_salutation",
    "Kontakt: Vorname": "contact_firstname",
    "Kontakt: Nachname": "contact_lastname",
    "E-Mail": "contact_email",
    "Telefon": "contact_phone",
    "PLZ": "plz",
    "Ort": "city",
    "Strasse": "address",
    "Website": "domain",
}


def _detect_delimiter(text: str) -> str:
    """Erkennt den Delimiter automatisch: Tab, Semikolon oder Komma."""
    first_line = text.split("\n", 1)[0]
    tab_count = first_line.count("\t")
    semi_count = first_line.count(";")
    comma_count = first_line.count(",")

    if tab_count >= semi_count and tab_count >= comma_count and tab_count > 0:
        return "\t"
    if semi_count >= comma_count and semi_count > 0:
        return ";"
    if comma_count > 0:
        return ","
    return "\t"  # Fallback


def _build_column_map(headers: list[str]) -> dict[str, str]:
    """Baut ein Mapping von tatsaechlichen CSV-Headern zu internen Feldnamen.

    Strategie: Spezifischere (laengere) Header-Matches zuerst.
    Bei advertsdata gibt es z.B. 'E-Mail' (Firma) UND 'E-Mail - AP Anzeige' (Kontakt).
    Wir bevorzugen den spezifischeren Match (AP Anzeige > AP Firma > generisch).
    """
    col_map = {}  # csv_header -> internal_field
    used_fields = set()  # Welche internen Felder schon vergeben sind

    # Sortiere Headers: laengere zuerst (spezifischere Matches bevorzugen)
    headers_sorted = sorted(headers, key=lambda h: -len(h.strip()))

    for original_header in headers_sorted:
        header_lower = original_header.strip().lower()
        if not header_lower:
            continue

        # Finde das erste Feld, dessen Alias matcht und noch nicht vergeben ist
        for field_name, aliases in COL_ALIASES.items():
            if field_name in used_fields:
                continue
            aliases_lower = [a.lower() for a in aliases]
            if header_lower in aliases_lower:
                col_map[original_header] = field_name
                used_fields.add(field_name)
                break

    return col_map


def parse_csv(file_bytes: bytes) -> list[dict]:
    """Parst eine CSV-Datei (Tab/Semikolon/Komma-getrennt).

    Erkennt automatisch:
    - Delimiter (Tab, Semikolon, Komma)
    - Spaltennamen (mehrere Aliase pro Feld, case-insensitive)

    Returns:
        Liste von Dicts mit den gemappten Feldern.
    """
    text = _decode_csv(file_bytes)
    delimiter = _detect_delimiter(text)
    logger.info(f"parse_csv: Delimiter erkannt: {repr(delimiter)}")

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

    # Dynamisches Column-Mapping basierend auf tatsaechlichen Headern
    headers = reader.fieldnames or []
    col_map = _build_column_map(headers)
    logger.info(f"parse_csv: Headers={headers}, Mapping={col_map}")

    if not col_map:
        logger.warning(f"parse_csv: Kein Spalten-Mapping gefunden! Headers: {headers}")
        return []

    rows = []
    for i, raw_row in enumerate(reader):
        mapped = {"_row_index": i}
        for csv_col, field_name in col_map.items():
            mapped[field_name] = (raw_row.get(csv_col) or "").strip()

        # Fehlende Felder mit leerem String fuellen
        for field_name in COL_ALIASES:
            if field_name not in mapped:
                mapped[field_name] = ""

        # Position aus Anzeigen-Text extrahieren wenn leer
        # advertsdata hat keine Position-Spalte — der Jobtitel ist die erste Zeile
        if not mapped.get("position") and mapped.get("job_text"):
            mapped["position"] = _extract_position_from_text(mapped["job_text"])

        # Kontaktname zusammensetzen (Einzelfelder AUCH behalten!)
        first = mapped.get("contact_firstname", "")
        last = mapped.get("contact_lastname", "")
        mapped["contact_name"] = f"{first} {last}".strip()

        # Beste E-Mail intelligent auswaehlen (alle 3 Spalten pruefen)
        # Prioritaet: persoenlich (vorname.nachname@) > bewerbung/karriere@ > info/kontakt@
        mapped["contact_email"] = _pick_best_email(raw_row, mapped.get("contact_email", ""))

        # Nur Zeilen mit Firma UND (Position ODER job_text)
        if mapped.get("company_name") and (mapped.get("position") or mapped.get("job_text")):
            rows.append(mapped)

    logger.info(f"parse_csv: {len(rows)} gueltige Zeilen geparst (Delimiter={repr(delimiter)})")
    return rows


async def preview_bulk(
    db_session_maker,
    candidate_id: UUID,
    rows: list[dict],
) -> list[dict]:
    """Vorschau fuer Bulk-Upload: Spam-Check + Duplikat-Check pro Zeile.

    Kein DB-Write, kein OpenAI-Call — nur Vorschau-Annotation.

    Returns:
        Liste mit annotierten Zeilen: {can_send, skip_reason, level, ...}
    """
    try:
        from app.database import async_session_maker
        from app.services.candidate_presentation_service import CandidatePresentationService
        from app.services.presentation_reply_service import PresentationReplyService

        annotated = []
        seen_companies: set[tuple[str, str]] = set()
        async with async_session_maker() as db:
            for row in rows:
                company_key = (
                    row.get("company_name", "").strip().lower(),
                    row.get("city", "").strip().lower(),
                )
                # CSV-internes Duplikat?
                if company_key in seen_companies:
                    annotated.append({
                        **row,
                        "can_send": False,
                        "skip_reason": "Duplikat in CSV (gleiche Firma)",
                        "level": "red",
                    })
                    continue
                seen_companies.add(company_key)

                # Blocklist-Check (Empfaenger-Domain gesperrt?)
                contact_email = row.get("contact_email", "")
                if contact_email:
                    domain_blocked = await PresentationReplyService.is_domain_blocked(db, contact_email)
                    if domain_blocked:
                        annotated.append({
                            **row,
                            "can_send": False,
                            "skip_reason": "Empfänger-Domain gesperrt",
                            "level": "blocked",
                        })
                        continue

                spam = await CandidatePresentationService.check_spam_block(
                    db,
                    company_name=row.get("company_name", ""),
                    city=row.get("city", ""),
                )

                # Bereits vorgestellt?
                if not spam["blocked"]:
                    already = await CandidatePresentationService.check_already_presented(
                        db,
                        candidate_id=candidate_id,
                        company_name=row.get("company_name", ""),
                        city=row.get("city", ""),
                    )
                    if already["already_presented"]:
                        annotated.append({
                            **row,
                            "can_send": False,
                            "skip_reason": already["reason"],
                            "level": "red",
                        })
                        continue

                annotated.append({
                    **row,
                    "can_send": not spam["blocked"],
                    "skip_reason": spam["reason"] if spam["blocked"] else None,
                    "level": spam["level"],
                })

        # Kosten-Schaetzung
        estimated_cost_per_row = 0.12  # ~$0.12 pro Zeile (2x Claude Opus Calls)
        sendable_count = len([r for r in annotated if r.get("can_send")])
        estimated_cost = round(sendable_count * estimated_cost_per_row, 2)

        return annotated, estimated_cost
    except Exception as e:
        logger.error(f"preview_bulk fehlgeschlagen: {e}")
        return [{"error": str(e)}], 0.0


async def process_bulk(
    candidate_id: UUID,
    rows: list[dict],
    batch_id: UUID,
) -> None:
    """Background-Task: Verarbeitet alle CSV-Zeilen.

    Pattern:
    - try/except/finally mit Imports im try-Block
    - Eigene DB-Session pro Zeile
    - OpenAI-Call OHNE offene DB-Session (Railway 30s)
    - Row-Level-Tracking fuer Absturz-Recovery
    """
    batch_status = {"running": True, "processed": 0, "errors": 0}

    try:
        # Imports IM try-Block (Railway-Pattern)
        from app.database import async_session_maker
        from app.services.candidate_presentation_service import CandidatePresentationService
        from app.services.company_service import CompanyService
        from app.services.distance_matrix_service import DistanceMatrixService
        from app.models.candidate import Candidate
        from app.models.presentation_batch import PresentationBatch
        from sqlalchemy import select, update, func
        from geoalchemy2.functions import ST_Y, ST_X, ST_GeomFromWKB

        # Kandidaten-Daten laden (eigene Session)
        async with async_session_maker() as db:
            candidate_data = await CandidatePresentationService.extract_candidate_data(db, candidate_id)
            # Koordinaten fuer Drive-Time
            cand_result = await db.execute(
                select(
                    func.ST_Y(func.ST_GeomFromWKB(Candidate.address_coords)).label("lat"),
                    func.ST_X(func.ST_GeomFromWKB(Candidate.address_coords)).label("lng"),
                    Candidate.postal_code,
                ).where(Candidate.id == candidate_id)
            )
            cand_coords = cand_result.first()
        # Session geschlossen!

        if not candidate_data:
            logger.error(f"process_bulk: Kandidat {candidate_id} nicht gefunden")
            return

        # Mailbox-Rotation (Round-Robin) + Domain-Schutz
        from app.services.domain_protection_service import (
            check_domain_capacity, get_domain_for_company, select_best_mailbox, get_domain_from_email,
        )
        mailbox_index = 0
        mailbox_counts = {mb["email"]: 0 for mb in MAILBOXES}
        exhausted_domains = set()
        seen_companies: set[tuple[str, str]] = set()

        for row in rows:
            row_index = row.get("_row_index", 0)
            try:
                # CSV-internes Duplikat pruefen
                company_key = (
                    row.get("company_name", "").strip().lower(),
                    row.get("city", "").strip().lower(),
                )
                if company_key in seen_companies:
                    logger.info(f"Zeile {row_index}: CSV-Duplikat uebersprungen ({company_key[0]}, {company_key[1]})")
                    await _update_batch_row(async_session_maker, batch_id, row_index, "skipped_csv_duplicate", None)
                    continue
                seen_companies.add(company_key)

                # 0. Domain-Kapazitaet pruefen (eigene Session)
                async with async_session_maker() as db:
                    # Domain-Konsistenz: gleiche Domain wie zuvor fuer diese Firma
                    preferred_domain = await get_domain_for_company(db, row.get("company_name", ""))
                # Session geschlossen!

                # Mailbox waehlen (Domain-Konsistenz + Round-Robin)
                mailbox = select_best_mailbox(
                    MAILBOXES,
                    preferred_domain=preferred_domain,
                    exclude_domains=list(exhausted_domains),
                    mailbox_counts=mailbox_counts,
                )
                if not mailbox:
                    logger.warning(f"Zeile {row_index}: Keine Mailbox verfuegbar (alle Domains erschoepft)")
                    await _update_batch_row(async_session_maker, batch_id, row_index, "skipped_domain_limit", None)
                    continue

                # Domain-Kapazitaet live pruefen
                async with async_session_maker() as db:
                    capacity = await check_domain_capacity(db, mailbox["email"])
                if not capacity["allowed"]:
                    exhausted_domains.add(get_domain_from_email(mailbox["email"]))
                    # Retry mit anderer Domain
                    mailbox = select_best_mailbox(MAILBOXES, exclude_domains=list(exhausted_domains), mailbox_counts=mailbox_counts)
                    if not mailbox:
                        await _update_batch_row(async_session_maker, batch_id, row_index, "skipped_domain_limit", None)
                        continue

                # 1. Spam-Check (eigene Session)
                async with async_session_maker() as db:
                    spam = await CandidatePresentationService.check_spam_block(
                        db, row.get("company_name", ""), row.get("city", "")
                    )
                if spam["blocked"]:
                    await _update_batch_row(async_session_maker, batch_id, row_index, "skipped", None)
                    continue

                # 1b. Already-presented Check (eigene Session)
                async with async_session_maker() as db:
                    already = await CandidatePresentationService.check_already_presented(
                        db,
                        candidate_id=candidate_id,
                        company_name=row.get("company_name", ""),
                        city=row.get("city", ""),
                    )
                if already["already_presented"]:
                    logger.info(f"Zeile {row_index}: Kandidat bereits vorgestellt bei {row.get('company_name', '')}")
                    await _update_batch_row(async_session_maker, batch_id, row_index, "skipped_already_presented", None)
                    continue

                # 1c. Blocklist-Check (Empfaenger-Domain gesperrt?)
                contact_email = row.get("contact_email", "")
                if contact_email:
                    async with async_session_maker() as db:
                        from app.services.presentation_reply_service import PresentationReplyService
                        domain_blocked = await PresentationReplyService.is_domain_blocked(db, contact_email)
                    if domain_blocked:
                        logger.info(f"Zeile {row_index}: Empfaenger-Domain gesperrt ({contact_email})")
                        await _update_batch_row(async_session_maker, batch_id, row_index, "skipped_domain_blocked", None)
                        continue

                # 2. Company erstellen/finden (eigene Session)
                async with async_session_maker() as db:
                    company_svc = CompanyService(db)

                    # Adresse zusammenbauen (PLZ + Strasse + Ort)
                    address_parts = [p for p in [
                        row.get("address", ""),
                        row.get("plz", ""),
                        row.get("city", ""),
                    ] if p and p.strip()]
                    full_address = ", ".join(address_parts) if address_parts else ""

                    company = await company_svc.get_or_create_by_name(
                        row.get("company_name", ""),
                        city=row.get("city", ""),
                        domain=row.get("domain", ""),
                        address=full_address,
                    )
                    if not company:
                        await _update_batch_row(async_session_maker, batch_id, row_index, "skipped_blacklist", None)
                        continue
                    company_id = company.id

                    # Contact erstellen/finden (mit Duplikat-Erkennung + Auto-Anrede)
                    contact_id = None
                    contact_email = row.get("contact_email", "")
                    first_name = row.get("contact_firstname", "").strip()
                    last_name = row.get("contact_lastname", "").strip()
                    if contact_email or first_name or last_name:
                        contact = await company_svc.get_or_create_contact(
                            company_id=company_id,
                            first_name=first_name or None,
                            last_name=last_name or None,
                            email=contact_email or None,
                            phone=row.get("contact_phone", "") or None,
                            salutation=row.get("contact_salutation", "") or None,
                            source="csv_bulk",
                        )
                        contact_id = contact.id
                    await db.commit()
                # Session geschlossen!

                # 3. Drive-Time (DB-Session schliessen BEVOR Google Maps API-Call!)
                drive_time = None
                if cand_coords and cand_coords.lat and cand_coords.lng:
                    try:
                        # Firma-Koordinaten aus DB laden (eigene Session)
                        comp_lat = None
                        comp_lng = None
                        comp_plz = None
                        async with async_session_maker() as db:
                            from app.models.company import Company
                            comp_result = await db.execute(
                                select(
                                    func.ST_Y(func.ST_GeomFromWKB(Company.location_coords)).label("lat"),
                                    func.ST_X(func.ST_GeomFromWKB(Company.location_coords)).label("lng"),
                                    Company.postal_code,
                                ).where(Company.id == company_id)
                            )
                            comp_row = comp_result.first()
                            if comp_row and comp_row.lat and comp_row.lng:
                                comp_lat = comp_row.lat
                                comp_lng = comp_row.lng
                                comp_plz = comp_row.postal_code
                        # Session geschlossen BEVOR API-Call!

                        if comp_lat and comp_lng:
                            dm_service = DistanceMatrixService()
                            dt_result = await dm_service.get_drive_time(
                                origin_lat=cand_coords.lat,
                                origin_lng=cand_coords.lng,
                                origin_plz=cand_coords.postal_code or "",
                                dest_lat=comp_lat,
                                dest_lng=comp_lng,
                                dest_plz=comp_plz or "",
                            )
                            if dt_result.status == "ok" or dt_result.status == "same_plz":
                                drive_time = {
                                    "car_min": dt_result.car_min,
                                    "transit_min": dt_result.transit_min,
                                    "car_km": dt_result.car_km,
                                }
                                logger.info(f"Zeile {row_index}: Fahrzeit berechnet — Auto: {dt_result.car_min}min, OEPNV: {dt_result.transit_min}min")
                            else:
                                logger.info(f"Zeile {row_index}: Fahrzeit-Status: {dt_result.status} — uebersprungen")
                        else:
                            logger.info(f"Zeile {row_index}: Firma hat keine Koordinaten — Fahrzeit uebersprungen")
                    except Exception as dt_err:
                        logger.warning(f"Zeile {row_index}: Fahrzeit-Berechnung fehlgeschlagen: {dt_err}")

                # 4. Skills-Match (OpenAI, KEINE DB-Session offen!)
                extracted_data = {
                    "company_name": row.get("company_name", ""),
                    "city": row.get("city", ""),
                    "job_title": row.get("position", ""),
                    "requirements": [],
                    "description_summary": row.get("job_text", "")[:500],
                }
                skills = await CandidatePresentationService.calculate_skills_match(
                    candidate_data, extracted_data
                )

                # 5. E-Mail generieren (OpenAI, KEINE DB-Session offen!)
                email_data = await CandidatePresentationService.generate_presentation_email(
                    candidate_data=candidate_data,
                    extracted_job_data={**extracted_data, "contact_name": row.get("contact_name", ""), "contact_salutation": row.get("contact_salutation", "")},
                    skills_comparison=skills.model_dump(),
                    drive_time=drive_time,
                    step=1,
                )

                # 6. Mailbox bereits oben gewaehlt (Domain-Konsistenz + Kapazitaet)
                mailbox_counts[mailbox["email"]] = mailbox_counts.get(mailbox["email"], 0) + 1

                # 6b. Profil-PDF generieren (eigene Session, BEVOR n8n-Trigger)
                pdf_base64 = None
                pdf_filename = None
                try:
                    import base64
                    from app.services.profile_pdf_service import ProfilePdfService
                    async with async_session_maker() as pdf_db:
                        pdf_service = ProfilePdfService(pdf_db)
                        pdf_bytes = await pdf_service.generate_profile_pdf(candidate_id)
                        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
                        pdf_filename = "Kandidatenprofil.pdf"
                        logger.info(f"Zeile {row_index}: PDF generiert ({len(pdf_bytes)} bytes)")
                    # Session geschlossen!
                except Exception as pdf_err:
                    logger.warning(f"Zeile {row_index}: PDF-Generierung fehlgeschlagen: {pdf_err} — E-Mail ohne Anhang")

                # 7. Presentation erstellen (neue Session)
                email_body_html = email_data.get("body_html", "")
                async with async_session_maker() as db:
                    presentation = await CandidatePresentationService.create_direct_presentation(
                        db=db,
                        candidate_id=candidate_id,
                        company_id=company_id,
                        contact_id=contact_id,
                        email_to=contact_email or f"info@{row.get('domain', 'unbekannt.de')}",
                        email_from=mailbox["email"],
                        email_subject=email_data["subject"],
                        email_body_text=email_data["body_text"],
                        email_body_html=email_body_html,
                        mailbox_used=mailbox["email"],
                        source="csv_bulk",
                        extracted_job_data=extracted_data,
                        skills_comparison=skills.model_dump(),
                        batch_id=batch_id,
                    )
                    await db.commit()
                    presentation_id = presentation.id
                # Session geschlossen!

                # 8. n8n triggern fuer E-Mail-Versand (KEINE DB-Session offen!)
                n8n_ok = await _trigger_n8n_for_bulk(
                    presentation_id=str(presentation_id),
                    candidate_id=str(candidate_id),
                    company_id=str(company_id),
                    contact_id=str(contact_id) if contact_id else None,
                    email_to=contact_email or f"info@{row.get('domain', 'unbekannt.de')}",
                    email_from=mailbox["email"],
                    email_subject=email_data["subject"],
                    email_body_text=email_data["body_text"],
                    email_body_html=email_body_html,
                    contact_name=row.get("contact_name", ""),
                    source="csv_bulk",
                    pdf_base64=pdf_base64,
                    pdf_filename=pdf_filename,
                )

                # Bei Erfolg: Status auf "sending" setzen (eigene Session!)
                if n8n_ok:
                    async with async_session_maker() as db:
                        from app.models.client_presentation import ClientPresentation
                        await db.execute(
                            update(ClientPresentation)
                            .where(ClientPresentation.id == presentation_id)
                            .values(status="sending")
                        )
                        await db.commit()
                    # Session geschlossen!

                await _update_batch_row(async_session_maker, batch_id, row_index, "created", str(presentation_id))
                batch_status["processed"] += 1

                # Live-Fortschritt in DB schreiben (fuer Frontend-Polling)
                async with async_session_maker() as db:
                    await db.execute(
                        update(PresentationBatch)
                        .where(PresentationBatch.id == batch_id)
                        .values(
                            processed=batch_status["processed"],
                            errors=batch_status["errors"],
                            skipped=batch_status["skipped"],
                            updated_at=func.now(),
                        )
                    )
                    await db.commit()

                # 9. Pause zwischen E-Mails (max 10/min fuer SMTP-Sicherheit)
                await asyncio.sleep(6)

            except Exception as e:
                logger.error(f"process_bulk Zeile {row_index}: {e}")
                batch_status["errors"] += 1
                await _update_batch_error(async_session_maker, batch_id, row_index, str(e), row.get("company_name", ""))

        # Batch abschliessen
        async with async_session_maker() as db:
            await db.execute(
                update(PresentationBatch)
                .where(PresentationBatch.id == batch_id)
                .values(
                    status="completed",
                    processed=batch_status["processed"],
                    errors=batch_status["errors"],
                    mailbox_distribution=mailbox_counts,
                    updated_at=func.now(),
                )
            )
            await db.commit()

    except Exception as e:
        logger.error(f"process_bulk FATAL: {e}")
        try:
            from app.database import async_session_maker
            from app.models.presentation_batch import PresentationBatch
            from sqlalchemy import update as sql_update
            async with async_session_maker() as db:
                await db.execute(
                    sql_update(PresentationBatch)
                    .where(PresentationBatch.id == batch_id)
                    .values(status="failed")
                )
                await db.commit()
        except Exception:
            pass
    finally:
        batch_status["running"] = False


async def _trigger_n8n_for_bulk(
    presentation_id: str,
    candidate_id: str,
    company_id: str,
    contact_id: Optional[str],
    email_to: str,
    email_from: str,
    email_subject: str,
    email_body_text: str,
    email_body_html: str = "",
    contact_name: str = "",
    source: str = "csv_bulk",
    pdf_base64: Optional[str] = None,
    pdf_filename: Optional[str] = None,
) -> bool:
    """Triggert n8n Webhook fuer eine einzelne Bulk-Presentation.

    Pattern: Kein DB-Zugriff hier — nur externer HTTP-Call.
    Bei Fehler: loggen + False zurueckgeben (naechste Zeile weiterverarbeiten).
    """
    try:
        import httpx
        from app.config import settings

        if not settings.n8n_webhook_url:
            logger.warning("_trigger_n8n_for_bulk: n8n_webhook_url nicht konfiguriert")
            return False

        webhook_url = f"{settings.n8n_webhook_url}/webhook/kunde-vorstellen"

        payload = {
            "presentation_id": presentation_id,
            "match_id": None,
            "candidate_id": candidate_id,
            "company_id": company_id,
            "contact_id": contact_id,
            "email_to": email_to,
            "email_from": email_from,
            "email_subject": email_subject,
            "email_body_text": email_body_text,
            "email_body_html": email_body_html,
            "email_signature_html": None,
            "mailbox_used": email_from,
            "pdf_attached": bool(pdf_base64),
            "pdf_base64": pdf_base64,
            "pdf_filename": pdf_filename,
            "presentation_mode": "ai_generated",
            "contact_name": contact_name,
            "reply_to": email_from or "hamdard@sincirus.com",
            "email_format": "html" if email_body_html else "plain_text",
            "source": source,
            "followup_schedule": {"step2_days": 3, "step3_days": 7},
            "callback_auth_token": f"Bearer {settings.n8n_api_token}" if settings.n8n_api_token else "",
        }

        headers = {}
        if settings.n8n_api_token:
            headers["Authorization"] = f"Bearer {settings.n8n_api_token}"

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(webhook_url, json=payload, headers=headers)

        if resp.status_code == 200:
            logger.info(f"n8n Bulk-Trigger OK fuer Presentation {presentation_id}")
            return True
        else:
            logger.error(f"n8n Bulk-Trigger Fehler: Status={resp.status_code}, Body={resp.text[:300]}")
            return False
    except Exception as e:
        logger.error(f"n8n Bulk-Trigger fehlgeschlagen fuer {presentation_id}: {e}")
        return False


async def _update_batch_row(session_maker, batch_id: UUID, row_index: int, status: str, presentation_id: Optional[str]):
    """Row-Level-Tracking updaten (fuer Absturz-Recovery)."""
    try:
        from app.models.presentation_batch import PresentationBatch
        from sqlalchemy import select, func
        async with session_maker() as db:
            batch = await db.get(PresentationBatch, batch_id)
            if batch:
                rows = batch.processed_rows or []
                rows.append({"row_index": row_index, "presentation_id": presentation_id, "status": status})
                batch.processed_rows = rows
                batch.processed = len([r for r in rows if r["status"] == "created"])
                batch.skipped = len([r for r in rows if r["status"].startswith("skipped")])
                await db.commit()
    except Exception as e:
        logger.warning(f"_update_batch_row fehlgeschlagen: {e}")


async def _update_batch_error(session_maker, batch_id: UUID, row_index: int, error: str, company_name: str):
    """Fehler-Details zum Batch hinzufuegen."""
    try:
        from app.models.presentation_batch import PresentationBatch
        async with session_maker() as db:
            batch = await db.get(PresentationBatch, batch_id)
            if batch:
                errors = batch.error_details or []
                errors.append({"row_index": row_index, "error": error[:500], "company_name": company_name})
                batch.error_details = errors
                batch.errors = len(errors)
                await db.commit()
    except Exception as e:
        logger.warning(f"_update_batch_error fehlgeschlagen: {e}")


def _pick_best_email(raw_row: dict, current_email: str) -> str:
    """Waehlt die beste E-Mail-Adresse aus allen verfuegbaren CSV-Spalten.

    Prioritaet:
    1. Persoenliche E-Mail (vorname.nachname@, v.nachname@) — BESTE
    2. HR-Postfaecher (bewerbung@, karriere@, recruiting@, hr@, personal@, jobs@) — GUT
    3. Nichts brauchbares → "NEEDS_MANUAL" markieren (Milad sucht die Adresse)

    VERBOTEN: info@, kontakt@, office@, verkauf@, mail@, service@ — NIEMALS verwenden!
    """
    # Alle verfuegbaren E-Mail-Adressen sammeln
    candidates = []
    for key in ["E-Mail - AP Anzeige", "E-Mail - AP Firma", "E-Mail"]:
        email = (raw_row.get(key) or "").strip().lower()
        if email and "@" in email:
            candidates.append(email)

    if not candidates:
        return current_email or ""

    # Verbotene Prefixe — NIEMALS verwenden
    forbidden_prefixes = ("info@", "kontakt@", "office@", "mail@", "post@",
                          "service@", "verkauf@", "vertrieb@", "einkauf@",
                          "buchhaltung@", "empfang@", "zentrale@", "sekretariat@")

    # HR-Postfaecher — GUT (akzeptabel)
    hr_prefixes = ("bewerbung@", "bewerbungen@", "karriere@", "career@",
                   "recruiting@", "jobs@", "hr@", "personal@", "people@",
                   "talent@", "hiring@", "job@", "stellenangebote@")

    def email_score(email: str) -> int:
        local = email.split("@")[0]
        # Verboten = -1 (rausfiltern)
        if any(email.startswith(p) for p in forbidden_prefixes):
            return -1
        # Persoenlich (vorname.nachname, v.nachname) = BEST
        if "." in local and len(local) > 3:
            return 3
        # HR-Postfach = GUT
        if any(email.startswith(p) for p in hr_prefixes):
            return 2
        # Einzelner Name (z.B. heinrich@, albrecht@) = GUT
        if local.isalpha() and len(local) > 3:
            return 2
        # Abkuerzung + Name (z.B. t.boeger@, ebrunsmann@) = GUT
        if local and not any(email.startswith(p) for p in forbidden_prefixes):
            return 1
        return -1  # Unbekannt = verboten

    # Nur erlaubte E-Mails behalten (Score >= 0)
    valid = [e for e in candidates if email_score(e) >= 0]

    if not valid:
        # KEINE brauchbare E-Mail gefunden — HR-Postfach raten
        domain = ""
        for e in candidates:
            if "@" in e:
                domain = e.split("@")[1]
                break
        if domain:
            # Versuche gaengige HR-Adressen zu generieren
            guesses = [f"bewerbung@{domain}", f"karriere@{domain}", f"jobs@{domain}"]
            logger.warning(f"_pick_best_email: Nur verbotene Adressen gefunden. "
                          f"Schlage vor: {guesses[0]} (MUSS VERIFIZIERT WERDEN)")
            return f"PRUEFEN:{guesses[0]}"
        return f"PRUEFEN:{current_email}"

    # Beste E-Mail waehlen
    best = max(valid, key=email_score)
    if best != current_email:
        logger.info(f"_pick_best_email: {current_email} → {best} (besserer Kontakt)")
    return best


def _extract_position_from_text(job_text: str) -> str:
    """Extrahiert den Jobtitel aus dem Anzeigen-Text.

    advertsdata: Erste Zeile / erster Satz ist typischerweise der Jobtitel.
    Beispiel: "Finanzbuchhalter (m/w/d) {Finanzbuchhalter/in} Friedrich Karl ..."
    -> "Finanzbuchhalter (m/w/d)"

    Haelt den Titel bei max ~80 Zeichen.
    """
    import re
    text = job_text.strip()
    if not text:
        return ""

    # URL am Anfang entfernen (advertsdata hat manchmal www.xyz.de davor)
    text = re.sub(r'^https?://\S+\s*', '', text)
    text = re.sub(r'^www\.\S+\s*', '', text)

    # Erste Zeile oder bis zum ersten Punkt/Semikolon nehmen
    first_line = text.split("\n")[0].strip()

    # Wenn {Berufsbezeichnung} vorkommt, alles danach abschneiden
    brace_match = re.search(r'\{[^}]+\}', first_line)
    if brace_match:
        first_line = first_line[:brace_match.start()].strip()

    # Auf max 80 Zeichen kuerzen (am Wort-Ende)
    if len(first_line) > 80:
        first_line = first_line[:80].rsplit(" ", 1)[0] + "..."

    return first_line if first_line else text[:60]


def _decode_csv(raw_bytes: bytes) -> str:
    """Versucht verschiedene Encodings fuer die CSV-Datei."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return raw_bytes.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    return raw_bytes.decode("utf-8", errors="replace")
