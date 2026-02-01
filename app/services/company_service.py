"""Company Service fuer das Matching-Tool."""

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.company import Company, CompanyStatus
from app.models.company_contact import CompanyContact
from app.models.company_correspondence import CompanyCorrespondence

logger = logging.getLogger(__name__)


class CompanyService:
    """Service fuer Unternehmensverwaltung."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Company CRUD ─────────────────────────────────

    async def create_company(self, name: str, **kwargs) -> Company:
        """Erstellt ein neues Unternehmen."""
        company = Company(name=name.strip(), **kwargs)
        self.db.add(company)
        await self.db.flush()
        logger.info(f"Company erstellt: {company.id} - {company.name}")
        return company

    async def get_company(self, company_id: UUID) -> Company | None:
        """Holt ein Unternehmen nach ID mit Relationships."""
        result = await self.db.execute(
            select(Company)
            .options(
                selectinload(Company.contacts),
                selectinload(Company.correspondence),
            )
            .where(Company.id == company_id)
        )
        return result.scalar_one_or_none()

    async def update_company(self, company_id: UUID, data: dict) -> Company | None:
        """Aktualisiert ein Unternehmen."""
        company = await self.db.get(Company, company_id)
        if not company:
            return None
        for key, value in data.items():
            if value is not None and hasattr(company, key):
                setattr(company, key, value)
        await self.db.flush()
        logger.info(f"Company aktualisiert: {company.id} - {company.name}")
        return company

    async def delete_company(self, company_id: UUID) -> bool:
        """Loescht ein Unternehmen."""
        company = await self.db.get(Company, company_id)
        if not company:
            return False
        await self.db.delete(company)
        await self.db.flush()
        logger.info(f"Company geloescht: {company_id}")
        return True

    async def set_status(self, company_id: UUID, status: str) -> Company | None:
        """Setzt den Status eines Unternehmens."""
        company = await self.db.get(Company, company_id)
        if not company:
            return None
        company.status = CompanyStatus(status)
        await self.db.flush()
        logger.info(f"Company Status: {company.name} -> {status}")
        return company

    async def list_companies(
        self,
        search: str | None = None,
        city: str | None = None,
        status: str | None = None,
        sort_by: str = "created_at",
        page: int = 1,
        per_page: int = 25,
    ) -> dict:
        """Listet Unternehmen mit Filter und Pagination."""
        from app.models.job import Job

        query = select(Company)
        count_query = select(func.count(Company.id))

        # Filter
        if search:
            search_term = f"%{search}%"
            query = query.where(
                Company.name.ilike(search_term) | Company.city.ilike(search_term)
            )
            count_query = count_query.where(
                Company.name.ilike(search_term) | Company.city.ilike(search_term)
            )

        if city:
            query = query.where(Company.city.ilike(f"%{city}%"))
            count_query = count_query.where(Company.city.ilike(f"%{city}%"))

        if status:
            query = query.where(Company.status == CompanyStatus(status))
            count_query = count_query.where(Company.status == CompanyStatus(status))

        # Sortierung
        if sort_by == "name":
            query = query.order_by(Company.name.asc())
        else:
            query = query.order_by(Company.created_at.desc())

        # Total Count
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # Pagination
        offset = (page - 1) * per_page
        query = query.offset(offset).limit(per_page)

        result = await self.db.execute(query)
        companies = result.scalars().all()

        # Job-Counts und Contact-Counts per Company
        items = []
        for company in companies:
            # Job Count
            job_count_result = await self.db.execute(
                select(func.count(Job.id)).where(
                    Job.company_id == company.id,
                    Job.deleted_at.is_(None),
                )
            )
            job_count = job_count_result.scalar() or 0

            # Contact Count
            contact_count_result = await self.db.execute(
                select(func.count(CompanyContact.id)).where(
                    CompanyContact.company_id == company.id
                )
            )
            contact_count = contact_count_result.scalar() or 0

            items.append({
                "company": company,
                "job_count": job_count,
                "contact_count": contact_count,
            })

        pages = (total + per_page - 1) // per_page if per_page > 0 else 0

        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }

    # ── Lookup / Auto-Create (fuer CSV Import) ──────

    async def get_or_create_by_name(self, name: str, **extra_fields) -> Company | None:
        """
        Sucht ein Unternehmen nach Name oder erstellt es.
        Gibt None zurueck wenn das Unternehmen auf der Blacklist steht.
        """
        normalized = name.strip()
        if not normalized:
            return None

        # Case-insensitive Suche
        result = await self.db.execute(
            select(Company).where(func.lower(Company.name) == normalized.lower())
        )
        company = result.scalar_one_or_none()

        if company:
            # Blacklist check
            if company.status == CompanyStatus.BLACKLIST:
                return None
            return company

        # Neue Company erstellen — nur nicht-leere Felder uebernehmen
        clean_fields = {k: v for k, v in extra_fields.items() if v and str(v).strip()}
        company = Company(name=normalized, **clean_fields)
        self.db.add(company)
        await self.db.flush()
        logger.info(f"Auto-created Company: {company.name}")
        return company

    async def is_blacklisted(self, company_name: str) -> bool:
        """Prueft ob ein Unternehmen auf der Blacklist steht."""
        result = await self.db.execute(
            select(Company.status).where(
                func.lower(Company.name) == company_name.strip().lower()
            )
        )
        row = result.scalar_one_or_none()
        return row == CompanyStatus.BLACKLIST if row else False

    # ── Contact CRUD ─────────────────────────────────

    async def add_contact(self, company_id: UUID, **kwargs) -> CompanyContact:
        """Fuegt einen Kontakt hinzu."""
        contact = CompanyContact(company_id=company_id, **kwargs)
        self.db.add(contact)
        await self.db.flush()
        logger.info(f"Contact erstellt: {contact.full_name} bei Company {company_id}")
        return contact

    async def get_or_create_contact(
        self, company_id: UUID, first_name: str | None, last_name: str | None, **kwargs
    ) -> CompanyContact:
        """Sucht oder erstellt einen Kontakt (fuer CSV Import)."""
        # Suche nach Name bei gleicher Company
        query = select(CompanyContact).where(CompanyContact.company_id == company_id)
        if first_name:
            query = query.where(func.lower(CompanyContact.first_name) == first_name.strip().lower())
        if last_name:
            query = query.where(func.lower(CompanyContact.last_name) == last_name.strip().lower())

        result = await self.db.execute(query)
        contact = result.scalar_one_or_none()

        if contact:
            return contact

        return await self.add_contact(
            company_id=company_id,
            first_name=first_name.strip() if first_name else None,
            last_name=last_name.strip() if last_name else None,
            **{k: v for k, v in kwargs.items() if v and str(v).strip()},
        )

    async def update_contact(self, contact_id: UUID, data: dict) -> CompanyContact | None:
        """Aktualisiert einen Kontakt."""
        contact = await self.db.get(CompanyContact, contact_id)
        if not contact:
            return None
        for key, value in data.items():
            if hasattr(contact, key):
                setattr(contact, key, value)
        await self.db.flush()
        return contact

    async def delete_contact(self, contact_id: UUID) -> bool:
        """Loescht einen Kontakt."""
        contact = await self.db.get(CompanyContact, contact_id)
        if not contact:
            return False
        await self.db.delete(contact)
        await self.db.flush()
        return True

    async def list_contacts(self, company_id: UUID) -> list[CompanyContact]:
        """Listet alle Kontakte eines Unternehmens."""
        result = await self.db.execute(
            select(CompanyContact)
            .where(CompanyContact.company_id == company_id)
            .order_by(CompanyContact.last_name.asc())
        )
        return list(result.scalars().all())

    # ── Correspondence ───────────────────────────────

    async def add_correspondence(self, company_id: UUID, **kwargs) -> CompanyCorrespondence:
        """Fuegt eine Korrespondenz hinzu."""
        corr = CompanyCorrespondence(company_id=company_id, **kwargs)
        self.db.add(corr)
        await self.db.flush()
        return corr

    async def list_correspondence(self, company_id: UUID) -> list[CompanyCorrespondence]:
        """Listet Korrespondenz eines Unternehmens (neueste zuerst)."""
        result = await self.db.execute(
            select(CompanyCorrespondence)
            .where(CompanyCorrespondence.company_id == company_id)
            .order_by(CompanyCorrespondence.sent_at.desc())
        )
        return list(result.scalars().all())

    async def delete_correspondence(self, correspondence_id: UUID) -> bool:
        """Loescht eine Korrespondenz."""
        corr = await self.db.get(CompanyCorrespondence, correspondence_id)
        if not corr:
            return False
        await self.db.delete(corr)
        await self.db.flush()
        return True

    # ── Statistics ────────────────────────────────────

    async def get_stats(self) -> dict:
        """Gibt Gesamtstatistiken zurueck."""
        total = await self.db.execute(select(func.count(Company.id)))
        active = await self.db.execute(
            select(func.count(Company.id)).where(Company.status == CompanyStatus.ACTIVE)
        )
        blacklisted = await self.db.execute(
            select(func.count(Company.id)).where(Company.status == CompanyStatus.BLACKLIST)
        )
        laufende = await self.db.execute(
            select(func.count(Company.id)).where(
                Company.status == CompanyStatus.LAUFENDE_PROZESSE
            )
        )
        return {
            "total": total.scalar() or 0,
            "active": active.scalar() or 0,
            "blacklisted": blacklisted.scalar() or 0,
            "laufende_prozesse": laufende.scalar() or 0,
        }
