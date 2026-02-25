"""PresentationService — Kandidaten-Vorstellung an Unternehmen (Kunde Vorstellen).

Verwaltet den gesamten Vorstellungsprozess:
- Modal-Daten laden (Match, Job, Kandidat, Unternehmen, Kontakte, Mailboxes)
- Vorstellung erstellen (ClientPresentation + CompanyCorrespondence + Candidate-Update)
- Follow-Up-Sequenz verwalten (Step 1→2→3)
- Kunden-Antwort verarbeiten (KI-klassifiziert)
- Sequenz stoppen
- Abfragen (pro Match, pro Unternehmen, aktive Sequenzen)
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update, func, and_, exists
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.client_presentation import ClientPresentation
from app.models.company import Company
from app.models.company_contact import CompanyContact
from app.models.company_correspondence import CompanyCorrespondence, CorrespondenceDirection
from app.models.job import Job
from app.models.match import Match

logger = logging.getLogger(__name__)

# ── Verfuegbare Mailboxes (fix konfiguriert) ──
MAILBOXES = [
    {"email": "hamdard@sincirus.com", "label": "Outlook (Haupt)", "type": "outlook"},
    {"email": "hamdard@sincirus-karriere.de", "label": "IONOS Karriere", "type": "ionos"},
    {"email": "m.hamdard@sincirus-karriere.de", "label": "IONOS M.Karriere", "type": "ionos"},
    {"email": "m.hamdard@jobs-sincirus.de", "label": "IONOS M.Jobs", "type": "ionos"},
    {"email": "hamdard@jobs-sincirus.de", "label": "IONOS Jobs", "type": "ionos"},
]


class PresentationService:
    """Service fuer die Kandidaten-Vorstellung an Unternehmen."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ═══════════════════════════════════════════════════════════════
    # 1. MODAL-DATEN LADEN
    # ═══════════════════════════════════════════════════════════════

    async def get_modal_data(self, match_id: UUID) -> dict:
        """Laedt alle Daten fuer das Vorstellungs-Modal.

        Laedt Match mit Job, Candidate, Company und CompanyContacts.
        Prueft ob der Kandidat bereits bei diesem Unternehmen vorgestellt wurde.

        Returns:
            dict mit match_info, job_info, candidate_info, company_info,
            contacts_list, mailboxes, already_presented
        """
        db = self.db

        # Match mit allen Relations laden
        result = await db.execute(
            select(Match)
            .options(
                selectinload(Match.job),
                selectinload(Match.candidate),
            )
            .where(Match.id == match_id)
        )
        match = result.scalar_one_or_none()

        if not match:
            logger.warning(f"get_modal_data: Match {match_id} nicht gefunden")
            return {"error": "Match nicht gefunden"}

        if not match.job:
            logger.warning(f"get_modal_data: Match {match_id} hat keinen Job")
            return {"error": "Job nicht gefunden"}

        if not match.candidate:
            logger.warning(f"get_modal_data: Match {match_id} hat keinen Kandidaten")
            return {"error": "Kandidat nicht gefunden"}

        # Company ueber Job laden (mit Kontakten)
        company = None
        contacts = []
        if match.job.company_id:
            company_result = await db.execute(
                select(Company)
                .options(selectinload(Company.contacts))
                .where(Company.id == match.job.company_id)
            )
            company = company_result.scalar_one_or_none()
            if company:
                contacts = company.contacts or []

        # Pruefen ob Kandidat bereits bei diesem Unternehmen vorgestellt wurde
        already_presented = False
        presented_entries = []
        if match.candidate.presented_at_companies and company:
            pac = match.candidate.presented_at_companies
            if isinstance(pac, list):
                for entry in pac:
                    if isinstance(entry, dict):
                        entry_company = entry.get("company", "").strip().lower()
                        entry_company_id = entry.get("company_id", "")
                        if (
                            entry_company == company.name.strip().lower()
                            or str(entry_company_id) == str(company.id)
                        ):
                            already_presented = True
                            presented_entries.append(entry)

        # Auch in client_presentations nachschauen
        existing_presentations = await db.execute(
            select(ClientPresentation)
            .where(
                and_(
                    ClientPresentation.candidate_id == match.candidate.id,
                    ClientPresentation.company_id == company.id if company else None,
                )
            )
            .order_by(ClientPresentation.created_at.desc())
        )
        existing_list = existing_presentations.scalars().all()
        if existing_list:
            already_presented = True

        # Match-Info zusammenstellen
        match_info = {
            "id": str(match.id),
            "v2_score": match.v2_score,
            "ai_score": match.ai_score,
            "empfehlung": match.empfehlung,
            "wow_faktor": match.wow_faktor,
            "wow_grund": match.wow_grund,
            "matching_method": match.matching_method,
            "presentation_status": match.presentation_status,
            "drive_time_car_min": match.drive_time_car_min,
        }

        # Job-Info
        job = match.job
        job_info = {
            "id": str(job.id),
            "position": job.position,
            "company_name": job.company_name,
            "city": job.city,
            "company_id": str(job.company_id) if job.company_id else None,
        }

        # Candidate-Info
        candidate = match.candidate
        candidate_info = {
            "id": str(candidate.id),
            "first_name": candidate.first_name,
            "last_name": candidate.last_name,
            "email": candidate.email,
            "phone": candidate.phone,
            "city": candidate.city,
            "current_position": candidate.current_position,
            "current_company": candidate.current_company,
            "candidate_number": candidate.candidate_number,
            "gender": candidate.gender,
        }

        # Company-Info
        company_info = None
        if company:
            company_info = {
                "id": str(company.id),
                "name": company.name,
                "city": company.city,
                "domain": company.domain,
                "phone": company.phone,
                "industry": company.industry,
            }

        # Kontakte serialisieren
        contacts_list = []
        for c in contacts:
            contacts_list.append({
                "id": str(c.id),
                "salutation": c.salutation,
                "first_name": c.first_name,
                "last_name": c.last_name,
                "full_name": c.full_name,
                "position": c.position,
                "email": c.email,
                "phone": c.phone,
                "mobile": c.mobile,
            })

        # Signature HTML laden (lazy import um Circular-Imports zu vermeiden)
        from app.services.email_generator_service import EmailGeneratorService
        signature_html = EmailGeneratorService(db).get_html_signature()

        # presented_at Datum ermitteln (letztes Vorstellungsdatum)
        presented_at = ""
        if presented_entries:
            presented_at = presented_entries[-1].get("date", "")
        elif existing_list:
            last = existing_list[0]  # Sortiert desc, also neueste zuerst
            if last.sent_at:
                presented_at = last.sent_at.strftime("%d.%m.%Y")

        return {
            "match": match_info,
            "job": job_info,
            "candidate": candidate_info,
            "company": company_info,
            "contacts": contacts_list,
            "mailboxes": MAILBOXES,
            "already_presented": already_presented,
            "presented_at": presented_at,
            "signature_html": signature_html,
            "previous_presentations": [
                {
                    "id": str(p.id),
                    "status": p.status,
                    "sent_at": p.sent_at.isoformat() if p.sent_at else None,
                    "email_to": p.email_to,
                    "client_response_category": p.client_response_category,
                }
                for p in existing_list
            ],
        }

    # ═══════════════════════════════════════════════════════════════
    # 2. VORSTELLUNG ERSTELLEN
    # ═══════════════════════════════════════════════════════════════

    async def create_presentation(self, data: dict) -> ClientPresentation:
        """Erstellt eine neue Kandidaten-Vorstellung.

        Erstellt:
        1. ClientPresentation Record
        2. CompanyCorrespondence Eintrag (Triple-Dokumentation)
        3. Updated presented_at_companies JSONB auf Candidate
        4. Updated Match.presentation_status und Match.presentation_sent_at

        Args:
            data: Dict mit match_id, contact_id, email_to, email_from,
                  email_subject, email_body_text, mailbox_used,
                  presentation_mode, pdf_attached, email_signature_html,
                  pdf_r2_key (optional)

        Returns:
            ClientPresentation Record
        """
        db = self.db
        now = datetime.now(timezone.utc)

        # Duplikat-Schutz: Keine aktive Vorstellung fuer diesen Match?
        dup_check = await db.execute(
            select(ClientPresentation)
            .where(
                and_(
                    ClientPresentation.match_id == data["match_id"],
                    ClientPresentation.status.in_(["sent", "followup_1", "followup_2"]),
                    ClientPresentation.sequence_active == True,
                )
            )
        )
        if dup_check.scalar_one_or_none():
            raise ValueError(
                "Es gibt bereits eine aktive Vorstellung fuer diesen Match. "
                "Bitte warten Sie auf eine Antwort oder stoppen Sie die laufende Sequenz."
            )

        # Match laden um candidate_id, job_id, company_id zu bekommen
        match_result = await db.execute(
            select(Match)
            .options(
                selectinload(Match.job),
                selectinload(Match.candidate),
            )
            .where(Match.id == data["match_id"])
        )
        match = match_result.scalar_one_or_none()

        if not match:
            raise ValueError(f"Match {data['match_id']} nicht gefunden")
        if not match.job:
            raise ValueError(f"Match {data['match_id']} hat keinen zugeordneten Job")
        if not match.candidate:
            raise ValueError(f"Match {data['match_id']} hat keinen zugeordneten Kandidaten")

        company_id = match.job.company_id
        candidate_id = match.candidate.id
        job_id = match.job.id

        # 1. ClientPresentation erstellen
        presentation = ClientPresentation(
            match_id=match.id,
            candidate_id=candidate_id,
            job_id=job_id,
            company_id=company_id,
            contact_id=data.get("contact_id"),
            email_to=data["email_to"],
            email_from=data["email_from"],
            email_subject=data["email_subject"],
            email_body_text=data.get("email_body_text"),
            email_signature_html=data.get("email_signature_html"),
            mailbox_used=data.get("mailbox_used"),
            presentation_mode=data.get("presentation_mode", "ai_generated"),
            pdf_attached=data.get("pdf_attached", True),
            pdf_r2_key=data.get("pdf_r2_key"),
            status="sent",
            sequence_active=True,
            sequence_step=1,
            sent_at=now,
        )
        db.add(presentation)
        await db.flush()  # ID generieren

        logger.info(
            f"ClientPresentation erstellt: {presentation.id} "
            f"(Match={match.id}, Kandidat={candidate_id}, "
            f"Company={company_id}, An={data['email_to']})"
        )

        # 2. CompanyCorrespondence erstellen (Triple-Dokumentation)
        if company_id:
            correspondence = CompanyCorrespondence(
                company_id=company_id,
                contact_id=data.get("contact_id"),
                direction=CorrespondenceDirection.OUTBOUND,
                subject=data["email_subject"],
                body=data.get("email_body_text"),
                sent_at=now,
            )
            db.add(correspondence)
            await db.flush()

            # Correspondence-ID auf Presentation setzen
            presentation.correspondence_id = correspondence.id

            logger.info(
                f"CompanyCorrespondence erstellt: {correspondence.id} "
                f"(Company={company_id})"
            )

        # 3. presented_at_companies JSONB auf Candidate updaten
        candidate = match.candidate
        pac = candidate.presented_at_companies or []
        if not isinstance(pac, list):
            pac = []

        # Company-Name ermitteln
        company_name = match.job.company_name
        if company_id:
            company_result = await db.execute(
                select(Company.name).where(Company.id == company_id)
            )
            company_name_row = company_result.scalar_one_or_none()
            if company_name_row:
                company_name = company_name_row

        new_entry = {
            "company": company_name,
            "company_id": str(company_id) if company_id else None,
            "date": now.strftime("%Y-%m-%d"),
            "type": "presented",
            "presentation_id": str(presentation.id),
        }
        pac.append(new_entry)

        await db.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(presented_at_companies=pac)
        )

        logger.info(
            f"Candidate {candidate_id} presented_at_companies aktualisiert: "
            f"{company_name}"
        )

        # 4. Match-Status updaten
        await db.execute(
            update(Match)
            .where(Match.id == match.id)
            .values(
                presentation_status="presented",
                presentation_sent_at=now,
            )
        )

        logger.info(f"Match {match.id} presentation_status='presented'")

        await db.commit()
        return presentation

    # ═══════════════════════════════════════════════════════════════
    # 3. FOLLOW-UP SEQUENZ-STEP UPDATEN
    # ═══════════════════════════════════════════════════════════════

    async def update_sequence_step(self, presentation_id: UUID, step: int) -> bool:
        """Updated den Sequenz-Step einer Vorstellung.

        Step 2 → followup_1 (nach 2 Tagen)
        Step 3 → followup_2 (nach 3 weiteren Tagen)

        Args:
            presentation_id: ID der Vorstellung
            step: Neuer Step (2 oder 3)

        Returns:
            True bei Erfolg, False bei Fehler
        """
        db = self.db
        now = datetime.now(timezone.utc)

        result = await db.execute(
            select(ClientPresentation)
            .where(ClientPresentation.id == presentation_id)
        )
        presentation = result.scalar_one_or_none()

        if not presentation:
            logger.warning(
                f"update_sequence_step: Presentation {presentation_id} nicht gefunden"
            )
            return False

        if not presentation.sequence_active:
            logger.warning(
                f"update_sequence_step: Presentation {presentation_id} "
                f"Sequenz ist nicht aktiv"
            )
            return False

        if step == 2:
            presentation.sequence_step = 2
            presentation.status = "followup_1"
            presentation.followup1_sent_at = now
            logger.info(
                f"Presentation {presentation_id}: Follow-Up 1 gesendet "
                f"(Step 2, Status=followup_1)"
            )
        elif step == 3:
            presentation.sequence_step = 3
            presentation.status = "followup_2"
            presentation.followup2_sent_at = now
            logger.info(
                f"Presentation {presentation_id}: Follow-Up 2 gesendet "
                f"(Step 3, Status=followup_2)"
            )
        else:
            logger.warning(
                f"update_sequence_step: Ungueltiger Step {step} "
                f"(erlaubt: 2, 3)"
            )
            return False

        db.add(presentation)
        await db.commit()
        return True

    # ═══════════════════════════════════════════════════════════════
    # 4. KUNDEN-ANTWORT VERARBEITEN
    # ═══════════════════════════════════════════════════════════════

    async def process_client_response(
        self,
        presentation_id: UUID,
        category: str,
        response_text: str,
        raw_email: str,
    ) -> bool:
        """Verarbeitet eine Kunden-Antwort auf die Vorstellung.

        Setzt:
        - client_response_category (KI-klassifiziert)
        - client_response_text (aufbereiteter Text)
        - client_response_raw (Original-E-Mail)
        - responded_at
        - sequence_active = False
        - status = "responded"

        Args:
            presentation_id: ID der Vorstellung
            category: KI-klassifizierte Kategorie
                (interesse_ja, termin_vorschlag, spaeter_melden,
                 kein_interesse, bereits_besetzt, sonstiges)
            response_text: Aufbereiteter Antwort-Text
            raw_email: Original-E-Mail-Text

        Returns:
            True bei Erfolg, False bei Fehler
        """
        db = self.db
        now = datetime.now(timezone.utc)

        result = await db.execute(
            select(ClientPresentation)
            .where(ClientPresentation.id == presentation_id)
        )
        presentation = result.scalar_one_or_none()

        if not presentation:
            logger.warning(
                f"process_client_response: Presentation {presentation_id} nicht gefunden"
            )
            return False

        presentation.client_response_category = category
        presentation.client_response_text = response_text
        presentation.client_response_raw = raw_email
        presentation.responded_at = now
        presentation.sequence_active = False
        presentation.status = "responded"

        db.add(presentation)
        await db.commit()

        logger.info(
            f"Presentation {presentation_id}: Kunden-Antwort verarbeitet "
            f"(Kategorie={category}, Status=responded)"
        )

        return True

    # ═══════════════════════════════════════════════════════════════
    # 5. SEQUENZ STOPPEN
    # ═══════════════════════════════════════════════════════════════

    async def stop_sequence(self, presentation_id: UUID) -> bool:
        """Stoppt die Follow-Up-Sequenz einer Vorstellung.

        Setzt:
        - sequence_active = False
        - status = "cancelled"

        Args:
            presentation_id: ID der Vorstellung

        Returns:
            True bei Erfolg, False bei Fehler
        """
        db = self.db

        result = await db.execute(
            select(ClientPresentation)
            .where(ClientPresentation.id == presentation_id)
        )
        presentation = result.scalar_one_or_none()

        if not presentation:
            logger.warning(
                f"stop_sequence: Presentation {presentation_id} nicht gefunden"
            )
            return False

        if not presentation.sequence_active:
            logger.info(
                f"stop_sequence: Presentation {presentation_id} "
                f"Sequenz war bereits inaktiv (Status={presentation.status})"
            )
            return True  # Idempotent — kein Fehler

        presentation.sequence_active = False
        presentation.status = "cancelled"

        db.add(presentation)
        await db.commit()

        logger.info(
            f"Presentation {presentation_id}: Sequenz gestoppt "
            f"(Status=cancelled)"
        )

        return True

    # ═══════════════════════════════════════════════════════════════
    # 6. VORSTELLUNGEN FUER EINEN MATCH
    # ═══════════════════════════════════════════════════════════════

    async def get_presentations_for_match(self, match_id: UUID) -> list:
        """Alle Vorstellungen fuer einen bestimmten Match.

        Args:
            match_id: Match-ID

        Returns:
            Liste von serialisierten Presentation-Dicts
        """
        db = self.db

        result = await db.execute(
            select(ClientPresentation)
            .options(
                selectinload(ClientPresentation.contact),
            )
            .where(ClientPresentation.match_id == match_id)
            .order_by(ClientPresentation.created_at.desc())
        )
        presentations = result.scalars().all()

        return [self._serialize_presentation(p) for p in presentations]

    # ═══════════════════════════════════════════════════════════════
    # 7. VORSTELLUNGEN FUER EIN UNTERNEHMEN
    # ═══════════════════════════════════════════════════════════════

    async def get_presentations_for_company(self, company_id: UUID) -> list:
        """Alle Vorstellungen fuer ein bestimmtes Unternehmen.

        Args:
            company_id: Unternehmens-ID

        Returns:
            Liste von serialisierten Presentation-Dicts
        """
        db = self.db

        result = await db.execute(
            select(ClientPresentation)
            .options(
                selectinload(ClientPresentation.candidate),
                selectinload(ClientPresentation.job),
                selectinload(ClientPresentation.contact),
            )
            .where(ClientPresentation.company_id == company_id)
            .order_by(ClientPresentation.created_at.desc())
        )
        presentations = result.scalars().all()

        return [self._serialize_presentation(p) for p in presentations]

    # ═══════════════════════════════════════════════════════════════
    # 8. AKTIVE SEQUENZEN
    # ═══════════════════════════════════════════════════════════════

    async def get_active_sequences(self) -> list:
        """Alle aktiven Follow-Up-Sequenzen.

        Returns:
            Liste von serialisierten Presentation-Dicts
            mit sequence_active=True
        """
        db = self.db

        result = await db.execute(
            select(ClientPresentation)
            .options(
                selectinload(ClientPresentation.candidate),
                selectinload(ClientPresentation.job),
                selectinload(ClientPresentation.company),
                selectinload(ClientPresentation.contact),
            )
            .where(ClientPresentation.sequence_active == True)
            .order_by(ClientPresentation.sent_at.asc())
        )
        presentations = result.scalars().all()

        return [self._serialize_presentation(p) for p in presentations]

    # ═══════════════════════════════════════════════════════════════
    # 9. EINZELNE VORSTELLUNG LADEN (fuer n8n Status-Checks)
    # ═══════════════════════════════════════════════════════════════

    async def get_presentation(self, presentation_id: UUID) -> dict | None:
        """Laedt eine einzelne Vorstellung per ID.

        Wird von n8n genutzt um den Status zu pruefen
        (z.B. ob Sequenz noch aktiv ist vor dem naechsten Follow-Up).

        Args:
            presentation_id: ID der Vorstellung

        Returns:
            Serialisiertes Presentation-Dict oder None
        """
        db = self.db

        result = await db.execute(
            select(ClientPresentation)
            .options(
                selectinload(ClientPresentation.candidate),
                selectinload(ClientPresentation.job),
                selectinload(ClientPresentation.company),
                selectinload(ClientPresentation.contact),
            )
            .where(ClientPresentation.id == presentation_id)
        )
        presentation = result.scalar_one_or_none()

        if not presentation:
            return None

        return self._serialize_presentation(presentation)

    # ═══════════════════════════════════════════════════════════════
    # 10. VORSTELLUNG PER E-MAIL FINDEN (fuer n8n Antwort-Matching)
    # ═══════════════════════════════════════════════════════════════

    async def find_by_email(self, email: str) -> dict | None:
        """Findet die neueste aktive Vorstellung fuer eine E-Mail-Adresse.

        Wird von n8n Workflow 3 (Antwort verarbeiten) genutzt:
        Wenn eine IMAP-Antwort eingeht, wird ueber die Absender-E-Mail
        die zugehoerige Vorstellung gesucht.

        Args:
            email: E-Mail-Adresse des Antwortenden (Kunde)

        Returns:
            Serialisiertes Presentation-Dict oder None
        """
        db = self.db

        result = await db.execute(
            select(ClientPresentation)
            .options(
                selectinload(ClientPresentation.candidate),
                selectinload(ClientPresentation.job),
                selectinload(ClientPresentation.company),
                selectinload(ClientPresentation.contact),
            )
            .where(func.lower(ClientPresentation.email_to) == email.strip().lower())
            .where(ClientPresentation.sequence_active == True)
            .order_by(ClientPresentation.created_at.desc())
        )
        presentation = result.scalar_one_or_none()

        if not presentation:
            # Auch nach inaktiven suchen (falls Sequenz bereits beendet)
            result = await db.execute(
                select(ClientPresentation)
                .options(
                    selectinload(ClientPresentation.candidate),
                    selectinload(ClientPresentation.job),
                    selectinload(ClientPresentation.company),
                    selectinload(ClientPresentation.contact),
                )
                .where(func.lower(ClientPresentation.email_to) == email.strip().lower())
                .order_by(ClientPresentation.created_at.desc())
            )
            presentation = result.scalar_one_or_none()

        if not presentation:
            return None

        return self._serialize_presentation(presentation)

    # ═══════════════════════════════════════════════════════════════
    # 11. SENT-BESTAETIGUNG (n8n meldet zurueck: E-Mail gesendet)
    # ═══════════════════════════════════════════════════════════════

    async def confirm_sent(self, presentation_id: UUID, n8n_execution_id: str | None = None) -> bool:
        """Bestaetigt, dass die E-Mail erfolgreich gesendet wurde.

        Wird von n8n Workflow 1 nach erfolgreichem E-Mail-Versand aufgerufen.
        Setzt sent_at (falls noch nicht gesetzt) und optional die n8n execution_id.

        Args:
            presentation_id: ID der Vorstellung
            n8n_execution_id: n8n Execution-ID (optional)

        Returns:
            True bei Erfolg, False bei Fehler
        """
        db = self.db
        now = datetime.now(timezone.utc)

        result = await db.execute(
            select(ClientPresentation)
            .where(ClientPresentation.id == presentation_id)
        )
        presentation = result.scalar_one_or_none()

        if not presentation:
            logger.warning(
                f"confirm_sent: Presentation {presentation_id} nicht gefunden"
            )
            return False

        if not presentation.sent_at:
            presentation.sent_at = now

        if n8n_execution_id:
            presentation.n8n_execution_id = n8n_execution_id

        db.add(presentation)
        await db.commit()

        logger.info(
            f"Presentation {presentation_id}: Versand bestaetigt "
            f"(n8n_execution_id={n8n_execution_id})"
        )

        return True

    # ═══════════════════════════════════════════════════════════════
    # 12. FALLBACK-ERGEBNIS VERARBEITEN
    # ═══════════════════════════════════════════════════════════════

    async def update_fallback_result(
        self,
        presentation_id: UUID,
        successful_email: str | None,
        attempts: list[dict],
    ) -> bool:
        """Updated das Fallback-Kaskade-Ergebnis.

        Wird von n8n Workflow 4 (Fallback-Kaskade) aufgerufen,
        nachdem die E-Mail-Kaskade durchlaufen wurde.

        Args:
            presentation_id: ID der Vorstellung
            successful_email: E-Mail die erfolgreich war (oder None wenn alle bouncten)
            attempts: Liste von Versuchen [{email, status, tried_at}, ...]

        Returns:
            True bei Erfolg, False bei Fehler
        """
        db = self.db

        result = await db.execute(
            select(ClientPresentation)
            .where(ClientPresentation.id == presentation_id)
        )
        presentation = result.scalar_one_or_none()

        if not presentation:
            logger.warning(
                f"update_fallback_result: Presentation {presentation_id} nicht gefunden"
            )
            return False

        presentation.fallback_attempts = attempts

        if successful_email:
            presentation.fallback_successful_email = successful_email
            presentation.email_to = successful_email  # Aktualisiere Empfaenger
            logger.info(
                f"Presentation {presentation_id}: Fallback erfolgreich — "
                f"{successful_email}"
            )
        else:
            # Alle E-Mails gebounced → Sequenz stoppen
            presentation.sequence_active = False
            presentation.status = "no_response"
            logger.warning(
                f"Presentation {presentation_id}: Fallback fehlgeschlagen — "
                f"Alle {len(attempts)} E-Mails gebounced"
            )

        db.add(presentation)
        await db.commit()
        return True

    # ═══════════════════════════════════════════════════════════════
    # HILFSFUNKTIONEN
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _serialize_presentation(p: ClientPresentation) -> dict:
        """Serialisiert eine ClientPresentation in ein JSON-faehiges Dict."""
        result = {
            "id": str(p.id),
            "match_id": str(p.match_id) if p.match_id else None,
            "candidate_id": str(p.candidate_id) if p.candidate_id else None,
            "job_id": str(p.job_id) if p.job_id else None,
            "company_id": str(p.company_id) if p.company_id else None,
            "contact_id": str(p.contact_id) if p.contact_id else None,
            "email_to": p.email_to,
            "email_from": p.email_from,
            "email_subject": p.email_subject,
            "mailbox_used": p.mailbox_used,
            "presentation_mode": p.presentation_mode,
            "pdf_attached": p.pdf_attached,
            "status": p.status,
            "sequence_active": p.sequence_active,
            "sequence_step": p.sequence_step,
            "sent_at": p.sent_at.isoformat() if p.sent_at else None,
            "followup1_sent_at": p.followup1_sent_at.isoformat() if p.followup1_sent_at else None,
            "followup2_sent_at": p.followup2_sent_at.isoformat() if p.followup2_sent_at else None,
            "client_response_category": p.client_response_category,
            "client_response_text": p.client_response_text,
            "responded_at": p.responded_at.isoformat() if p.responded_at else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }

        # Optionale Relations
        if hasattr(p, "candidate") and p.candidate:
            result["candidate_name"] = (
                f"{p.candidate.first_name or ''} {p.candidate.last_name or ''}".strip()
                or "Unbekannt"
            )
        if hasattr(p, "job") and p.job:
            result["job_position"] = p.job.position
            result["job_company"] = p.job.company_name
        if hasattr(p, "company") and p.company:
            result["company_name"] = p.company.name
        if hasattr(p, "contact") and p.contact:
            result["contact_name"] = p.contact.full_name

        return result
