"""CSV-Import: Unternehmen + Kontakte aus Recruit CRM Export.

Liest company_data.csv und contact_data.csv aus dem Export-Ordner,
importiert alle Daten direkt in die PostgreSQL-Datenbank.

Verwendung:
    python3 scripts/import_csv.py /pfad/zum/crm-export-ordner

Mapping:
    Companies:  Company → name, Website → domain, Full Address → address,
                City → city, Telefonzentrale → phone, Slug → lookup-key
    Contacts:   First Name, Last Name, Email, Contact Number → phone,
                Designation → position, Anrede → salutation, Mobil → mobile,
                City → city, Company Slug → company_id (via slug-map)
"""

import asyncio
import csv
import os
import sys
import uuid

# Projekt-Root zum Python-Path hinzufuegen
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def run_import(export_dir: str):
    from sqlalchemy import delete as sa_delete, func as sa_func, select, text
    from app.database import async_session_maker, init_db, engine
    from app.models.company import Company
    from app.models.company_contact import CompanyContact

    # DB initialisieren
    await init_db()

    company_csv = os.path.join(export_dir, "company_data.csv")
    contact_csv = os.path.join(export_dir, "contact_data.csv")

    if not os.path.exists(company_csv):
        print(f"FEHLER: {company_csv} nicht gefunden!")
        return
    if not os.path.exists(contact_csv):
        print(f"FEHLER: {contact_csv} nicht gefunden!")
        return

    # ══════════════════════════════════════════════
    #  Phase 1: Unternehmen
    # ══════════════════════════════════════════════
    print("=== Phase 1: Unternehmen importieren ===")

    with open(company_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        companies_raw = list(reader)
    print(f"  CSV: {len(companies_raw)} Unternehmen gelesen")

    # Clean Slate
    async with async_session_maker() as session:
        count = (await session.execute(select(sa_func.count(Company.id)))).scalar() or 0
        if count > 0:
            print(f"  Lösche {count} bestehende Unternehmen...")
            await session.execute(sa_delete(Company))
            await session.commit()
            print("  Gelöscht.")

    # Slug → Company-ID Map
    slug_to_id: dict[str, uuid.UUID] = {}
    stats = {"imported": 0, "skipped": 0, "errors": 0, "duplicates": 0}
    seen_names: set[str] = set()

    # Batch-Insert (100er Batches)
    batch: list[Company] = []
    batch_slugs: list[tuple[str, uuid.UUID]] = []

    for i, row in enumerate(companies_raw):
        name = (row.get("Company") or "").strip()
        if not name:
            stats["skipped"] += 1
            continue

        # Duplikat-Check (gleicher Name)
        name_lower = name.lower()
        slug = (row.get("Slug") or "").strip()

        if name_lower in seen_names:
            stats["duplicates"] += 1
            # Slug trotzdem auf bestehende ID mappen
            if slug:
                for s, cid in batch_slugs:
                    if s == slug:
                        break
                else:
                    # Suche die ID des bereits gesehenen Namens
                    for existing_slug, existing_id in slug_to_id.items():
                        pass  # Wir holen die ID nachher aus der DB
            continue
        seen_names.add(name_lower)

        # Domain bereinigen
        domain = (row.get("Website") or "").strip()
        if domain:
            domain = domain.replace("https://", "").replace("http://", "").rstrip("/")

        # Adresse
        address = (row.get("Full Address") or "").strip() or None
        city = (row.get("City") or "").strip() or None
        phone = (row.get("Telefonzentrale") or "").strip() or None

        company_id = uuid.uuid4()
        company = Company(
            id=company_id,
            name=name,
            domain=domain or None,
            address=address,
            city=city,
            phone=phone,
            status="active",
        )
        batch.append(company)
        if slug:
            batch_slugs.append((slug, company_id))
            slug_to_id[slug] = company_id
        stats["imported"] += 1

        # Batch einfügen alle 100
        if len(batch) >= 100:
            async with async_session_maker() as session:
                try:
                    session.add_all(batch)
                    await session.commit()
                except Exception as e:
                    await session.rollback()
                    # Einzeln einfügen
                    for c in batch:
                        try:
                            async with async_session_maker() as ss:
                                ss.add(c)
                                await ss.commit()
                        except Exception:
                            stats["errors"] += 1
                            stats["imported"] -= 1
            batch = []
            batch_slugs = []
            if stats["imported"] % 1000 == 0:
                print(f"  Fortschritt: {stats['imported']} importiert...")

    # Rest-Batch
    if batch:
        async with async_session_maker() as session:
            try:
                session.add_all(batch)
                await session.commit()
            except Exception:
                await session.rollback()
                for c in batch:
                    try:
                        async with async_session_maker() as ss:
                            ss.add(c)
                            await ss.commit()
                    except Exception:
                        stats["errors"] += 1
                        stats["imported"] -= 1

    # Duplikat-Slugs nachträglich auflösen
    async with async_session_maker() as session:
        for row in companies_raw:
            slug = (row.get("Slug") or "").strip()
            if slug and slug not in slug_to_id:
                name = (row.get("Company") or "").strip()
                if name:
                    res = await session.execute(
                        select(Company.id).where(Company.name == name).limit(1)
                    )
                    existing = res.scalar_one_or_none()
                    if existing:
                        slug_to_id[slug] = existing

    print(f"  ✅ Unternehmen: {stats}")
    print(f"  Slug-Map: {len(slug_to_id)} Zuordnungen")

    # ══════════════════════════════════════════════
    #  Phase 2: Kontakte
    # ══════════════════════════════════════════════
    print("\n=== Phase 2: Kontakte importieren ===")

    with open(contact_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        contacts_raw = list(reader)
    print(f"  CSV: {len(contacts_raw)} Kontakte gelesen")

    ct_stats = {"imported": 0, "skipped_empty": 0, "skipped_no_company": 0, "errors": 0}
    batch = []

    for i, row in enumerate(contacts_raw):
        first_name = (row.get("First Name") or "").strip() or None
        last_name = (row.get("Last Name") or "").strip() or None

        if not first_name and not last_name:
            ct_stats["skipped_empty"] += 1
            continue

        # Firma zuordnen per Company Slug
        company_slug = (row.get("Company Slug") or "").strip()
        if not company_slug or company_slug not in slug_to_id:
            ct_stats["skipped_no_company"] += 1
            continue

        company_id = slug_to_id[company_slug]

        # Anrede
        salutation = (row.get("Anrede") or "").strip() or None

        # Position
        position = (row.get("Designation") or "").strip() or None

        # Kontaktdaten
        email = (row.get("Email") or "").strip() or None
        phone = (row.get("Contact Number") or "").strip() or None
        mobile = (row.get("Mobil") or "").strip() or None
        city = (row.get("City") or "").strip() or None

        contact = CompanyContact(
            id=uuid.uuid4(),
            company_id=company_id,
            salutation=salutation,
            first_name=first_name,
            last_name=last_name,
            position=position,
            email=email,
            phone=phone,
            mobile=mobile,
            city=city,
        )
        batch.append(contact)
        ct_stats["imported"] += 1

        # Batch einfügen alle 100
        if len(batch) >= 100:
            async with async_session_maker() as session:
                try:
                    session.add_all(batch)
                    await session.commit()
                except Exception as e:
                    await session.rollback()
                    for c in batch:
                        try:
                            async with async_session_maker() as ss:
                                ss.add(c)
                                await ss.commit()
                        except Exception:
                            ct_stats["errors"] += 1
                            ct_stats["imported"] -= 1
            batch = []
            if ct_stats["imported"] % 1000 == 0:
                print(f"  Fortschritt: {ct_stats['imported']} importiert...")

    # Rest-Batch
    if batch:
        async with async_session_maker() as session:
            try:
                session.add_all(batch)
                await session.commit()
            except Exception:
                await session.rollback()
                for c in batch:
                    try:
                        async with async_session_maker() as ss:
                            ss.add(c)
                            await ss.commit()
                    except Exception:
                        ct_stats["errors"] += 1
                        ct_stats["imported"] -= 1

    print(f"  ✅ Kontakte: {ct_stats}")

    # ══════════════════════════════════════════════
    #  Zusammenfassung
    # ══════════════════════════════════════════════
    print("\n" + "=" * 50)
    print("IMPORT ABGESCHLOSSEN")
    print(f"  Unternehmen: {stats['imported']} importiert, {stats['duplicates']} Duplikate, {stats['errors']} Fehler")
    print(f"  Kontakte:    {ct_stats['imported']} importiert, {ct_stats['skipped_no_company']} ohne Firma, {ct_stats['errors']} Fehler")
    print(f"  Slug-Map:    {len(slug_to_id)} Zuordnungen")
    print("=" * 50)

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Verwendung: python3 scripts/import_csv.py /pfad/zum/export-ordner")
        sys.exit(1)
    asyncio.run(run_import(sys.argv[1]))
