"""
Service für die Generierung von Sincirus Branded Job-Description-PDFs.

Phase 11a: Generiert ein professionelles PDF mit Stellendetails
für die Kandidaten-Ansprache. Enthält:
- Persönliche Begrüßung
- Fahrzeit (Auto + ÖPNV via Google Maps)
- Unternehmen + Branche
- Stellenbezeichnung + Level
- Aufgabenbeschreibung
- Sincirus Branding (Dark Design)

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
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match

logger = logging.getLogger(__name__)


class JobDescriptionPdfService:
    """Generiert Sincirus Branded Job-Description-PDFs für Kandidaten-Ansprache."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def generate_job_pdf(
        self,
        match_id: UUID,
    ) -> bytes:
        """
        Hauptmethode: Lädt Match/Job/Kandidat, bereitet Daten auf, rendert PDF.
        Gibt PDF als bytes zurück.

        Args:
            match_id: UUID des Match-Eintrags (enthält Job + Kandidat + Fahrzeit)

        Returns:
            PDF als bytes
        """
        # Match mit Job und Kandidat laden
        match = await self._load_match(match_id)
        if not match:
            raise ValueError(f"Match {match_id} nicht gefunden")

        job = match.job
        candidate = match.candidate

        if not job:
            raise ValueError(f"Job für Match {match_id} nicht gefunden")
        if not candidate:
            raise ValueError(f"Kandidat für Match {match_id} nicht gefunden")

        context = self._prepare_template_context(match, job, candidate)

        # Jinja2 Template rendern
        template_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
        env = Environment(loader=FileSystemLoader(os.path.abspath(template_dir)))
        template = env.get_template("job_description_sincirus.html")
        html_string = template.render(**context)

        # WeasyPrint: HTML → PDF (CPU-bound, in Executor)
        from weasyprint import HTML

        static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
        base_url = os.path.abspath(static_dir)

        loop = asyncio.get_event_loop()
        pdf_bytes = await loop.run_in_executor(
            None,
            lambda: HTML(string=html_string, base_url=base_url).write_pdf(),
        )

        logger.info(
            f"Job-PDF generiert für Match {match_id} "
            f"(Job: {job.position} @ {job.company_name}, "
            f"Kandidat: {candidate.full_name}, "
            f"{len(pdf_bytes)} bytes)"
        )
        return pdf_bytes

    async def _load_match(self, match_id: UUID) -> Match | None:
        """Lädt Match mit Job und Kandidat via eager loading."""
        from sqlalchemy.orm import selectinload

        query = (
            select(Match)
            .options(
                selectinload(Match.job),
                selectinload(Match.candidate),
            )
            .where(Match.id == match_id)
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    # ──────────────────────────────────────────────
    # Daten-Aufbereitung
    # ──────────────────────────────────────────────

    def _prepare_template_context(
        self, match: Match, job: Job, candidate: Candidate
    ) -> dict[str, Any]:
        """Bereitet alle Template-Variablen auf."""

        static_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "static")
        )

        return {
            # Meta
            "today_date": self._format_german_date(datetime.now()),

            # Begrüßung
            "salutation": self._build_salutation(candidate),
            "candidate_first_name": candidate.first_name or "",
            "candidate_last_name": candidate.last_name or "",

            # Fahrzeit (Google Maps Phase 10)
            "drive_time_car_min": match.drive_time_car_min,
            "drive_time_transit_min": match.drive_time_transit_min,
            "distance_km": round(match.distance_km, 1) if match.distance_km else None,

            # Job-Details
            "job_position": job.position or "Offene Stelle",
            "job_company": job.company_name or "Unternehmen",
            "job_city": job.display_city,
            "job_industry": job.industry or "",
            "job_employment_type": self._format_employment_type(job.employment_type),
            "job_work_arrangement": self._format_work_arrangement(job.work_arrangement),
            "job_company_size": job.company_size or "",

            # Aufgaben + Anforderungen (aus job_text extrahiert)
            "job_tasks": self._extract_tasks(job.job_text),
            "job_requirements": self._extract_requirements(job.job_text),
            "job_text_raw": job.job_text or "",

            # Classification (Phase 1)
            "job_role": self._get_classification_field(job, "primary_role"),
            "job_sub_level": self._get_classification_field(job, "sub_level"),
            "job_quality": job.quality_score or "",

            # Matching-Score
            "match_score": round(match.v2_score, 1) if match.v2_score else None,

            # Consultant (hardcoded wie bei Profil-PDF)
            "consultant": {
                "name": "Milad Hamdard",
                "title": "Senior Consultant Finance & Engineering",
                "email": "hamdard@sincirus.com",
                "phone": "0176 8000 47 41",
                "phone2": "040 238 345 320",
                "address": "Ballindamm 3, 20095 Hamburg",
            },

            # Asset-Pfade (absolute Pfade für WeasyPrint)
            "logo_path": os.path.join(static_dir, "images", "sincirus_logo.png"),
            "font_dir": os.path.join(static_dir, "fonts"),
        }

    def _build_salutation(self, candidate: Candidate) -> str:
        """Erstellt die passende Anrede (Herr/Frau + Nachname)."""
        gender = (candidate.gender or "").strip().lower()
        last_name = candidate.last_name or ""

        if gender in ("frau", "female", "f", "w"):
            return f"Sehr geehrte Frau {last_name}"
        elif gender in ("herr", "male", "m"):
            return f"Sehr geehrter Herr {last_name}"
        else:
            # Fallback: Vorname + Nachname ohne Geschlecht
            name = f"{candidate.first_name or ''} {last_name}".strip()
            return f"Guten Tag {name}" if name else "Guten Tag"

    def _format_employment_type(self, emp_type: str | None) -> str:
        """Formatiert den Beschäftigungstyp."""
        if not emp_type:
            return ""
        mapping = {
            "vollzeit": "Vollzeit",
            "teilzeit": "Teilzeit",
            "full-time": "Vollzeit",
            "part-time": "Teilzeit",
            "befristet": "Befristet",
            "unbefristet": "Unbefristet",
        }
        return mapping.get(emp_type.lower().strip(), emp_type)

    def _format_work_arrangement(self, arrangement: str | None) -> str:
        """Formatiert die Arbeitsweise."""
        if not arrangement:
            return ""
        mapping = {
            "vor_ort": "Vor Ort",
            "hybrid": "Hybrid",
            "remote": "Remote",
        }
        return mapping.get(arrangement.lower().strip(), arrangement)

    def _extract_tasks(self, job_text: str | None) -> list[str]:
        """Extrahiert Aufgaben/Tätigkeiten aus dem Freitext.

        Sucht nach Abschnitten wie 'Aufgaben:', 'Tätigkeiten:', 'Ihre Aufgaben:' etc.
        und gibt die Bulletpoints zurück.
        """
        if not job_text:
            return []

        tasks = []
        lines = job_text.split("\n")
        in_tasks_section = False

        task_headers = [
            "aufgaben", "tätigkeiten", "ihre aufgaben", "das erwartet sie",
            "ihre tätigkeiten", "aufgabenbereich", "stellenbeschreibung",
            "was sie erwartet", "das sind ihre aufgaben",
        ]
        end_headers = [
            "anforderungen", "profil", "ihr profil", "was sie mitbringen",
            "qualifikation", "voraussetzung", "wir bieten", "benefits",
            "das bieten wir", "wir erwarten", "was wir bieten",
        ]

        for line in lines:
            stripped = line.strip()
            lower = stripped.lower().rstrip(":")

            # Prüfe ob Aufgaben-Section startet
            if any(header in lower for header in task_headers) and len(stripped) < 80:
                in_tasks_section = True
                continue

            # Prüfe ob Section endet
            if in_tasks_section and any(header in lower for header in end_headers) and len(stripped) < 80:
                break

            # Bullets sammeln
            if in_tasks_section and stripped:
                # Bullet-Zeichen entfernen
                clean = re.sub(r"^[\-\*\•\–\—\›\»\·\○\●]\s*", "", stripped)
                if clean and len(clean) > 5:
                    tasks.append(clean)

        return tasks[:12]  # Max 12 Aufgaben

    def _extract_requirements(self, job_text: str | None) -> list[str]:
        """Extrahiert Anforderungen/Profil aus dem Freitext."""
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
            "unser angebot", "kontakt", "bewerbung", "über uns",
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

        return requirements[:10]  # Max 10 Anforderungen

    def _get_classification_field(self, job: Job, field: str) -> str:
        """Liest ein Feld aus classification_data (JSONB)."""
        if not job.classification_data:
            return ""
        return job.classification_data.get(field, "")

    def _format_german_date(self, dt: datetime) -> str:
        """Formatiert ein Datum auf Deutsch (z.B. '16. Februar 2026')."""
        months = {
            1: "Januar", 2: "Februar", 3: "März", 4: "April",
            5: "Mai", 6: "Juni", 7: "Juli", 8: "August",
            9: "September", 10: "Oktober", 11: "November", 12: "Dezember",
        }
        return f"{dt.day}. {months.get(dt.month, '')} {dt.year}"
