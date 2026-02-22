"""Claude Matching Service v4 — DEPRECATED.

Dieser Service wurde durch V5 Rollen+Geo Matching ersetzt.
Alle Funktionen werden aus v5_matching_service.py re-exportiert.

Siehe: app/services/v5_matching_service.py
Siehe: MATCHING-V5.md
"""

from app.services.v5_matching_service import (  # noqa: F401
    get_status,
    run_matching,
    request_stop,
    _extract_candidate_data,
    _extract_job_data,
)
