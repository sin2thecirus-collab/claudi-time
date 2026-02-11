"""In-Memory Pipeline Progress Tracking.

Railway = Single Instance, daher reicht ein globaler Python-Dict.
Background-Task schreibt Fortschritt hierher, Polling-Endpoint liest.
Kein DB-Zugriff noetig waehrend der Pipeline laeuft.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Globaler Dict: import_job_id (str) → pipeline_progress (dict)
_pipeline_progress: dict[str, dict[str, Any]] = {}

# Set von import_job_ids die abgebrochen werden sollen
_cancel_requested: set[str] = set()


def set_progress(import_job_id: str, data: dict[str, Any]) -> None:
    """Schreibt Pipeline-Fortschritt in Memory."""
    _pipeline_progress[str(import_job_id)] = data


def get_progress(import_job_id: str) -> dict[str, Any] | None:
    """Liest Pipeline-Fortschritt aus Memory. None wenn nicht vorhanden."""
    return _pipeline_progress.get(str(import_job_id))


def cleanup_progress(import_job_id: str) -> None:
    """Entfernt Pipeline-Fortschritt nach Abschluss."""
    key = str(import_job_id)
    if key in _pipeline_progress:
        del _pipeline_progress[key]
    _cancel_requested.discard(key)
    logger.info(f"Pipeline-Progress fuer {key} aus Memory entfernt")


def request_cancel(import_job_id: str) -> None:
    """Fordert den Abbruch einer laufenden Pipeline an."""
    key = str(import_job_id)
    _cancel_requested.add(key)
    logger.info(f"Pipeline-Cancel angefordert fuer {key}")


def is_cancelled(import_job_id: str) -> bool:
    """Prueft ob ein Abbruch angefordert wurde."""
    return str(import_job_id) in _cancel_requested
