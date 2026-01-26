"""Settings Models - Prio-Städte und Filter-Presets."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Boolean, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PriorityCity(Base):
    """Model für priorisierte Städte (oben in der Liste angezeigt)."""

    __tablename__ = "priority_cities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    city_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    priority_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class FilterPreset(Base):
    """Model für gespeicherte Filter-Kombinationen."""

    __tablename__ = "filter_presets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    filter_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
