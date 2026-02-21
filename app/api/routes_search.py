"""
Global CRM Search Endpoint — durchsucht alle Entitaeten.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.search_service import SearchService

router = APIRouter(tags=["search"])


@router.get(
    "/search/global",
    summary="Globale CRM-Suche ueber alle Entitaeten",
)
async def global_search(
    q: str = Query(..., min_length=2, description="Suchbegriff"),
    limit: int = Query(default=5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
):
    """
    Durchsucht Kandidaten, Unternehmen, Kontakte, Jobs und Stellen.
    Gibt nur Kategorien zurueck, die Treffer enthalten.
    """
    service = SearchService()
    return await service.global_search(db, q, limit=limit)
