"""MT Training Data - Lern-Daten aus manuellen Jobtitel-Zuweisungen."""

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MTTrainingData(Base):
    """Speichert jede manuelle Jobtitel-Zuweisung als Trainingsdaten.

    Jede Zeile = eine Entscheidung des Users:
    - Welcher CV-Inhalt war da?
    - Was hat MT vorgeschlagen?
    - Was hat der User bestimmt?
    - Warum war es anders? (optional)
    """

    __tablename__ = "mt_training_data"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Was wurde klassifiziert? (candidate oder job)
    entity_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "candidate" oder "job"
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )

    # Der Input-Text (CV-Zusammenfassung oder Job-Beschreibung)
    input_text: Mapped[str | None] = mapped_column(Text)

    # Was MT dachte vs. was der User bestimmt hat
    predicted_titles: Mapped[list | None] = mapped_column(
        JSONB
    )  # Was MT vorgeschlagen hat (Liste)
    assigned_titles: Mapped[list] = mapped_column(
        JSONB, nullable=False
    )  # Was der User bestimmt hat (Liste)

    # War der Vorschlag richtig?
    was_correct: Mapped[bool | None] = mapped_column(
        default=None
    )  # True = User hat Vorschlag bestaetigt

    # Optionale Begruendung
    reasoning: Mapped[str | None] = mapped_column(Text)

    # Embedding des Inputs (fuer Aehnlichkeitssuche, als JSONB-Array)
    embedding = Column(JSONB)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
