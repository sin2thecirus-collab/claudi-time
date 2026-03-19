"""API-Routen fuer ATS-Pipeline (Kanban)."""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.company import Company, CompanyStatus
from app.models.company_contact import CompanyContact
from app.models.match import Match
from app.services.ats_pipeline_service import ATSPipelineService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ats/pipeline", tags=["ATS Pipeline"])


# ── Pydantic Schemas ─────────────────────────────

class AddCandidateRequest(BaseModel):
    candidate_id: UUID
    stage: Optional[str] = "sent"
    notes: Optional[str] = None


class MoveStageRequest(BaseModel):
    stage: str


class ReorderRequest(BaseModel):
    sort_order: int


class BulkMoveRequest(BaseModel):
    entry_ids: list[UUID]
    stage: str


class ScheduleInterviewRequest(BaseModel):
    interview_at: datetime
    interview_type: str  # "vor_ort" / "digital"
    interview_location: Optional[str] = None
    interview_hint: Optional[str] = None
    interview_participants: Optional[list[dict]] = None
    interview_invite_by: str  # "recruiter" / "kunde"


class ParseJobTextRequest(BaseModel):
    raw_text: str


class CreateFromRawtextRequest(BaseModel):
    # Company
    company_name: str
    company_address: Optional[str] = None
    company_postal_code: Optional[str] = None
    company_city: Optional[str] = None
    company_domain: Optional[str] = None
    company_phone: Optional[str] = None
    use_existing_company_id: Optional[UUID] = None
    # Contact
    contact_salutation: Optional[str] = None
    contact_first_name: Optional[str] = None
    contact_last_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_position: Optional[str] = None
    use_existing_contact_id: Optional[UUID] = None
    # Job
    job_title: str
    job_description: Optional[str] = None
    job_requirements: Optional[str] = None
    job_location_city: Optional[str] = None
    job_employment_type: Optional[str] = None
    job_salary_min: Optional[int] = None
    job_salary_max: Optional[int] = None
    job_priority: Optional[str] = "medium"


# ── GPT-4o Prompt fuer Stellentext-Extraktion ─────

RAWTEXT_EXTRACTION_PROMPT = """Du bist ein Experte fuer die Analyse von Stellenanzeigen und Unternehmenstexten.
Extrahiere strukturierte Daten aus dem folgenden Text.

Antworte NUR mit validem JSON. Kein Markdown, kein erklaerenter Text, keine Code-Bloecke.

JSON-Schema:
{
  "company_name": "Firmenname oder null",
  "company_address": "Strasse + Hausnummer oder null",
  "company_postal_code": "PLZ (5-stellig) oder null",
  "company_city": "Stadt oder null",
  "company_domain": "Website-Domain ohne https:// oder null",
  "company_phone": "Telefonnummer der Firma oder null",
  "job_title": "Stellentitel oder null",
  "job_description": "Aufgabenbeschreibung — formatiert mit \\n fuer Absaetze und \\n- fuer Aufzaehlungspunkte. Struktur und Gliederung des Originals beibehalten!",
  "job_requirements": "Anforderungen — formatiert mit \\n fuer Absaetze und \\n- fuer Aufzaehlungspunkte. Struktur und Gliederung des Originals beibehalten!",
  "salary_info": "Gehaltsangabe als Text oder null",
  "employment_type": "vollzeit oder teilzeit oder befristet oder freelance oder null",
  "contact_salutation": "Herr oder Frau oder null",
  "contact_first_name": "Vorname des Ansprechpartners oder null",
  "contact_last_name": "Nachname des Ansprechpartners oder null",
  "contact_email": "E-Mail des Ansprechpartners oder null",
  "contact_phone": "Telefon des Ansprechpartners (nicht Firmentelefon) oder null",
  "contact_position": "Position/Funktion des Ansprechpartners oder null"
}

WICHTIGE Regeln:
- Extrahiere NUR Informationen die EXPLIZIT im Text stehen
- Erfinde KEINE Daten — wenn etwas nicht im Text steht, setze es auf null
- Bei Gehaeltern: Uebernimm den originalen Text (z.B. "60.000-75.000 EUR brutto")
- company_domain: Nur die Domain (z.B. "example.com"), nicht die volle URL
- Unterscheide zwischen Firmen-Telefon (company_phone) und Ansprechpartner-Telefon (contact_phone)
- job_description und job_requirements: BEHALTE die Struktur bei! Verwende \\n fuer Zeilenumbrueche und \\n- fuer Aufzaehlungspunkte. Wenn im Original Bulletpoints oder nummerierte Listen stehen, uebernimm diese als \\n- Punkt1\\n- Punkt2 etc. NIEMALS alles in einen Fliesstext quetschen!"""


# ── Endpoints (statische Pfade VOR {job_id} Wildcard!) ──

@router.get("/search-companies")
async def search_companies_for_pipeline(
    q: str = Query(default="", min_length=1),
    db: AsyncSession = Depends(get_db),
):
    """Firmensuche fuer Quick-Add — gibt alle relevanten Felder zurueck."""
    if not q or len(q.strip()) < 2:
        return []
    search_term = f"%{q.strip()}%"
    result = await db.execute(
        select(Company)
        .where(Company.name.ilike(search_term), Company.status != CompanyStatus.BLACKLIST)
        .order_by(Company.name)
        .limit(8)
    )
    return [
        {
            "id": str(c.id),
            "name": c.name,
            "address": c.address,
            "postal_code": c.postal_code,
            "city": c.city,
            "domain": c.domain,
            "phone": c.phone,
        }
        for c in result.scalars().all()
    ]


@router.get("/company-contacts/{company_id}")
async def get_company_contacts_for_pipeline(
    company_id: UUID, db: AsyncSession = Depends(get_db),
):
    """Laedt alle Kontakte einer Firma fuer Dropdown-Auswahl."""
    result = await db.execute(
        select(CompanyContact)
        .where(CompanyContact.company_id == company_id)
        .order_by(CompanyContact.last_name)
    )
    return [
        {
            "id": str(c.id),
            "salutation": c.salutation,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "full_name": f"{c.first_name or ''} {c.last_name or ''}".strip(),
            "email": c.email,
            "phone": c.phone,
            "position": c.position,
        }
        for c in result.scalars().all()
    ]


@router.get("/{job_id}")
async def get_pipeline(job_id: UUID, db: AsyncSession = Depends(get_db)):
    """Laedt die Pipeline fuer eine Stelle (Kanban-Daten)."""
    service = ATSPipelineService(db)
    pipeline = await service.get_pipeline(job_id)

    # Drive-Time Lookup: Lade Fahrzeit-Daten aus matches fuer alle Kandidaten dieses Jobs
    drive_time_map: dict[str, dict] = {}
    try:
        dt_result = await db.execute(
            select(
                Match.candidate_id,
                Match.drive_time_car_min,
                Match.drive_time_transit_min,
            ).where(Match.job_id == job_id)
        )
        for row in dt_result.all():
            drive_time_map[str(row.candidate_id)] = {
                "drive_time_car_min": row.drive_time_car_min,
                "drive_time_transit_min": row.drive_time_transit_min,
            }
    except Exception:
        pass  # Fahrzeit ist optional — Pipeline funktioniert auch ohne

    # Entries serialisieren — Pipeline-Entry Fahrzeit hat Vorrang vor Match-Daten
    result = {}
    for stage_key, stage_data in pipeline.items():
        entries = []
        for entry in stage_data["entries"]:
            candidate = entry.candidate
            cand_id_str = str(entry.candidate_id) if entry.candidate_id else None
            match_dt = drive_time_map.get(cand_id_str, {})
            # Pipeline-Entry Fahrzeit hat Vorrang
            car_min = entry.drive_time_car_min if entry.drive_time_car_min is not None else match_dt.get("drive_time_car_min")
            transit_min = entry.drive_time_transit_min if entry.drive_time_transit_min is not None else match_dt.get("drive_time_transit_min")
            entries.append({
                "id": str(entry.id),
                "candidate_id": cand_id_str,
                "candidate_name": f"{candidate.first_name or ''} {candidate.last_name or ''}".strip() if candidate else "Unbekannt",
                "candidate_position": candidate.current_position if candidate else None,
                "candidate_city": candidate.city if candidate else None,
                "drive_time_car_min": car_min,
                "drive_time_transit_min": transit_min,
                "stage": entry.stage.value,
                "stage_label": entry.stage_label,
                "notes": entry.notes,
                "sort_order": entry.sort_order,
                "stage_changed_at": entry.stage_changed_at.isoformat() if entry.stage_changed_at else None,
                "created_at": entry.created_at.isoformat() if entry.created_at else None,
                "interview_at": entry.interview_at.isoformat() if entry.interview_at else None,
                "interview_type": entry.interview_type,
                "interview_location": entry.interview_location,
                "interview_invite_sent": entry.interview_invite_sent,
            })
        result[stage_key] = {
            "label": stage_data["label"],
            "entries": entries,
            "count": len(entries),
        }

    return result


@router.post("/{job_id}/candidates")
async def add_candidate_to_pipeline(
    job_id: UUID,
    data: AddCandidateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Fuegt einen Kandidaten zur Pipeline hinzu."""
    service = ATSPipelineService(db)
    try:
        entry = await service.add_candidate(
            ats_job_id=job_id,
            candidate_id=data.candidate_id,
            stage=data.stage,
            notes=data.notes,
        )
    except Exception as e:
        if "uq_ats_pipeline_job_candidate" in str(e):
            raise HTTPException(
                status_code=409,
                detail="Kandidat ist bereits in dieser Pipeline",
            )
        raise
    await db.commit()

    # Fahrzeit im Background berechnen
    entry_id = entry.id
    background_tasks.add_task(_calculate_drive_time_for_entry, str(entry_id))

    return {
        "id": str(entry.id),
        "stage": entry.stage.value,
        "message": "Kandidat zur Pipeline hinzugefuegt",
    }


@router.patch("/entries/{entry_id}/stage")
async def move_pipeline_stage(
    entry_id: UUID, data: MoveStageRequest, db: AsyncSession = Depends(get_db)
):
    """Verschiebt einen Kandidaten in eine neue Stage (Drag & Drop)."""
    valid_stages = [
        "matched", "sent", "feedback",
        "interview_1", "interview_2", "interview_3",
        "offer", "placed", "rejected",
    ]
    if data.stage not in valid_stages:
        raise HTTPException(status_code=400, detail=f"Ungueltiger Stage: {data.stage}")

    service = ATSPipelineService(db)
    entry = await service.move_stage(entry_id, data.stage)
    if not entry:
        raise HTTPException(status_code=404, detail="Pipeline-Eintrag nicht gefunden")
    await db.commit()

    response = {
        "id": str(entry.id),
        "stage": entry.stage.value,
        "stage_label": entry.stage_label,
        "message": f"Stage auf '{entry.stage_label}' geaendert",
        "needs_interview": False,
    }

    # Bei Interview-Stages: Company-Daten mitliefern fuer Modal
    if data.stage.startswith("interview_"):
        response["needs_interview"] = True
        from app.models.ats_pipeline import ATSPipelineEntry
        from app.models.ats_job import ATSJob
        from sqlalchemy.orm import selectinload

        entry_full = await db.execute(
            select(ATSPipelineEntry)
            .options(
                selectinload(ATSPipelineEntry.ats_job).selectinload(ATSJob.company),
                selectinload(ATSPipelineEntry.candidate),
            )
            .where(ATSPipelineEntry.id == entry_id)
        )
        entry_obj = entry_full.scalar_one_or_none()
        if entry_obj:
            job = entry_obj.ats_job
            company = job.company if job else None
            candidate = entry_obj.candidate
            response["company_address"] = company.address if company else ""
            response["company_postal_code"] = company.postal_code if company else ""
            response["company_city"] = company.city if company else ""
            response["company_id"] = str(company.id) if company else None
            response["candidate_email"] = candidate.email if candidate else ""

    return response


# ── Interview-Scheduling ──────────────────────────────────


@router.post("/entries/{entry_id}/interview")
async def schedule_interview_endpoint(
    entry_id: UUID,
    data: ScheduleInterviewRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Plant ein Interview (Datum, Uhrzeit, Art, Teilnehmer) und verschickt optional Einladung."""
    from app.services.interview_service import schedule_interview, send_interview_invite
    from app.models.ats_pipeline import ATSPipelineEntry

    entry = await db.get(ATSPipelineEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Pipeline-Eintrag nicht gefunden")

    if not entry.stage.value.startswith("interview_"):
        raise HTTPException(status_code=400, detail=f"Entry ist in Stage '{entry.stage.value}' — nicht Interview")

    # Validierung: Bei "recruiter" muss Kandidat Email haben
    if data.interview_invite_by == "recruiter" and entry.candidate_id:
        from app.models.candidate import Candidate
        cand = await db.get(Candidate, entry.candidate_id)
        if not cand or not cand.email:
            raise HTTPException(status_code=400, detail="Kandidat hat keine E-Mail — Einladung nicht moeglich")

    # Log was vom Frontend kommt
    participants_raw = data.interview_participants
    logger.info(f"[Interview-Endpoint] entry_id={entry_id}, participants empfangen: {participants_raw} (Anzahl: {len(participants_raw) if participants_raw else 0})")

    result = await schedule_interview(
        entry_id=entry_id,
        data={
            "interview_at": data.interview_at,
            "interview_type": data.interview_type,
            "interview_location": data.interview_location,
            "interview_hint": data.interview_hint,
            "interview_participants": participants_raw,
            "interview_invite_by": data.interview_invite_by,
        },
        db=db,
    )

    if not result["success"]:
        raise HTTPException(status_code=500, detail=result["error"])

    await db.commit()

    # Background-Task: Einladung senden (participants direkt uebergeben als Fallback)
    if data.interview_invite_by == "recruiter":
        logger.info(f"[Interview-Endpoint] Background-Task starten mit {len(participants_raw) if participants_raw else 0} Teilnehmern")
        background_tasks.add_task(
            send_interview_invite,
            entry_id,
            participants_override=participants_raw,
        )

    return {
        "id": str(entry_id),
        "interview_at": data.interview_at.isoformat(),
        "interview_type": data.interview_type,
        "interview_invite_by": data.interview_invite_by,
        "invite_will_be_sent": data.interview_invite_by == "recruiter",
        "message": "Interview geplant" + (" — Einladung wird verschickt" if data.interview_invite_by == "recruiter" else ""),
    }


@router.get("/entries/{entry_id}/interview")
async def get_interview_details(
    entry_id: UUID, db: AsyncSession = Depends(get_db),
):
    """Gibt Interview-Details zurueck (fuer Modal-Prefill beim Bearbeiten)."""
    from app.models.ats_pipeline import ATSPipelineEntry

    entry = await db.get(ATSPipelineEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Pipeline-Eintrag nicht gefunden")

    return {
        "interview_at": entry.interview_at.isoformat() if entry.interview_at else None,
        "interview_type": entry.interview_type,
        "interview_location": entry.interview_location,
        "interview_hint": entry.interview_hint,
        "interview_participants": entry.interview_participants,
        "interview_invite_by": entry.interview_invite_by,
        "interview_invite_sent": entry.interview_invite_sent,
        "interview_event_id": entry.interview_event_id,
    }


@router.patch("/entries/{entry_id}/reorder")
async def reorder_pipeline_entry(
    entry_id: UUID, data: ReorderRequest, db: AsyncSession = Depends(get_db)
):
    """Aendert die Reihenfolge eines Eintrags innerhalb einer Stage."""
    service = ATSPipelineService(db)
    entry = await service.reorder(entry_id, data.sort_order)
    if not entry:
        raise HTTPException(status_code=404, detail="Pipeline-Eintrag nicht gefunden")
    await db.commit()
    return {"id": str(entry.id), "sort_order": entry.sort_order}


@router.delete("/entries/{entry_id}")
async def remove_pipeline_entry(
    entry_id: UUID,
    reason: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Entfernt einen Kandidaten aus der Pipeline."""
    service = ATSPipelineService(db)
    removed = await service.remove_candidate(entry_id, reason=reason)
    if not removed:
        raise HTTPException(status_code=404, detail="Pipeline-Eintrag nicht gefunden")
    await db.commit()
    return {"message": "Kandidat aus Pipeline entfernt"}


@router.post("/entries/bulk-move")
async def bulk_move_pipeline_entries(
    data: BulkMoveRequest, db: AsyncSession = Depends(get_db)
):
    """Verschiebt mehrere Kandidaten in eine neue Stage."""
    service = ATSPipelineService(db)
    entries = await service.bulk_move(data.entry_ids, data.stage)
    await db.commit()
    return {
        "moved": len(entries),
        "stage": data.stage,
        "message": f"{len(entries)} Eintraege verschoben",
    }


@router.post("/drive-times/backfill")
async def backfill_pipeline_drive_times(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Berechnet Fahrzeit fuer alle Pipeline-Eintraege ohne Fahrzeit."""
    from app.models.ats_pipeline import ATSPipelineEntry

    # Zaehle Eintraege ohne Fahrzeit
    result = await db.execute(
        select(ATSPipelineEntry.id).where(
            ATSPipelineEntry.drive_time_car_min.is_(None),
            ATSPipelineEntry.candidate_id.isnot(None),
        )
    )
    entry_ids = [str(row[0]) for row in result.all()]

    if not entry_ids:
        return {"message": "Alle Pipeline-Eintraege haben bereits Fahrzeit", "count": 0}

    background_tasks.add_task(_backfill_drive_times, entry_ids)

    return {
        "message": f"Fahrzeit-Berechnung fuer {len(entry_ids)} Eintraege gestartet",
        "count": len(entry_ids),
    }


# ── Rawtext-Import Endpoints ────────────────────

@router.post("/parse-job-text")
async def parse_job_text(data: ParseJobTextRequest, db: AsyncSession = Depends(get_db)):
    """Analysiert Stellentext mit GPT-4o und prueft DB auf bestehende Firmen/Kontakte."""
    raw_text = data.raw_text.strip()
    if not raw_text or len(raw_text) < 20:
        raise HTTPException(status_code=400, detail="Text ist zu kurz (min. 20 Zeichen)")
    if len(raw_text) > 15000:
        raise HTTPException(status_code=400, detail="Text ist zu lang (max. 15.000 Zeichen)")

    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="Kein OpenAI API Key konfiguriert")

    # ── GPT-4o API Call (KEINE DB-Session offen!) ──
    extracted = {}
    try:
        async with httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=60.0,
        ) as client:
            response = await client.post(
                "/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "system", "content": RAWTEXT_EXTRACTION_PROMPT},
                        {"role": "user", "content": raw_text},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 3000,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            extracted = json.loads(content)
    except httpx.HTTPStatusError as e:
        logger.error(f"GPT-4o API Fehler: {e.response.status_code} - {e.response.text[:200]}")
        raise HTTPException(status_code=502, detail="GPT-4o API Fehler — bitte erneut versuchen")
    except json.JSONDecodeError:
        logger.error(f"GPT-4o JSON Parse Fehler: {content[:200] if content else 'leer'}")
        raise HTTPException(status_code=422, detail="GPT-4o hat kein gueltiges JSON zurueckgegeben")
    except Exception as e:
        logger.error(f"GPT-4o Call fehlgeschlagen: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"GPT-4o Fehler: {str(e)[:100]}")

    # ── DB-Abgleich: Firma + Kontakt suchen ──
    company_match = None
    contact_match = None
    company_name = extracted.get("company_name")
    contact_email = extracted.get("contact_email")
    contact_first = extracted.get("contact_first_name")
    contact_last = extracted.get("contact_last_name")

    if company_name and company_name.strip():
        result = await db.execute(
            select(Company.id, Company.name, Company.city, Company.status)
            .where(func.lower(Company.name) == company_name.strip().lower())
            .limit(1)
        )
        row = result.one_or_none()
        if row:
            company_match = {
                "id": str(row.id),
                "name": row.name,
                "city": row.city,
                "status": row.status.value if row.status else "active",
            }

            # Kontakt in dieser Firma suchen
            if contact_email and contact_email.strip():
                c_result = await db.execute(
                    select(CompanyContact.id, CompanyContact.first_name, CompanyContact.last_name, CompanyContact.email)
                    .where(
                        CompanyContact.company_id == row.id,
                        func.lower(CompanyContact.email) == contact_email.strip().lower(),
                    ).limit(1)
                )
                c_row = c_result.one_or_none()
                if c_row:
                    contact_match = {
                        "id": str(c_row.id),
                        "full_name": f"{c_row.first_name or ''} {c_row.last_name or ''}".strip(),
                        "email": c_row.email,
                    }
            elif contact_first and contact_last:
                c_result = await db.execute(
                    select(CompanyContact.id, CompanyContact.first_name, CompanyContact.last_name, CompanyContact.email)
                    .where(
                        CompanyContact.company_id == row.id,
                        func.lower(CompanyContact.first_name) == contact_first.strip().lower(),
                        func.lower(CompanyContact.last_name) == contact_last.strip().lower(),
                    ).limit(1)
                )
                c_row = c_result.one_or_none()
                if c_row:
                    contact_match = {
                        "id": str(c_row.id),
                        "full_name": f"{c_row.first_name or ''} {c_row.last_name or ''}".strip(),
                        "email": c_row.email,
                    }

    return {
        **extracted,
        "company_match": company_match,
        "contact_match": contact_match,
        "company_is_new": company_match is None,
        "contact_is_new": contact_match is None,
    }


@router.post("/create-from-rawtext")
async def create_from_rawtext(
    data: CreateFromRawtextRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Erstellt Company (falls neu) + Contact (falls neu) + ATSJob aus Rawtext-Daten."""
    from app.models.job import Job
    from app.services.ats_job_service import ATSJobService
    from app.services.company_service import CompanyService

    service = CompanyService(db)

    # ── 1. Company ──
    if data.use_existing_company_id:
        company = await db.get(Company, data.use_existing_company_id)
        if not company:
            raise HTTPException(status_code=404, detail="Angegebene Firma nicht gefunden")
    else:
        company_fields = {
            k: v for k, v in {
                "address": data.company_address,
                "postal_code": data.company_postal_code,
                "city": data.company_city,
                "domain": data.company_domain,
                "phone": data.company_phone,
            }.items() if v and str(v).strip()
        }
        company = await service.get_or_create_by_name(data.company_name, **company_fields)
        if company is None:
            raise HTTPException(status_code=409, detail="Firma ist auf der Blacklist")

    # ── 2. Contact ──
    contact = None
    if data.use_existing_contact_id:
        contact = await db.get(CompanyContact, data.use_existing_contact_id)
    elif data.contact_first_name or data.contact_last_name or data.contact_email:
        contact_fields = {
            k: v for k, v in {
                "salutation": data.contact_salutation,
                "email": data.contact_email,
                "phone": data.contact_phone,
                "position": data.contact_position,
            }.items() if v and str(v).strip()
        }
        contact = await service.get_or_create_contact(
            company_id=company.id,
            first_name=data.contact_first_name,
            last_name=data.contact_last_name,
            **contact_fields,
        )

    # ── 3. ATSJob (in_pipeline=True damit sofort in Pipeline-Sidebar sichtbar) ──
    ats_service = ATSJobService(db)
    ats_job = await ats_service.create_job(
        title=data.job_title,
        company_id=company.id,
        contact_id=contact.id if contact else None,
        description=data.job_description,
        requirements=data.job_requirements,
        location_city=data.job_location_city or data.company_city,
        employment_type=data.job_employment_type,
        salary_min=data.job_salary_min,
        salary_max=data.job_salary_max,
        priority=data.job_priority or "medium",
        source="Rawtext-Import",
    )
    # Sofort in Pipeline sichtbar machen
    ats_job.in_pipeline = True
    await db.flush()

    # ── 4. Source-Job fuer Matching ──
    try:
        hash_input = f"rawtext|{data.company_name.strip().lower()}|{data.job_title.strip().lower()}|{datetime.now(timezone.utc).isoformat()}"
        content_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
        regular_job = Job(
            company_name=data.company_name.strip()[:255],
            company_id=company.id,
            position=data.job_title.strip()[:255],
            city=data.job_location_city or data.company_city,
            job_text=data.job_description,
            employment_type=data.job_employment_type,
            content_hash=content_hash,
        )
        db.add(regular_job)
        await db.flush()
    except Exception as e:
        logger.error(f"Source-Job Erstellung fehlgeschlagen: {e}", exc_info=True)
        regular_job = None

    await db.commit()

    # Background-Geocoding
    if regular_job:
        try:
            from app.services.geocoding_service import process_job_after_create
            background_tasks.add_task(process_job_after_create, regular_job.id)
        except Exception:
            pass

    return {
        "ats_job_id": str(ats_job.id),
        "company_id": str(company.id),
        "contact_id": str(contact.id) if contact else None,
        "job_title": ats_job.title,
        "company_name": company.name,
        "message": f"Stelle '{ats_job.title}' bei {company.name} erfolgreich erstellt",
    }


# ── Background Tasks ────────────────────────────


async def _calculate_drive_time_for_entry(entry_id: str) -> None:
    """Berechnet Fahrzeit fuer einen einzelnen Pipeline-Eintrag.

    Nutzt Google Maps direkt mit ADRESSEN — kein Geocoding noetig!

    Adress-Kette fuer Job/Unternehmen:
    1. Company-Adresse (Strasse + PLZ + Stadt) — Unternehmen hat IMMER eine Adresse
    2. ATSJob.location_city als Fallback
    3. Source-Job Stadt als letzter Fallback

    Adress-Kette fuer Kandidat:
    1. Kandidat-Adresse (Strasse + PLZ + Stadt)
    2. Kandidat Stadt als Fallback
    """
    try:
        from app.database import async_session_maker
        from app.models.ats_job import ATSJob
        from app.models.ats_pipeline import ATSPipelineEntry
        from app.models.candidate import Candidate
        from app.models.company import Company
        from app.models.job import Job
        from app.services.distance_matrix_service import distance_matrix_service

        if not distance_matrix_service.has_api_key:
            logger.info("Kein Google Maps API Key — ueberspringe Fahrzeit")
            return

        dest_address = None
        dest_plz = None
        origin_address = None
        origin_plz = None

        # ── Daten laden (eigene Session — Railway 30s Timeout!) ──
        async with async_session_maker() as db:
            # Entry laden
            entry_result = await db.execute(
                select(ATSPipelineEntry).where(ATSPipelineEntry.id == entry_id)
            )
            entry = entry_result.scalar_one_or_none()
            if not entry or not entry.candidate_id:
                logger.info(f"Pipeline-Entry {entry_id}: Kein Kandidat verknuepft")
                return

            ats_job_id = entry.ats_job_id
            candidate_id = entry.candidate_id

            # ── ATSJob laden (fuer company_id + source_job_id + city) ──
            job_result = await db.execute(
                select(
                    ATSJob.location_city,
                    ATSJob.company_id,
                    ATSJob.source_job_id,
                ).where(ATSJob.id == ats_job_id)
            )
            job_row = job_result.one_or_none()
            if not job_row:
                logger.warning(f"Pipeline-Entry {entry_id}: ATSJob {ats_job_id} nicht gefunden")
                return

            # ── ZIEL-ADRESSE: Company hat IMMER eine vollstaendige Adresse ──
            comp_id = job_row.company_id

            # Falls ATSJob keine company_id hat, ueber Source-Job suchen
            if not comp_id and job_row.source_job_id:
                src_result = await db.execute(
                    select(Job.company_name, Job.city, Job.postal_code).where(Job.id == job_row.source_job_id)
                )
                src_row = src_result.one_or_none()
                if src_row and src_row.company_name:
                    # Company per Name finden
                    comp_by_name = await db.execute(
                        select(Company.id).where(
                            Company.name.ilike(src_row.company_name)
                        ).limit(1)
                    )
                    comp_id = comp_by_name.scalar_one_or_none()

                    # Notfalls Source-Job Stadt als Fallback
                    if not comp_id and src_row.city:
                        dest_address = f"{src_row.postal_code + ' ' if src_row.postal_code else ''}{src_row.city}, Deutschland"
                        dest_plz = src_row.postal_code

            if comp_id:
                comp_result = await db.execute(
                    select(
                        Company.name, Company.address, Company.postal_code, Company.city,
                    ).where(Company.id == comp_id)
                )
                comp = comp_result.one_or_none()
                if comp:
                    # Adresse zusammenbauen: Adresse + PLZ + Stadt
                    parts = []
                    if comp.address:
                        parts.append(comp.address)
                    if comp.postal_code:
                        parts.append(comp.postal_code)
                    if comp.city:
                        parts.append(comp.city)
                    if parts:
                        dest_address = ", ".join(parts) + ", Deutschland"
                        dest_plz = comp.postal_code
                        logger.info(f"Pipeline-Entry {entry_id}: Ziel-Adresse von Company '{comp.name}': {dest_address}")

            # Letzter Fallback: ATSJob location_city
            if not dest_address and job_row.location_city:
                dest_address = f"{job_row.location_city}, Deutschland"
                logger.info(f"Pipeline-Entry {entry_id}: Ziel-Adresse Fallback ATSJob Stadt: {dest_address}")

            # ── KANDIDATEN-ADRESSE ──
            cand_result = await db.execute(
                select(
                    Candidate.street_address, Candidate.postal_code, Candidate.city,
                    Candidate.first_name, Candidate.last_name,
                ).where(Candidate.id == candidate_id)
            )
            cand = cand_result.one_or_none()
            if not cand:
                logger.warning(f"Pipeline-Entry {entry_id}: Kandidat {candidate_id} nicht gefunden")
                return

            # Adresse zusammenbauen
            cand_parts = []
            if cand.street_address:
                cand_parts.append(cand.street_address)
            if cand.postal_code:
                cand_parts.append(cand.postal_code)
            if cand.city:
                cand_parts.append(cand.city)
            if cand_parts:
                origin_address = ", ".join(cand_parts) + ", Deutschland"
                origin_plz = cand.postal_code
            else:
                logger.warning(f"Pipeline-Entry {entry_id}: Kandidat {cand.first_name} {cand.last_name} hat keine Adresse")
                return

        # ── Session geschlossen — Google Maps API Call ──

        if not dest_address:
            logger.warning(f"Pipeline-Entry {entry_id}: Keine Ziel-Adresse gefunden (kein Company, kein Stadt)")
            return

        logger.info(f"Pipeline-Entry {entry_id}: Google Maps: '{origin_address}' -> '{dest_address}'")

        result = await distance_matrix_service.get_drive_time_by_address(
            origin_address=origin_address,
            dest_address=dest_address,
            origin_plz=origin_plz,
            dest_plz=dest_plz,
        )

        if result.status in ("ok", "same_plz"):
            async with async_session_maker() as db2:
                await db2.execute(
                    update(ATSPipelineEntry)
                    .where(ATSPipelineEntry.id == entry_id)
                    .values(
                        drive_time_car_min=result.car_min,
                        drive_time_transit_min=result.transit_min,
                        drive_time_car_km=result.car_km,
                    )
                )
                await db2.commit()
            logger.info(
                f"Pipeline-Entry {entry_id}: Fahrzeit berechnet — "
                f"Auto {result.car_min}min, OEPNV {result.transit_min}min, {result.car_km}km"
            )
        else:
            logger.warning(f"Pipeline-Entry {entry_id}: Google Maps Fehler: {result.status}")

    except Exception as e:
        logger.error(f"Fahrzeit-Berechnung fuer Pipeline-Entry {entry_id} fehlgeschlagen: {e}", exc_info=True)


async def _backfill_drive_times(entry_ids: list[str]) -> None:
    """Backfill: Berechnet Fahrzeit fuer mehrere Pipeline-Eintraege."""
    logger.info(f"Fahrzeit-Backfill gestartet fuer {len(entry_ids)} Eintraege")
    success = 0
    errors = 0
    for entry_id in entry_ids:
        try:
            await _calculate_drive_time_for_entry(entry_id)
            success += 1
        except Exception as e:
            logger.error(f"Backfill-Fehler fuer {entry_id}: {e}")
            errors += 1
        # Kleine Pause um Rate-Limits zu vermeiden
        await asyncio.sleep(0.2)
    logger.info(f"Fahrzeit-Backfill fertig: {success} OK, {errors} Fehler")
