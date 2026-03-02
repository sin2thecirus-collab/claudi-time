"""AcquisitionQualifyService — Lead → ATSJob Konvertierung.

Uebertraegt Akquise-Dossier (Calls, Emails, Timeline) in die ATS-Pipeline,
triggert Classification-Pipeline und Claude-Matching.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.acquisition_call import AcquisitionCall
from app.models.acquisition_email import AcquisitionEmail
from app.models.job import Job

logger = logging.getLogger(__name__)


class AcquisitionQualifyService:
    """Konvertiert qualifizierte Akquise-Leads in ATS-Jobs."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def convert_to_ats(
        self,
        job_id: uuid.UUID,
        qualification_data: dict | None = None,
    ) -> dict:
        """Konvertiert einen Akquise-Lead in einen ATSJob.

        Args:
            job_id: Der Akquise-Job
            qualification_data: Zusaetzliche Daten aus Qualifizierung
                (budget, software, team_size, etc.)

        Returns:
            {
                "ats_job_id": UUID,
                "job_id": UUID,
                "dossier_summary": str,
                "calls_transferred": int,
                "emails_transferred": int,
            }
        """
        now = datetime.now(timezone.utc)

        # Job laden
        job = await self.db.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} nicht gefunden")

        if not job.acquisition_source:
            raise ValueError(f"Job {job_id} ist kein Akquise-Job")

        if job.akquise_status == "stelle_erstellt":
            raise ValueError(f"Job {job_id} wurde bereits konvertiert")

        # ── ATSJob erstellen ──
        from app.services.ats_job_service import ATSJobService

        ats_service = ATSJobService(self.db)

        # source_job_id = der existierende Akquise-Job (kein neuer Source-Job noetig)
        # NUR Felder uebergeben die auf ATSJob existieren (KEIN postal_code, KEIN industry)
        ats_job = await ats_service.create_job(
            title=job.position or "Stelle ohne Titel",
            source_job_id=job.id,
            company_id=job.company_id,
            location_city=job.city,
            description=job.job_text,
            employment_type=job.employment_type,
        )

        # ── Qualifizierungsdaten uebertragen (JSONB komplett + Feld-Mapping) ──
        # 1. Komplettes JSONB kopieren (17 Fragen + Antworten)
        if job.qualification_answers:
            ats_job.qualification_answers = dict(job.qualification_answers)
            logger.info(
                f"Qualification_answers uebertragen: {len(job.qualification_answers)} Eintraege"
            )

            # 2. Zusaetzlich passende ATS-Felder befuellen (Best-Effort)
            self._map_quali_to_ats_fields(ats_job, job.qualification_answers)

        # Legacy: Wenn qualification_data als Dict uebergeben (von D10 Disposition)
        if qualification_data:
            try:
                await ats_service.update_qualification_fields(
                    job_id=ats_job.id,
                    data=qualification_data,
                    overwrite=False,
                )
            except Exception as e:
                logger.warning(f"Legacy qualification_data Transfer: {e}")

        # Kontakt uebertragen
        if job.company_id:
            # Ersten Kontakt des Unternehmens als Ansprechpartner setzen
            from app.models.company_contact import CompanyContact

            contact_result = await self.db.execute(
                select(CompanyContact.id)
                .where(CompanyContact.company_id == job.company_id)
                .limit(1)
            )
            contact_row = contact_result.fetchone()
            if contact_row:
                ats_job.contact_id = contact_row[0]

        # ── Akquise-Dossier zusammenstellen ──
        dossier = await self._build_dossier(job_id)

        # Dossier in Notes speichern
        ats_job.notes = dossier["summary"]

        # ── Calls als ATSCallNotes kopieren ──
        calls_transferred = await self._transfer_calls(job_id, ats_job.id)

        # ── Emails als ATSActivity verlinken ──
        emails_transferred = await self._transfer_emails(job_id, ats_job.id)

        # ── Job-Status aktualisieren ──
        job.akquise_status = "stelle_erstellt"
        job.akquise_status_changed_at = now

        await self.db.commit()

        # ── Classification-Pipeline triggern (Background) ──
        # Das passiert normalerweise automatisch wenn der ATSJob
        # durch die CSV-Pipeline laeuft. Hier manuell triggern.
        logger.info(
            f"ATSJob {ats_job.id} erstellt aus Akquise-Lead {job_id}. "
            f"Dossier: {calls_transferred} Calls, {emails_transferred} Emails."
        )

        return {
            "ats_job_id": str(ats_job.id),
            "job_id": str(job_id),
            "dossier_summary": dossier["summary"],
            "calls_transferred": calls_transferred,
            "emails_transferred": emails_transferred,
        }

    @staticmethod
    def _map_quali_to_ats_fields(ats_job, qa: dict) -> None:
        """Mappt Akquise-Qualifizierungsantworten auf typisierte ATS-Felder (Best-Effort)."""
        import re

        def _get_answer(key: str) -> str | None:
            entry = qa.get(key)
            if entry and isinstance(entry, dict):
                return entry.get("answer")
            return None

        # Budget → salary_min / salary_max
        budget = _get_answer("budget")
        if budget:
            # Versuche Zahlen zu extrahieren (z.B. "54.000 bis 60.000€")
            numbers = re.findall(r"[\d]+[.\d]*", budget.replace(".", "").replace(",", "."))
            nums = [int(float(n)) for n in numbers if float(n) > 1000]
            if len(nums) >= 2:
                ats_job.salary_min = min(nums)
                ats_job.salary_max = max(nums)
            elif len(nums) == 1:
                ats_job.salary_min = nums[0]

        # Direkte Textfeld-Mappings (ats_field, max_length passend zum DB-Schema)
        mapping = {
            "teamgroesse": ("team_size", 100),
            "home_office": ("home_office_days", 100),
            "software": ("erp_system", 255),
            "arbeitszeiten": ("core_hours", 100),
            "ueberstunden": ("overtime_handling", 255),
            "bewerbungsprozess": ("hiring_process_steps", 500),
            "englisch": ("english_requirements", 255),
            "timeline": ("desired_start_date", 100),
        }
        for qa_key, (ats_field, max_len) in mapping.items():
            answer = _get_answer(qa_key)
            if answer and not getattr(ats_job, ats_field, None):
                setattr(ats_job, ats_field, answer[:max_len])

        # Boolean-Mappings
        aeltere = _get_answer("aeltere_kandidaten")
        if aeltere and ats_job.older_candidates_ok is None:
            lower = aeltere.lower()
            if "ja" in lower or "willkommen" in lower or "kein problem" in lower:
                ats_job.older_candidates_ok = True
            elif "nein" in lower or "unter" in lower or "nicht" in lower:
                ats_job.older_candidates_ok = False

    async def _build_dossier(self, job_id: uuid.UUID) -> dict:
        """Baut das Akquise-Dossier (Timeline-Zusammenfassung)."""
        # Calls laden
        calls_result = await self.db.execute(
            select(AcquisitionCall)
            .where(AcquisitionCall.job_id == job_id)
            .order_by(AcquisitionCall.created_at.asc())
        )
        calls = calls_result.scalars().all()

        # Emails laden
        emails_result = await self.db.execute(
            select(AcquisitionEmail)
            .where(AcquisitionEmail.job_id == job_id)
            .order_by(AcquisitionEmail.created_at.asc())
        )
        emails = emails_result.scalars().all()

        # Timeline bauen
        timeline_parts = []

        for call in calls:
            date_str = call.created_at.strftime("%d.%m.%Y") if call.created_at else "?"
            timeline_parts.append(
                f"[{date_str}] Anruf: {call.disposition}"
                + (f" — {call.notes[:100]}" if call.notes else "")
            )

        for email in emails:
            date_str = (email.sent_at or email.created_at).strftime("%d.%m.%Y")
            timeline_parts.append(
                f"[{date_str}] E-Mail ({email.email_type}): {email.subject or 'Kein Betreff'} "
                f"— Status: {email.status}"
            )

        # Zusammenfassung
        first_contact = calls[0].created_at.strftime("%d.%m.%Y") if calls else "?"
        summary = (
            f"=== AKQUISE-DOSSIER ===\n"
            f"Erstkontakt: {first_contact}\n"
            f"Anrufe: {len(calls)}\n"
            f"E-Mails: {len(emails)}\n\n"
            f"Timeline:\n" + "\n".join(timeline_parts)
        )

        return {
            "summary": summary,
            "calls_count": len(calls),
            "emails_count": len(emails),
            "timeline": timeline_parts,
        }

    async def _transfer_calls(self, job_id: uuid.UUID, ats_job_id: uuid.UUID) -> int:
        """Kopiert Akquise-Calls als ATSCallNotes."""
        from app.models.ats_call_note import ATSCallNote, CallType

        calls_result = await self.db.execute(
            select(AcquisitionCall)
            .where(AcquisitionCall.job_id == job_id)
            .order_by(AcquisitionCall.created_at.asc())
        )
        calls = calls_result.scalars().all()

        for call in calls:
            note = ATSCallNote(
                ats_job_id=ats_job_id,
                call_type=CallType.OUTBOUND,
                contact_id=call.contact_id,
                notes=(
                    f"[Akquise] {call.disposition}: "
                    + (call.notes or "Keine Notizen")
                ),
                duration_seconds=call.duration_seconds,
            )
            self.db.add(note)

        return len(calls)

    async def _transfer_emails(self, job_id: uuid.UUID, ats_job_id: uuid.UUID) -> int:
        """Verlinkt Akquise-Emails als ATSActivity."""
        from app.models.ats_activity import ATSActivity, ActivityType

        emails_result = await self.db.execute(
            select(AcquisitionEmail)
            .where(
                AcquisitionEmail.job_id == job_id,
                AcquisitionEmail.status == "sent",
            )
            .order_by(AcquisitionEmail.created_at.asc())
        )
        emails = emails_result.scalars().all()

        for email in emails:
            activity = ATSActivity(
                ats_job_id=ats_job_id,
                activity_type=ActivityType.EMAIL_SENT,
                description=(
                    f"[Akquise] {email.email_type}: {email.subject or 'Kein Betreff'}"
                ),
            )
            self.db.add(activity)

        return len(emails)
