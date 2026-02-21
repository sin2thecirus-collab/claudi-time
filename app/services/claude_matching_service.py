"""Claude Matching Service v4 — Direktvergleich von Kandidaten und Jobs via Claude API.

Drei-Stufen-Architektur:
  Stufe 0: Harte DB-Filter (PostGIS, Rolle, Qualitaet, Duplikate)
  Stufe 1: Claude Haiku Quick-Check (JA/NEIN)
  Stufe 2: Claude Haiku/Sonnet Deep Assessment (Score + Begruendung)

WICHTIG:
  - NIEMALS persoenliche Daten an Claude senden (Namen, Email, Telefon, Adresse)
  - NIEMALS ORM-Objekte ueber API-Calls hinweg halten
  - IMMER eigene DB-Session pro Write-Operation (Railway 30s Timeout)
  - IMMER ai_score UND v2_score dual schreiben
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Konstanten ──

PROMPT_VERSION = "v4.0.1"
DEFAULT_MODEL_QUICK = "claude-haiku-4-5-20251001"
DEFAULT_MODEL_DEEP = "claude-haiku-4-5-20251001"

# Distanz-Limits (Meter fuer PostGIS)
MAX_DISTANCE_M = 40_000       # 40km fuer Claude-Matches
PROXIMITY_DISTANCE_M = 10_000  # 10km fuer Naehe-Matches (ohne Claude)

# Concurrency
SEMAPHORE_LIMIT = 3  # Max parallele Claude-Calls

# Score-Thresholds
MIN_SCORE_SAVE = 40  # Matches unter 40 werden nicht gespeichert

# Alle Finance-Rollen = eine Familie (kein Familienfilter noetig)
FINANCE_ROLES = ["fibu", "bibu", "kredibu", "debibu", "lohnbu", "stfa"]

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


# ── Quick-Check Prompt (Stufe 1) ──

QUICK_CHECK_SYSTEM = """Du bist ein Finance/Accounting Recruiter-Assistent.
Pruefe ob ein Kandidat grundsaetzlich fuer einen Job geeignet ist.
Bewerte NUR die FACHLICHE Passung anhand der TAETIGKEITEN, nicht der Berufsbezeichnung.
Berufsbezeichnungen sind in 20-30% der Faelle falsch.

Antworte IMMER als JSON:
{"pass": true/false, "reason": "Maximal 1 Satz Begruendung"}

Kein anderer Text. Nur das JSON-Objekt."""

QUICK_CHECK_USER = """KANDIDAT (ID: {candidate_id}):
- Aktuelle Taetigkeiten: {activities}
- Skills: {skills}
- ERP-Systeme: {erp}
- Gewuenschte Positionen: {desired_positions}

JOB:
- Titel: {job_position}
- Unternehmen: {job_company}, {job_city}
- Beschreibung (Auszug): {job_text_short}

Ist dieser Kandidat grundsaetzlich fachlich geeignet?"""


# ── Deep Assessment Prompt (Stufe 2) ──

DEEP_ASSESSMENT_SYSTEM = """Du bist ein erfahrener Personalberater fuer Finance/Accounting.
Bewerte ob ein Kandidat fuer eine Stelle geeignet ist.

WICHTIG: Bewerte anhand der TAETIGKEITEN im Lebenslauf, NICHT anhand der Berufsbezeichnung.
Berufsbezeichnungen sind in 20-30% der Faelle falsch oder ungenau.

Antworte IMMER als JSON:
{{
  "score": 0-100,
  "zusammenfassung": "1-2 Saetze",
  "staerken": ["Aufzaehlung der Passungspunkte"],
  "luecken": ["Aufzaehlung der Abweichungen"],
  "empfehlung": "vorstellen" | "beobachten" | "nicht_passend",
  "begruendung": "2-3 Saetze ausfuehrliche Begruendung",
  "wow_faktor": true/false,
  "wow_grund": "Nur wenn wow_faktor=true"
}}

Scoring-Richtlinien:
- 90-100: Perfekte Passung, sofort vorstellen
- 75-89: Starke Passung, kleine Luecken
- 60-74: Moderate Passung, signifikante Luecken aber Potenzial
- 40-59: Schwache Passung, groessere Abweichungen
- 0-39: Keine Passung

wow_faktor = true wenn:
- Entfernung <10 km UND fachlich >75
- Kandidat ist nicht wechselwillig ABER Match ist >85
- Kandidat bringt seltene Zusatzqualifikation mit (IFRS, Konsolidierung, etc.)

Kein anderer Text. Nur das JSON-Objekt."""

DEEP_ASSESSMENT_USER = """KANDIDAT (ID: {candidate_id}):
- Berufserfahrung: {work_history}
- Ausbildung: {education}
- Weiterbildungen/Zertifikate: {further_education}
- Skills: {skills}
- IT-Skills: {it_skills}
- ERP-Systeme: {erp}
- Gehaltsvorstellung: {salary}
- Kuendigungsfrist: {notice_period}
- Wechselbereitschaft: {willingness_to_change}
- Gewuenschte Positionen: {desired_positions}
- Kernkompetenzen: {key_activities}
- Bevorzugte Branchen: {preferred_industries}
- Vermiedene Branchen: {avoided_industries}
- Maximaler Arbeitsweg: {commute_max}
- Beschaeftigungsart: {employment_type}
- Gespraechszusammenfassung: {call_summary}
- Standort: {candidate_city}, {candidate_plz}

JOB:
- Titel: {job_position}
- Unternehmen: {job_company}
- Standort: {job_city}
- Stellenbeschreibung: {job_text}
- Unternehmensgroesse: {company_size}
- Beschaeftigungsart: {job_employment_type}
- Branche: {industry}
- Arbeitsmodell: {work_arrangement}

KONTEXT:
- Entfernung (Luftlinie): {distance_km} km"""


# ── Daten-Extraktion (ORM → Dict, KEINE persoenlichen Daten) ──

def _extract_candidate_data(row: dict) -> dict:
    """Extrahiert fachliche Kandidaten-Daten. KEINE persoenlichen Daten."""
    # work_history: Nur Positionen + Taetigkeiten, KEINE Firmennamen entfernen (sind oeffentlich)
    work_history = row.get("work_history") or []
    work_history_str = ""
    if work_history:
        entries = []
        for entry in work_history[:5]:  # Max 5 letzte Stationen
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

    # Aktuelle Taetigkeiten (fuer Stufe 1: nur die letzte Position)
    activities = ""
    if work_history and isinstance(work_history[0], dict):
        activities = work_history[0].get("description", "")[:300]

    # Education
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

    # Further education
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

    # cv_text Fallback: Wenn work_history leer, aber cv_text vorhanden
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
        "candidate_city": row.get("city") or "Unbekannt",
        "candidate_plz": row.get("postal_code") or "",
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


# ── Claude API Calls ──

async def _call_claude(
    client,
    model: str,
    system: str,
    user_message: str,
    semaphore: asyncio.Semaphore,
) -> tuple[dict | None, int, int]:
    """Ruft Claude API auf mit Semaphore-Schutz.

    Returns: (parsed_response, input_tokens, output_tokens)
    """
    async with semaphore:
        try:
            response = await asyncio.to_thread(
                client.messages.create,
                model=model,
                max_tokens=500,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            text = response.content[0].text.strip()
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens

            # JSON parsen
            # Manchmal wrapped Claude das JSON in ```json...```
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            parsed = json.loads(text)
            return parsed, input_tokens, output_tokens

        except json.JSONDecodeError as e:
            logger.warning(f"Claude JSON parse error: {e}, raw: {text[:200]}")
            return None, 0, 0
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return None, 0, 0


# ── Haupt-Matching Logik ──

async def run_matching(
    candidate_id: str | None = None,
    model_quick: str = DEFAULT_MODEL_QUICK,
    model_deep: str = DEFAULT_MODEL_DEEP,
) -> dict:
    """Fuehrt das 3-Stufen Claude Matching durch.

    Args:
        candidate_id: Wenn gesetzt, nur fuer diesen Kandidaten matchen (Ad-hoc)
        model_quick: Modell fuer Stufe 1 (Default: Haiku)
        model_deep: Modell fuer Stufe 2 (Default: Haiku)

    Returns:
        Ergebnis-Dict mit Statistiken
    """
    global _matching_status

    if _matching_status["running"]:
        return {"status": "already_running", "message": "Matching laeuft bereits"}

    _matching_status["running"] = True
    _matching_status["progress"] = {
        "stufe": "initialisierung",
        "total_pairs": 0,
        "processed_stufe_1": 0,
        "passed_stufe_1": 0,
        "processed_stufe_2": 0,
        "top_matches": 0,
        "wow_matches": 0,
        "proximity_matches": 0,
        "errors": 0,
        "cost_estimate_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
    }

    result = {
        "status": "completed",
        "pairs_filtered": 0,
        "quick_checked": 0,
        "quick_passed": 0,
        "deep_assessed": 0,
        "top_matches": 0,
        "wow_matches": 0,
        "proximity_matches": 0,
        "errors": 0,
        "cost_estimate_usd": 0.0,
    }

    try:
        from anthropic import Anthropic
        from app.config import settings
        from app.database import async_session_maker
        from app.models.match import Match, MatchStatus
        from app.models.candidate import Candidate
        from app.models.job import Job
        from sqlalchemy import select, and_, func, text, literal_column
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        if not settings.anthropic_api_key:
            _matching_status["running"] = False
            return {"status": "error", "message": "ANTHROPIC_API_KEY nicht konfiguriert"}

        client = Anthropic(api_key=settings.anthropic_api_key)
        semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)

        # ══════════════════════════════════════════════
        # STUFE 0: Harte DB-Filter
        # ══════════════════════════════════════════════
        _matching_status["progress"]["stufe"] = "stufe_0"
        logger.info("Stufe 0: Lade Kandidat-Job-Paare aus DB...")

        async with async_session_maker() as db:
            # Basis-Bedingungen fuer Kandidaten
            cand_conditions = [
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.classification_data.isnot(None),
            ]

            # Basis-Bedingungen fuer Jobs
            job_conditions = [
                Job.deleted_at.is_(None),
                Job.quality_score.in_(["high", "medium"]),
                Job.classification_data.isnot(None),
            ]

            # expires_at Filter (wenn gesetzt)
            job_conditions.append(
                (Job.expires_at.is_(None)) | (Job.expires_at > func.now())
            )

            if candidate_id:
                cand_conditions.append(Candidate.id == candidate_id)

            # Query: Alle Kandidat-Job-Paare mit Distanz
            # Distanz-Expression
            distance_expr = func.ST_Distance(
                Candidate.address_coords,
                Job.location_coords,
            ).label("distance_m")

            # Lat/Lng fuer Google Maps spaeter
            cand_lat = func.ST_Y(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lat")
            cand_lng = func.ST_X(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lng")
            job_lat = func.ST_Y(func.ST_GeomFromWKB(Job.location_coords)).label("job_lat")
            job_lng = func.ST_X(func.ST_GeomFromWKB(Job.location_coords)).label("job_lng")

            # Subquery: Bereits existierende Match-Paare
            existing_match = select(Match.id).where(
                and_(
                    Match.candidate_id == Candidate.id,
                    Match.job_id == Job.id,
                )
            ).correlate(Candidate, Job).exists()

            # ── Kategorie A: Vollstaendige Daten (Claude-Matches) ──
            claude_conditions = [
                *cand_conditions,
                *job_conditions,
                # Mindestens work_history ODER cv_text
                (Candidate.work_history.isnot(None)) | (Candidate.cv_text.isnot(None)),
                # Job muss job_text haben
                Job.job_text.isnot(None),
                func.length(Job.job_text) > 50,
                # Distanz <= 40km (wenn beide Koordinaten haben)
                func.ST_DWithin(
                    Candidate.address_coords,
                    Job.location_coords,
                    MAX_DISTANCE_M,
                ),
                # Kein existierender Match
                ~existing_match,
            ]

            claude_query = (
                select(
                    Candidate.id.label("candidate_id"),
                    # Fachliche Daten (KEINE persoenlichen!)
                    Candidate.work_history,
                    Candidate.cv_text,
                    Candidate.education,
                    Candidate.further_education,
                    Candidate.skills,
                    Candidate.it_skills,
                    Candidate.erp,
                    Candidate.salary,
                    Candidate.notice_period,
                    Candidate.willingness_to_change,
                    Candidate.desired_positions,
                    Candidate.key_activities,
                    Candidate.preferred_industries,
                    Candidate.avoided_industries,
                    Candidate.commute_max,
                    Candidate.employment_type,
                    Candidate.call_summary,
                    Candidate.city.label("candidate_city"),
                    Candidate.postal_code.label("candidate_plz"),
                    cand_lat,
                    cand_lng,
                    # Job-Daten
                    Job.id.label("job_id"),
                    Job.position,
                    Job.company_name,
                    Job.city.label("job_city"),
                    Job.job_text,
                    Job.company_size,
                    Job.employment_type.label("job_employment_type"),
                    Job.industry,
                    Job.work_arrangement,
                    Job.postal_code.label("job_plz"),
                    job_lat,
                    job_lng,
                    # Distanz
                    distance_expr,
                )
                .where(and_(*claude_conditions))
                .order_by(distance_expr.asc())
                .limit(2000)
            )

            rows = await db.execute(claude_query)
            claude_pairs = [dict(row._mapping) for row in rows.all()]

            # ── Kategorie B: Naehe-Matches (ohne Claude, <10km) ──
            proximity_conditions = [
                *cand_conditions,
                *job_conditions,
                # Unvollstaendige Daten
                (
                    (Candidate.work_history.is_(None) & Candidate.cv_text.is_(None))
                    | Job.job_text.is_(None)
                    | (func.length(func.coalesce(Job.job_text, "")) <= 50)
                ),
                # Sehr nah (<10km)
                func.ST_DWithin(
                    Candidate.address_coords,
                    Job.location_coords,
                    PROXIMITY_DISTANCE_M,
                ),
                # Kein existierender Match
                ~existing_match,
            ]

            proximity_query = (
                select(
                    Candidate.id.label("candidate_id"),
                    Candidate.city.label("candidate_city"),
                    Candidate.postal_code.label("candidate_plz"),
                    Candidate.hotlist_job_title,
                    Candidate.classification_data.label("cand_classification"),
                    cand_lat,
                    cand_lng,
                    Job.id.label("job_id"),
                    Job.position,
                    Job.company_name,
                    Job.city.label("job_city"),
                    Job.postal_code.label("job_plz"),
                    job_lat,
                    job_lng,
                    distance_expr,
                )
                .where(and_(*proximity_conditions))
                .order_by(distance_expr.asc())
                .limit(500)
            )

            rows = await db.execute(proximity_query)
            proximity_pairs = [dict(row._mapping) for row in rows.all()]

        # DB-Session GESCHLOSSEN!

        total_pairs = len(claude_pairs) + len(proximity_pairs)
        _matching_status["progress"]["total_pairs"] = total_pairs
        result["pairs_filtered"] = total_pairs

        logger.info(
            f"Stufe 0 fertig: {len(claude_pairs)} Claude-Paare, "
            f"{len(proximity_pairs)} Naehe-Paare"
        )

        if not claude_pairs and not proximity_pairs:
            result["status"] = "completed"
            result["message"] = "Keine neuen Paare gefunden."
            _matching_status["running"] = False
            _matching_status["last_run"] = datetime.now(timezone.utc).isoformat()
            _matching_status["last_run_result"] = result
            return result

        # ══════════════════════════════════════════════
        # STUFE 1: Claude Quick-Check (JA/NEIN)
        # ══════════════════════════════════════════════
        _matching_status["progress"]["stufe"] = "stufe_1"
        logger.info(f"Stufe 1: Quick-Check fuer {len(claude_pairs)} Paare...")

        passed_pairs = []
        total_tokens_in = 0
        total_tokens_out = 0

        for i, pair in enumerate(claude_pairs):
            cand_data = _extract_candidate_data(pair)
            job_data = _extract_job_data(pair)

            user_msg = QUICK_CHECK_USER.format(**cand_data, **job_data)
            parsed, t_in, t_out = await _call_claude(
                client, model_quick, QUICK_CHECK_SYSTEM, user_msg, semaphore
            )
            total_tokens_in += t_in
            total_tokens_out += t_out

            _matching_status["progress"]["processed_stufe_1"] = i + 1

            if parsed is None:
                result["errors"] += 1
                _matching_status["progress"]["errors"] += 1
                continue

            if parsed.get("pass", False):
                passed_pairs.append({
                    **pair,
                    "quick_reason": parsed.get("reason", ""),
                })
                _matching_status["progress"]["passed_stufe_1"] = len(passed_pairs)

        result["quick_checked"] = len(claude_pairs)
        result["quick_passed"] = len(passed_pairs)

        logger.info(
            f"Stufe 1 fertig: {len(passed_pairs)}/{len(claude_pairs)} bestanden"
        )

        # ══════════════════════════════════════════════
        # STUFE 2: Claude Deep Assessment
        # ══════════════════════════════════════════════
        _matching_status["progress"]["stufe"] = "stufe_2"
        logger.info(f"Stufe 2: Deep Assessment fuer {len(passed_pairs)} Paare...")

        deep_results = []

        for i, pair in enumerate(passed_pairs):
            cand_data = _extract_candidate_data(pair)
            job_data = _extract_job_data(pair)

            distance_m = pair.get("distance_m")
            distance_km = round(distance_m / 1000, 1) if distance_m else None

            user_msg = DEEP_ASSESSMENT_USER.format(
                **cand_data,
                **job_data,
                distance_km=distance_km or "Unbekannt",
            )
            parsed, t_in, t_out = await _call_claude(
                client, model_deep, DEEP_ASSESSMENT_SYSTEM, user_msg, semaphore
            )
            total_tokens_in += t_in
            total_tokens_out += t_out

            _matching_status["progress"]["processed_stufe_2"] = i + 1

            if parsed is None:
                result["errors"] += 1
                _matching_status["progress"]["errors"] += 1
                continue

            # Score clampen
            score = max(0, min(100, int(parsed.get("score", 0))))

            # Empfehlung validieren
            empfehlung = parsed.get("empfehlung", "beobachten")
            if empfehlung not in ["vorstellen", "beobachten", "nicht_passend"]:
                empfehlung = "beobachten"

            if score >= MIN_SCORE_SAVE:
                deep_results.append({
                    "candidate_id": pair["candidate_id"],
                    "job_id": pair["job_id"],
                    "distance_km": distance_km,
                    "cand_lat": pair.get("cand_lat"),
                    "cand_lng": pair.get("cand_lng"),
                    "cand_plz": pair.get("candidate_plz"),
                    "job_lat": pair.get("job_lat"),
                    "job_lng": pair.get("job_lng"),
                    "job_plz": pair.get("job_plz"),
                    "score": score,
                    "zusammenfassung": parsed.get("zusammenfassung", ""),
                    "staerken": parsed.get("staerken", []),
                    "luecken": parsed.get("luecken", []),
                    "empfehlung": empfehlung,
                    "begruendung": parsed.get("begruendung", ""),
                    "wow_faktor": bool(parsed.get("wow_faktor", False)),
                    "wow_grund": parsed.get("wow_grund"),
                    "quick_reason": pair.get("quick_reason", ""),
                    "tokens_in": t_in,
                    "tokens_out": t_out,
                })

        result["deep_assessed"] = len(passed_pairs)
        logger.info(
            f"Stufe 2 fertig: {len(deep_results)} Matches mit Score >= {MIN_SCORE_SAVE}"
        )

        # ══════════════════════════════════════════════
        # MATCHES SPEICHERN (eigene DB-Session)
        # ══════════════════════════════════════════════
        _matching_status["progress"]["stufe"] = "speichern"
        logger.info(f"Speichere {len(deep_results)} Claude-Matches + {len(proximity_pairs)} Naehe-Matches...")

        # ── Claude-Matches speichern ──
        from app.database import async_session_maker as session_maker_save

        for dr in deep_results:
            try:
                async with session_maker_save() as db:
                    match = Match(
                        id=uuid.uuid4(),
                        candidate_id=dr["candidate_id"],
                        job_id=dr["job_id"],
                        matching_method="claude_match",
                        status=MatchStatus.AI_CHECKED,
                        # Scores (Dual-Write!)
                        v2_score=float(dr["score"]),
                        ai_score=float(dr["score"]) / 100.0,
                        # Claude-Bewertung
                        ai_explanation=dr["zusammenfassung"],
                        ai_strengths=dr["staerken"] if dr["staerken"] else None,
                        ai_weaknesses=dr["luecken"] if dr["luecken"] else None,
                        ai_checked_at=datetime.now(timezone.utc),
                        # v4 Felder
                        empfehlung=dr["empfehlung"],
                        wow_faktor=dr["wow_faktor"],
                        wow_grund=dr["wow_grund"],
                        # Distanz
                        distance_km=dr["distance_km"],
                        # Quick-Check (Stufe 1 bestanden = 100)
                        quick_score=100,
                        quick_reason=dr["quick_reason"],
                        quick_scored_at=datetime.now(timezone.utc),
                        # Breakdown mit Prompt-Version + Token-Tracking
                        v2_score_breakdown={
                            "scoring_version": "v4_claude",
                            "prompt_version": PROMPT_VERSION,
                            "model_quick": model_quick,
                            "model_deep": model_deep,
                            "zusammenfassung": dr["zusammenfassung"],
                            "staerken": dr["staerken"],
                            "luecken": dr["luecken"],
                            "empfehlung": dr["empfehlung"],
                            "begruendung": dr["begruendung"],
                            "wow_faktor": dr["wow_faktor"],
                            "wow_grund": dr["wow_grund"],
                            "distance_km": dr["distance_km"],
                            "tokens_in": dr["tokens_in"],
                            "tokens_out": dr["tokens_out"],
                        },
                        v2_matched_at=datetime.now(timezone.utc),
                    )
                    db.add(match)
                    await db.commit()

                    if dr["empfehlung"] == "vorstellen":
                        result["top_matches"] += 1
                        _matching_status["progress"]["top_matches"] += 1
                    if dr["wow_faktor"]:
                        result["wow_matches"] += 1
                        _matching_status["progress"]["wow_matches"] += 1

            except Exception as e:
                logger.error(f"Match speichern fehlgeschlagen: {e}")
                result["errors"] += 1
                _matching_status["progress"]["errors"] += 1

        # ── Naehe-Matches speichern ──
        for pp in proximity_pairs:
            try:
                async with session_maker_save() as db:
                    distance_m = pp.get("distance_m")
                    distance_km = round(distance_m / 1000, 1) if distance_m else None

                    match = Match(
                        id=uuid.uuid4(),
                        candidate_id=pp["candidate_id"],
                        job_id=pp["job_id"],
                        matching_method="proximity_match",
                        status=MatchStatus.NEW,
                        distance_km=distance_km,
                        v2_score=None,  # Kein Score fuer Naehe-Matches
                        ai_score=None,
                        v2_score_breakdown={
                            "scoring_version": "v4_proximity",
                            "distance_km": distance_km,
                        },
                        v2_matched_at=datetime.now(timezone.utc),
                    )
                    db.add(match)
                    await db.commit()
                    result["proximity_matches"] += 1
                    _matching_status["progress"]["proximity_matches"] += 1

            except Exception as e:
                logger.error(f"Naehe-Match speichern fehlgeschlagen: {e}")
                result["errors"] += 1

        # ══════════════════════════════════════════════
        # GOOGLE MAPS FAHRZEIT (fuer Top-Matches)
        # ══════════════════════════════════════════════
        _matching_status["progress"]["stufe"] = "fahrzeit"

        try:
            from app.services.distance_matrix_service import distance_matrix_service
            from app.api.routes_settings import get_drive_time_threshold

            # Threshold aus Settings laden
            async with session_maker_save() as db:
                threshold = await get_drive_time_threshold(db)

            # Matches gruppiert nach Job fuer batch_drive_times
            from collections import defaultdict
            jobs_map: dict[str, list[dict]] = defaultdict(list)

            for dr in deep_results:
                if dr["score"] >= threshold and dr.get("job_lat") and dr.get("cand_lat"):
                    jobs_map[dr["job_id"]].append({
                        "candidate_id": str(dr["candidate_id"]),
                        "lat": dr["cand_lat"],
                        "lng": dr["cand_lng"],
                        "plz": dr.get("cand_plz"),
                        "job_lat": dr["job_lat"],
                        "job_lng": dr["job_lng"],
                        "job_plz": dr.get("job_plz"),
                    })

            for job_id, candidates in jobs_map.items():
                if not candidates:
                    continue

                first = candidates[0]
                drive_results = await distance_matrix_service.batch_drive_times(
                    job_lat=first["job_lat"],
                    job_lng=first["job_lng"],
                    job_plz=first.get("job_plz"),
                    candidates=candidates,
                )

                # Fahrzeiten auf Matches schreiben (eigene Session pro Job)
                async with session_maker_save() as db:
                    for cand_id_str, dt_result in drive_results.items():
                        if dt_result.status == "ok" or dt_result.status == "same_plz":
                            await db.execute(
                                text("""
                                    UPDATE matches
                                    SET drive_time_car_min = :car,
                                        drive_time_transit_min = :transit
                                    WHERE candidate_id = :cand_id
                                      AND job_id = :job_id
                                      AND matching_method = 'claude_match'
                                """),
                                {
                                    "car": dt_result.car_min,
                                    "transit": dt_result.transit_min,
                                    "cand_id": cand_id_str,
                                    "job_id": str(job_id),
                                },
                            )
                    await db.commit()

            logger.info(f"Fahrzeit fuer {len(jobs_map)} Jobs berechnet")

        except Exception as e:
            logger.warning(f"Fahrzeit-Berechnung fehlgeschlagen (nicht kritisch): {e}")

        # ══════════════════════════════════════════════
        # KOSTEN BERECHNEN
        # ══════════════════════════════════════════════
        # Haiku: $0.80/1M input, $4/1M output (Stand Feb 2026)
        cost_in = total_tokens_in * 0.80 / 1_000_000
        cost_out = total_tokens_out * 4.0 / 1_000_000
        result["cost_estimate_usd"] = round(cost_in + cost_out, 4)
        _matching_status["progress"]["cost_estimate_usd"] = result["cost_estimate_usd"]
        _matching_status["progress"]["tokens_in"] = total_tokens_in
        _matching_status["progress"]["tokens_out"] = total_tokens_out

        result["status"] = "completed"

    except Exception as e:
        logger.error(f"Matching fehlgeschlagen: {e}", exc_info=True)
        result["status"] = "error"
        result["message"] = str(e)

    finally:
        _matching_status["running"] = False
        _matching_status["last_run"] = datetime.now(timezone.utc).isoformat()
        _matching_status["last_run_result"] = result
        _matching_status["progress"]["stufe"] = "fertig"

    logger.info(f"Matching abgeschlossen: {result}")
    return result
