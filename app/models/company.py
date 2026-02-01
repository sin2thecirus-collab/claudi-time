"""Company Model - Unternehmensverwaltung."""

import enum
import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import DateTime, Enum, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CompanyStatus(str, enum.Enum):
    """Status eines Unternehmens."""

    ACTIVE = "active"
    BLACKLIST = "blacklist"
    LAUFENDE_PROZESSE = "laufende_prozesse"


class Company(Base):
    """Model fuer Unternehmen."""

    __tablename__ = "companies"

    # Primaerschluessel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Basis-Informationen
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    domain: Mapped[str | None] = mapped_column(String(255))

    # Adresse
    street: Mapped[str | None] = mapped_column(String(255))
    house_number: Mapped[str | None] = mapped_column(String(20))
    postal_code: Mapped[str | None] = mapped_column(String(10))
    city: Mapped[str | None] = mapped_column(String(100))

    # Koordinaten (PostGIS)
    location_coords: Mapped[str | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326),
    )

    # Unternehmensgroesse
    employee_count: Mapped[str | None] = mapped_column(String(50))

    # Status
    status: Mapped[CompanyStatus] = mapped_column(
        Enum(CompanyStatus, values_callable=lambda x: [e.value for e in x]),
        default=CompanyStatus.ACTIVE,
    )

    # Notizen
    notes: Mapped[str | None] = mapped_column(Text)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    jobs: Mapped[list["Job"]] = relationship(
        "Job", back_populates="company",
    )
    contacts: Mapped[list["CompanyContact"]] = relationship(
        "CompanyContact", back_populates="company", cascade="all, delete-orphan",
    )
    correspondence: Mapped[list["CompanyCorrespondence"]] = relationship(
        "CompanyCorrespondence",
        back_populates="company",
        cascade="all, delete-orphan",
        order_by="CompanyCorrespondence.sent_at.desc()",
    )

    # Indizes
    __table_args__ = (
        Index("ix_companies_name", "name"),
        Index("ix_companies_city", "city"),
        Index("ix_companies_status", "status"),
        Index("ix_companies_created_at", "created_at"),
    )

    @property
    def display_address(self) -> str:
        """Gibt die formatierte Adresse zurueck."""
        parts = []
        if self.street:
            addr = self.street
            if self.house_number:
                addr += f" {self.house_number}"
            parts.append(addr)
        if self.postal_code or self.city:
            loc = " ".join(p for p in [self.postal_code, self.city] if p)
            parts.append(loc)
        return ", ".join(parts) if parts else ""
