"""V5 Matching Service — Rollen + Geographie Matching.

Ersetzt V4 Claude-basiertes 3-Stufen-Matching durch:
  Phase A: PostGIS Geo-Filter (27km)
  Phase B: Rollen-Kompatibilitaet (hotlist_job_titles Ueberlappung + 3 Regeln)
  Phase C: Google Maps Fahrzeit (fuer ALLE Matches)
  Phase D: Match speichern
  Phase E: Telegram-Benachrichtigung (Auto <= 60 Min UND OEPNV <= 30 Min)

Optionale manuelle KI-Bewertung:
  run_ai_assessment() — Recruiter triggert fuer ausgewaehlte Matches

WICHTIG:
  - NIEMALS persoenliche Daten an Claude senden (Namen, Email, Telefon, Adresse)
  - NIEMALS ORM-Objekte ueber API-Calls hinweg halten
  - IMMER eigene DB-Session pro Write-Operation (Railway 30s Timeout)
  - IMMER async_session_maker verwenden (NICHT async_session_factory)
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Konstanten ──

PROMPT_VERSION = "v5.0.0"
MAX_DISTANCE_M = 27_000  # 27km fuer V5 Matches

# Telegram Fahrzeit-Limits fuer Notification
TELEGRAM_MAX_CAR_MIN = 60
TELEGRAM_MAX_TRANSIT_MIN = 30

# KI-Bewertung
DEFAULT_MODEL_AI = "claude-haiku-4-5-20251001"
AI_SEMAPHORE_LIMIT = 3


# ── Rollen-Kompatibilitaets-Matrix ──

ROLE_COMPATIBILITY = {
    # StFA <-> FiBu (bidirektional)
    "Steuerfachangestellte/r / Finanzbuchhalter/in": {"Finanzbuchhalter/in"},
    # Senior FiBu <-> FiBu (bidirektional)
    "Senior Finanzbuchhalter/in": {"Finanzbuchhalter/in"},
    # KrediBu -> FiBu (einseitig)
    "Kreditorenbuchhalter/in": {"Finanzbuchhalter/in"},
    # Rueckrichtung fuer bidirektionale Regeln
    "Finanzbuchhalter/in": {
        "Steuerfachangestellte/r / Finanzbuchhalter/in",
        "Senior Finanzbuchhalter/in",
    },
}


def _normalize_role(role: str) -> str:
    """Normalisiert Rollen-Namen fuer Vergleich.

    Behandelt Varianten wie 'Steuerfachangestellte/r' (kurz)
    vs 'Steuerfachangestellte/r / Finanzbuchhalter/in' (lang).
    """
    if role.startswith("Steuerfachangestellte"):
        return "Steuerfachangestellte/r / Finanzbuchhalter/in"
    return role


def _roles_match(candidate_roles: list[str], job_roles: list[str]) -> list[str]:
    """Prueft ob Kandidat und Job ueber Rollen zusammenpassen.

    Returns: Liste der gematchten Rollen (leer = kein Match)
    """
    cand_set = {_normalize_role(r) for r in (candidate_roles or [])}
    job_set = {_normalize_role(r) for r in (job_roles or [])}

    # 1. Direkte Ueberlappung
    direct = cand_set & job_set
    if direct:
        return list(direct)

    # 2. Kompatibilitaetsregeln
    matched = []
    for cand_role in cand_set:
        compat = ROLE_COMPATIBILITY.get(cand_role, set())
        overlap = compat & job_set
        if overlap:
            matched.extend(f"{cand_role} -> {jr}" for jr in overlap)

    return matched


# ── Status-Dict (In-Memory, fuer Live-Fortschritt) ──

_matching_status: dict = {
    "running": False,
    "progress": {},
    "last_run": None,
    "last_run_result": None,
}


def get_status() -> dict:
    """Gibt den aktuellen Matching-Status zurueck."""
    return _matching_status.copy()


# ── Stop-Flag ──
_stop_requested = False


def request_stop() -> dict:
    """Fordert den laufenden Matching-Prozess auf, sich zu stoppen."""
    global _stop_requested
    if not _matching_status["running"]:
        return {"status": "ok", "message": "Kein Lauf aktiv."}
    _stop_requested = True
    return {"status": "ok", "message": "Stop angefordert."}


def is_stop_requested() -> bool:
    """Prueft ob ein Stop angefordert wurde."""
    return _stop_requested


def clear_stop() -> None:
    """Setzt das Stop-Flag zurueck."""
    global _stop_requested
    _stop_requested = False


# ══════════════════════════════════════════════════════════════
# DATEN-EXTRAKTION (fuer optionale KI-Bewertung)
# ══════════════════════════════════════════════════════════════


def _extract_candidate_data(row: dict) -> dict:
    """Extrahiert fachliche Kandidaten-Daten. KEINE persoenlichen Daten."""
    work_history = row.get("work_history") or []
    work_history_str = ""
    if work_history:
        entries = []
        for entry in work_history[:5]:
            if isinstance(entry, dict):
                parts = []
                if entry.get("position"):
                    parts.append(f"Position: {entry['position']}")
                if entry.get("company"):
                    parts.append(f"bei {entry['company']}")
                if entry.get("start_date"):
                    parts.append(f"({entry.get('start_date', '?')} - {entry.get('end_date', 'heute')})")
                if entry.get("description"):
                    parts.append(f"Taetigkeiten: {entry['description'][:300]}")
                entries.append(" | ".join(parts))
        work_history_str = "\n".join(entries)

    activities = ""
    if work_history and isinstance(work_history[0], dict):
        activities = (work_history[0].get("description") or "")[:300]

    education = row.get("education") or []
    education_str = ""
    if education:
        edu_parts = []
        for edu in education[:3]:
            if isinstance(edu, dict):
                edu_parts.append(
                    f"{edu.get('degree', '')} {edu.get('field_of_study', '')} "
                    f"({edu.get('institution', '')})"
                )
        education_str = "; ".join(edu_parts)

    further_education = row.get("further_education") or []
    further_str = ""
    if further_education:
        if isinstance(further_education, list):
            fe_parts = []
            for fe in further_education[:5]:
                if isinstance(fe, dict):
                    fe_parts.append(fe.get("name", fe.get("title", str(fe))))
                elif isinstance(fe, str):
                    fe_parts.append(fe)
            further_str = ", ".join(fe_parts)

    skills = row.get("skills") or []
    it_skills = row.get("it_skills") or []
    erp = row.get("erp") or []

    if not work_history_str:
        cv_text = row.get("cv_text")
        if cv_text:
            work_history_str = cv_text[:2000]

    return {
        "candidate_id": str(row["candidate_id"]),
        "work_history": work_history_str or "Nicht verfuegbar",
        "activities": activities or "Nicht verfuegbar",
        "education": education_str or "Nicht verfuegbar",
        "further_education": further_str or "Keine",
        "skills": ", ".join(skills[:15]) if skills else "Keine angegeben",
        "it_skills": ", ".join(it_skills[:10]) if it_skills else "Keine angegeben",
        "erp": ", ".join(erp[:5]) if erp else "Keine angegeben",
        "salary": row.get("salary") or "Nicht angegeben",
        "notice_period": row.get("notice_period") or "Nicht angegeben",
        "willingness_to_change": row.get("willingness_to_change") or "unbekannt",
        "desired_positions": row.get("desired_positions") or "Nicht angegeben",
        "key_activities": row.get("key_activities") or "Nicht angegeben",
        "preferred_industries": row.get("preferred_industries") or "Keine Praeferenz",
        "avoided_industries": row.get("avoided_industries") or "Keine",
        "commute_max": row.get("commute_max") or "Nicht angegeben",
        "employment_type": row.get("employment_type") or "Nicht angegeben",
        "call_summary": (row.get("call_summary") or "Kein Gespraech")[:500],
        "candidate_city": row.get("candidate_city") or row.get("city") or "Unbekannt",
        "candidate_plz": row.get("candidate_plz") or row.get("postal_code") or "",
    }


def _extract_job_data(row: dict) -> dict:
    """Extrahiert Job-Daten fuer Claude."""
    job_text = row.get("job_text") or ""
    return {
        "job_id": str(row["job_id"]),
        "job_position": row.get("position") or "Unbekannt",
        "job_company": row.get("company_name") or "Unbekannt",
        "job_city": row.get("job_city") or "Unbekannt",
        "job_text": job_text[:2000] if job_text else "",
        "job_text_short": job_text[:500] if job_text else "Keine Beschreibung",
        "company_size": row.get("company_size") or "Nicht angegeben",
        "job_employment_type": row.get("job_employment_type") or "Nicht angegeben",
        "industry": row.get("industry") or "Nicht angegeben",
        "work_arrangement": row.get("work_arrangement") or "Nicht angegeben",
    }


# ── Claude API Call (fuer optionale KI-Bewertung) ──

async def _call_claude(
    client,
    model: str,
    system: str,
    user_message: str,
    semaphore: asyncio.Semaphore,
    max_tokens: int = 500,
) -> tuple[dict | None, int, int]:
    """Ruft Claude API auf mit Semaphore-Schutz.

    Returns: (parsed_response, input_tokens, output_tokens)
    """
    async with semaphore:
        try:
            response = await asyncio.to_thread(
                client.messages.create,
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            text = response.content[0].text.strip()
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens

            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            parsed = json.loads(text)
            return parsed, input_tokens, output_tokens

        except json.JSONDecodeError as e:
            logger.warning(f"Claude JSON parse error: {e}, raw: {text[:500]}")
            return None, input_tokens, output_tokens
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return None, 0, 0


# ══════════════════════════════════════════════════════════════
# V5 MATCHING — Rollen + Geographie
# ══════════════════════════════════════════════════════════════


async def run_matching(candidate_id: str | None = None) -> dict:
    """Startet V5 Matching als Background-Task.

    Phase A: Geo-Filter (27km, PostGIS)
    Phase B: Rollen-Filter (hotlist_job_titles Ueberlappung + Kompatibilitaet)
    Phase C: Google Maps Fahrzeit
    Phase D: Matches speichern
    Phase E: Telegram-Benachrichtigung
    """
    global _matching_status
    if _matching_status["running"]:
        return {"status": "error", "message": "Ein Matching laeuft bereits"}

    _matching_status["running"] = True
    _matching_status["progress"] = {
        "phase": "starting",
        "geo_pairs_found": 0,
        "role_matches": 0,
        "drive_time_done": 0,
        "drive_time_total": 0,
        "matches_saved": 0,
        "telegram_sent": 0,
        "errors": 0,
    }
    clear_stop()

    start_time = datetime.now(timezone.utc)

    async def _run_background():
        try:
            from app.database import async_session_maker
            from app.models.match import Match, MatchStatus
            from app.models.candidate import Candidate
            from app.models.job import Job
            from sqlalchemy import select, and_, func, or_, exists
            from app.services.distance_matrix_service import distance_matrix_service
            from app.services.telegram_bot_service import send_message

            progress = _matching_status["progress"]

            # ── Phase A: Geo-Filter (27km) ──
            logger.info("V5 Phase A: Geo-Filter (27km)...")
            progress["phase"] = "geo_filter"

            geo_pairs = []

            async with async_session_maker() as db:
                # Koordinaten-Spalten
                cand_lat = func.ST_Y(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lat")
                cand_lng = func.ST_X(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lng")
                job_lat = func.ST_Y(func.ST_GeomFromWKB(Job.location_coords)).label("job_lat")
                job_lng = func.ST_X(func.ST_GeomFromWKB(Job.location_coords)).label("job_lng")
                distance_expr = func.ST_Distance(
                    Candidate.address_coords, Job.location_coords
                ).label("distance_m")

                # Existing match check — NUR V5 Duplikate verhindern
                # Alte V4-Matches sollen NICHT blockieren
                existing_match = exists(
                    select(Match.id).where(
                        Match.candidate_id == Candidate.id,
                        Match.job_id == Job.id,
                        Match.matching_method == "v5_role_geo",
                    )
                )

                # Basis-Filter
                base_cand = [
                    Candidate.deleted_at.is_(None),
                    Candidate.hidden == False,  # noqa: E712
                    Candidate.classification_data.isnot(None),
                    Candidate.address_coords.isnot(None),
                ]
                base_job = [
                    Job.deleted_at.is_(None),
                    Job.quality_score.in_(["high", "medium"]),
                    Job.classification_data.isnot(None),
                    (Job.expires_at.is_(None)) | (Job.expires_at > func.now()),
                    Job.location_coords.isnot(None),
                ]

                if candidate_id:
                    base_cand.append(Candidate.id == candidate_id)

                # Haupt-Query: 27km Radius
                query = (
                    select(
                        Candidate.id.label("candidate_id"),
                        Candidate.first_name.label("candidate_first_name"),
                        Candidate.last_name.label("candidate_last_name"),
                        Candidate.hotlist_job_titles.label("candidate_roles"),
                        Candidate.hotlist_job_title.label("candidate_role"),
                        Candidate.city.label("candidate_city"),
                        Candidate.postal_code.label("candidate_plz"),
                        Candidate.current_position.label("candidate_position"),
                        cand_lat,
                        cand_lng,
                        Job.id.label("job_id"),
                        Job.position.label("job_position"),
                        Job.company_name.label("job_company"),
                        Job.city.label("job_city"),
                        Job.postal_code.label("job_plz"),
                        Job.hotlist_job_titles.label("job_roles"),
                        Job.hotlist_job_title.label("job_role"),
                        job_lat,
                        job_lng,
                        distance_expr,
                    )
                    .where(
                        and_(
                            *base_cand,
                            *base_job,
                            func.ST_DWithin(
                                Candidate.address_coords,
                                Job.location_coords,
                                MAX_DISTANCE_M,
                            ),
                            ~existing_match,
                        )
                    )
                )

                result = await db.execute(query)
                rows = result.all()

                for row in rows:
                    pair = dict(row._mapping)
                    # Distance in km
                    pair["distance_km"] = round(pair["distance_m"] / 1000, 1) if pair.get("distance_m") else None
                    geo_pairs.append(pair)

            # Session geschlossen

            progress["geo_pairs_found"] = len(geo_pairs)
            logger.info(f"V5 Phase A: {len(geo_pairs)} Paare innerhalb 27km gefunden")

            if not geo_pairs:
                logger.info("V5: Keine Geo-Paare gefunden, beende.")
                return

            if is_stop_requested():
                logger.info("V5: Stop angefordert nach Phase A")
                return

            # ── Phase B: Rollen-Filter ──
            logger.info("V5 Phase B: Rollen-Filter...")
            progress["phase"] = "role_filter"

            role_matched_pairs = []
            for pair in geo_pairs:
                # Kandidaten-Rollen: hotlist_job_titles (ARRAY) oder Fallback auf hotlist_job_title
                cand_roles = pair.get("candidate_roles") or []
                if not cand_roles and pair.get("candidate_role"):
                    cand_roles = [pair["candidate_role"]]

                # Job-Rollen: hotlist_job_titles (ARRAY) oder Fallback auf hotlist_job_title
                job_roles_list = pair.get("job_roles") or []
                if not job_roles_list and pair.get("job_role"):
                    job_roles_list = [pair["job_role"]]

                matched_roles = _roles_match(cand_roles, job_roles_list)
                if matched_roles:
                    pair["matched_roles"] = matched_roles
                    pair["candidate_roles_list"] = cand_roles
                    pair["job_roles_list"] = job_roles_list
                    role_matched_pairs.append(pair)

            progress["role_matches"] = len(role_matched_pairs)
            logger.info(f"V5 Phase B: {len(role_matched_pairs)} Rollen-Matches (von {len(geo_pairs)} Geo-Paaren)")

            if not role_matched_pairs:
                logger.info("V5: Keine Rollen-Matches, beende.")
                return

            if is_stop_requested():
                logger.info("V5: Stop angefordert nach Phase B")
                return

            # ── Phase C: Google Maps Fahrzeit ──
            logger.info("V5 Phase C: Google Maps Fahrzeit...")
            progress["phase"] = "drive_time"
            progress["drive_time_total"] = len(role_matched_pairs)

            # Gruppiere nach Job
            jobs_map: dict[str, list[dict]] = {}
            for pair in role_matched_pairs:
                jid = str(pair["job_id"])
                jobs_map.setdefault(jid, []).append(pair)

            drive_time_results: dict[str, dict] = {}  # candidate_id -> {car_min, transit_min}

            for jid, pairs_for_job in jobs_map.items():
                if is_stop_requested():
                    logger.info("V5: Stop angefordert in Phase C")
                    break

                first = pairs_for_job[0]
                j_lat = first.get("job_lat")
                j_lng = first.get("job_lng")
                j_plz = first.get("job_plz") or ""

                if not j_lat or not j_lng:
                    progress["errors"] += 1
                    continue

                candidates_batch = []
                for p in pairs_for_job:
                    c_lat = p.get("cand_lat")
                    c_lng = p.get("cand_lng")
                    if c_lat and c_lng:
                        candidates_batch.append({
                            "candidate_id": str(p["candidate_id"]),
                            "lat": c_lat,
                            "lng": c_lng,
                            "plz": p.get("candidate_plz") or "",
                        })

                if not candidates_batch:
                    continue

                try:
                    results = await distance_matrix_service.batch_drive_times(
                        job_lat=j_lat,
                        job_lng=j_lng,
                        job_plz=j_plz,
                        candidates=candidates_batch,
                    )
                    for cid, dt_result in results.items():
                        drive_time_results[cid + "_" + jid] = {
                            "car_min": dt_result.car_min,
                            "transit_min": dt_result.transit_min,
                        }
                    progress["drive_time_done"] += len(pairs_for_job)
                except Exception as e:
                    logger.error(f"V5 Google Maps Fehler fuer Job {jid}: {e}")
                    progress["errors"] += 1
                    progress["drive_time_done"] += len(pairs_for_job)

            logger.info(f"V5 Phase C: Fahrzeit fuer {len(drive_time_results)} Paare berechnet")

            if is_stop_requested():
                logger.info("V5: Stop angefordert nach Phase C")
                return

            # ── Phase D: Matches speichern ──
            logger.info("V5 Phase D: Matches speichern...")
            progress["phase"] = "saving"

            telegram_candidates = []
            now = datetime.now(timezone.utc)

            for pair in role_matched_pairs:
                if is_stop_requested():
                    break

                cid = str(pair["candidate_id"])
                jid = str(pair["job_id"])
                dt_key = cid + "_" + jid
                dt = drive_time_results.get(dt_key, {})

                try:
                    async with async_session_maker() as db:
                        match = Match(
                            id=uuid.uuid4(),
                            candidate_id=pair["candidate_id"],
                            job_id=pair["job_id"],
                            matching_method="v5_role_geo",
                            status=MatchStatus.NEW,
                            distance_km=pair.get("distance_km"),
                            drive_time_car_min=dt.get("car_min"),
                            drive_time_transit_min=dt.get("transit_min"),
                            v2_score_breakdown={
                                "scoring_version": "v5_role_geo",
                                "prompt_version": PROMPT_VERSION,
                                "candidate_roles": pair.get("candidate_roles_list", []),
                                "job_roles": pair.get("job_roles_list", []),
                                "matched_roles": pair.get("matched_roles", []),
                            },
                            v2_matched_at=now,
                        )
                        db.add(match)
                        await db.commit()

                    progress["matches_saved"] += 1

                    # Telegram-Kandidat merken
                    car = dt.get("car_min")
                    transit = dt.get("transit_min")
                    if (
                        car is not None
                        and transit is not None
                        and car <= TELEGRAM_MAX_CAR_MIN
                        and transit <= TELEGRAM_MAX_TRANSIT_MIN
                    ):
                        telegram_candidates.append({
                            "name": f"{pair.get('candidate_first_name', '')} {pair.get('candidate_last_name', '')}".strip(),
                            "role": pair.get("candidate_role") or "Unbekannt",
                            "job_position": pair.get("job_position") or "Unbekannt",
                            "job_company": pair.get("job_company") or "Unbekannt",
                            "job_city": pair.get("job_city") or "Unbekannt",
                            "distance_km": pair.get("distance_km"),
                            "car_min": car,
                            "transit_min": transit,
                        })

                except Exception as e:
                    logger.error(f"V5 Match-Speicherung Fehler: {e}")
                    progress["errors"] += 1

            logger.info(f"V5 Phase D: {progress['matches_saved']} Matches gespeichert")

            # ── Phase E: Telegram-Benachrichtigung ──
            if telegram_candidates:
                logger.info(f"V5 Phase E: {len(telegram_candidates)} Telegram-Notifications...")
                progress["phase"] = "telegram"

                for tc in telegram_candidates:
                    try:
                        msg = (
                            f"<b>Neuer Match!</b>\n\n"
                            f"Kandidat: {tc['name']} ({tc['role']})\n"
                            f"Stelle: {tc['job_position']} bei {tc['job_company']}, {tc['job_city']}\n"
                            f"Distanz: {tc['distance_km']} km\n"
                            f"Auto: {tc['car_min']} Min | OEPNV: {tc['transit_min']} Min"
                        )
                        await send_message(text=msg)
                        progress["telegram_sent"] += 1
                    except Exception as e:
                        logger.warning(f"V5 Telegram Fehler: {e}")

                logger.info(f"V5 Phase E: {progress['telegram_sent']} Notifications gesendet")

        except Exception as e:
            logger.error(f"V5 Matching Fehler: {e}", exc_info=True)
            _matching_status["progress"]["errors"] = _matching_status["progress"].get("errors", 0) + 1
        finally:
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            _matching_status["running"] = False
            _matching_status["last_run"] = start_time.isoformat()
            _matching_status["last_run_result"] = {
                "geo_pairs": _matching_status["progress"].get("geo_pairs_found", 0),
                "role_matches": _matching_status["progress"].get("role_matches", 0),
                "matches_saved": _matching_status["progress"].get("matches_saved", 0),
                "telegram_notifications": _matching_status["progress"].get("telegram_sent", 0),
                "errors": _matching_status["progress"].get("errors", 0),
                "duration_seconds": round(duration, 1),
            }
            _matching_status["progress"]["phase"] = "done"
            clear_stop()
            logger.info(f"V5 Matching abgeschlossen in {duration:.1f}s")

    asyncio.create_task(_run_background())
    return {"status": "started", "message": "V5 Matching gestartet"}


# ══════════════════════════════════════════════════════════════
# OPTIONALE KI-BEWERTUNG (manuell getriggert)
# ══════════════════════════════════════════════════════════════


async def run_ai_assessment(
    match_ids: list[str],
    custom_prompt: str | None = None,
) -> dict:
    """Bewertet ausgewaehlte Matches via Claude AI.

    Wird manuell vom Recruiter getriggert.
    Setzt ai_score, ai_strengths, ai_weaknesses, ai_checked_at auf dem Match.

    WICHTIG: Keine persoenlichen Daten an Claude senden!
    """
    global _matching_status
    if _matching_status["running"]:
        return {"status": "error", "message": "Ein Matching/Assessment laeuft bereits"}

    _matching_status["running"] = True
    _matching_status["progress"] = {
        "phase": "ai_assessment",
        "total": len(match_ids),
        "done": 0,
        "errors": 0,
    }

    async def _run_ai_background():
        try:
            from app.database import async_session_maker
            from app.models.match import Match, MatchStatus
            from app.models.candidate import Candidate
            from app.models.job import Job
            from app.models.settings import SystemSetting
            from sqlalchemy import select
            from anthropic import Anthropic
            from app.config import settings

            progress = _matching_status["progress"]

            # System-Prompt laden
            system_prompt = custom_prompt
            if not system_prompt:
                async with async_session_maker() as db:
                    result = await db.execute(
                        select(SystemSetting.value).where(
                            SystemSetting.key == "ai_assessment_prompt"
                        )
                    )
                    row = result.scalar_one_or_none()
                    system_prompt = row or _DEFAULT_AI_PROMPT

            client = Anthropic(api_key=settings.anthropic_api_key)
            semaphore = asyncio.Semaphore(AI_SEMAPHORE_LIMIT)

            for match_id_str in match_ids:
                if is_stop_requested():
                    break

                try:
                    # Daten laden (eigene Session)
                    match_data = None
                    async with async_session_maker() as db:
                        match = await db.get(Match, match_id_str)
                        if not match or not match.candidate_id or not match.job_id:
                            progress["errors"] += 1
                            progress["done"] += 1
                            continue

                        # Kandidaten-Daten
                        cand = await db.get(Candidate, match.candidate_id)
                        job = await db.get(Job, match.job_id)

                        if not cand or not job:
                            progress["errors"] += 1
                            progress["done"] += 1
                            continue

                        match_data = {
                            "match_id": str(match.id),
                            "candidate_id": str(cand.id),
                            "job_id": str(job.id),
                            "work_history": cand.work_history,
                            "cv_text": cand.cv_text,
                            "education": cand.education,
                            "further_education": cand.further_education,
                            "skills": cand.skills,
                            "it_skills": cand.it_skills,
                            "erp": cand.erp,
                            "salary": cand.salary,
                            "notice_period": cand.notice_period,
                            "desired_positions": cand.desired_positions,
                            "key_activities": cand.key_activities,
                            "candidate_city": cand.city,
                            "candidate_plz": cand.postal_code,
                            "job_text": job.job_text,
                            "position": job.position,
                            "company_name": job.company_name,
                            "job_city": job.city,
                            "company_size": job.company_size,
                            "industry": job.industry,
                            "job_employment_type": job.employment_type,
                            "work_arrangement": job.work_arrangement,
                            "distance_km": match.distance_km,
                            "drive_time_car_min": match.drive_time_car_min,
                            "drive_time_transit_min": match.drive_time_transit_min,
                        }
                    # Session geschlossen

                    if not match_data:
                        continue

                    # Daten extrahieren (Privacy!)
                    cand_data = _extract_candidate_data(match_data)
                    job_data = _extract_job_data(match_data)

                    # User-Message bauen
                    user_msg = (
                        f"KANDIDAT (ID: {cand_data['candidate_id']}):\n"
                        f"Werdegang:\n{cand_data['work_history']}\n\n"
                        f"Ausbildung: {cand_data['education']}\n"
                        f"Weiterbildung: {cand_data['further_education']}\n"
                        f"Skills: {cand_data['skills']}\n"
                        f"IT/ERP: {cand_data['it_skills']}, {cand_data['erp']}\n"
                        f"Gehalt: {cand_data['salary']}\n"
                        f"Kuendigungsfrist: {cand_data['notice_period']}\n"
                        f"Wunschposition: {cand_data['desired_positions']}\n\n"
                        f"STELLE:\n"
                        f"Position: {job_data['job_position']} bei {job_data['job_company']}\n"
                        f"Ort: {job_data['job_city']}\n"
                        f"Beschreibung:\n{job_data['job_text']}\n\n"
                        f"Entfernung: {match_data.get('distance_km', '?')} km\n"
                        f"Fahrzeit Auto: {match_data.get('drive_time_car_min', '?')} Min\n"
                        f"Fahrzeit OEPNV: {match_data.get('drive_time_transit_min', '?')} Min"
                    )

                    # Claude aufrufen (KEINE DB-Session offen!)
                    parsed, in_tokens, out_tokens = await _call_claude(
                        client=client,
                        model=DEFAULT_MODEL_AI,
                        system=system_prompt,
                        user_message=user_msg,
                        semaphore=semaphore,
                        max_tokens=800,
                    )

                    if not parsed:
                        progress["errors"] += 1
                        progress["done"] += 1
                        continue

                    # Ergebnis validieren
                    score = parsed.get("score", 0)
                    score = max(0, min(100, int(score)))
                    staerken = parsed.get("staerken", [])
                    luecken = parsed.get("luecken", [])

                    if not isinstance(staerken, list):
                        staerken = []
                    if not isinstance(luecken, list):
                        luecken = []

                    # Match aktualisieren (eigene Session)
                    async with async_session_maker() as db:
                        from sqlalchemy import update
                        await db.execute(
                            update(Match)
                            .where(Match.id == match_data["match_id"])
                            .values(
                                ai_score=score / 100.0,
                                v2_score=float(score),
                                ai_strengths=staerken[:5],
                                ai_weaknesses=luecken[:5],
                                ai_checked_at=datetime.now(timezone.utc),
                                status=MatchStatus.AI_CHECKED,
                            )
                        )
                        await db.commit()

                    progress["done"] += 1
                    logger.info(f"V5 KI-Assessment: Match {match_data['match_id']} -> Score {score}")

                except Exception as e:
                    logger.error(f"V5 KI-Assessment Fehler fuer Match {match_id_str}: {e}")
                    progress["errors"] += 1
                    progress["done"] += 1

        except Exception as e:
            logger.error(f"V5 KI-Assessment Fehler: {e}", exc_info=True)
        finally:
            _matching_status["running"] = False
            _matching_status["progress"]["phase"] = "done"
            clear_stop()
            logger.info("V5 KI-Assessment abgeschlossen")

    asyncio.create_task(_run_ai_background())
    return {"status": "started", "count": len(match_ids), "message": f"KI-Bewertung gestartet fuer {len(match_ids)} Matches"}


# ── Default KI-Prompt ──

_DEFAULT_AI_PROMPT = """Du bist ein extrem erfahrener Personalberater mit 20 Jahre Berufserfahrung im Bereich Finance und Accounting.

Bewerte bitte nach deiner Meinung und nach deinem Ermessen ob dieser Kandidat fuer diese Stelle geeignet ist.

Achte besonders auf:
- Uebereinstimmung der Taetigkeiten
- Qualifikations-Level
- Branchenerfahrung
- Software-Kenntnisse (DATEV, SAP, etc.)
- Soft Skills und Entwicklungspotenzial

Antworte NUR als JSON:
{"score": 0-100, "staerken": ["...", "..."], "luecken": ["...", "..."]}"""
