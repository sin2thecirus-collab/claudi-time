"""AcquisitionCallService — Anrufe protokollieren, Dispositionen verarbeiten.

Verwaltet die State-Machine fuer akquise_status,
erstellt automatische Wiedervorlagen und Blacklist-Cascades.
"""

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.acquisition_call import AcquisitionCall
from app.models.company import Company
from app.models.company_contact import CompanyContact
from app.models.job import Job

logger = logging.getLogger(__name__)

# ── State-Machine: Erlaubte Uebergaenge ──
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "neu": {"angerufen", "verloren", "kontakt_fehlt"},
    "angerufen": {
        "kontaktiert", "wiedervorlage", "email_gesendet",
        "kontakt_fehlt", "blacklist_weich", "blacklist_hart", "verloren",
        "qualifiziert",  # D9: Erstanruf kann direkt zur Qualifizierung fuehren
    },
    "kontaktiert": {
        "qualifiziert", "wiedervorlage", "email_gesendet",
        "blacklist_weich", "blacklist_hart",
    },
    "kontakt_fehlt": {"angerufen", "verloren"},
    "email_gesendet": {
        "email_followup", "qualifiziert", "blacklist_weich",
        "blacklist_hart", "followup_abgeschlossen", "angerufen",
    },
    "email_followup": {
        "qualifiziert", "blacklist_weich", "blacklist_hart",
        "followup_abgeschlossen", "angerufen",
    },
    "wiedervorlage": {"angerufen", "kontaktiert", "verloren"},
    "qualifiziert": {"stelle_erstellt", "verloren"},
    "stelle_erstellt": set(),  # Endstatus
    "blacklist_hart": set(),   # Endstatus
    "blacklist_weich": {"neu"},  # Nur durch Re-Import nach 180 Tagen
    "followup_abgeschlossen": {"neu"},  # Nur durch Re-Import nach 30 Tagen
    "verloren": {"neu"},  # Nur durch Re-Import
}

# ── Disposition → Aktion Mapping ──
# D1a: nicht_erreicht → angerufen, Wiedervorlage +1 Tag
# D1b: mailbox_besprochen → angerufen, Wiedervorlage +1 Tag
# D2:  besetzt → angerufen, Wiedervorlage +1 Tag
# D3:  falsche_nummer → kontakt_fehlt
# D4:  sekretariat → angerufen, Wiedervorlage +1 Tag
# D5:  kein_bedarf → blacklist_weich, Wiedervorlage +180 Tage
# D6:  nie_wieder → blacklist_hart, Blacklist-Cascade
# D7:  interesse_spaeter → wiedervorlage (custom Datum+Uhrzeit)
# D8:  will_infos → email_gesendet
# D9:  qualifiziert_erst → qualifiziert (custom Datum+Uhrzeit)
# D10: voll_qualifiziert → stelle_erstellt
# D11: ap_nicht_mehr_da → Contact inaktiv, Lead bleibt
# D12: andere_stelle_offen → neuer Job-Draft
# D13: weiterverbunden → neuer Contact


class AcquisitionCallService:
    """Verarbeitet Akquise-Anrufe mit State-Machine und automatischen Aktionen."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def record_call(
        self,
        job_id: uuid.UUID,
        contact_id: uuid.UUID | None,
        disposition: str,
        call_type: str = "erstanruf",
        notes: str | None = None,
        qualification_data: dict | None = None,
        duration_seconds: int | None = None,
        follow_up_date: datetime | None = None,
        follow_up_note: str | None = None,
        email_consent: bool = True,
        extra_data: dict | None = None,
    ) -> dict:
        """Protokolliert einen Anruf und verarbeitet die Disposition.

        Returns:
            {
                "call_id": UUID,
                "new_status": str,
                "actions": list[str],  # Was automatisch passiert ist
            }
        """
        now = datetime.now(timezone.utc)
        actions: list[str] = []

        # Job laden
        job = await self.db.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} nicht gefunden")

        if not job.acquisition_source:
            raise ValueError(f"Job {job_id} ist kein Akquise-Job")

        # Company laden
        company = await self.db.get(Company, job.company_id) if job.company_id else None

        # Call erstellen
        call = AcquisitionCall(
            job_id=job_id,
            contact_id=contact_id,
            company_id=job.company_id,
            call_type=call_type,
            disposition=disposition,
            qualification_data=qualification_data,
            notes=notes,
            duration_seconds=duration_seconds,
            follow_up_date=follow_up_date,
            follow_up_note=follow_up_note,
            email_consent=email_consent,
        )
        self.db.add(call)

        # ── Disposition verarbeiten ──
        new_status, dispo_actions, auto_follow_up = await self._process_disposition(
            job=job,
            company=company,
            contact_id=contact_id,
            disposition=disposition,
            follow_up_date=follow_up_date,
            follow_up_note=follow_up_note,
            extra_data=extra_data or {},
            now=now,
        )

        actions.extend(dispo_actions)

        # Auto-Wiedervorlage setzen (wenn Disposition automatisches Datum vorgibt)
        if auto_follow_up and not call.follow_up_date:
            call.follow_up_date = auto_follow_up

        # ── D10: ATS-Konvertierung automatisch triggern ──
        ats_result = None
        if disposition == "voll_qualifiziert":
            try:
                from app.services.acquisition_qualify_service import AcquisitionQualifyService

                qualify_svc = AcquisitionQualifyService(self.db)
                ats_result = await qualify_svc.convert_to_ats(job_id, qualification_data)
                actions.append(f"ATSJob {ats_result['ats_job_id']} erstellt")
                actions.append(
                    f"Dossier: {ats_result['calls_transferred']} Calls, "
                    f"{ats_result['emails_transferred']} Emails uebertragen"
                )
                # convert_to_ats setzt Status + committed (inkl. Call-Record)
                return {
                    "call_id": call.id,
                    "new_status": job.akquise_status,
                    "actions": actions,
                    "ats_conversion": ats_result,
                }
            except Exception as e:
                logger.error(f"ATS-Konvertierung fehlgeschlagen fuer Job {job_id}: {e}")
                actions.append(f"WARNUNG: ATS-Konvertierung fehlgeschlagen: {str(e)}")
                # Bei Fehler: Call trotzdem speichern, Status normal weitersetzen

        # Status aktualisieren (mit State-Machine-Validierung)
        if new_status and new_status != job.akquise_status:
            self._validate_transition(job.akquise_status, new_status)
            job.akquise_status = new_status
            job.akquise_status_changed_at = now
            actions.append(f"Status → {new_status}")

        await self.db.commit()

        return {
            "call_id": call.id,
            "new_status": job.akquise_status,
            "actions": actions,
        }

    async def _process_disposition(
        self,
        job: Job,
        company: Company | None,
        contact_id: uuid.UUID | None,
        disposition: str,
        follow_up_date: datetime | None,
        follow_up_note: str | None,
        extra_data: dict,
        now: datetime,
    ) -> tuple[str | None, list[str], datetime | None]:
        """Verarbeitet Disposition und gibt (new_status, actions, auto_follow_up_date) zurueck."""
        actions: list[str] = []
        tomorrow = now + timedelta(days=1)

        # D1a: nicht_erreicht
        if disposition == "nicht_erreicht":
            return "angerufen", ["Wiedervorlage morgen"], tomorrow

        # D1b: mailbox_besprochen
        if disposition == "mailbox_besprochen":
            return "angerufen", [f"AB besprochen am {now.strftime('%d.%m.%Y')}", "Wiedervorlage morgen"], tomorrow

        # D2: besetzt
        if disposition == "besetzt":
            return "angerufen", ["Wiedervorlage morgen"], tomorrow

        # D3: falsche_nummer
        if disposition == "falsche_nummer":
            return "kontakt_fehlt", ["Kontaktdaten falsch — Lead bleibt aktiv"], None

        # D4: sekretariat
        if disposition == "sekretariat":
            # Optional: Durchwahl/Name Sekretariat auf Contact speichern
            durchwahl = extra_data.get("durchwahl")
            sek_name = extra_data.get("sekretariat_name")
            if contact_id and (durchwahl or sek_name):
                contact = await self.db.get(CompanyContact, contact_id)
                if contact:
                    if durchwahl and not contact.phone:
                        contact.phone = durchwahl
                        actions.append(f"Durchwahl {durchwahl} gespeichert")
                    if sek_name:
                        actions.append(f"Sekretariat: {sek_name}")
            return "angerufen", actions + ["Wiedervorlage morgen"], tomorrow

        # D5: kein_bedarf
        if disposition == "kein_bedarf":
            follow_180 = now + timedelta(days=180)
            return "blacklist_weich", ["Blacklist weich — Wiedervorlage in 180 Tagen"], follow_180

        # D6: nie_wieder → Blacklist-Cascade
        if disposition == "nie_wieder":
            cascade_count = await self._blacklist_cascade(job, company, now)
            actions.append(f"Blacklist hart — {cascade_count} weitere Stellen geschlossen")
            return "blacklist_hart", actions, None

        # D7: interesse_spaeter
        if disposition == "interesse_spaeter":
            if not follow_up_date:
                raise ValueError("Wiedervorlage-Datum ist Pflicht bei interesse_spaeter")
            return "wiedervorlage", [f"Wiedervorlage am {follow_up_date.strftime('%d.%m.%Y %H:%M')}"], follow_up_date

        # D8: will_infos — Kunde will Infos (Status-Aenderung passiert beim E-Mail-Versand)
        if disposition == "will_infos":
            follow_3d = now + timedelta(days=3)
            actions.append("Kunde will Infos — bitte E-Mail ueber die E-Mail-Buttons senden")
            return "kontaktiert", actions, follow_3d

        # D9: qualifiziert_erst
        if disposition == "qualifiziert_erst":
            if not follow_up_date:
                raise ValueError("Zweitkontakt-Datum ist Pflicht bei qualifiziert_erst")
            return "qualifiziert", [f"Zweitkontakt am {follow_up_date.strftime('%d.%m.%Y %H:%M')}"], follow_up_date

        # D10: voll_qualifiziert → ATSJob erstellen (wird durch QualifyService gemacht)
        if disposition == "voll_qualifiziert":
            return "stelle_erstellt", ["ATSJob wird erstellt"], None

        # D11: ap_nicht_mehr_da
        if disposition == "ap_nicht_mehr_da":
            nachfolger = extra_data.get("nachfolger")
            if contact_id:
                contact = await self.db.get(CompanyContact, contact_id)
                if contact:
                    contact.notes = (contact.notes or "") + f"\nNicht mehr im Unternehmen ({now.strftime('%d.%m.%Y')})"
                    actions.append("Contact als inaktiv markiert")
            if nachfolger:
                actions.append(f"Nachfolger: {nachfolger}")
            # Status bleibt gleich — Lead ist noch aktiv
            return None, actions, None

        # D12: andere_stelle_offen → neuer Job-Draft
        if disposition == "andere_stelle_offen":
            new_position = extra_data.get("position", "Neue Stelle")
            new_job = Job(
                company_name=job.company_name,
                company_id=job.company_id,
                position=new_position,
                city=job.city,
                postal_code=job.postal_code,
                employment_type=extra_data.get("employment_type") or job.employment_type,
                job_text=extra_data.get("notes", ""),
                acquisition_source="manual",
                akquise_status="neu",
                akquise_status_changed_at=now,
                akquise_priority=5,
                first_seen_at=now,
                last_seen_at=now,
            )
            self.db.add(new_job)
            actions.append(f"Neue Stelle '{new_position}' angelegt")
            return None, actions, None

        # D13: weiterverbunden → neuer Contact
        if disposition == "weiterverbunden":
            if job.company_id:
                new_contact = CompanyContact(
                    company_id=job.company_id,
                    first_name=extra_data.get("first_name"),
                    last_name=extra_data.get("last_name"),
                    position=extra_data.get("function"),
                    phone=extra_data.get("phone"),
                    source="manual",
                    contact_role="empfehlung",
                    phone_normalized=_normalize_phone_simple(extra_data.get("phone")),
                )
                self.db.add(new_contact)
                actions.append(f"Neuer Contact: {new_contact.first_name} {new_contact.last_name}")
            return "kontaktiert", actions, None

        logger.warning(f"Unbekannte Disposition: {disposition}")
        return None, [], None

    async def _blacklist_cascade(
        self, job: Job, company: Company | None, now: datetime,
    ) -> int:
        """Setzt alle offenen Stellen einer Firma auf blacklist_hart."""
        if not company:
            return 0

        # Company auf Blacklist setzen
        company.acquisition_status = "blacklist"

        # Alle offenen Akquise-Jobs dieser Firma auf blacklist_hart
        result = await self.db.execute(
            select(Job.id).where(
                Job.company_id == company.id,
                Job.acquisition_source.isnot(None),
                Job.id != job.id,
                Job.akquise_status.notin_(["blacklist_hart", "stelle_erstellt"]),
            )
        )
        other_job_ids = [row[0] for row in result.all()]

        if other_job_ids:
            await self.db.execute(
                update(Job)
                .where(Job.id.in_(other_job_ids))
                .values(
                    akquise_status="blacklist_hart",
                    akquise_status_changed_at=now,
                )
            )

            # Cascade-Calls dokumentieren
            for jid in other_job_ids:
                cascade_call = AcquisitionCall(
                    job_id=jid,
                    company_id=company.id,
                    call_type="erstanruf",
                    disposition="nie_wieder",
                    notes=f"Cascade von Job {job.id}",
                )
                self.db.add(cascade_call)

        return len(other_job_ids)

    def _validate_transition(self, current: str | None, target: str) -> None:
        """Prueft ob der Status-Uebergang erlaubt ist."""
        if current is None:
            return  # Erster Status-Setzen ist immer erlaubt
        allowed = ALLOWED_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise ValueError(
                f"Status-Uebergang '{current}' → '{target}' nicht erlaubt. "
                f"Erlaubt: {', '.join(sorted(allowed)) or 'keine (Endstatus)'}"
            )

    async def get_wiedervorlagen(
        self, target_date: datetime | None = None,
    ) -> list[dict]:
        """Holt faellige Wiedervorlagen fuer ein Datum (default: heute)."""
        if target_date is None:
            target_date = datetime.now(timezone.utc)

        # Wiedervorlagen: Calls mit follow_up_date <= target_date
        start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        result = await self.db.execute(
            select(AcquisitionCall)
            .where(
                AcquisitionCall.follow_up_date >= start_of_day,
                AcquisitionCall.follow_up_date < end_of_day,
            )
            .options(
                selectinload(AcquisitionCall.job),
                selectinload(AcquisitionCall.contact),
                selectinload(AcquisitionCall.company),
            )
            .order_by(AcquisitionCall.follow_up_date.asc())
        )
        calls = result.scalars().all()

        # Nur offene Wiedervorlagen (Job noch nicht abgeschlossen)
        wiedervorlagen = []
        for call in calls:
            if call.job and call.job.akquise_status not in (
                "blacklist_hart", "stelle_erstellt", "blacklist_weich",
            ):
                wiedervorlagen.append({
                    "call_id": str(call.id),
                    "job_id": str(call.job_id),
                    "company_name": call.job.company_name if call.job else None,
                    "position": call.job.position if call.job else None,
                    "contact_name": call.contact.full_name if call.contact else None,
                    "contact_phone": call.contact.phone if call.contact else None,
                    "follow_up_date": call.follow_up_date.isoformat() if call.follow_up_date else None,
                    "follow_up_note": call.follow_up_note,
                    "disposition": call.disposition,
                })

        return wiedervorlagen

    async def get_call_history(self, job_id: uuid.UUID) -> list[dict]:
        """Holt alle Anrufe zu einem Lead, neueste zuerst."""
        result = await self.db.execute(
            select(AcquisitionCall)
            .where(AcquisitionCall.job_id == job_id)
            .options(selectinload(AcquisitionCall.contact))
            .order_by(AcquisitionCall.created_at.desc())
        )
        calls = result.scalars().all()

        return [
            {
                "id": str(c.id),
                "call_type": c.call_type,
                "disposition": c.disposition,
                "notes": c.notes,
                "duration_seconds": c.duration_seconds,
                "contact_name": c.contact.full_name if c.contact else None,
                "follow_up_date": c.follow_up_date.isoformat() if c.follow_up_date else None,
                "follow_up_note": c.follow_up_note,
                "created_at": c.created_at.isoformat(),
            }
            for c in calls
        ]

    async def lookup_phone(self, phone: str) -> dict | None:
        """Sucht Telefonnummer in der DB und gibt Company/Contact/Jobs zurueck."""
        normalized = _normalize_phone_simple(phone)
        if not normalized:
            return None

        # Suche in company_contacts.phone_normalized
        result = await self.db.execute(
            select(CompanyContact)
            .where(CompanyContact.phone_normalized == normalized)
            .options(selectinload(CompanyContact.company))
            .limit(1)
        )
        contact = result.scalar_one_or_none()

        if not contact:
            # Fallback: Suche in rohen Telefonnummern
            result = await self.db.execute(
                select(CompanyContact)
                .where(
                    CompanyContact.phone.contains(phone[-8:])  # Letzte 8 Ziffern
                )
                .options(selectinload(CompanyContact.company))
                .limit(1)
            )
            contact = result.scalar_one_or_none()

        if not contact:
            return None

        # Offene Akquise-Jobs dieser Firma laden
        jobs_result = await self.db.execute(
            select(Job)
            .where(
                Job.company_id == contact.company_id,
                Job.acquisition_source.isnot(None),
                Job.akquise_status.notin_(["blacklist_hart", "stelle_erstellt"]),
                Job.deleted_at.is_(None),
            )
            .order_by(Job.akquise_priority.desc())
        )
        jobs = jobs_result.scalars().all()

        return {
            "contact": {
                "id": str(contact.id),
                "name": contact.full_name,
                "phone": contact.phone,
                "email": contact.email,
                "position": contact.position,
            },
            "company": {
                "id": str(contact.company.id) if contact.company else None,
                "name": contact.company.name if contact.company else None,
            },
            "open_jobs": [
                {
                    "id": str(j.id),
                    "position": j.position,
                    "status": j.akquise_status,
                    "priority": j.akquise_priority,
                }
                for j in jobs
            ],
        }


def _normalize_phone_simple(raw: str | None) -> str | None:
    """Einfache Telefonnummer-Normalisierung fuer Lookup."""
    if not raw or not raw.strip():
        return None
    phone = re.sub(r"[^0-9+]", "", raw.strip())
    if not phone:
        return None
    if phone.startswith("0") and not phone.startswith("00"):
        phone = "+49" + phone[1:]
    elif phone.startswith("00"):
        phone = "+" + phone[2:]
    elif not phone.startswith("+"):
        phone = "+49" + phone
    return phone[:20]
