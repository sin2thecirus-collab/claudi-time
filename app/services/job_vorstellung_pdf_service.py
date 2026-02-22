"""
Service fuer die Generierung von Sincirus Branded Job-Vorstellungs-PDFs.

Erstellt ein professionelles PDF im gleichen Dark-Design wie das Kandidaten-Profil-PDF.
Kann optional fuer einen bestimmten Kandidaten personalisiert werden (Fahrzeit, Einschaetzung).

Nutzt das gleiche WeasyPrint + Jinja2 Pattern wie profile_pdf_service.py.
"""

import asyncio
import logging
import os
import re
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job

logger = logging.getLogger(__name__)


class JobVorstellungPdfService:
    """Generiert Sincirus Branded Job-Vorstellungs-PDFs."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def generate_job_vorstellung_pdf(
        self,
        job_id: UUID,
        candidate_id: UUID | None = None,
        match_id: UUID | None = None,
    ) -> bytes:
        """
        Hauptmethode: Laedt Job (+ optional Kandidat/Match), rendert PDF.

        Args:
            job_id: UUID des Jobs
            candidate_id: Optional — fuer personalisierte Version mit Fahrzeit
            match_id: Optional — fuer Score + Fahrzeit aus bestehendem Match

        Returns:
            PDF als bytes
        """
        # Job laden
        result = await self.db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if not job:
            raise ValueError(f"Job {job_id} nicht gefunden")

        # Optional: Kandidat + Match laden
        candidate = None
        match = None

        if match_id:
            from app.models.match import Match
            m_result = await self.db.execute(select(Match).where(Match.id == match_id))
            match = m_result.scalar_one_or_none()

        if candidate_id:
            from app.models.candidate import Candidate
            c_result = await self.db.execute(select(Candidate).where(Candidate.id == candidate_id))
            candidate = c_result.scalar_one_or_none()

        context = self._prepare_template_context(job, candidate, match)

        # Jinja2 Template rendern
        template_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
        env = Environment(loader=FileSystemLoader(os.path.abspath(template_dir)))
        template = env.get_template("job_vorstellung_sincirus.html")
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

        logger.info(
            f"Job-Vorstellungs-PDF generiert fuer Job {job_id} "
            f"({job.position} @ {job.company_name}, "
            f"personalisiert={candidate_id is not None}, "
            f"{len(pdf_bytes)} bytes)"
        )
        return pdf_bytes

    def _prepare_template_context(
        self, job: Job, candidate=None, match=None
    ) -> dict[str, Any]:
        """Bereitet alle Template-Variablen auf."""

        static_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "static")
        )

        # Aufgaben + Anforderungen aus job_text extrahieren
        tasks = self._extract_tasks(job.job_text)
        requirements = self._extract_requirements(job.job_text)

        context = {
            # Meta
            "today_date": self._format_german_date(datetime.now()),

            # Job-Details
            "job_position": job.position or "Offene Stelle",
            "job_company": job.company_name or "Unternehmen",
            "job_city": getattr(job, "display_city", None) or job.city or "",
            "job_industry": job.industry or "",
            "job_employment_type": self._format_employment_type(job.employment_type),
            "job_work_arrangement": self._format_work_arrangement(job.work_arrangement),
            "job_company_size": job.company_size or "",
            "job_role": self._get_classification_field(job, "primary_role"),
            "job_sub_level": self._get_classification_field(job, "sub_level"),

            # Aufgaben + Anforderungen
            "job_tasks": tasks,
            "job_requirements": requirements,
            "job_text_raw": job.job_text or "",
            "has_structured_content": len(tasks) > 0 or len(requirements) > 0,

            # Personalisierung (leer wenn kein Kandidat)
            "is_personalized": candidate is not None,
            "candidate_name": "",
            "candidate_city": "",
            "salutation": "",
            "drive_time_car_min": None,
            "drive_time_transit_min": None,
            "distance_km": None,
            "match_score": None,

            # Consultant
            "consultant": {
                "name": "Milad Hamdard",
                "title": "Senior Consultant Finance & Engineering",
                "email": "hamdard@sincirus.com",
                "phone": "0176 8000 47 41",
                "phone2": "040 238 345 320",
                "address": "Ballindamm 3, 20095 Hamburg",
            },

            # Asset-Pfade
            "logo_path": os.path.join(static_dir, "images", "sincirus_logo.png"),
            "font_dir": os.path.join(static_dir, "fonts"),
        }

        # Personalisierung wenn Kandidat vorhanden
        if candidate:
            first = candidate.first_name or ""
            last = candidate.last_name or ""
            context["candidate_name"] = f"{first} {last}".strip()
            context["candidate_city"] = candidate.city or ""
            context["salutation"] = self._build_salutation(candidate)

        if match:
            context["drive_time_car_min"] = match.drive_time_car_min
            context["drive_time_transit_min"] = match.drive_time_transit_min
            context["distance_km"] = round(match.distance_km, 1) if match.distance_km else None
            if match.v2_score:
                context["match_score"] = round(match.v2_score, 1)
            elif match.ai_score:
                context["match_score"] = round(match.ai_score * 100, 1)

        return context

    def _build_salutation(self, candidate) -> str:
        """Erstellt die passende Anrede."""
        gender = (getattr(candidate, "gender", "") or "").strip().lower()
        last_name = candidate.last_name or ""

        if gender in ("frau", "female", "f", "w"):
            return f"Sehr geehrte Frau {last_name}"
        elif gender in ("herr", "male", "m"):
            return f"Sehr geehrter Herr {last_name}"
        else:
            name = f"{candidate.first_name or ''} {last_name}".strip()
            return f"Guten Tag {name}" if name else "Guten Tag"

    def _format_employment_type(self, emp_type: str | None) -> str:
        if not emp_type:
            return ""
        mapping = {
            "vollzeit": "Vollzeit", "teilzeit": "Teilzeit",
            "full-time": "Vollzeit", "part-time": "Teilzeit",
            "befristet": "Befristet", "unbefristet": "Unbefristet",
        }
        return mapping.get(emp_type.lower().strip(), emp_type)

    def _format_work_arrangement(self, arrangement: str | None) -> str:
        if not arrangement:
            return ""
        mapping = {"vor_ort": "Vor Ort", "hybrid": "Hybrid", "remote": "Remote"}
        return mapping.get(arrangement.lower().strip(), arrangement)

    def _extract_tasks(self, job_text: str | None) -> list[str]:
        """Extrahiert Aufgaben aus dem Freitext."""
        if not job_text:
            return []

        tasks = []
        lines = job_text.split("\n")
        in_tasks_section = False

        task_headers = [
            "aufgaben", "taetigkeiten", "tätigkeiten", "ihre aufgaben",
            "das erwartet sie", "ihre tätigkeiten", "aufgabenbereich",
            "stellenbeschreibung", "was sie erwartet", "das sind ihre aufgaben",
        ]
        end_headers = [
            "anforderungen", "profil", "ihr profil", "was sie mitbringen",
            "qualifikation", "voraussetzung", "wir bieten", "benefits",
            "das bieten wir", "wir erwarten", "was wir bieten",
        ]

        for line in lines:
            stripped = line.strip()
            lower = stripped.lower().rstrip(":")

            if any(header in lower for header in task_headers) and len(stripped) < 80:
                in_tasks_section = True
                continue

            if in_tasks_section and any(header in lower for header in end_headers) and len(stripped) < 80:
                break

            if in_tasks_section and stripped:
                clean = re.sub(r"^[\-\*\•\–\—\›\»\·\○\●]\s*", "", stripped)
                if clean and len(clean) > 5:
                    tasks.append(clean)

        return tasks[:12]

    def _extract_requirements(self, job_text: str | None) -> list[str]:
        """Extrahiert Anforderungen aus dem Freitext."""
        if not job_text:
            return []

        requirements = []
        lines = job_text.split("\n")
        in_req_section = False

        req_headers = [
            "anforderungen", "profil", "ihr profil", "was sie mitbringen",
            "qualifikation", "voraussetzung", "das bringen sie mit",
            "wir erwarten", "ihre qualifikationen",
        ]
        end_headers = [
            "wir bieten", "benefits", "das bieten wir", "was wir bieten",
            "unser angebot", "kontakt", "bewerbung", "ueber uns", "über uns",
        ]

        for line in lines:
            stripped = line.strip()
            lower = stripped.lower().rstrip(":")

            if any(header in lower for header in req_headers) and len(stripped) < 80:
                in_req_section = True
                continue

            if in_req_section and any(header in lower for header in end_headers) and len(stripped) < 80:
                break

            if in_req_section and stripped:
                clean = re.sub(r"^[\-\*\•\–\—\›\»\·\○\●]\s*", "", stripped)
                if clean and len(clean) > 5:
                    requirements.append(clean)

        return requirements[:10]

    def _get_classification_field(self, job: Job, field: str) -> str:
        if not job.classification_data:
            return ""
        return job.classification_data.get(field, "")

    def _format_german_date(self, dt: datetime) -> str:
        months = {
            1: "Januar", 2: "Februar", 3: "Maerz", 4: "April",
            5: "Mai", 6: "Juni", 7: "Juli", 8: "August",
            9: "September", 10: "Oktober", 11: "November", 12: "Dezember",
        }
        return f"{dt.day}. {months.get(dt.month, '')} {dt.year}"
