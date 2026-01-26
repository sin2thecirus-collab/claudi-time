"""Pagination Schemas für das Matching-Tool."""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from app.config import Limits

T = TypeVar("T")


class PaginationParams(BaseModel):
    """Parameter für Pagination."""

    page: int = Field(default=1, ge=1, description="Seitennummer (1-basiert)")
    per_page: int = Field(
        default=Limits.PAGE_SIZE_DEFAULT,
        ge=1,
        le=Limits.PAGE_SIZE_MAX,
        description="Einträge pro Seite",
    )

    @property
    def offset(self) -> int:
        """Berechnet den Offset für die Datenbankabfrage."""
        return (self.page - 1) * self.per_page


class PaginatedResponse(BaseModel, Generic[T]):
    """Generische paginierte Response."""

    items: list[T]
    total: int = Field(description="Gesamtanzahl der Einträge")
    page: int = Field(description="Aktuelle Seite")
    per_page: int = Field(description="Einträge pro Seite")
    pages: int = Field(description="Gesamtanzahl der Seiten")

    @classmethod
    def create(
        cls,
        items: list[T],
        total: int,
        page: int,
        per_page: int,
    ) -> "PaginatedResponse[T]":
        """Factory-Methode für PaginatedResponse."""
        pages = (total + per_page - 1) // per_page if per_page > 0 else 0
        return cls(
            items=items,
            total=total,
            page=page,
            per_page=per_page,
            pages=pages,
        )

    @property
    def has_next(self) -> bool:
        """Prüft, ob eine nächste Seite existiert."""
        return self.page < self.pages

    @property
    def has_prev(self) -> bool:
        """Prüft, ob eine vorherige Seite existiert."""
        return self.page > 1
