"""Company Routes - API-Endpunkte fuer Unternehmensverwaltung."""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete, func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.company_contact import CompanyContact
from app.models.company_document import CompanyDocument
from app.models.company_note import CompanyNote
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
            "address": company.address,
            "city": company.city,
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


# ── CRM Import Endpoints (TEMPORAER — nach Import loeschen!) ──

@router.post("/crm-import-trigger")
async def trigger_csv_import(
    background_tasks: BackgroundTasks,
):
    """Startet CSV-Import als Background-Task."""
    if _import_status.get("running"):
        return JSONResponse(status_code=409, content={"error": "Import laeuft bereits"})

    background_tasks.add_task(_run_csv_import)
    return {"message": "CSV-Import gestartet", "status_url": "/api/companies/crm-import-status"}


@router.get("/crm-import-status")
async def get_import_status():
    """Gibt den aktuellen Import-Status zurueck."""
    all_logs = _import_status.get("log", [])
    return {
        "running": _import_status.get("running", False),
        "stats": _import_status.get("stats", {}),
        "log_count": len(all_logs),
        "first_logs": all_logs[:10],
        "last_logs": all_logs[-20:],
    }


@router.get("/search/json")
async def search_companies_json(
    q: str = Query(default="", min_length=1),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """JSON-Unternehmenssuche fuer Quick-Add Modals."""
    if not q or len(q.strip()) < 1:
        return []

    search_term = f"%{q.strip()}%"
    result = await db.execute(
        select(Company)
        .where(Company.name.ilike(search_term))
        .order_by(Company.name)
        .limit(limit)
    )
    companies = result.scalars().all()

    return [
        {
            "id": str(c.id),
            "name": c.name,
            "city": c.city,
            "domain": c.domain,
        }
        for c in companies
    ]


@router.get("/search", response_class=HTMLResponse)
async def search_companies_quick(
    q: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Schnelle Unternehmenssuche fuer Autocomplete."""
    if not q or len(q.strip()) < 1:
        return HTMLResponse("")

    search_term = f"%{q.strip()}%"
    result = await db.execute(
        select(Company)
        .where(Company.name.ilike(search_term))
        .order_by(Company.name)
        .limit(limit)
    )
    companies = result.scalars().all()

    if not companies:
        return HTMLResponse("<div class='p-2 text-sm text-gray-500'>Keine Unternehmen gefunden</div>")

    html = '<div class="border border-gray-200 rounded-lg shadow-sm bg-white max-h-48 overflow-y-auto">'
    for c in companies:
        safe_name = str(c.name).replace("'", "&#39;").replace('"', '&quot;')
        city_span = "<span class='text-gray-400 ml-2'>" + str(c.city) + "</span>" if c.city else ""
        company_id = str(c.id)
        html += '<div class="px-3 py-2 hover:bg-gray-100 cursor-pointer text-sm" onclick="selectCompany(' + "'" + company_id + "', '" + safe_name + "')" + '"><span class="font-medium">' + str(c.name) + '</span>' + city_span + '</div>'
    html += '</div>'
    return HTMLResponse(html)


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
        "address": company.address,
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
    address: str | None = None
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
            "address": data.address,
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

    pdf_bytes = await file.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="PDF zu gross (max 10MB)")

    try:
        from app.services.cv_parser_service import CVParserService
        parser = CVParserService(db)
        raw_text = parser.extract_text_from_pdf(pdf_bytes)
    except Exception as e:
        logger.error(f"PDF-Extraktion fehlgeschlagen: {e}")
        raise HTTPException(status_code=400, detail=f"PDF konnte nicht gelesen werden: {e}")

    if not raw_text or len(raw_text.strip()) < 20:
        raise HTTPException(status_code=400, detail="PDF enthaelt keinen extrahierbaren Text")

    return {
        "title": "",
        "description": raw_text.strip(),
        "requirements": "",
        "location_city": "",
        "employment_type": "",
    }


# ── Contacts ─────────────────────────────────────────


@router.get("/contacts/{contact_id}")
async def get_contact(contact_id: UUID, db: AsyncSession = Depends(get_db)):
    """Holt einen einzelnen Kontakt mit Company-Daten."""
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(CompanyContact)
        .options(selectinload(CompanyContact.company))
        .where(CompanyContact.id == contact_id)
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Kontakt nicht gefunden")
    return {
        "id": str(contact.id),
        "company_id": str(contact.company_id),
        "salutation": contact.salutation,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "full_name": contact.full_name,
        "position": contact.position,
        "email": contact.email,
        "phone": contact.phone,
        "mobile": contact.mobile,
        "city": contact.city,
        "notes": contact.notes,
        "created_at": contact.created_at.isoformat() if contact.created_at else None,
        "updated_at": contact.updated_at.isoformat() if contact.updated_at else None,
        "company_name": contact.company.name if contact.company else None,
        "company_address": contact.company.display_address if contact.company else None,
        "company_phone": contact.company.phone if contact.company else None,
    }


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
            "mobile": c.mobile,
            "city": c.city,
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


# ── Documents ────────────────────────────────────────


@router.get("/{company_id}/documents")
async def list_documents(company_id: UUID, db: AsyncSession = Depends(get_db)):
    """Listet Dokumente eines Unternehmens."""
    result = await db.execute(
        select(CompanyDocument)
        .where(CompanyDocument.company_id == company_id)
        .order_by(CompanyDocument.created_at.desc())
    )
    docs = result.scalars().all()
    return [
        {
            "id": str(d.id),
            "company_id": str(d.company_id),
            "filename": d.filename,
            "file_path": d.file_path,
            "file_size": d.file_size,
            "mime_type": d.mime_type,
            "notes": d.notes,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in docs
    ]


@router.post("/{company_id}/documents")
async def upload_document(
    company_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Laedt ein Dokument zu einem Unternehmen hoch (R2 Storage)."""
    company = await db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Unternehmen nicht gefunden")

    file_bytes = await file.read()
    if len(file_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Datei zu gross (max 20MB)")

    filename = file.filename or "dokument"
    mime_type = file.content_type or "application/octet-stream"
    short_id = str(company.id)[:8]
    r2_key = f"documents/company_{short_id}/{filename}"

    try:
        from app.services.r2_storage_service import R2StorageService
        r2 = R2StorageService()
        r2.upload_file(r2_key, file_bytes, content_type=mime_type)
    except Exception as e:
        logger.error(f"R2 Upload fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=f"Upload fehlgeschlagen: {e}")

    doc = CompanyDocument(
        company_id=company_id,
        filename=filename,
        file_path=r2_key,
        file_size=len(file_bytes),
        mime_type=mime_type,
    )
    db.add(doc)
    await db.commit()

    return {
        "id": str(doc.id),
        "filename": doc.filename,
        "file_size": doc.file_size,
        "message": "Dokument hochgeladen",
    }


@router.delete("/documents/{document_id}")
async def delete_document(document_id: UUID, db: AsyncSession = Depends(get_db)):
    """Loescht ein Dokument (DB + R2)."""
    doc = await db.get(CompanyDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden")

    try:
        from app.services.r2_storage_service import R2StorageService
        r2 = R2StorageService()
        r2.delete_file(doc.file_path)
    except Exception as e:
        logger.warning(f"R2 Cleanup fehlgeschlagen: {e}")

    await db.delete(doc)
    await db.commit()
    return {"message": "Dokument geloescht"}


# ══════════════════════════════════════════════════════════════
#  Company Notes (Notizen-Verlauf)
# ══════════════════════════════════════════════════════════════


class CompanyNoteCreate(BaseModel):
    """Schema fuer neue Notiz."""

    content: str = Field(..., min_length=1)
    title: str | None = None
    contact_id: UUID | None = None


@router.get("/{company_id}/notes")
async def list_company_notes(company_id: UUID, db: AsyncSession = Depends(get_db)):
    """Listet alle Notizen eines Unternehmens (neueste zuerst)."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(CompanyNote)
        .options(selectinload(CompanyNote.contact))
        .where(CompanyNote.company_id == company_id)
        .order_by(CompanyNote.created_at.desc())
    )
    notes = result.scalars().all()
    return [
        {
            "id": str(n.id),
            "company_id": str(n.company_id),
            "contact_id": str(n.contact_id) if n.contact_id else None,
            "contact_name": n.contact.full_name if n.contact else None,
            "title": n.title,
            "content": n.content,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in notes
    ]


@router.post("/{company_id}/notes")
async def create_company_note(
    company_id: UUID,
    data: CompanyNoteCreate,
    db: AsyncSession = Depends(get_db),
):
    """Erstellt eine neue Notiz fuer ein Unternehmen."""
    company = await db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Unternehmen nicht gefunden")

    note = CompanyNote(
        company_id=company_id,
        contact_id=data.contact_id,
        title=data.title,
        content=data.content,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)

    return {
        "id": str(note.id),
        "company_id": str(note.company_id),
        "title": note.title,
        "content": note.content,
        "created_at": note.created_at.isoformat() if note.created_at else None,
        "message": "Notiz erstellt",
    }


@router.delete("/notes/{note_id}")
async def delete_company_note(note_id: UUID, db: AsyncSession = Depends(get_db)):
    """Loescht eine Notiz."""
    note = await db.get(CompanyNote, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Notiz nicht gefunden")

    await db.delete(note)
    await db.commit()
    return {"message": "Notiz geloescht"}


# ══════════════════════════════════════════════════════════════
#  TEMPORAERER CSV-Import Endpoint (nach Import loeschen!)
# ══════════════════════════════════════════════════════════════

import asyncio
import csv
import os
import uuid as uuid_mod

# Global import status
_import_status: dict[str, Any] = {"running": False, "log": [], "stats": {}}


async def _run_csv_import():
    """Background-Task fuer CSV Import aus scripts/crm-data/."""
    global _import_status
    _import_status = {"running": True, "log": [], "stats": {}}

    def log(msg: str):
        logger.info(f"[CSV-Import] {msg}")
        _import_status["log"].append(msg)

    try:
        from app.database import async_session_maker

        # CSV-Dateien finden
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        csv_dir = os.path.join(base_dir, "scripts", "crm-data")
        company_csv = os.path.join(csv_dir, "company_data.csv")
        contact_csv = os.path.join(csv_dir, "contact_data.csv")

        if not os.path.exists(company_csv) or not os.path.exists(contact_csv):
            log(f"FEHLER: CSV-Dateien nicht gefunden in {csv_dir}")
            return

        slug_to_company_id: dict[str, str] = {}

        # ── Phase 1: Unternehmen aus CSV ──
        log("=== Phase 1: Unternehmen aus CSV importieren ===")
        with open(company_csv, "r", encoding="utf-8") as f:
            companies_raw = list(csv.DictReader(f))
        log(f"CSV: {len(companies_raw)} Unternehmen gelesen")

        # Clean Slate
        async with async_session_maker() as session:
            count_result = await session.execute(select(sa_func.count(Company.id)))
            existing = count_result.scalar() or 0
            if existing > 0:
                log(f"Loesche {existing} bestehende Unternehmen...")
                await session.execute(sa_delete(Company))
                await session.commit()
                log("Geloescht.")

        company_stats = {"imported": 0, "skipped": 0, "duplicates": 0, "errors": 0}
        seen_names: set[str] = set()
        batch: list = []

        for row in companies_raw:
            name = (row.get("Company") or "").strip()
            if not name:
                company_stats["skipped"] += 1
                continue

            name_lower = name.lower()
            slug = (row.get("Slug") or "").strip()

            if name_lower in seen_names:
                company_stats["duplicates"] += 1
                continue
            seen_names.add(name_lower)

            domain = (row.get("Website") or "").strip()
            if domain:
                domain = domain.replace("https://", "").replace("http://", "").rstrip("/")

            address = (row.get("Full Address") or "").strip() or None
            city = (row.get("City") or "").strip() or None
            phone = (row.get("Telefonzentrale") or "").strip() or None

            company_id = uuid_mod.uuid4()
            company = Company(
                id=company_id, name=name, domain=domain or None,
                address=address, city=city, phone=phone, status="active",
            )
            batch.append(company)
            if slug:
                slug_to_company_id[slug] = str(company_id)
            company_stats["imported"] += 1

            if len(batch) >= 100:
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
                                company_stats["errors"] += 1
                                company_stats["imported"] -= 1
                batch = []
                _import_status["stats"]["companies"] = dict(company_stats)
                if company_stats["imported"] % 1000 == 0:
                    log(f"  Fortschritt: {company_stats['imported']} Unternehmen...")

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
                            company_stats["errors"] += 1
                            company_stats["imported"] -= 1

        # Duplikat-Slugs aufloesen
        async with async_session_maker() as session:
            for row in companies_raw:
                slug = (row.get("Slug") or "").strip()
                if slug and slug not in slug_to_company_id:
                    name = (row.get("Company") or "").strip()
                    if name:
                        res = await session.execute(select(Company.id).where(Company.name == name).limit(1))
                        found = res.scalar_one_or_none()
                        if found:
                            slug_to_company_id[slug] = str(found)

        log(f"Unternehmen fertig: {company_stats}")
        log(f"Slug-Map: {len(slug_to_company_id)} Zuordnungen")
        _import_status["stats"]["companies"] = dict(company_stats)

        # ── Phase 2: Kontakte aus CSV ──
        log("=== Phase 2: Kontakte aus CSV importieren ===")
        with open(contact_csv, "r", encoding="utf-8") as f:
            contacts_raw = list(csv.DictReader(f))
        log(f"CSV: {len(contacts_raw)} Kontakte gelesen")

        contact_stats = {"imported": 0, "skipped_empty": 0, "skipped_no_company": 0, "errors": 0}
        batch = []

        for row in contacts_raw:
            first_name = (row.get("First Name") or "").strip() or None
            last_name = (row.get("Last Name") or "").strip() or None

            if not first_name and not last_name:
                contact_stats["skipped_empty"] += 1
                continue

            company_slug = (row.get("Company Slug") or "").strip()
            if not company_slug or company_slug not in slug_to_company_id:
                contact_stats["skipped_no_company"] += 1
                continue

            cid_str = slug_to_company_id[company_slug]
            salutation = (row.get("Anrede") or "").strip() or None
            position = (row.get("Designation") or "").strip() or None
            email = (row.get("Email") or "").strip() or None
            phone = (row.get("Contact Number") or "").strip() or None
            mobile = (row.get("Mobil") or "").strip() or None
            city = (row.get("City") or "").strip() or None

            contact = CompanyContact(
                id=uuid_mod.uuid4(),
                company_id=uuid_mod.UUID(cid_str),
                salutation=salutation, first_name=first_name, last_name=last_name,
                position=position, email=email, phone=phone, mobile=mobile, city=city,
            )
            batch.append(contact)
            contact_stats["imported"] += 1

            if len(batch) >= 100:
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
                                contact_stats["errors"] += 1
                                contact_stats["imported"] -= 1
                batch = []
                _import_status["stats"]["contacts"] = dict(contact_stats)
                if contact_stats["imported"] % 1000 == 0:
                    log(f"  Fortschritt: {contact_stats['imported']} Kontakte...")

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
                            contact_stats["errors"] += 1
                            contact_stats["imported"] -= 1

        log(f"Kontakte fertig: {contact_stats}")
        _import_status["stats"]["contacts"] = dict(contact_stats)

        log("=== IMPORT ABGESCHLOSSEN ===")

    except Exception as e:
        log(f"FEHLER: {e}")
        import traceback
        log(traceback.format_exc())
    finally:
        _import_status["running"] = False


# (Import endpoints moved above /{company_id} to avoid route conflict)
