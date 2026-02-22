"""Claude Matching Service v4 — Direktvergleich von Kandidaten und Jobs via Claude API.

Drei-Stufen-Architektur:
  Stufe 0: Harte DB-Filter (PostGIS, Rolle, Qualitaet, Duplikate)
  Stufe 1: Claude Haiku Quick-Check (JA/NEIN)
  Stufe 2: Claude Haiku/Sonnet Deep Assessment (Score + Begruendung)

Kontrollierter Modus:
  - run_stufe_0() → Paare laden, Session erstellen, Milad prueft
  - run_stufe_1(session_id) → Quick-Check, Milad prueft Ergebnisse
  - run_stufe_2(session_id) → Deep Assessment, Matches speichern

Automatischer Modus:
  - run_matching() → Alle 3 Stufen hintereinander (fuer n8n Cron)

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
SEMAPHORE_LIMIT = 3       # Max parallele Claude-Calls (Stufe 1/2 — teuer)
VORFILTER_SEMAPHORE = 15  # Max parallele Haiku-Calls (Stufe 0 — billig + schnell)
VORFILTER_CHUNK_SIZE = 50 # Paare pro Chunk im Vorfilter

# Score-Thresholds
MIN_SCORE_SAVE = 75  # Nur starke Matches (75+) werden gespeichert

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


# ── Stop-Flag (wird von Background-Tasks geprueft) ──
_stop_requested = False


def request_stop() -> dict:
    """Fordert den laufenden Matching-Prozess auf, sich zu stoppen."""
    global _stop_requested
    if not _matching_status["running"]:
        return {"status": "ok", "message": "Kein Lauf aktiv."}
    _stop_requested = True
    return {"status": "ok", "message": "Stop angefordert. Lauf wird nach aktuellem Paar beendet."}


def is_stop_requested() -> bool:
    """Prueft ob ein Stop angefordert wurde."""
    return _stop_requested


def clear_stop() -> None:
    """Setzt das Stop-Flag zurueck."""
    global _stop_requested
    _stop_requested = False


# ── Session-Storage (In-Memory, fuer kontrolliertes Matching) ──

_matching_sessions: dict[str, dict] = {}


def get_session(session_id: str) -> dict | None:
    """Gibt eine Matching-Session zurueck."""
    return _matching_sessions.get(session_id)


def get_all_sessions() -> dict:
    """Gibt Uebersicht aller Sessions zurueck."""
    result = {}
    for sid, sess in _matching_sessions.items():
        result[sid] = {
            "session_id": sid,
            "created_at": sess.get("created_at"),
            "current_stufe": sess.get("current_stufe"),
            "total_claude_pairs": len(sess.get("claude_pairs", [])),
            "total_proximity_pairs": len(sess.get("proximity_pairs", [])),
            "excluded_count": len(sess.get("excluded_pairs", set())),
            "stufe_1_passed": len(sess.get("passed_pairs", [])),
            "stufe_1_failed": len(sess.get("failed_pairs", [])),
            "stufe_2_results": len(sess.get("deep_results", [])),
            "stufe_2_saved": sess.get("matches_saved", 0),
        }
    return result


def exclude_pairs_from_session(session_id: str, pairs: list[dict]) -> dict:
    """Schliesst Paare aus einer Session aus.

    Args:
        session_id: Session ID
        pairs: Liste von {"candidate_id": "...", "job_id": "..."} Dicts

    Returns:
        {"excluded": Anzahl neu ausgeschlossener, "total_excluded": Gesamtzahl}
    """
    session = _matching_sessions.get(session_id)
    if not session:
        return {"error": "Session nicht gefunden"}

    excluded = session.setdefault("excluded_pairs", set())
    new_count = 0
    for p in pairs:
        key = (str(p["candidate_id"]), str(p["job_id"]))
        if key not in excluded:
            excluded.add(key)
            new_count += 1

    return {"excluded": new_count, "total_excluded": len(excluded)}


# ── Stufe-0 LLM-Vorfilter Prompt ──

VORFILTER_SYSTEM = """Du bist ein strenger Finance-Recruiter-Filter. Deine Aufgabe: Aussortieren was NICHT passt.

STRENGE REGELN:
- Die TAETIGKEITEN des Kandidaten muessen zu den AUFGABEN der Stelle passen
- Ein Bilanzbuchhalter passt NICHT auf eine Lohnbuchhaltung-Stelle (und umgekehrt)
- Ein FiBu-Sachbearbeiter passt NICHT auf eine Leiter-Stelle
- Nur wenn die KERNAUFGABEN ueberlappen: hohe Zahl
- Im Zweifel NIEDRIG bewerten (lieber zu streng als zu locker)

Antworte NUR mit einer Zahl von 0 bis 100. Kein Text, nur die Zahl.

Orientierung:
0-30 = Komplett anderes Fachgebiet oder Level
31-50 = Gleicher Bereich aber andere Spezialisierung
51-70 = Teilweise passend, aber wichtige Luecken
71-85 = Gute Passung, Kerntaetigkeiten ueberlappen
86-100 = Sehr gute Passung, fast identisches Profil"""

VORFILTER_USER = """KANDIDAT:
- Rolle: {candidate_role}
- Position: {current_position}
- Taetigkeiten: {current_activities}

STELLE:
- Rolle: {job_role}
- Titel: {job_position}
- Aufgaben: {job_tasks}

Passung (0-100):"""

VORFILTER_MIN_SCORE = 65  # Strenger Prompt → niedrigerer Threshold reicht


# ── Quick-Check Prompt (Stufe 1) ──

QUICK_CHECK_SYSTEM = """Du bist ein Finance/Accounting Recruiter-Assistent.
Pruefe ob ein Kandidat grundsaetzlich fuer einen Job geeignet ist.
Bewerte NUR die FACHLICHE Passung anhand der TAETIGKEITEN, nicht der Berufsbezeichnung.
Berufsbezeichnungen sind in 20-30% der Faelle falsch.

Antworte IMMER als JSON:
{"pass": true/false, "reason": "Maximal 1 Satz Begruendung"}

Kein anderer Text. Nur das JSON-Objekt."""

QUICK_CHECK_USER = """KANDIDAT (ID: {candidate_id}):
- Berufserfahrung:
{work_history}
- Ausbildung / Qualifikationen: {education}
- Weiterbildung / Zertifikate: {further_education}
- Skills: {skills}
- ERP-Systeme: {erp}
- Gewuenschte Positionen: {desired_positions}

JOB:
- Titel: {job_position}
- Unternehmen: {job_company}, {job_city}
- Beschreibung (Auszug): {job_text_short}

Ist dieser Kandidat grundsaetzlich fachlich geeignet?"""


# ── Deep Assessment Prompt (Stufe 2) ──

DEEP_ASSESSMENT_SYSTEM = """Du bist ein erfahrener Personalberater fuer Finance & Accounting.

Schau dir den Werdegang des Kandidaten an: Wo kommt er her, was hat er gemacht, wo steht er jetzt? Dann schau dir die Stellenausschreibung an: Welche Aufgaben, welche Anforderungen? Passt das zusammen?

Ein wichtiger Hinweis: Bei Bilanzbuchhalter-Stellen muss der Kandidat eine Bilanzbuchhalter-Zertifizierung haben (steht bei Ausbildung oder Weiterbildung). Ausserdem: "Eigenstaendige Erstellung der Abschluesse" = Bilanzbuchhalter. "Unterstuetzung/Mitwirkung/Zuarbeit bei Abschluessen" = Finanzbuchhalter, KEIN Bilanzbuchhalter.

Antworte NUR als JSON:
- Gute Passung (Score >= 75):
{{"score": 75-100, "zusammenfassung": "1-2 Saetze", "staerken": ["..."], "luecken": ["..."], "empfehlung": "vorstellen", "wow_faktor": true/false, "wow_grund": "nur wenn true"}}
- Schwache Passung:
{{"score": 0, "empfehlung": "nicht_passend"}}"""

DEEP_ASSESSMENT_USER = """KANDIDAT:
- Berufserfahrung:
{work_history}
- Ausbildung: {education}
- Weiterbildungen/Zertifikate: {further_education}
- IT-Skills/ERP: {it_skills}

JOB:
- Titel: {job_position}
- Stellenbeschreibung: {job_text}

Entfernung: {distance_km} km"""


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
        activities = (work_history[0].get("description") or "")[:300]

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


# ── Claude API Calls ──

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

            # JSON parsen
            # Manchmal wrapped Claude das JSON in ```json...```
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            parsed = json.loads(text)
            logger.info(f"Claude response: {text[:300]}")
            return parsed, input_tokens, output_tokens

        except json.JSONDecodeError as e:
            logger.warning(f"Claude JSON parse error: {e}, raw: {text[:500]}")
            # Tokens trotzdem tracken (API wurde aufgerufen!)
            return None, input_tokens, output_tokens
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return None, 0, 0


# ── Kosten-Schaetzung ──

def _estimate_cost(pairs_count: int, stufe: str) -> dict:
    """Schaetzt die Kosten fuer eine Stufe.

    Haiku: $0.80/1M input, $4.00/1M output
    Stufe 0 Vorfilter: ~100 input tokens, ~5 output tokens pro Paar
    Stufe 1: ~400 input tokens, ~50 output tokens pro Paar
    Stufe 2: ~1200 input tokens, ~400 output tokens pro Paar
    """
    if stufe == "stufe_0":
        est_in = pairs_count * 100
        est_out = pairs_count * 5
    elif stufe == "stufe_1":
        est_in = pairs_count * 400
        est_out = pairs_count * 50
    elif stufe == "stufe_2":
        est_in = pairs_count * 1200
        est_out = pairs_count * 400
    else:
        return {"cost_estimate_usd": 0.0, "tokens_estimate_in": 0, "tokens_estimate_out": 0}

    cost = (est_in * 0.80 + est_out * 4.0) / 1_000_000
    return {
        "cost_estimate_usd": round(cost, 4),
        "tokens_estimate_in": est_in,
        "tokens_estimate_out": est_out,
    }


# ══════════════════════════════════════════════════════════════
# KONTROLLIERTES MATCHING — Stufe fuer Stufe
# ══════════════════════════════════════════════════════════════

async def run_stufe_0(candidate_id: str | None = None) -> dict:
    """Stufe 0: Geo-Kaskade + Claude Haiku LLM-Vorfilter.

    Schritt 1 (Geo-Kaskade): PLZ gleich → Paar, Stadt gleich → Paar, ≤40km → Paar
    Schritt 2 (LLM-Vorfilter): Haiku prueft aktuelle Position vs. job_tasks → Prozent ≥ 70%

    Laeuft als Background-Task. Fortschritt via get_status().

    Returns:
        {"session_id": "...", "status": "running", ...} — sofort
        Ergebnis wird in _matching_sessions gespeichert
    """
    global _matching_status
    if _matching_status["running"]:
        return {"status": "error", "message": "Ein Matching laeuft bereits"}

    session_id = str(uuid.uuid4())[:8]

    _matching_status["running"] = True
    _matching_status["progress"] = {
        "stufe": "stufe_0",
        "session_id": session_id,
        "phase": "geo_plz",
        "total_geo_pairs": 0,
        "vorfilter_total": 0,
        "vorfilter_done": 0,
        "vorfilter_passed": 0,
        "vorfilter_failed": 0,
        "errors": 0,
        "cost_estimate_usd": 0.0,
    }

    async def _run_stufe_0_background():
        try:
            from app.database import async_session_maker
            from app.models.match import Match
            from app.models.candidate import Candidate
            from app.models.job import Job
            from sqlalchemy import select, and_, func, or_
            from anthropic import Anthropic
            from app.config import settings

            # ── Schritt 1: Geo-Kaskade (3 separate schnelle Queries) ──
            logger.info("Stufe 0, Schritt 1: Geo-Kaskade (3 Queries: PLZ → Stadt → 40km)...")
            _matching_status["progress"]["phase"] = "geo_plz"

            # Gemeinsame Select-Spalten fuer alle 3 Queries
            def _build_select_columns(cand_lat, cand_lng, job_lat, job_lng, distance_expr):
                return [
                    Candidate.id.label("candidate_id"),
                    Candidate.first_name.label("candidate_first_name"),
                    Candidate.last_name.label("candidate_last_name"),
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
                    Candidate.hotlist_job_title.label("candidate_role"),
                    Candidate.current_position.label("candidate_position"),
                    Candidate.classification_data.label("cand_classification"),
                    cand_lat,
                    cand_lng,
                    Job.id.label("job_id"),
                    Job.position,
                    Job.company_name,
                    Job.city.label("job_city"),
                    Job.job_text,
                    Job.job_tasks,
                    Job.company_size,
                    Job.employment_type.label("job_employment_type"),
                    Job.industry,
                    Job.work_arrangement,
                    Job.postal_code.label("job_plz"),
                    Job.classification_data.label("job_classification"),
                    job_lat,
                    job_lng,
                    distance_expr,
                ]

            seen_pair_keys = set()  # Duplikate vermeiden (cand_id, job_id)
            geo_pairs = []

            # Gemeinsame Basis-Bedingungen
            base_cand = [
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.classification_data.isnot(None),
            ]
            base_job = [
                Job.deleted_at.is_(None),
                Job.quality_score.in_(["high", "medium"]),
                Job.classification_data.isnot(None),
                (Job.expires_at.is_(None)) | (Job.expires_at > func.now()),
            ]
            base_data = [
                (Candidate.work_history.isnot(None)) | (Candidate.cv_text.isnot(None)),
                Job.job_text.isnot(None),
                func.length(Job.job_text) > 50,
            ]

            if candidate_id:
                base_cand.append(Candidate.id == candidate_id)

            # ── Query 1: PLZ gleich ──
            async with async_session_maker() as db:
                existing_match = select(Match.id).where(
                    and_(Match.candidate_id == Candidate.id, Match.job_id == Job.id)
                ).correlate(Candidate, Job).exists()

                distance_expr = func.ST_Distance(
                    Candidate.address_coords, Job.location_coords
                ).label("distance_m")
                cand_lat = func.ST_Y(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lat")
                cand_lng = func.ST_X(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lng")
                job_lat = func.ST_Y(func.ST_GeomFromWKB(Job.location_coords)).label("job_lat")
                job_lng = func.ST_X(func.ST_GeomFromWKB(Job.location_coords)).label("job_lng")

                cols = _build_select_columns(cand_lat, cand_lng, job_lat, job_lng, distance_expr)

                plz_query = select(*cols).where(and_(
                    *base_cand, *base_job, *base_data,
                    Candidate.postal_code.isnot(None),
                    Job.postal_code.isnot(None),
                    Candidate.postal_code == Job.postal_code,
                    ~existing_match,
                ))
                rows = await db.execute(plz_query)
                for row in rows.all():
                    d = dict(row._mapping)
                    key = (str(d["candidate_id"]), str(d["job_id"]))
                    if key not in seen_pair_keys:
                        seen_pair_keys.add(key)
                        geo_pairs.append(d)

            logger.info(f"Geo-Kaskade PLZ: {len(geo_pairs)} Paare")
            _matching_status["progress"]["phase"] = "geo_stadt"
            _matching_status["progress"]["total_geo_pairs"] = len(geo_pairs)

            # ── Query 2: Stadt gleich (nicht schon durch PLZ gefunden) ──
            async with async_session_maker() as db:
                existing_match = select(Match.id).where(
                    and_(Match.candidate_id == Candidate.id, Match.job_id == Job.id)
                ).correlate(Candidate, Job).exists()

                distance_expr = func.ST_Distance(
                    Candidate.address_coords, Job.location_coords
                ).label("distance_m")
                cand_lat = func.ST_Y(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lat")
                cand_lng = func.ST_X(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lng")
                job_lat = func.ST_Y(func.ST_GeomFromWKB(Job.location_coords)).label("job_lat")
                job_lng = func.ST_X(func.ST_GeomFromWKB(Job.location_coords)).label("job_lng")

                cols = _build_select_columns(cand_lat, cand_lng, job_lat, job_lng, distance_expr)

                city_query = select(*cols).where(and_(
                    *base_cand, *base_job, *base_data,
                    Candidate.city.isnot(None),
                    or_(Job.city.isnot(None), Job.work_location_city.isnot(None)),
                    or_(
                        func.lower(Candidate.city) == func.lower(Job.city),
                        func.lower(Candidate.city) == func.lower(Job.work_location_city),
                    ),
                    ~existing_match,
                ))
                rows = await db.execute(city_query)
                new_city = 0
                for row in rows.all():
                    d = dict(row._mapping)
                    key = (str(d["candidate_id"]), str(d["job_id"]))
                    if key not in seen_pair_keys:
                        seen_pair_keys.add(key)
                        geo_pairs.append(d)
                        new_city += 1

            logger.info(f"Geo-Kaskade Stadt: +{new_city} neue Paare (gesamt: {len(geo_pairs)})")
            _matching_status["progress"]["phase"] = "geo_distanz"
            _matching_status["progress"]["total_geo_pairs"] = len(geo_pairs)

            # ── Query 3: ≤40km Luftlinie — PRO JOB (Spatial Index nutzen) ──
            # Cross-Join ST_DWithin ueber alle Kandidaten×Jobs ist zu langsam.
            # Stattdessen: Jobs laden, pro Job die Kandidaten im Umkreis suchen.
            new_dist = 0

            # Zuerst alle relevanten Job-IDs + Koordinaten laden
            async with async_session_maker() as db:
                job_rows = await db.execute(
                    select(
                        Job.id,
                        func.ST_Y(func.ST_GeomFromWKB(Job.location_coords)).label("lat"),
                        func.ST_X(func.ST_GeomFromWKB(Job.location_coords)).label("lng"),
                    ).where(and_(
                        *base_job,
                        Job.location_coords.isnot(None),
                        Job.job_text.isnot(None),
                        func.length(Job.job_text) > 50,
                    ))
                )
                job_list = [(str(r.id), r.lat, r.lng) for r in job_rows.all()]

            logger.info(f"Geo-Kaskade 40km: {len(job_list)} Jobs mit Koordinaten")

            for job_idx, (job_id_str, _, _) in enumerate(job_list):
                if job_idx % 50 == 0 and job_idx > 0:
                    logger.info(f"Geo-Kaskade 40km: Job {job_idx}/{len(job_list)}, +{new_dist} neue Paare")

                async with async_session_maker() as db:
                    from sqlalchemy import cast
                    from sqlalchemy.dialects.postgresql import UUID as PG_UUID

                    existing_match = select(Match.id).where(
                        and_(Match.candidate_id == Candidate.id, Match.job_id == Job.id)
                    ).correlate(Candidate, Job).exists()

                    distance_expr = func.ST_Distance(
                        Candidate.address_coords, Job.location_coords
                    ).label("distance_m")
                    cand_lat = func.ST_Y(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lat")
                    cand_lng = func.ST_X(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lng")
                    job_lat = func.ST_Y(func.ST_GeomFromWKB(Job.location_coords)).label("job_lat")
                    job_lng = func.ST_X(func.ST_GeomFromWKB(Job.location_coords)).label("job_lng")

                    cols = _build_select_columns(cand_lat, cand_lng, job_lat, job_lng, distance_expr)

                    dist_query = select(*cols).where(and_(
                        *base_cand, *base_data,
                        Job.id == cast(job_id_str, PG_UUID),
                        Candidate.address_coords.isnot(None),
                        func.ST_DWithin(
                            Candidate.address_coords,
                            Job.location_coords,
                            MAX_DISTANCE_M,
                        ),
                        ~existing_match,
                    ))
                    rows = await db.execute(dist_query)
                    for row in rows.all():
                        d = dict(row._mapping)
                        key = (str(d["candidate_id"]), str(d["job_id"]))
                        if key not in seen_pair_keys:
                            seen_pair_keys.add(key)
                            geo_pairs.append(d)
                            new_dist += 1

            logger.info(f"Geo-Kaskade 40km: +{new_dist} neue Paare (gesamt: {len(geo_pairs)})")
            _matching_status["progress"]["total_geo_pairs"] = len(geo_pairs)

            # ── Naehe-Matches (ohne Claude, unvollstaendige Daten, <10km) ──
            proximity_pairs = []
            async with async_session_maker() as db:
                existing_match = select(Match.id).where(
                    and_(Match.candidate_id == Candidate.id, Match.job_id == Job.id)
                ).correlate(Candidate, Job).exists()

                distance_expr = func.ST_Distance(
                    Candidate.address_coords, Job.location_coords
                ).label("distance_m")
                cand_lat = func.ST_Y(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lat")
                cand_lng = func.ST_X(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lng")
                job_lat = func.ST_Y(func.ST_GeomFromWKB(Job.location_coords)).label("job_lat")
                job_lng = func.ST_X(func.ST_GeomFromWKB(Job.location_coords)).label("job_lng")

                proximity_query = (
                    select(
                        Candidate.id.label("candidate_id"),
                        Candidate.first_name.label("candidate_first_name"),
                        Candidate.last_name.label("candidate_last_name"),
                        Candidate.city.label("candidate_city"),
                        Candidate.postal_code.label("candidate_plz"),
                        Candidate.hotlist_job_title.label("candidate_role"),
                        Candidate.classification_data.label("cand_classification"),
                        cand_lat, cand_lng,
                        Job.id.label("job_id"),
                        Job.position,
                        Job.company_name,
                        Job.city.label("job_city"),
                        Job.postal_code.label("job_plz"),
                        job_lat, job_lng,
                        distance_expr,
                    )
                    .where(and_(
                        *base_cand, *base_job,
                        (
                            (Candidate.work_history.is_(None) & Candidate.cv_text.is_(None))
                            | Job.job_text.is_(None)
                            | (func.length(func.coalesce(Job.job_text, "")) <= 50)
                        ),
                        func.ST_DWithin(
                            Candidate.address_coords,
                            Job.location_coords,
                            PROXIMITY_DISTANCE_M,
                        ),
                        ~existing_match,
                    ))
                    .order_by(distance_expr.asc())
                    .limit(500)
                )
                rows = await db.execute(proximity_query)
                proximity_pairs = [dict(row._mapping) for row in rows.all()]

            # Alle DB-Sessions GESCHLOSSEN!

            total_geo_raw = len(geo_pairs)
            logger.info(f"Geo-Kaskade: {total_geo_raw} rohe Paare, {len(proximity_pairs)} Naehe-Paare")

            # ── Schritt 1b: Hard-Filter (Rollen-Kompatibilitaet, OHNE LLM) ──
            _matching_status["progress"]["phase"] = "hard_filter"
            _matching_status["progress"]["total_geo_pairs"] = total_geo_raw

            # Welche Rollen passen zusammen? (Reihe = Kandidat, Spalte = Job)
            # FiBu + BiBu sind kompatibel (FiBu kann aufsteigen, BiBu kann FiBu machen)
            # KrediBu + DebiBu sind kompatibel (verwandt)
            # FiBu + KrediBu/DebiBu sind kompatibel (FiBu ist generalistischer)
            # LohnBu + StFA sind kompatibel (verwandt)
            # LohnBu + FiBu/BiBu = INKOMPATIBEL
            # StFA + FiBu/BiBu = INKOMPATIBEL
            ROLE_COMPAT = {
                "Finanzbuchhalter/in":      {"Finanzbuchhalter/in", "Bilanzbuchhalter/in", "Kreditorenbuchhalter/in", "Debitorenbuchhalter/in"},
                "Bilanzbuchhalter/in":      {"Finanzbuchhalter/in", "Bilanzbuchhalter/in"},
                "Kreditorenbuchhalter/in":  {"Kreditorenbuchhalter/in", "Debitorenbuchhalter/in", "Finanzbuchhalter/in"},
                "Debitorenbuchhalter/in":   {"Debitorenbuchhalter/in", "Kreditorenbuchhalter/in", "Finanzbuchhalter/in"},
                "Lohnbuchhalter/in":        {"Lohnbuchhalter/in", "Steuerfachangestellte/r"},
                "Steuerfachangestellte/r":  {"Steuerfachangestellte/r", "Lohnbuchhalter/in"},
            }

            def _roles_compatible(cand_data: dict | None, job_data: dict | None) -> bool:
                """Prueft ob Kandidat-Rolle und Job-Rolle kompatibel sind."""
                if not cand_data or not job_data:
                    return True  # Keine Daten → durchlassen
                cand_role = cand_data.get("primary_role") if isinstance(cand_data, dict) else None
                job_role = job_data.get("primary_role") if isinstance(job_data, dict) else None
                if not cand_role or not job_role:
                    return True  # Keine Rolle → durchlassen
                compatible = ROLE_COMPAT.get(cand_role)
                if compatible is None:
                    return True  # Unbekannte Rolle → durchlassen
                return job_role in compatible

            filtered_pairs = []
            hard_filtered = 0
            for pair in geo_pairs:
                if _roles_compatible(pair.get("cand_classification"), pair.get("job_classification")):
                    filtered_pairs.append(pair)
                else:
                    hard_filtered += 1
            geo_pairs = filtered_pairs

            total_geo = len(geo_pairs)
            logger.info(f"Hard-Filter: {hard_filtered} Paare rausgefiltert (Rollen inkompatibel), {total_geo} bleiben")
            _matching_status["progress"]["total_geo_pairs"] = total_geo
            _matching_status["progress"]["vorfilter_total"] = total_geo
            _matching_status["progress"]["hard_filtered"] = hard_filtered

            if total_geo == 0:
                # Keine Paare → Session erstellen mit leeren Daten
                _matching_sessions[session_id] = {
                    "session_id": session_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "current_stufe": 0,
                    "claude_pairs": [],
                    "proximity_pairs": proximity_pairs,
                    "excluded_pairs": set(),
                    "passed_pairs": [],
                    "failed_pairs": [],
                    "deep_results": [],
                    "matches_saved": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                }
                _matching_status["progress"]["stufe"] = "stufe_0_fertig"
                _matching_status["progress"]["phase"] = "fertig"
                _matching_status["running"] = False
                return

            # ── Schritt 2: LLM-Vorfilter (Claude Haiku) ──
            _matching_status["progress"]["phase"] = "llm_vorfilter"
            logger.info(f"Stufe 0, Schritt 2: LLM-Vorfilter fuer {total_geo} Paare...")

            if not settings.anthropic_api_key:
                logger.error("ANTHROPIC_API_KEY nicht konfiguriert — Vorfilter uebersprungen")
                passed_pairs = geo_pairs  # Alle durchlassen wenn kein API Key
            else:
                client = Anthropic(api_key=settings.anthropic_api_key)
                semaphore = asyncio.Semaphore(VORFILTER_SEMAPHORE)
                passed_pairs = []
                total_cost = 0.0

                import re as re_module

                async def _vorfilter_one(pair: dict) -> bool:
                    """LLM-Vorfilter fuer ein Paar. Gibt True zurueck wenn >= 65%."""
                    nonlocal total_cost
                    passed = True  # Default: durchlassen
                    try:
                        # Aktuelle Position + Taetigkeiten extrahieren
                        work_history = pair.get("work_history") or []
                        current_position = pair.get("candidate_position") or ""
                        current_activities = ""

                        if work_history and isinstance(work_history, list) and len(work_history) > 0:
                            first_entry = work_history[0]
                            if isinstance(first_entry, dict):
                                if not current_position:
                                    current_position = first_entry.get("position", "")
                                current_activities = (first_entry.get("description") or "")[:300]

                        # Job Tasks (aus neuem Feld, Fallback: job_text[:300])
                        job_tasks = pair.get("job_tasks") or ""
                        if not job_tasks:
                            job_tasks = (pair.get("job_text") or "")[:300]

                        if not current_position and not current_activities:
                            return True  # Kein Werdegang → durchlassen

                        # Rollen aus classification_data
                        cand_cls = pair.get("cand_classification") or {}
                        job_cls = pair.get("job_classification") or {}
                        candidate_role = cand_cls.get("primary_role", "") if isinstance(cand_cls, dict) else ""
                        job_role = job_cls.get("primary_role", "") if isinstance(job_cls, dict) else ""

                        user_msg = VORFILTER_USER.format(
                            candidate_role=candidate_role or "Unbekannt",
                            current_position=current_position or "Nicht angegeben",
                            current_activities=current_activities or "Nicht angegeben",
                            job_role=job_role or "Unbekannt",
                            job_position=pair.get("position") or "Unbekannt",
                            job_tasks=job_tasks or "Nicht angegeben",
                        )

                        async with semaphore:
                            # 30s Timeout pro Call
                            response = await asyncio.wait_for(
                                asyncio.to_thread(
                                    client.messages.create,
                                    model=DEFAULT_MODEL_QUICK,
                                    max_tokens=10,
                                    system=VORFILTER_SYSTEM,
                                    messages=[{"role": "user", "content": user_msg}],
                                ),
                                timeout=30.0,
                            )
                            text = response.content[0].text.strip()
                            in_tokens = response.usage.input_tokens
                            out_tokens = response.usage.output_tokens

                            cost = (in_tokens * 0.80 + out_tokens * 4.0) / 1_000_000
                            total_cost += cost

                            match = re_module.search(r"(\d+)", text)
                            if match:
                                score = int(match.group(1))
                                passed = score >= VORFILTER_MIN_SCORE
                            else:
                                logger.warning(f"Vorfilter: Konnte keine Zahl parsen aus '{text}'")
                                passed = True

                    except asyncio.TimeoutError:
                        logger.warning("Vorfilter: API Timeout (30s) — Paar durchgelassen")
                        _matching_status["progress"]["errors"] += 1
                        passed = True
                    except Exception as e:
                        logger.error(f"Vorfilter-Fehler: {e}")
                        _matching_status["progress"]["errors"] += 1
                        passed = True
                    finally:
                        # Fortschritt SOFORT nach jedem einzelnen Call aktualisieren
                        if passed:
                            _matching_status["progress"]["vorfilter_passed"] += 1
                        else:
                            _matching_status["progress"]["vorfilter_failed"] += 1
                        _matching_status["progress"]["vorfilter_done"] += 1
                        _matching_status["progress"]["cost_estimate_usd"] = round(total_cost, 4)

                    return passed

                # Alle Paare als Tasks starten (Semaphore begrenzt echte Parallelitaet)
                # Kein Chunk-System mehr — Tasks laufen frei, Fortschritt pro Call
                tasks = []
                for idx, pair in enumerate(geo_pairs):
                    # Stop-Flag alle 100 Paare pruefen
                    if idx % 100 == 0 and idx > 0:
                        if is_stop_requested():
                            logger.info("Vorfilter: Stop angefordert, breche ab.")
                            clear_stop()
                            break
                        done = _matching_status["progress"]["vorfilter_done"]
                        passed_count = _matching_status["progress"]["vorfilter_passed"]
                        logger.info(f"Vorfilter: {done}/{total_geo} — {passed_count} bestanden (${total_cost:.4f})")

                    tasks.append(_vorfilter_one(pair))

                # Alle Tasks parallel starten (Semaphore limitiert auf 15 gleichzeitig)
                results = await asyncio.gather(*tasks)
                for pair, passed in zip(geo_pairs[:len(results)], results):
                    if passed:
                        passed_pairs.append(pair)

                done = _matching_status["progress"]["vorfilter_done"]
                passed_count = _matching_status["progress"]["vorfilter_passed"]
                logger.info(f"Vorfilter fertig: {done}/{total_geo} — {passed_count} bestanden (${total_cost:.4f})")

            # ── Session erstellen mit gefilterten Paaren ──
            _matching_sessions[session_id] = {
                "session_id": session_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "current_stufe": 0,
                "claude_pairs": passed_pairs,
                "proximity_pairs": proximity_pairs,
                "excluded_pairs": set(),
                "passed_pairs": [],
                "failed_pairs": [],
                "deep_results": [],
                "matches_saved": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "vorfilter_stats": {
                    "total_geo_pairs": total_geo,
                    "passed": len(passed_pairs),
                    "failed": total_geo - len(passed_pairs),
                    "pass_rate": round(len(passed_pairs) / total_geo * 100, 1) if total_geo > 0 else 0,
                },
            }

            # Paare fuer Anzeige aufbereiten
            display_pairs = []
            for p in passed_pairs:
                distance_m = p.get("distance_m")
                fn = p.get("candidate_first_name") or ""
                ln = p.get("candidate_last_name") or ""
                display_pairs.append({
                    "candidate_id": str(p["candidate_id"]),
                    "job_id": str(p["job_id"]),
                    "candidate_name": f"{fn} {ln}".strip() or "Unbekannt",
                    "candidate_role": p.get("candidate_role") or "Unbekannt",
                    "candidate_position": p.get("candidate_position") or "",
                    "candidate_city": p.get("candidate_city") or "Unbekannt",
                    "job_position": p.get("position") or "Unbekannt",
                    "job_company": p.get("company_name") or "Unbekannt",
                    "job_city": p.get("job_city") or "Unbekannt",
                    "distance_km": round(distance_m / 1000, 1) if distance_m else None,
                })

            _matching_sessions[session_id]["display_pairs"] = display_pairs

            display_proximity = []
            for p in proximity_pairs:
                distance_m = p.get("distance_m")
                fn = p.get("candidate_first_name") or ""
                ln = p.get("candidate_last_name") or ""
                display_proximity.append({
                    "candidate_id": str(p["candidate_id"]),
                    "job_id": str(p["job_id"]),
                    "candidate_name": f"{fn} {ln}".strip() or "Unbekannt",
                    "candidate_role": p.get("candidate_role") or "Unbekannt",
                    "candidate_city": p.get("candidate_city") or "Unbekannt",
                    "job_position": p.get("position") or "Unbekannt",
                    "job_company": p.get("company_name") or "Unbekannt",
                    "job_city": p.get("job_city") or "Unbekannt",
                    "distance_km": round(distance_m / 1000, 1) if distance_m else None,
                })

            _matching_sessions[session_id]["display_proximity"] = display_proximity

            _matching_status["progress"]["stufe"] = "stufe_0_fertig"
            _matching_status["progress"]["phase"] = "fertig"

            logger.info(
                f"Stufe 0 fertig: {total_geo} Geo-Paare → {len(passed_pairs)} nach Vorfilter, "
                f"{len(proximity_pairs)} Naehe-Paare — Session {session_id}"
            )

        except Exception as e:
            logger.error(f"Stufe 0 fehlgeschlagen: {e}", exc_info=True)
            _matching_status["progress"]["phase"] = "error"
            _matching_status["progress"]["error_message"] = str(e)
        finally:
            _matching_status["running"] = False

    asyncio.create_task(_run_stufe_0_background())

    return {
        "status": "running",
        "session_id": session_id,
        "message": "Stufe 0 laeuft im Hintergrund (Geo-Kaskade + LLM-Vorfilter). Fortschritt via Status-Endpoint.",
    }


async def run_stufe_1(
    session_id: str,
    model_quick: str = DEFAULT_MODEL_QUICK,
) -> dict:
    """Stufe 1: Claude Quick-Check — JA/NEIN fuer jedes Paar.

    Beruecksichtigt ausgeschlossene Paare. Ergebnisse werden in Session gespeichert.

    Returns:
        {"status": "ok", "passed": [...], "failed": [...], ...}
    """
    session = _matching_sessions.get(session_id)
    if not session:
        return {"status": "error", "message": "Session nicht gefunden"}

    if session["current_stufe"] >= 1:
        return {"status": "error", "message": "Stufe 1 wurde bereits ausgefuehrt"}

    global _matching_status
    if _matching_status["running"]:
        return {"status": "error", "message": "Ein Matching laeuft bereits"}

    _matching_status["running"] = True
    _matching_status["progress"] = {
        "stufe": "stufe_1",
        "session_id": session_id,
        "total_pairs": 0,
        "processed_stufe_1": 0,
        "passed_stufe_1": 0,
        "errors": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_estimate_usd": 0.0,
    }

    try:
        from anthropic import Anthropic
        from app.config import settings

        if not settings.anthropic_api_key:
            _matching_status["running"] = False
            return {"status": "error", "message": "ANTHROPIC_API_KEY nicht konfiguriert"}

        client = Anthropic(api_key=settings.anthropic_api_key)
        semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)

        excluded = session.get("excluded_pairs", set())
        claude_pairs = session["claude_pairs"]

        # Paare filtern (ausgeschlossene entfernen)
        active_pairs = [
            p for p in claude_pairs
            if (str(p["candidate_id"]), str(p["job_id"])) not in excluded
        ]

        _matching_status["progress"]["total_pairs"] = len(active_pairs)

        passed_pairs = []
        failed_pairs = []
        total_tokens_in = 0
        total_tokens_out = 0
        errors = 0

        for i, pair in enumerate(active_pairs):
            cand_data = _extract_candidate_data(pair)
            job_data = _extract_job_data(pair)

            user_msg = QUICK_CHECK_USER.format(**cand_data, **job_data)
            parsed, t_in, t_out = await _call_claude(
                client, model_quick, QUICK_CHECK_SYSTEM, user_msg, semaphore
            )
            total_tokens_in += t_in
            total_tokens_out += t_out

            _matching_status["progress"]["processed_stufe_1"] = i + 1
            _matching_status["progress"]["tokens_in"] = total_tokens_in
            _matching_status["progress"]["tokens_out"] = total_tokens_out

            distance_m = pair.get("distance_m")
            distance_km = round(distance_m / 1000, 1) if distance_m else None

            fn = pair.get("candidate_first_name") or ""
            ln = pair.get("candidate_last_name") or ""

            if parsed is None:
                errors += 1
                _matching_status["progress"]["errors"] = errors
                error_info = {
                    "candidate_id": str(pair["candidate_id"]),
                    "job_id": str(pair["job_id"]),
                    "candidate_name": f"{fn} {ln}".strip() or "Unbekannt",
                    "job_position": pair.get("position") or "Unbekannt",
                    "job_company": pair.get("company_name") or "Unbekannt",
                    "quick_reason": "FEHLER: Claude-Antwort konnte nicht geparst werden",
                }
                logger.warning(f"Stufe 1 [{i+1}/{len(active_pairs)}]: ERROR (parse) — {error_info['candidate_name']} → {error_info['job_position']}")
                session.setdefault("error_pairs", []).append(error_info)
                continue

            pair_info = {
                "candidate_id": str(pair["candidate_id"]),
                "job_id": str(pair["job_id"]),
                "candidate_name": f"{fn} {ln}".strip() or "Unbekannt",
                "candidate_role": pair.get("candidate_role") or "Unbekannt",
                "candidate_position": pair.get("candidate_position") or "",
                "candidate_city": pair.get("candidate_city") or "Unbekannt",
                "job_position": pair.get("position") or "Unbekannt",
                "job_company": pair.get("company_name") or "Unbekannt",
                "job_city": pair.get("job_city") or "Unbekannt",
                "distance_km": distance_km,
                "quick_reason": parsed.get("reason", ""),
            }

            did_pass = parsed.get("pass", False)
            reason = parsed.get("reason", "")
            logger.info(
                f"Stufe 1 [{i+1}/{len(active_pairs)}]: "
                f"{'PASS' if did_pass else 'FAIL'} — "
                f"{pair_info['candidate_name']} → {pair_info['job_position']} @ {pair_info['job_company']} — "
                f"Grund: {reason}"
            )

            if did_pass:
                passed_pairs.append(pair_info)
                # Volles Paar mit Daten in Session speichern fuer Stufe 2
                session["passed_pairs"].append({
                    **pair,
                    "quick_reason": reason,
                })
                _matching_status["progress"]["passed_stufe_1"] = len(passed_pairs)
            else:
                pair_info["quick_reason"] = reason
                failed_pairs.append(pair_info)
                session["failed_pairs"].append(pair_info)

        # Kosten berechnen
        cost_in = total_tokens_in * 0.80 / 1_000_000
        cost_out = total_tokens_out * 4.0 / 1_000_000
        actual_cost = round(cost_in + cost_out, 4)

        _matching_status["progress"]["cost_estimate_usd"] = actual_cost

        # Session aktualisieren
        session["current_stufe"] = 1
        session["tokens_in"] += total_tokens_in
        session["tokens_out"] += total_tokens_out

        cost_stufe_2 = _estimate_cost(len(passed_pairs), "stufe_2")

        logger.info(
            f"Stufe 1 fertig: {len(passed_pairs)}/{len(active_pairs)} bestanden, "
            f"{errors} Fehler, ${actual_cost} — Session {session_id}"
        )

        return {
            "status": "ok",
            "session_id": session_id,
            "total_checked": len(active_pairs),
            "passed": passed_pairs,
            "failed": failed_pairs,
            "passed_count": len(passed_pairs),
            "failed_count": len(failed_pairs),
            "errors": errors,
            "cost_usd": actual_cost,
            "tokens_in": total_tokens_in,
            "tokens_out": total_tokens_out,
            "cost_stufe_2": cost_stufe_2,
            "message": f"{len(passed_pairs)} Paare bestanden. Pruefe und schliesse Paare aus, dann starte Stufe 2.",
        }

    except Exception as e:
        logger.error(f"Stufe 1 fehlgeschlagen: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

    finally:
        _matching_status["running"] = False
        _matching_status["last_run"] = datetime.now(timezone.utc).isoformat()
        _matching_status["progress"]["stufe"] = "stufe_1_fertig"


async def run_stufe_2(
    session_id: str,
    model_deep: str = DEFAULT_MODEL_DEEP,
    model_quick: str = DEFAULT_MODEL_QUICK,
) -> dict:
    """Stufe 2: Deep Assessment + Matches speichern + Fahrzeit.

    Beruecksichtigt ausgeschlossene Paare (auch Paare die NACH Stufe 1 ausgeschlossen wurden).

    Returns:
        {"status": "ok", "matches_saved": [...], ...}
    """
    session = _matching_sessions.get(session_id)
    if not session:
        return {"status": "error", "message": "Session nicht gefunden"}

    if session["current_stufe"] < 1:
        return {"status": "error", "message": "Stufe 1 muss zuerst ausgefuehrt werden"}

    if session["current_stufe"] >= 2:
        return {"status": "error", "message": "Stufe 2 wurde bereits ausgefuehrt"}

    global _matching_status
    if _matching_status["running"]:
        return {"status": "error", "message": "Ein Matching laeuft bereits"}

    _matching_status["running"] = True
    _matching_status["progress"] = {
        "stufe": "stufe_2",
        "session_id": session_id,
        "total_pairs": 0,
        "processed_stufe_2": 0,
        "top_matches": 0,
        "wow_matches": 0,
        "errors": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_estimate_usd": 0.0,
    }

    try:
        from anthropic import Anthropic
        from app.config import settings
        from app.database import async_session_maker
        from app.models.match import Match, MatchStatus
        from sqlalchemy import text

        if not settings.anthropic_api_key:
            _matching_status["running"] = False
            return {"status": "error", "message": "ANTHROPIC_API_KEY nicht konfiguriert"}

        client = Anthropic(api_key=settings.anthropic_api_key)
        semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)

        excluded = session.get("excluded_pairs", set())
        passed_pairs_full = session["passed_pairs"]

        # Paare filtern (auch nach Stufe 1 ausgeschlossene entfernen)
        active_pairs = [
            p for p in passed_pairs_full
            if (str(p["candidate_id"]), str(p["job_id"])) not in excluded
        ]

        _matching_status["progress"]["total_pairs"] = len(active_pairs)

        deep_results = []
        total_tokens_in = 0
        total_tokens_out = 0
        errors = 0

        for i, pair in enumerate(active_pairs):
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
                client, model_deep, DEEP_ASSESSMENT_SYSTEM, user_msg, semaphore,
                max_tokens=800,
            )
            total_tokens_in += t_in
            total_tokens_out += t_out

            _matching_status["progress"]["processed_stufe_2"] = i + 1
            _matching_status["progress"]["tokens_in"] = total_tokens_in
            _matching_status["progress"]["tokens_out"] = total_tokens_out

            fn2 = pair.get("candidate_first_name") or ""
            ln2 = pair.get("candidate_last_name") or ""
            cand_name = f"{fn2} {ln2}".strip() or "Unbekannt"

            if parsed is None:
                errors += 1
                _matching_status["progress"]["errors"] = errors
                error_info = {
                    "candidate_id": str(pair["candidate_id"]),
                    "job_id": str(pair["job_id"]),
                    "candidate_name": cand_name,
                    "job_position": pair.get("position") or "Unbekannt",
                    "job_company": pair.get("company_name") or "Unbekannt",
                    "reason": "Claude-Antwort konnte nicht geparst werden",
                }
                logger.warning(f"Stufe 2 [{i+1}/{len(active_pairs)}]: ERROR (parse) — {cand_name}")
                session.setdefault("stufe2_errors", []).append(error_info)
                continue

            # Score clampen
            score = max(0, min(100, int(parsed.get("score", 0))))

            # Empfehlung validieren
            empfehlung = parsed.get("empfehlung", "beobachten")
            if empfehlung not in ["vorstellen", "beobachten", "nicht_passend"]:
                empfehlung = "beobachten"

            logger.info(
                f"Stufe 2 [{i+1}/{len(active_pairs)}]: "
                f"Score={score} Empf={empfehlung} WOW={'JA' if parsed.get('wow_faktor') else 'nein'} — "
                f"{cand_name} → {pair.get('position') or '?'} @ {pair.get('company_name') or '?'}"
            )

            if score < MIN_SCORE_SAVE:
                # Track rejected pairs (Score zu niedrig)
                session.setdefault("stufe2_rejected", []).append({
                    "candidate_id": str(pair["candidate_id"]),
                    "job_id": str(pair["job_id"]),
                    "candidate_name": cand_name,
                    "job_position": pair.get("position") or "Unbekannt",
                    "job_company": pair.get("company_name") or "Unbekannt",
                    "score": score,
                    "empfehlung": empfehlung,
                    "zusammenfassung": parsed.get("zusammenfassung", ""),
                    "reason": f"Score {score} unter Minimum {MIN_SCORE_SAVE}",
                })

            if score >= MIN_SCORE_SAVE:
                deep_results.append({
                    "candidate_id": pair["candidate_id"],
                    "job_id": pair["job_id"],
                    "candidate_name": cand_name,
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

        logger.info(f"Stufe 2 fertig: {len(deep_results)} Matches mit Score >= {MIN_SCORE_SAVE}")

        # ── MATCHES SPEICHERN ──
        _matching_status["progress"]["stufe"] = "speichern"

        saved_matches = []
        top_count = 0
        wow_count = 0

        for dr in deep_results:
            try:
                async with async_session_maker() as db:
                    match = Match(
                        id=uuid.uuid4(),
                        candidate_id=dr["candidate_id"],
                        job_id=dr["job_id"],
                        matching_method="claude_match",
                        status=MatchStatus.AI_CHECKED,
                        v2_score=float(dr["score"]),
                        ai_score=float(dr["score"]) / 100.0,
                        ai_explanation=dr["zusammenfassung"],
                        ai_strengths=dr["staerken"] if dr["staerken"] else None,
                        ai_weaknesses=dr["luecken"] if dr["luecken"] else None,
                        ai_checked_at=datetime.now(timezone.utc),
                        empfehlung=dr["empfehlung"],
                        wow_faktor=dr["wow_faktor"],
                        wow_grund=dr["wow_grund"],
                        distance_km=dr["distance_km"],
                        quick_score=100,
                        quick_reason=dr["quick_reason"],
                        quick_scored_at=datetime.now(timezone.utc),
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

                    saved_matches.append({
                        "match_id": str(match.id),
                        "candidate_id": str(dr["candidate_id"]),
                        "job_id": str(dr["job_id"]),
                        "candidate_name": dr.get("candidate_name", "Unbekannt"),
                        "score": dr["score"],
                        "empfehlung": dr["empfehlung"],
                        "wow_faktor": dr["wow_faktor"],
                        "zusammenfassung": dr["zusammenfassung"],
                        "distance_km": dr["distance_km"],
                    })

                    if dr["empfehlung"] == "vorstellen":
                        top_count += 1
                        _matching_status["progress"]["top_matches"] = top_count
                    if dr["wow_faktor"]:
                        wow_count += 1
                        _matching_status["progress"]["wow_matches"] = wow_count

            except Exception as e:
                logger.error(f"Match speichern fehlgeschlagen: {e}")
                errors += 1
                _matching_status["progress"]["errors"] = errors

        # ── Naehe-Matches speichern ──
        proximity_saved = 0
        for pp in session.get("proximity_pairs", []):
            try:
                async with async_session_maker() as db:
                    distance_m = pp.get("distance_m")
                    distance_km = round(distance_m / 1000, 1) if distance_m else None

                    match = Match(
                        id=uuid.uuid4(),
                        candidate_id=pp["candidate_id"],
                        job_id=pp["job_id"],
                        matching_method="proximity_match",
                        status=MatchStatus.NEW,
                        distance_km=distance_km,
                        v2_score=None,
                        ai_score=None,
                        v2_score_breakdown={
                            "scoring_version": "v4_proximity",
                            "distance_km": distance_km,
                        },
                        v2_matched_at=datetime.now(timezone.utc),
                    )
                    db.add(match)
                    await db.commit()
                    proximity_saved += 1

            except Exception as e:
                logger.error(f"Naehe-Match speichern fehlgeschlagen: {e}")
                errors += 1

        # ── GOOGLE MAPS FAHRZEIT ──
        _matching_status["progress"]["stufe"] = "fahrzeit"

        try:
            from app.services.distance_matrix_service import distance_matrix_service
            from app.api.routes_settings import get_drive_time_threshold

            async with async_session_maker() as db:
                threshold = await get_drive_time_threshold(db)

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
                async with async_session_maker() as db:
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

        # ── Kosten berechnen ──
        cost_in = total_tokens_in * 0.80 / 1_000_000
        cost_out = total_tokens_out * 4.0 / 1_000_000
        actual_cost = round(cost_in + cost_out, 4)

        # Session aktualisieren
        session["current_stufe"] = 2
        session["deep_results"] = deep_results
        session["matches_saved"] = len(saved_matches)
        session["tokens_in"] += total_tokens_in
        session["tokens_out"] += total_tokens_out

        # Gesamt-Kosten der Session
        total_session_cost = round(
            (session["tokens_in"] * 0.80 + session["tokens_out"] * 4.0) / 1_000_000, 4
        )

        logger.info(
            f"Stufe 2 + Speichern fertig: {len(saved_matches)} Matches gespeichert, "
            f"{proximity_saved} Naehe-Matches, ${actual_cost} — Session {session_id}"
        )

        return {
            "status": "ok",
            "session_id": session_id,
            "deep_assessed": len(active_pairs),
            "matches_saved": saved_matches,
            "matches_saved_count": len(saved_matches),
            "proximity_saved": proximity_saved,
            "top_matches": top_count,
            "wow_matches": wow_count,
            "errors": errors,
            "cost_usd": actual_cost,
            "total_session_cost_usd": total_session_cost,
            "tokens_in": total_tokens_in,
            "tokens_out": total_tokens_out,
        }

    except Exception as e:
        logger.error(f"Stufe 2 fehlgeschlagen: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

    finally:
        _matching_status["running"] = False
        _matching_status["last_run"] = datetime.now(timezone.utc).isoformat()
        _matching_status["progress"]["stufe"] = "fertig"


# ══════════════════════════════════════════════════════════════
# AUTOMATISCHES MATCHING — Alle Stufen hintereinander (n8n Cron)
# ══════════════════════════════════════════════════════════════

async def run_matching(
    candidate_id: str | None = None,
    model_quick: str = DEFAULT_MODEL_QUICK,
    model_deep: str = DEFAULT_MODEL_DEEP,
) -> dict:
    """Fuehrt das 3-Stufen Claude Matching durch (AUTOMATISCH, ohne Kontrolle).

    Fuer n8n Morgen-Cron und Ad-hoc Matching.
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
                    Candidate.first_name.label("candidate_first_name"),
                    Candidate.last_name.label("candidate_last_name"),
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
                    Candidate.first_name.label("candidate_first_name"),
                    Candidate.last_name.label("candidate_last_name"),
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
                client, model_deep, DEEP_ASSESSMENT_SYSTEM, user_msg, semaphore,
                max_tokens=800,
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
                fn2 = pair.get("candidate_first_name") or ""
                ln2 = pair.get("candidate_last_name") or ""
                deep_results.append({
                    "candidate_id": pair["candidate_id"],
                    "job_id": pair["job_id"],
                    "candidate_name": f"{fn2} {ln2}".strip() or "Unbekannt",
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
