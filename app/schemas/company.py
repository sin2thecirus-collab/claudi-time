"""Company Schemas fuer das Matching-Tool."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ── Company ──────────────────────────────────────────


class CompanyBase(BaseModel):
    """Basis-Schema fuer Unternehmen."""

    name: str = Field(min_length=1, max_length=255, description="Firmenname")
    domain: str | None = Field(default=None, max_length=255, description="Website/Domain")
    street: str | None = Field(default=None, max_length=255, description="Strasse")
    house_number: str | None = Field(default=None, max_length=20, description="Hausnummer")
    postal_code: str | None = Field(default=None, max_length=10, description="PLZ")
    city: str | None = Field(default=None, max_length=100, description="Stadt")
    phone: str | None = Field(default=None, max_length=100, description="Telefon Zentrale")
    employee_count: str | None = Field(
        default=None, max_length=50, description="Unternehmensgroesse"
    )
    notes: str | None = Field(default=None, description="Notizen")


class CompanyCreate(CompanyBase):
    """Schema fuer Unternehmen-Erstellung."""

    pass


class CompanyUpdate(BaseModel):
    """Schema fuer Unternehmen-Update (alle Felder optional)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    domain: str | None = Field(default=None, max_length=255)
    street: str | None = Field(default=None, max_length=255)
    house_number: str | None = Field(default=None, max_length=20)
    postal_code: str | None = Field(default=None, max_length=10)
    city: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=100)
    employee_count: str | None = Field(default=None, max_length=50)
    notes: str | None = None
    status: str | None = Field(default=None, description="active/blacklist/laufende_prozesse")


class CompanyResponse(BaseModel):
    """Schema fuer Unternehmen-Response."""

    id: UUID
    name: str
    domain: str | None
    street: str | None
    house_number: str | None
    postal_code: str | None
    city: str | None
    phone: str | None
    employee_count: str | None
    status: str
    notes: str | None
    display_address: str
    created_at: datetime
    updated_at: datetime

    # Aggregierte Felder
    job_count: int = 0
    contact_count: int = 0

    model_config = {"from_attributes": True}


class CompanyListResponse(BaseModel):
    """Schema fuer paginierte Unternehmen-Listen."""

    items: list[CompanyResponse]
    total: int
    page: int
    per_page: int
    pages: int


# ── CompanyContact ───────────────────────────────────


class CompanyContactCreate(BaseModel):
    """Schema fuer Kontakt-Erstellung."""

    salutation: str | None = Field(default=None, max_length=20)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    position: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=100)
    notes: str | None = None


class CompanyContactUpdate(BaseModel):
    """Schema fuer Kontakt-Update."""

    salutation: str | None = Field(default=None, max_length=20)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    position: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=100)
    notes: str | None = None


class CompanyContactResponse(BaseModel):
    """Schema fuer Kontakt-Response."""

    id: UUID
    company_id: UUID
    salutation: str | None
    first_name: str | None
    last_name: str | None
    full_name: str
    position: str | None
    email: str | None
    phone: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── CompanyCorrespondence ────────────────────────────


class CompanyCorrespondenceCreate(BaseModel):
    """Schema fuer Korrespondenz-Erstellung."""

    contact_id: UUID | None = None
    direction: str = Field(default="outbound", description="inbound/outbound")
    subject: str = Field(min_length=1, max_length=500)
    body: str | None = None
    sent_at: datetime | None = None


class CompanyCorrespondenceResponse(BaseModel):
    """Schema fuer Korrespondenz-Response."""

    id: UUID
    company_id: UUID
    contact_id: UUID | None
    direction: str
    subject: str
    body: str | None
    sent_at: datetime
    created_at: datetime

    # Optional: Kontaktname
    contact_name: str | None = None

    model_config = {"from_attributes": True}
