"""Service fuer die Generierung von Sincirus Branded ATSJob/Stelle PDFs.

Generiert ein professionelles PDF mit allen Stellendetails
aus dem ATSJob-Model (qualifizierte Stelle). Enthält:
- Position + Firma + Standort
- Aufgabenbeschreibung + Anforderungen
- Job-Qualifizierungsdaten (Team, ERP, HO, Gehalt, etc.)
- Optional: Fahrzeit fuer einen bestimmten Kandidaten
- Sincirus Branding (Dark Navy/Green Design)

Nutzt das gleiche WeasyPrint + Jinja2 Pattern wie job_description_pdf_service.py.
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Any
from uuid import UUID

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ats_job import ATSJob

logger = logging.getLogger(__name__)


class ATSJobPdfService:
    """Generiert Sincirus Branded PDFs fuer qualifizierte Stellen (ATSJob)."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def generate_stelle_pdf(
        self,
        ats_job_id: UUID,
        candidate_id: UUID | None = None,
    ) -> bytes:
        """Hauptmethode: Laedt ATSJob, bereitet Daten auf, rendert PDF.

        Args:
            ats_job_id: UUID der ATSJob-Stelle
            candidate_id: Optional — wenn angegeben, wird die personalisierte
                          Fahrzeit (Google Maps) berechnet und angezeigt

        Returns:
            PDF als bytes
        """
        # ATSJob laden mit Company-Beziehung
        stmt = (
            select(ATSJob)
            .options(selectinload(ATSJob.company))
            .where(ATSJob.id == ats_job_id)
        )
        result = await self.db.execute(stmt)
        ats_job = result.scalar_one_or_none()

        if not ats_job:
            raise ValueError(f"ATSJob {ats_job_id} nicht gefunden")

        # Optionale Fahrzeit berechnen
        drive_time_car = None
        drive_time_transit = None

        if candidate_id:
            drive_time_car, drive_time_transit = await self._calculate_drive_time(
                ats_job, candidate_id
            )

        context = self._prepare_template_context(ats_job, drive_time_car, drive_time_transit)

        # Jinja2 Template rendern
        template_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
        env = Environment(loader=FileSystemLoader(os.path.abspath(template_dir)))
        template = env.get_template("ats_job_sincirus.html")
        html_string = template.render(**context)

        # WeasyPrint: HTML -> PDF (CPU-bound, in Executor)
        from weasyprint import HTML

        static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
        base_url = os.path.abspath(static_dir)

        loop = asyncio.get_event_loop()
        pdf_bytes = await loop.run_in_executor(
            None,
            lambda: HTML(string=html_string, base_url=base_url).write_pdf(),
        )

        company_name = ats_job.company.name if ats_job.company else "Unbekannt"
        logger.info(
            f"Stelle-PDF generiert fuer ATSJob {ats_job_id} "
            f"({ats_job.title} @ {company_name}, {len(pdf_bytes)} bytes)"
        )
        return pdf_bytes

    async def _calculate_drive_time(
        self, ats_job: ATSJob, candidate_id: UUID
    ) -> tuple[int | None, int | None]:
        """Berechnet Fahrzeit zwischen Kandidat und Stelle via Google Maps."""
        try:
            from app.models.candidate import Candidate
            from app.services.distance_matrix_service import DistanceMatrixService
            from geoalchemy2.functions import ST_Y, ST_X
            from sqlalchemy import func

            # Kandidat-Koordinaten laden
            candidate = await self.db.get(Candidate, candidate_id)
            if not candidate or not candidate.address_coords:
                return None, None

            # ATSJob-Koordinaten
            if not ats_job.location_coords:
                return None, None

            # Koordinaten extrahieren
            cand_lat_query = select(
                func.ST_Y(func.ST_GeomFromWKB(Candidate.address_coords))
            ).where(Candidate.id == candidate_id)
            cand_lng_query = select(
                func.ST_X(func.ST_GeomFromWKB(Candidate.address_coords))
            ).where(Candidate.id == candidate_id)

            job_lat_query = select(
                func.ST_Y(func.ST_GeomFromWKB(ATSJob.location_coords))
            ).where(ATSJob.id == ats_job.id)
            job_lng_query = select(
                func.ST_X(func.ST_GeomFromWKB(ATSJob.location_coords))
            ).where(ATSJob.id == ats_job.id)

            cand_lat = (await self.db.execute(cand_lat_query)).scalar()
            cand_lng = (await self.db.execute(cand_lng_query)).scalar()
            job_lat = (await self.db.execute(job_lat_query)).scalar()
            job_lng = (await self.db.execute(job_lng_query)).scalar()

            if not all([cand_lat, cand_lng, job_lat, job_lng]):
                return None, None

            # Google Maps API
            service = DistanceMatrixService()
            results = await service.batch_drive_times(
                origins=[(cand_lat, cand_lng)],
                destinations=[(job_lat, job_lng)],
            )

            if results and len(results) > 0:
                r = results[0]
                return r.get("driving_minutes"), r.get("transit_minutes")

        except Exception as e:
            logger.warning(f"Fahrzeit-Berechnung fuer Stelle-PDF fehlgeschlagen: {e}")

        return None, None

    def _prepare_template_context(
        self,
        ats_job: ATSJob,
        drive_time_car: int | None,
        drive_time_transit: int | None,
    ) -> dict[str, Any]:
        """Bereitet alle Template-Variablen auf."""
        static_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "static")
        )

        company = ats_job.company
        company_name = company.name if company else "Unternehmen"

        # Tasks und Requirements aufbereiten
        tasks = []
        if ats_job.candidate_tasks:
            tasks = [t.strip() for t in ats_job.candidate_tasks.split("\n") if t.strip() and len(t.strip()) > 3]
        elif ats_job.description:
            tasks = [t.strip() for t in ats_job.description.split("\n") if t.strip() and len(t.strip()) > 3]

        requirements = []
        if ats_job.requirements:
            requirements = [r.strip() for r in ats_job.requirements.split("\n") if r.strip() and len(r.strip()) > 3]

        # Bullet-Zeichen entfernen
        clean_chars = "-*\u2022\u2013\u2014\u203a\u00bb\u00b7\u25cb\u25cf"
        tasks = [t.lstrip(clean_chars).strip() for t in tasks[:12]]
        requirements = [r.lstrip(clean_chars).strip() for r in requirements[:10]]

        return {
            # Meta
            "today_date": self._format_german_date(datetime.now()),
            "static_dir": static_dir,

            # Position
            "job_title": ats_job.title or "Offene Stelle",
            "company_name": company_name,
            "location_city": ats_job.location_city or "",
            "employment_type": ats_job.employment_type or "",
            "salary_display": ats_job.salary_display,

            # Inhalte
            "tasks": tasks,
            "requirements": requirements,
            "description": ats_job.description or "",

            # Qualifizierungsdaten
            "team_size": ats_job.team_size,
            "erp_system": ats_job.erp_system,
            "home_office_days": ats_job.home_office_days,
            "flextime": ats_job.flextime,
            "core_hours": ats_job.core_hours,
            "vacation_days": ats_job.vacation_days,
            "overtime_handling": ats_job.overtime_handling,
            "open_office": ats_job.open_office,
            "english_requirements": ats_job.english_requirements,
            "hiring_process_steps": ats_job.hiring_process_steps,
            "feedback_timeline": ats_job.feedback_timeline,
            "digitalization_level": ats_job.digitalization_level,
            "desired_start_date": ats_job.desired_start_date,

            # Fahrzeit (optional)
            "drive_time_car_min": drive_time_car,
            "drive_time_transit_min": drive_time_transit,

            # Consultant
            "consultant_name": "Milad Hamdard",
            "consultant_title": "Senior Consultant Finance & Engineering",
            "consultant_email": "hamdard@sincirus.com",
            "consultant_phone": "0176 8000 47 41",
            "consultant_phone2": "040 238 345 320",
            "consultant_address": "Ballindamm 3, 20095 Hamburg",
        }

    @staticmethod
    def _format_german_date(dt: datetime) -> str:
        """Formatiert Datum als 'DD.MM.YYYY'."""
        return dt.strftime("%d.%m.%Y")
