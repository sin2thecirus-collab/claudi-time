"""UnassignedCall Model - Zwischenspeicher fuer unzugeordnete Anrufe.

Wenn ein Anruf transkribiert wird, aber die Telefonnummer keinem
Kandidaten/Kontakt/Unternehmen zugeordnet werden kann, wird das
Gespraech hier zwischengespeichert. Der User kann es spaeter manuell
zuordnen oder loeschen.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.ats_call_note import CallDirection

# Reuse CallDirection enum from ats_call_note (inbound/outbound)
# Import is above — no new enum needed


class UnassignedCall(Base):
    """Zwischenspeicher fuer Anrufe ohne bekannte Telefonnummer."""

    __tablename__ = "unassigned_calls"

    # Primaerschluessel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Anruf-Metadaten
    phone_number: Mapped[str | None] = mapped_column(String(100))
    direction: Mapped[str | None] = mapped_column(
        String(20),  # "inbound" / "outbound" — als String, nicht Enum (einfacher fuer Staging)
        nullable=True,
    )
    call_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    # Transkription + KI-Analyse
    transcript: Mapped[str | None] = mapped_column(Text)
    call_summary: Mapped[str | None] = mapped_column(Text)
    extracted_data: Mapped[dict | None] = mapped_column(JSONB)

    # Webex-Metadaten
    recording_topic: Mapped[str | None] = mapped_column(String(500))
    webex_recording_id: Mapped[str | None] = mapped_column(String(255))

    # MT-Payload (fertig aufbereitetes Kandidaten-Update)
    mt_payload: Mapped[dict | None] = mapped_column(JSONB)

    # ── Job-Quali Staging (Kontakt-Calls) ──
    call_subtype: Mapped[str | None] = mapped_column(String(50))  # kein_bedarf/follow_up/job_quali/sonstiges
    extracted_job_data: Mapped[dict | None] = mapped_column(JSONB)  # Alle Job-Quali-Felder
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    call_note_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Zuordnungs-Status
    assigned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    assigned_to_type: Mapped[str | None] = mapped_column(String(20))  # "candidate"/"company"/"contact"
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Indizes
    __table_args__ = (
        Index("ix_unassigned_calls_assigned", "assigned"),
        Index("ix_unassigned_calls_phone", "phone_number"),
        Index("ix_unassigned_calls_created", "created_at"),
    )
