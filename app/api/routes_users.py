"""User Management API Routes - Benutzerverwaltung."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_password
from app.database import get_db
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["Benutzerverwaltung"])


# ==================== Schemas ====================


class UserResponse(BaseModel):
    """Schema fuer User Response."""

    id: UUID
    email: str
    name: str | None = None
    role: str
    is_active: bool
    created_at: str | None = None  # ISO String
    last_login_at: str | None = None  # ISO String

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    """Schema fuer neuen User."""

    email: EmailStr
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=128)


class UserUpdate(BaseModel):
    """Schema fuer User-Update (partial)."""

    email: EmailStr | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    is_active: bool | None = None


# ==================== Helpers ====================


def _serialize_user(user: User) -> dict:
    """Konvertiert User-ORM zu Dict fuer JSON-Response."""
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


# ==================== Endpoints ====================


@router.get(
    "/",
    summary="Alle Benutzer auflisten",
)
async def list_users(db: AsyncSession = Depends(get_db)):
    """Gibt alle Benutzer zurueck."""
    result = await db.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()
    return [_serialize_user(u) for u in users]


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    summary="Neuen Benutzer anlegen",
)
async def create_user(data: UserCreate, db: AsyncSession = Depends(get_db)):
    """Erstellt einen neuen Benutzer."""
    # Email-Duplikat pruefen
    email_lower = data.email.strip().lower()
    existing = await db.execute(
        select(User).where(User.email == email_lower)
    )
    if existing.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"E-Mail '{email_lower}' ist bereits vergeben.",
        )

    user = User(
        email=email_lower,
        name=data.name.strip(),
        hashed_password=hash_password(data.password),
        role="admin",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    logger.info(f"Neuer Benutzer erstellt: {user.email} (Name: {user.name})")
    return _serialize_user(user)


@router.patch(
    "/{user_id}",
    summary="Benutzer bearbeiten",
)
async def update_user(
    user_id: UUID,
    data: UserUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Aktualisiert einen Benutzer (Name, E-Mail, Passwort, Status)."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Benutzer nicht gefunden.")

    current_email = getattr(request.state, "user_email", None)

    # Selbst-Deaktivierung verhindern
    if data.is_active is False and user.email == current_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Du kannst dich nicht selbst deaktivieren.",
        )

    if data.email is not None:
        email_lower = data.email.strip().lower()
        if email_lower != user.email:
            # Duplikat pruefen
            existing = await db.execute(
                select(User).where(User.email == email_lower)
            )
            if existing.scalars().first():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"E-Mail '{email_lower}' ist bereits vergeben.",
                )
            user.email = email_lower

    if data.name is not None:
        user.name = data.name.strip()

    if data.password is not None:
        user.hashed_password = hash_password(data.password)

    if data.is_active is not None:
        user.is_active = data.is_active

    await db.commit()
    await db.refresh(user)

    logger.info(f"Benutzer aktualisiert: {user.email}")
    return _serialize_user(user)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Benutzer loeschen",
)
async def delete_user(
    user_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Loescht einen Benutzer. Man kann sich nicht selbst loeschen."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Benutzer nicht gefunden.")

    current_email = getattr(request.state, "user_email", None)
    if user.email == current_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Du kannst dich nicht selbst loeschen.",
        )

    await db.delete(user)
    await db.commit()

    logger.info(f"Benutzer geloescht: {user.email}")
    return {"status": "ok", "message": f"Benutzer {user.email} geloescht."}
