"""Company Routes - API-Endpunkte fuer Unternehmensverwaltung."""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.company import (
    CompanyContactCreate,
    CompanyContactResponse,
    CompanyContactUpdate,
    CompanyCorrespondenceCreate,
    CompanyCorrespondenceResponse,
    CompanyCreate,
    CompanyResponse,
    CompanyUpdate,
)
from app.services.company_service import CompanyService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/companies", tags=["Companies"])


# ── Company CRUD ─────────────────────────────────────


@router.get("")
async def list_companies(
    search: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    sort_by: str = Query("created_at"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Listet alle Unternehmen mit Filtern."""
    service = CompanyService(db)
    result = await service.list_companies(
        search=search,
        city=city,
        status=status,
        sort_by=sort_by,
        page=page,
        per_page=per_page,
    )

    items = []
    for item in result["items"]:
        company = item["company"]
        items.append({
            "id": str(company.id),
            "name": company.name,
            "domain": company.domain,
            "city": company.city,
            "postal_code": company.postal_code,
            "status": company.status.value if company.status else "active",
            "employee_count": company.employee_count,
            "display_address": company.display_address,
            "created_at": company.created_at.isoformat() if company.created_at else None,
            "job_count": item["job_count"],
            "contact_count": item["contact_count"],
        })

    return {
        "items": items,
        "total": result["total"],
        "page": result["page"],
        "per_page": result["per_page"],
        "pages": result["pages"],
    }


@router.get("/stats")
async def company_stats(db: AsyncSession = Depends(get_db)):
    """Gibt Unternehmens-Statistiken zurueck."""
    service = CompanyService(db)
    return await service.get_stats()


@router.get("/{company_id}")
async def get_company(company_id: UUID, db: AsyncSession = Depends(get_db)):
    """Holt ein Unternehmen nach ID."""
    service = CompanyService(db)
    company = await service.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Unternehmen nicht gefunden")
    return {
        "id": str(company.id),
        "name": company.name,
        "domain": company.domain,
        "street": company.street,
        "house_number": company.house_number,
        "postal_code": company.postal_code,
        "city": company.city,
        "employee_count": company.employee_count,
        "status": company.status.value if company.status else "active",
        "notes": company.notes,
        "display_address": company.display_address,
        "created_at": company.created_at.isoformat() if company.created_at else None,
        "updated_at": company.updated_at.isoformat() if company.updated_at else None,
    }


@router.post("")
async def create_company(data: CompanyCreate, db: AsyncSession = Depends(get_db)):
    """Erstellt ein neues Unternehmen."""
    service = CompanyService(db)
    company = await service.create_company(**data.model_dump(exclude_unset=True))
    await db.commit()
    return {"id": str(company.id), "name": company.name, "message": "Unternehmen erstellt"}


@router.patch("/{company_id}")
async def update_company(
    company_id: UUID, data: CompanyUpdate, db: AsyncSession = Depends(get_db)
):
    """Aktualisiert ein Unternehmen."""
    service = CompanyService(db)
    company = await service.update_company(
        company_id, data.model_dump(exclude_unset=True)
    )
    if not company:
        raise HTTPException(status_code=404, detail="Unternehmen nicht gefunden")
    await db.commit()
    return {"message": "Unternehmen aktualisiert", "id": str(company.id)}


@router.delete("/{company_id}")
async def delete_company(company_id: UUID, db: AsyncSession = Depends(get_db)):
    """Loescht ein Unternehmen."""
    service = CompanyService(db)
    deleted = await service.delete_company(company_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Unternehmen nicht gefunden")
    await db.commit()
    return {"message": "Unternehmen geloescht"}


@router.put("/{company_id}/status")
async def set_company_status(
    company_id: UUID,
    status: str = Query(..., description="active/blacklist/laufende_prozesse"),
    db: AsyncSession = Depends(get_db),
):
    """Setzt den Status eines Unternehmens."""
    if status not in ("active", "blacklist", "laufende_prozesse"):
        raise HTTPException(status_code=400, detail="Ungueltiger Status")
    service = CompanyService(db)
    company = await service.set_status(company_id, status)
    if not company:
        raise HTTPException(status_code=404, detail="Unternehmen nicht gefunden")
    await db.commit()
    return {"message": f"Status auf '{status}' gesetzt", "id": str(company.id)}


# ── Contacts ─────────────────────────────────────────


@router.get("/{company_id}/contacts")
async def list_contacts(company_id: UUID, db: AsyncSession = Depends(get_db)):
    """Listet Kontakte eines Unternehmens."""
    service = CompanyService(db)
    contacts = await service.list_contacts(company_id)
    return [
        {
            "id": str(c.id),
            "company_id": str(c.company_id),
            "salutation": c.salutation,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "full_name": c.full_name,
            "position": c.position,
            "email": c.email,
            "phone": c.phone,
            "notes": c.notes,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in contacts
    ]


@router.post("/{company_id}/contacts")
async def add_contact(
    company_id: UUID, data: CompanyContactCreate, db: AsyncSession = Depends(get_db)
):
    """Fuegt einen Kontakt hinzu."""
    service = CompanyService(db)
    contact = await service.add_contact(
        company_id, **data.model_dump(exclude_unset=True)
    )
    await db.commit()
    return {"id": str(contact.id), "full_name": contact.full_name, "message": "Kontakt erstellt"}


@router.patch("/contacts/{contact_id}")
async def update_contact(
    contact_id: UUID, data: CompanyContactUpdate, db: AsyncSession = Depends(get_db)
):
    """Aktualisiert einen Kontakt."""
    service = CompanyService(db)
    contact = await service.update_contact(
        contact_id, data.model_dump(exclude_unset=True)
    )
    if not contact:
        raise HTTPException(status_code=404, detail="Kontakt nicht gefunden")
    await db.commit()
    return {"message": "Kontakt aktualisiert", "id": str(contact.id)}


@router.delete("/contacts/{contact_id}")
async def delete_contact(contact_id: UUID, db: AsyncSession = Depends(get_db)):
    """Loescht einen Kontakt."""
    service = CompanyService(db)
    deleted = await service.delete_contact(contact_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Kontakt nicht gefunden")
    await db.commit()
    return {"message": "Kontakt geloescht"}


# ── Correspondence ───────────────────────────────────


@router.get("/{company_id}/correspondence")
async def list_correspondence(company_id: UUID, db: AsyncSession = Depends(get_db)):
    """Listet Korrespondenz eines Unternehmens."""
    service = CompanyService(db)
    corrs = await service.list_correspondence(company_id)
    return [
        {
            "id": str(c.id),
            "company_id": str(c.company_id),
            "contact_id": str(c.contact_id) if c.contact_id else None,
            "direction": c.direction.value if c.direction else "outbound",
            "subject": c.subject,
            "body": c.body,
            "sent_at": c.sent_at.isoformat() if c.sent_at else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in corrs
    ]


@router.post("/{company_id}/correspondence")
async def add_correspondence(
    company_id: UUID,
    data: CompanyCorrespondenceCreate,
    db: AsyncSession = Depends(get_db),
):
    """Fuegt eine Korrespondenz hinzu."""
    service = CompanyService(db)
    corr = await service.add_correspondence(
        company_id,
        **data.model_dump(exclude_unset=True),
    )
    await db.commit()
    return {"id": str(corr.id), "message": "Korrespondenz erstellt"}


@router.delete("/correspondence/{correspondence_id}")
async def delete_correspondence(
    correspondence_id: UUID, db: AsyncSession = Depends(get_db)
):
    """Loescht eine Korrespondenz."""
    service = CompanyService(db)
    deleted = await service.delete_correspondence(correspondence_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Korrespondenz nicht gefunden")
    await db.commit()
    return {"message": "Korrespondenz geloescht"}
