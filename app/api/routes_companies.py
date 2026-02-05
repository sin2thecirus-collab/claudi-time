"""Company Routes - API-Endpunkte fuer Unternehmensverwaltung."""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
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
        "phone": company.phone,
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


# ── Create Full (Company + Contact + Job) ────────────


class CompanyFullCreate(BaseModel):
    """Schema fuer Unternehmen + Kontakt + optionaler Job."""

    # Company
    name: str = Field(min_length=1, max_length=255)
    domain: str | None = None
    street: str | None = None
    house_number: str | None = None
    postal_code: str | None = None
    city: str | None = None
    phone: str | None = None

    # Contact
    contact_first_name: str | None = None
    contact_last_name: str | None = None
    contact_position: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None

    # Job (optional)
    job_title: str | None = None
    job_description: str | None = None
    job_requirements: str | None = None
    job_location_city: str | None = None
    job_employment_type: str | None = None
    job_priority: str | None = None
    job_source: str | None = None


@router.post("/create-full")
async def create_company_full(data: CompanyFullCreate, db: AsyncSession = Depends(get_db)):
    """Erstellt Unternehmen + Kontakt + optionalen ATS-Job in einem Request."""
    service = CompanyService(db)

    # 1. Company erstellen
    company_data = {
        k: v for k, v in {
            "name": data.name,
            "domain": data.domain,
            "street": data.street,
            "house_number": data.house_number,
            "postal_code": data.postal_code,
            "city": data.city,
            "phone": data.phone,
        }.items() if v is not None
    }
    company = await service.create_company(**company_data)

    # 2. Contact erstellen (wenn Daten vorhanden)
    contact_data = {
        k: v for k, v in {
            "first_name": data.contact_first_name,
            "last_name": data.contact_last_name,
            "position": data.contact_position,
            "phone": data.contact_phone,
            "email": data.contact_email,
        }.items() if v is not None
    }
    contact = None
    if any(contact_data.values()):
        contact = await service.add_contact(company.id, **contact_data)

    # 3. ATS-Job erstellen (wenn Titel vorhanden)
    ats_job = None
    job_error = None
    if data.job_title:
        try:
            from app.services.ats_job_service import ATSJobService
            ats_service = ATSJobService(db)
            job_data = {
                "company_id": company.id,
                "title": data.job_title,
                "description": data.job_description,
                "requirements": data.job_requirements,
                "location_city": data.job_location_city or data.city,
                "employment_type": data.job_employment_type,
                "priority": data.job_priority or "medium",
                "source": data.job_source or "Manuell",
            }
            if contact:
                job_data["contact_id"] = contact.id
            ats_job = await ats_service.create_job(**job_data)
        except Exception as e:
            logger.error(f"ATS-Job Erstellung fehlgeschlagen: {e}", exc_info=True)
            job_error = str(e)

    # 4. Regular Job erstellen (fuer /jobs + Zugehoerige Jobs auf Unternehmensseite)
    regular_job = None
    if data.job_title:
        try:
            from app.models.job import Job

            hash_input = f"manual|{data.name.strip().lower()}|{data.job_title.strip().lower()}|{datetime.now(timezone.utc).isoformat()}"
            content_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

            regular_job = Job(
                company_name=data.name.strip()[:255],
                company_id=company.id,
                position=data.job_title.strip()[:255],
                city=data.job_location_city or data.city,
                job_text=data.job_description,
                employment_type=data.job_employment_type,
                content_hash=content_hash,
            )
            db.add(regular_job)
            await db.flush()
        except Exception as e:
            logger.error(f"Regular Job Erstellung fehlgeschlagen: {e}", exc_info=True)
            if not job_error:
                job_error = str(e)

    await db.commit()

    message = "Unternehmen erfolgreich erstellt"
    if ats_job and regular_job:
        message += f" (inkl. Stelle '{ats_job.title}')"
    elif data.job_title and job_error:
        message += f" — Stelle konnte nicht erstellt werden: {job_error}"

    return {
        "company_id": str(company.id),
        "company_name": company.name,
        "contact_id": str(contact.id) if contact else None,
        "ats_job_id": str(ats_job.id) if ats_job else None,
        "job_id": str(regular_job.id) if regular_job else None,
        "redirect_url": f"/unternehmen/{company.id}",
        "message": message,
    }


@router.post("/extract-job-pdf")
async def extract_job_from_pdf(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    """Extrahiert Job-Daten aus einem PDF via PyMuPDF + GPT-4."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Nur PDF-Dateien erlaubt")

    # PDF lesen
    pdf_bytes = await file.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:  # Max 10MB
        raise HTTPException(status_code=400, detail="PDF zu gross (max 10MB)")

    # Text extrahieren mit PyMuPDF
    try:
        from app.services.cv_parser_service import CVParserService
        parser = CVParserService(db)
        raw_text = parser.extract_text_from_pdf(pdf_bytes)
    except Exception as e:
        logger.error(f"PDF-Extraktion fehlgeschlagen: {e}")
        raise HTTPException(status_code=400, detail=f"PDF konnte nicht gelesen werden: {e}")

    if not raw_text or len(raw_text.strip()) < 20:
        raise HTTPException(status_code=400, detail="PDF enthaelt keinen extrahierbaren Text")

    # Rohtext 1:1 zurueckgeben — kein GPT, kein Zusammenfassen
    return {
        "title": "",
        "description": raw_text.strip(),
        "requirements": "",
        "location_city": "",
        "employment_type": "",
    }


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
