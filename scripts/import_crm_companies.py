#!/usr/bin/env python3
"""Einmaliges Import-Skript: Unternehmen + Kontakte aus Recruit CRM importieren.

ACHTUNG: Dieses Skript loescht ALLE bestehenden Unternehmen (Clean Slate)!
Nach erfolgreichem Import bitte loeschen.

Usage:
    python scripts/import_crm_companies.py
    python scripts/import_crm_companies.py --dry-run
    python scripts/import_crm_companies.py --max-pages=5
    python scripts/import_crm_companies.py --skip-contacts
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Projekt-Root zum Path hinzufuegen
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select, text

from app.config import settings
from app.database import async_session_maker, engine
from app.models.company import Company
from app.models.company_contact import CompanyContact
from app.services.crm_client import RecruitCRMClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def import_companies(
    client: RecruitCRMClient,
    dry_run: bool = False,
    max_pages: int | None = None,
) -> dict[str, int]:
    """Importiert Unternehmen aus CRM.

    Returns:
        Stats-Dictionary mit Zaehlern
    """
    stats = {"imported": 0, "skipped_empty": 0, "skipped_duplicate": 0, "errors": 0}

    if not dry_run:
        # Clean Slate: Alle bestehenden Companies loeschen
        async with async_session_maker() as session:
            count_result = await session.execute(select(func.count(Company.id)))
            existing_count = count_result.scalar() or 0
            if existing_count > 0:
                logger.warning(f"Loesche {existing_count} bestehende Unternehmen (Clean Slate)...")
                await session.execute(delete(Company))
                await session.commit()
                logger.info("Bestehende Unternehmen geloescht.")

    # Unternehmen paginiert abrufen
    async for page_num, companies, total in client.get_all_companies_paginated(
        per_page=100, max_pages=max_pages
    ):
        logger.info(f"Seite {page_num}: {len(companies)} Unternehmen (geschaetzt {total} gesamt)")

        if dry_run:
            for crm_company in companies:
                mapped = client.map_to_company_data(crm_company)
                if mapped and mapped.get("name"):
                    stats["imported"] += 1
                    if stats["imported"] <= 5:
                        logger.info(f"  [DRY] {mapped['name']} | {mapped.get('city', '-')} | {mapped.get('domain', '-')}")
                else:
                    stats["skipped_empty"] += 1
            continue

        # Batch-Insert
        async with async_session_maker() as session:
            batch = []
            for crm_company in companies:
                try:
                    mapped = client.map_to_company_data(crm_company)
                    if not mapped or not mapped.get("name"):
                        stats["skipped_empty"] += 1
                        continue

                    company = Company(**mapped)
                    batch.append(company)
                    stats["imported"] += 1

                except Exception as e:
                    stats["errors"] += 1
                    name = crm_company.get("company_name", "?")
                    logger.error(f"  Fehler bei '{name}': {e}")

            if batch:
                try:
                    session.add_all(batch)
                    await session.commit()
                except Exception as e:
                    await session.rollback()
                    # Bei Unique-Constraint Fehler: einzeln einfuegen
                    logger.warning(f"  Batch-Insert fehlgeschlagen ({e}), versuche einzeln...")
                    for company in batch:
                        try:
                            async with async_session_maker() as single_session:
                                single_session.add(company)
                                await single_session.commit()
                        except Exception as e2:
                            stats["skipped_duplicate"] += 1
                            stats["imported"] -= 1
                            logger.debug(f"  Duplikat: {company.name}")

        if stats["imported"] % 500 == 0 and stats["imported"] > 0:
            logger.info(f"  Fortschritt: {stats['imported']} importiert...")

    return stats


async def import_contacts(
    client: RecruitCRMClient,
    dry_run: bool = False,
    max_pages: int | None = None,
) -> dict[str, int]:
    """Importiert Kontakte aus CRM und ordnet sie Unternehmen zu.

    Returns:
        Stats-Dictionary mit Zaehlern
    """
    stats = {"imported": 0, "skipped_empty": 0, "skipped_no_company": 0, "errors": 0}

    # Company-Name → ID Mapping laden
    company_map: dict[str, str] = {}
    async with async_session_maker() as session:
        result = await session.execute(select(Company.name, Company.id))
        for name, company_id in result.all():
            company_map[name.lower().strip()] = str(company_id)

    logger.info(f"Company-Map geladen: {len(company_map)} Unternehmen")

    # Kontakte paginiert abrufen
    async for page_num, contacts, total in client.get_all_contacts_paginated(
        per_page=100, max_pages=max_pages
    ):
        logger.info(f"Seite {page_num}: {len(contacts)} Kontakte (geschaetzt {total} gesamt)")

        if dry_run:
            for crm_contact in contacts:
                mapped = client.map_to_contact_data(crm_contact)
                if mapped and (mapped.get("first_name") or mapped.get("last_name")):
                    company_name = mapped.pop("_company_name", None)
                    if company_name and company_name.lower().strip() in company_map:
                        stats["imported"] += 1
                        if stats["imported"] <= 5:
                            name = f"{mapped.get('first_name', '')} {mapped.get('last_name', '')}".strip()
                            logger.info(f"  [DRY] {name} @ {company_name}")
                    else:
                        stats["skipped_no_company"] += 1
                else:
                    stats["skipped_empty"] += 1
            continue

        # Batch-Insert
        async with async_session_maker() as session:
            batch = []
            for crm_contact in contacts:
                try:
                    mapped = client.map_to_contact_data(crm_contact)
                    if not mapped or (not mapped.get("first_name") and not mapped.get("last_name")):
                        stats["skipped_empty"] += 1
                        continue

                    # Company zuordnen
                    company_name = mapped.pop("_company_name", None)
                    if not company_name:
                        stats["skipped_no_company"] += 1
                        continue

                    company_id_str = company_map.get(company_name.lower().strip())
                    if not company_id_str:
                        stats["skipped_no_company"] += 1
                        continue

                    import uuid
                    mapped["company_id"] = uuid.UUID(company_id_str)

                    contact = CompanyContact(**mapped)
                    batch.append(contact)
                    stats["imported"] += 1

                except Exception as e:
                    stats["errors"] += 1
                    name = f"{crm_contact.get('first_name', '')} {crm_contact.get('last_name', '')}".strip()
                    logger.error(f"  Fehler bei '{name}': {e}")

            if batch:
                try:
                    session.add_all(batch)
                    await session.commit()
                except Exception as e:
                    await session.rollback()
                    logger.warning(f"  Batch-Insert fehlgeschlagen ({e}), versuche einzeln...")
                    for contact in batch:
                        try:
                            async with async_session_maker() as single_session:
                                single_session.add(contact)
                                await single_session.commit()
                        except Exception as e2:
                            stats["errors"] += 1
                            stats["imported"] -= 1
                            logger.debug(f"  Kontakt-Fehler: {e2}")

        if stats["imported"] % 500 == 0 and stats["imported"] > 0:
            logger.info(f"  Fortschritt: {stats['imported']} Kontakte importiert...")

    return stats


async def main():
    parser = argparse.ArgumentParser(description="CRM Unternehmen + Kontakte importieren")
    parser.add_argument("--dry-run", action="store_true", help="Nur simulieren, keine DB-Aenderungen")
    parser.add_argument("--max-pages", type=int, default=None, help="Maximale Seiten (fuer Tests)")
    parser.add_argument("--skip-contacts", action="store_true", help="Nur Unternehmen importieren")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("CRM Import: Unternehmen + Kontakte")
    logger.info(f"  Dry-Run: {args.dry_run}")
    logger.info(f"  Max Pages: {args.max_pages or 'alle'}")
    logger.info(f"  Skip Contacts: {args.skip_contacts}")
    logger.info("=" * 60)

    if not args.dry_run:
        logger.warning("ACHTUNG: Alle bestehenden Unternehmen werden GELOESCHT!")
        logger.warning("Starte in 5 Sekunden... (Ctrl+C zum Abbrechen)")
        await asyncio.sleep(5)

    async with RecruitCRMClient() as client:
        # 1. Unternehmen importieren
        logger.info("\n=== Phase 1: Unternehmen importieren ===")
        company_stats = await import_companies(
            client, dry_run=args.dry_run, max_pages=args.max_pages
        )
        logger.info(f"Unternehmen: {company_stats}")

        # 2. Kontakte importieren
        if not args.skip_contacts:
            logger.info("\n=== Phase 2: Kontakte importieren ===")
            contact_stats = await import_contacts(
                client, dry_run=args.dry_run, max_pages=args.max_pages
            )
            logger.info(f"Kontakte: {contact_stats}")

    logger.info("\n=== Import abgeschlossen ===")
    if args.dry_run:
        logger.info("(Dry-Run - keine Aenderungen vorgenommen)")


if __name__ == "__main__":
    asyncio.run(main())
