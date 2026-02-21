"""
Global CRM Search Service — sucht quer ueber alle Entitaeten.
"""
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession


class SearchService:
    """Durchsucht Kandidaten, Unternehmen, Kontakte, Jobs und Stellen."""

    async def global_search(self, db: AsyncSession, query: str, limit: int = 5) -> dict:
        term = f"%{query}%"
        results = {}

        # Kandidaten
        from app.models.candidate import Candidate
        cand_q = (
            select(Candidate)
            .where(
                Candidate.deleted_at.is_(None),
                Candidate.hidden.is_(False),
                or_(
                    Candidate.first_name.ilike(term),
                    Candidate.last_name.ilike(term),
                    (Candidate.first_name + " " + Candidate.last_name).ilike(term),
                    Candidate.email.ilike(term),
                ),
            )
            .order_by(Candidate.last_name, Candidate.first_name)
            .limit(limit)
        )
        cand_result = await db.execute(cand_q)
        candidates = cand_result.scalars().all()
        if candidates:
            results["candidates"] = [
                {
                    "id": str(c.id),
                    "name": f"{c.first_name or ''} {c.last_name or ''}".strip() or "Unbekannt",
                    "position": c.current_position,
                    "city": c.city,
                }
                for c in candidates
            ]

        # Unternehmen
        from app.models.company import Company
        comp_q = (
            select(Company)
            .where(
                Company.deleted_at.is_(None),
                or_(
                    Company.name.ilike(term),
                    Company.city.ilike(term),
                    Company.domain.ilike(term),
                ),
            )
            .order_by(Company.name)
            .limit(limit)
        )
        comp_result = await db.execute(comp_q)
        companies = comp_result.scalars().all()
        if companies:
            results["companies"] = [
                {
                    "id": str(c.id),
                    "name": c.name or "Unbekannt",
                    "city": c.city,
                }
                for c in companies
            ]

        # Kontakte
        from app.models.company_contact import CompanyContact
        cont_q = (
            select(CompanyContact)
            .where(
                or_(
                    CompanyContact.first_name.ilike(term),
                    CompanyContact.last_name.ilike(term),
                    (CompanyContact.first_name + " " + CompanyContact.last_name).ilike(term),
                    CompanyContact.email.ilike(term),
                ),
            )
            .order_by(CompanyContact.last_name, CompanyContact.first_name)
            .limit(limit)
        )
        cont_result = await db.execute(cont_q)
        contacts = cont_result.scalars().all()
        if contacts:
            results["contacts"] = [
                {
                    "id": str(c.id),
                    "company_id": str(c.company_id),
                    "name": f"{c.first_name or ''} {c.last_name or ''}".strip() or "Unbekannt",
                    "position": c.position,
                    "company": "",
                }
                for c in contacts
            ]

        # Jobs
        from app.models.job import Job
        job_q = (
            select(Job)
            .where(
                Job.deleted_at.is_(None),
                or_(
                    Job.position.ilike(term),
                    Job.company_name.ilike(term),
                    Job.city.ilike(term),
                ),
            )
            .order_by(Job.created_at.desc())
            .limit(limit)
        )
        job_result = await db.execute(job_q)
        jobs = job_result.scalars().all()
        if jobs:
            results["jobs"] = [
                {
                    "id": str(j.id),
                    "position": j.position or "Unbekannt",
                    "company": j.company_name,
                    "city": j.city,
                }
                for j in jobs
            ]

        # Stellen (ATSJob)
        from app.models.ats_job import ATSJob
        stelle_q = (
            select(ATSJob)
            .where(
                or_(
                    ATSJob.title.ilike(term),
                    ATSJob.location_city.ilike(term),
                ),
            )
            .order_by(ATSJob.created_at.desc())
            .limit(limit)
        )
        stelle_result = await db.execute(stelle_q)
        stellen = stelle_result.scalars().all()
        if stellen:
            results["stellen"] = [
                {
                    "id": str(s.id),
                    "title": s.title or "Unbekannt",
                    "city": s.location_city,
                }
                for s in stellen
            ]

        return results
