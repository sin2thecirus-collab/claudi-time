"""Wiederverwendbare Validatoren für das Matching-Tool."""

import re
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, Field

from app.config import Limits


def validate_postal_code(value: str | None) -> str | None:
    """Validiert deutsche Postleitzahlen (5 Ziffern)."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not re.match(r"^\d{5}$", value):
        raise ValueError("Postleitzahl muss 5 Ziffern haben")
    return value


def validate_city(value: str | None) -> str | None:
    """Validiert Städtenamen."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) < 2:
        raise ValueError("Stadtname muss mindestens 2 Zeichen haben")
    if len(value) > 100:
        raise ValueError("Stadtname darf maximal 100 Zeichen haben")
    return value


def validate_search_term(value: str | None) -> str | None:
    """Validiert Suchbegriffe."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) < Limits.SEARCH_MIN_LENGTH:
        raise ValueError(
            f"Suchbegriff muss mindestens {Limits.SEARCH_MIN_LENGTH} Zeichen haben"
        )
    if len(value) > Limits.SEARCH_MAX_LENGTH:
        raise ValueError(
            f"Suchbegriff darf maximal {Limits.SEARCH_MAX_LENGTH} Zeichen haben"
        )
    return value


def validate_uuid_list(value: list[UUID]) -> list[UUID]:
    """Validiert eine Liste von UUIDs für Batch-Operationen."""
    if len(value) == 0:
        raise ValueError("Liste darf nicht leer sein")
    if len(value) > Limits.BATCH_DELETE_MAX:
        raise ValueError(
            f"Maximal {Limits.BATCH_DELETE_MAX} Einträge pro Batch-Operation erlaubt"
        )
    return value


def validate_skills_list(value: list[str] | None) -> list[str] | None:
    """Validiert eine Liste von Skills."""
    if value is None:
        return None
    if len(value) > Limits.FILTER_MULTI_SELECT_MAX:
        raise ValueError(
            f"Maximal {Limits.FILTER_MULTI_SELECT_MAX} Skills auswählbar"
        )
    return [s.strip() for s in value if s.strip()]


def validate_cities_list(value: list[str] | None) -> list[str] | None:
    """Validiert eine Liste von Städten."""
    if value is None:
        return None
    if len(value) > Limits.FILTER_MULTI_SELECT_MAX:
        raise ValueError(
            f"Maximal {Limits.FILTER_MULTI_SELECT_MAX} Städte auswählbar"
        )
    return [c.strip() for c in value if c.strip()]


# Annotated Types für einfache Wiederverwendung
PostalCode = Annotated[str | None, AfterValidator(validate_postal_code)]
CityName = Annotated[str | None, AfterValidator(validate_city)]
SearchTerm = Annotated[str | None, AfterValidator(validate_search_term)]
SkillsList = Annotated[list[str] | None, AfterValidator(validate_skills_list)]
CitiesList = Annotated[list[str] | None, AfterValidator(validate_cities_list)]
BatchUUIDList = Annotated[list[UUID], AfterValidator(validate_uuid_list)]


from pydantic import BaseModel


class BatchDeleteRequest(BaseModel):
    """Request für Batch-Löschung."""

    ids: BatchUUIDList = Field(
        description=f"Liste der IDs (max. {Limits.BATCH_DELETE_MAX})"
    )


class BatchHideRequest(BaseModel):
    """Request für Batch-Ausblenden."""

    ids: BatchUUIDList = Field(
        description=f"Liste der IDs (max. {Limits.BATCH_HIDE_MAX})"
    )
