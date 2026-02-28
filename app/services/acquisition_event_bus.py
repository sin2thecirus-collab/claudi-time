"""In-Memory Event-Bus fuer Akquise-Events (SSE Pub-Sub).

Einfacher Broadcast: Webhook schreibt Event → alle SSE-Clients bekommen es.
Kein Redis noetig — Single-Process (Railway hat 1 Instanz).
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Alle aktiven SSE-Subscriber (asyncio.Queue pro Client)
_subscribers: list[asyncio.Queue] = []


def subscribe() -> asyncio.Queue:
    """Neuen SSE-Client registrieren. Gibt Queue zurueck."""
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers.append(q)
    logger.info("SSE-Client connected (total: %d)", len(_subscribers))
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    """SSE-Client abmelden."""
    try:
        _subscribers.remove(q)
    except ValueError:
        pass
    logger.info("SSE-Client disconnected (total: %d)", len(_subscribers))


async def publish(event_type: str, data: dict[str, Any]) -> int:
    """Event an alle SSE-Clients senden. Gibt Anzahl erreichter Clients zurueck."""
    delivered = 0
    dead: list[asyncio.Queue] = []

    for q in _subscribers:
        try:
            q.put_nowait({"event": event_type, "data": data})
            delivered += 1
        except asyncio.QueueFull:
            dead.append(q)

    # Tote Queues aufraeumen
    for q in dead:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass

    if delivered > 0:
        logger.info("Event '%s' an %d Client(s) gesendet", event_type, delivered)

    return delivered
