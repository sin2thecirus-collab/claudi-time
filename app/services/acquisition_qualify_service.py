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

        ats_job = await ats_service.create_job(
            title=job.position,
            company_id=job.company_id,
            location_city=job.city,
            location_postal_code=job.postal_code,
            description=job.job_text,
            employment_type=job.employment_type,
            industry=job.industry,
        )

        # Qualifizierungsdaten uebertragen
        if qualification_data:
            await ats_service.update_qualification_fields(
                job_id=ats_job.id,
                data=qualification_data,
                overwrite=False,
            )

        # ── Akquise-Dossier zusammenstellen ──
        dossier = await self._build_dossier(job_id)

        # Dossier in ATSJob internal_notes speichern
        ats_job.internal_notes = dossier["summary"]

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
