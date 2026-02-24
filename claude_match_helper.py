#!/usr/bin/env python3
"""Claude Match Helper — CLI-Tool fuer Claude-basiertes Matching.

Dieses Script wird von Claude Code verwendet, um:
1. Unbewertete Kandidat-Job-Paare aus der DB zu holen
2. Kandidaten- und Job-Daten fuer die Bewertung aufzubereiten
3. Bewertungsergebnisse in die matches-Tabelle zu schreiben

Usage:
    python claude_match_helper.py --status
    python claude_match_helper.py --batch [--city X] [--role Y] [--limit N]
    python claude_match_helper.py --job JOB_ID
    python claude_match_helper.py --candidate CANDIDATE_ID
    python claude_match_helper.py --save 'JSON_STRING'
    python claude_match_helper.py --stale-check
    python claude_match_helper.py --clear-old-matches
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from uuid import UUID

# Projekt-Root zum Path hinzufuegen
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ── DB Connection ────────────────────────────────────────────
# Direkt asyncpg verwenden (kein App-Startup noetig)
DB_URL = os.environ.get("DATABASE_URL", "")
if not DB_URL:
    print("FEHLER: DATABASE_URL ist nicht gesetzt. Bitte als Environment Variable setzen.")
    print("  export DATABASE_URL='postgresql://user:pass@host:port/dbname'")
    sys.exit(1)

# asyncpg braucht postgresql:// (nicht postgresql+asyncpg://)
ASYNCPG_URL = DB_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
    "postgres://", "postgresql://"
)

# Max Luftlinie in Metern fuer PostGIS-Vorfilter
MAX_LUFTLINIE_M = 30_000  # 30 km

# Minimum-Score: Matches unter diesem Wert werden NICHT gespeichert + keine Fahrzeit
MIN_SCORE = 75  # Unter 75% = nicht speichern, keine Fahrzeit, nichts

# Fahrzeit-Limits (in Minuten)
MAX_DRIVE_TIME_DEFAULT = 80   # Standard: >80 Min Auto UND OEPNV → nicht speichern
MAX_DRIVE_TIME_HOMEOFFICE = 90  # Mit >=2 Tage Home-Office: bis 90 Min erlaubt

# Google Maps API
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
if not GOOGLE_MAPS_API_KEY:
    print("WARNUNG: GOOGLE_MAPS_API_KEY nicht gesetzt. Fahrzeiten werden nicht berechnet.")
DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"


def _parse_jsonb(val) -> dict | list | None:
    """Parst JSONB-Felder die als String oder Dict zurueckkommen."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _is_company_excluded(company_name: str, excluded: list) -> bool:
    """Prueft ob eine Firma in der Ausschlussliste ist (Substring-Matching).
    'Allianz' blockiert auch 'Allianz SE', 'Allianz Deutschland AG' etc.
    """
    if not company_name or not excluded:
        return False
    cn = company_name.lower()
    for e in excluded:
        if not isinstance(e, str) or not e.strip():
            continue
        ex = e.strip().lower()
        # Beide Richtungen: "Allianz" in "Allianz SE" ODER "Allianz SE" in "Allianz"
        if ex in cn or cn in ex:
            return True
    return False


async def get_pool():
    """Erstellt einen asyncpg Connection Pool."""
    import asyncpg

    return await asyncpg.create_pool(ASYNCPG_URL, min_size=1, max_size=3, command_timeout=30)


# ── STATUS ───────────────────────────────────────────────────


async def cmd_status():
    """Zeigt Uebersicht: unbewertete Paare, letzter Lauf, Feedback."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            # Gesamtzahlen
            total_candidates = await conn.fetchval(
                "SELECT COUNT(*) FROM candidates WHERE hidden = false AND deleted_at IS NULL"
            )
            available_candidates = await conn.fetchval(
                "SELECT COUNT(*) FROM candidates WHERE hidden = false AND deleted_at IS NULL "
                "AND (availability_status = 'available' OR availability_status IS NULL)"
            )
            total_jobs = await conn.fetchval(
                "SELECT COUNT(*) FROM jobs WHERE deleted_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > NOW())"
            )
            total_matches = await conn.fetchval("SELECT COUNT(*) FROM matches")
            claude_code_matches = await conn.fetchval(
                "SELECT COUNT(*) FROM matches WHERE matching_method = 'claude_code'"
            )

            # Letzter Claude-Code Match
            last_match = await conn.fetchval(
                "SELECT MAX(created_at) FROM matches WHERE matching_method = 'claude_code'"
            )

            # Paare die noch nicht bewertet wurden (Kandidat x Job ohne Match-Record)
            # Schnelle Schaetzung: aktive Kandidaten * aktive Jobs - bestehende Matches
            possible_pairs = total_candidates * total_jobs
            unrated_estimate = possible_pairs - total_matches

            # Matches mit Feedback
            feedback_count = await conn.fetchval(
                "SELECT COUNT(*) FROM matches WHERE user_feedback IS NOT NULL AND user_feedback != ''"
            )

            # Stale Matches
            stale_count = await conn.fetchval(
                "SELECT COUNT(*) FROM matches WHERE stale = true"
            )

            # Empfehlung-Verteilung (claude_code)
            empfehlung_stats = await conn.fetch(
                "SELECT empfehlung, COUNT(*) as cnt FROM matches "
                "WHERE matching_method = 'claude_code' AND empfehlung IS NOT NULL "
                "GROUP BY empfehlung ORDER BY cnt DESC"
            )

            # WOW Matches
            wow_count = await conn.fetchval(
                "SELECT COUNT(*) FROM matches WHERE matching_method = 'claude_code' AND wow_faktor = true"
            )

        print("=" * 60)
        print("  CLAUDE MATCHING — STATUS")
        print("=" * 60)
        print()
        print(f"  Kandidaten gesamt:     {total_candidates}")
        print(f"  Kandidaten verfuegbar: {available_candidates}")
        print(f"  Jobs aktiv:            {total_jobs}")
        print(f"  Moegliche Paare:       ~{possible_pairs:,}")
        print()
        print(f"  Matches gesamt:        {total_matches}")
        print(f"  davon Claude Code:     {claude_code_matches}")
        print(f"  davon WOW:             {wow_count}")
        print(f"  Noch nicht bewertet:   ~{max(0, unrated_estimate):,}")
        print()
        if last_match:
            print(f"  Letzter Claude-Match:  {last_match.strftime('%d.%m.%Y %H:%M')}")
        else:
            print("  Letzter Claude-Match:  — (noch keiner)")
        print(f"  Stale Matches:         {stale_count}")
        print(f"  User-Feedback:         {feedback_count}")
        print()
        if empfehlung_stats:
            print("  Empfehlung-Verteilung:")
            for row in empfehlung_stats:
                print(f"    {row['empfehlung']:20s} {row['cnt']}")
        print()
        print("=" * 60)

    finally:
        await pool.close()


# ── BATCH ────────────────────────────────────────────────────


async def cmd_batch(city: str | None, role: str | None, limit: int):
    """Holt naechsten Batch unbewerteter Paare."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            # Aktive Jobs mit Koordinaten laden
            job_filter = """
                j.deleted_at IS NULL
                AND (j.expires_at IS NULL OR j.expires_at > NOW())
                AND j.location_coords IS NOT NULL
                AND j.job_text IS NOT NULL AND j.job_text != ''
            """
            job_params = []
            param_idx = 1

            if city:
                job_filter += f" AND (LOWER(j.city) LIKE ${param_idx} OR LOWER(j.work_location_city) LIKE ${param_idx})"
                job_params.append(f"%{city.lower()}%")
                param_idx += 1

            if role:
                job_filter += f" AND j.classification_data->>'primary_role' = ${param_idx}"
                job_params.append(role)
                param_idx += 1

            # Jobs laden
            jobs = await conn.fetch(
                f"""
                SELECT j.id, j.company_name, j.position, j.city, j.work_location_city,
                       j.work_arrangement, j.classification_data, j.quality_score,
                       j.job_text, j.job_tasks,
                       ST_Y(j.location_coords::geometry) as lat,
                       ST_X(j.location_coords::geometry) as lng,
                       j.postal_code
                FROM jobs j
                WHERE {job_filter}
                ORDER BY j.created_at DESC
                """,
                *job_params,
            )

            if not jobs:
                print("Keine aktiven Jobs gefunden (mit den angegebenen Filtern).")
                return

            # Kandidaten laden — NUR klassifizierte Finance-Leute
            candidates = await conn.fetch(
                """
                SELECT c.id, c.city, c.current_position,
                       c.work_history, c.education, c.further_education,
                       c.skills, c.it_skills, c.erp,
                       c.classification_data,
                       c.hotlist_job_titles, c.hotlist_job_title,
                       c.desired_positions, c.key_activities,
                       c.excluded_companies,
                       ST_Y(c.address_coords::geometry) as lat,
                       ST_X(c.address_coords::geometry) as lng,
                       c.postal_code
                FROM candidates c
                WHERE c.hidden = false
                  AND c.deleted_at IS NULL
                  AND c.address_coords IS NOT NULL
                  AND c.classification_data IS NOT NULL
                  AND (c.availability_status = 'available' OR c.availability_status IS NULL)
                ORDER BY c.created_at DESC
                """
            )

            if not candidates:
                print("Keine verfuegbaren Kandidaten mit Koordinaten gefunden.")
                return

            # Bestehende Matches laden (um Duplikate zu vermeiden)
            existing_matches = await conn.fetch(
                "SELECT job_id, candidate_id FROM matches WHERE job_id IS NOT NULL AND candidate_id IS NOT NULL"
            )
            existing_set = {(str(r["job_id"]), str(r["candidate_id"])) for r in existing_matches}

            # Paare bilden mit PostGIS-Vorfilter
            pairs = []
            for job in jobs:
                if (job["quality_score"] or "").lower() == "low":
                    continue  # Schlechte Job-Qualitaet ueberspringen

                job_is_remote = job["work_arrangement"] == "remote"

                for cand in candidates:
                    # Bereits bewertet?
                    if (str(job["id"]), str(cand["id"])) in existing_set:
                        continue

                    # Excluded Companies pruefen (Substring-Matching)
                    excluded = _parse_jsonb(cand["excluded_companies"]) or []
                    if _is_company_excluded(job["company_name"], excluded):
                        continue

                    # Kandidat muss eine Finance-Rolle haben
                    cand_cd = _parse_jsonb(cand["classification_data"]) or {}
                    cand_primary = cand_cd.get("primary_role", "")

                    if not cand_primary or cand_primary not in FINANCE_ROLES:
                        continue  # Kein Finance-Kandidat → ueberspringen

                    # Rollen-Kompatibilitaet mit Job (nur primary_role)
                    job_role = (_parse_jsonb(job["classification_data"]) or {}).get("primary_role", "")
                    if job_role:
                        compatible = _roles_compatible(cand_primary, job_role)
                        if not compatible:
                            continue

                    # PostGIS Luftlinie (nur fuer nicht-remote Jobs)
                    if not job_is_remote and job["lat"] and cand["lat"]:
                        dist_m = _haversine_m(
                            job["lat"], job["lng"], cand["lat"], cand["lng"]
                        )
                        if dist_m > MAX_LUFTLINIE_M:
                            continue

                    pairs.append({"job": job, "candidate": cand})

                    if len(pairs) >= limit:
                        break
                if len(pairs) >= limit:
                    break

            if not pairs:
                print("Keine neuen unbewerteten Paare gefunden. Alles bewertet!")
                return

            # Ausgabe
            print(f"=== {len(pairs)} UNBEWERTETE PAARE ===\n")
            for i, pair in enumerate(pairs, 1):
                j = pair["job"]
                c = pair["candidate"]
                print(f"--- Paar {i}/{len(pairs)} ---")
                print(f"Job ID:        {j['id']}")
                print(f"  Position:    {j['position']}")
                print(f"  Firma:       {j['company_name']}")
                print(f"  Stadt:       {j['work_location_city'] or j['city']}")
                print(f"  Arrangement: {j['work_arrangement'] or 'k.A.'}")
                print(f"  Rolle:       {(_parse_jsonb(j['classification_data']) or {}).get('primary_role', 'k.A.')}")
                print(f"  Quality:     {j['quality_score'] or 'k.A.'}")
                if j["job_tasks"]:
                    print(f"  Aufgaben:    {j['job_tasks']}")
                print()
                print(f"Kandidat ID:   {c['id']}")
                print(f"  Stadt:       {c['city'] or 'k.A.'}")
                print(f"  Position:    {c['current_position'] or 'k.A.'}")
                print(f"  Rolle:       {(_parse_jsonb(c['classification_data']) or {}).get('primary_role', 'k.A.')}")
                print(f"  Skills:      {', '.join(c['skills'] or []) or 'k.A.'}")
                print(f"  IT/ERP:      {', '.join(c['it_skills'] or []) or 'k.A.'}")
                if c["work_history"]:
                    wh = c["work_history"]
                    if isinstance(wh, list):
                        for pos in wh:
                            if isinstance(pos, dict):
                                print(f"  Erfahrung:   {pos.get('position', '')} bei {pos.get('company', '')} ({pos.get('period', '')})")
                                if pos.get("tasks"):
                                    print(f"               Taetigkeiten: {pos['tasks']}")
                    elif isinstance(wh, dict):
                        for key, val in wh.items():
                            print(f"  Erfahrung:   {key}: {val}")
                if c["education"]:
                    edu = c["education"]
                    if isinstance(edu, list):
                        for e in edu:
                            if isinstance(e, dict):
                                print(f"  Ausbildung:  {e.get('degree', '')} — {e.get('institution', '')} ({e.get('period', '')})")
                    elif isinstance(edu, dict):
                        for key, val in edu.items():
                            print(f"  Ausbildung:  {key}: {val}")
                if c.get("further_education"):
                    fe = c["further_education"]
                    if isinstance(fe, list):
                        for f in fe:
                            if isinstance(f, dict):
                                print(f"  Weiterbildung: {f.get('title', '')} — {f.get('institution', '')} ({f.get('year', '')})")
                            elif isinstance(f, str):
                                print(f"  Weiterbildung: {f}")
                    elif isinstance(fe, str):
                        print(f"  Weiterbildung: {fe}")
                print()
                print(f"  Job-Text (vollstaendig):")
                print(f"  {j['job_text'] or 'k.A.'}")
                print()
                print("-" * 50)
                print()

    finally:
        await pool.close()


# ── JOB (einzeln) ───────────────────────────────────────────


async def cmd_job(job_id: str):
    """Ein Job mit allen passenden Kandidaten."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            # Job laden
            job = await conn.fetchrow(
                """
                SELECT j.id, j.company_name, j.position, j.city, j.work_location_city,
                       j.work_arrangement, j.classification_data, j.quality_score,
                       j.job_text, j.job_tasks,
                       ST_Y(j.location_coords::geometry) as lat,
                       ST_X(j.location_coords::geometry) as lng,
                       j.postal_code
                FROM jobs j
                WHERE j.id = $1
                """,
                UUID(job_id),
            )

            if not job:
                print(f"Job {job_id} nicht gefunden.")
                return

            print(f"=== JOB: {job['position']} bei {job['company_name']} ===")
            print(f"  Stadt:       {job['work_location_city'] or job['city']}")
            print(f"  Arrangement: {job['work_arrangement'] or 'k.A.'}")
            print(f"  Rolle:       {(job['classification_data'] or {}).get('primary_role', 'k.A.')}")
            print(f"  Quality:     {job['quality_score'] or 'k.A.'}")
            print()
            print(f"  Aufgaben: {job['job_tasks'] or 'k.A.'}")
            print()
            print(f"  Job-Text (vollstaendig):")
            print(f"  {job['job_text'] or 'k.A.'}")
            print()

            # Passende Kandidaten (PostGIS Vorfilter + Rollen)
            job_is_remote = job["work_arrangement"] == "remote"
            job_role = (_parse_jsonb(job["classification_data"]) or {}).get("primary_role", "")

            if job_is_remote or not job["lat"]:
                # Remote oder keine Koordinaten: alle Kandidaten
                candidates = await conn.fetch(
                    """
                    SELECT c.id, c.city, c.current_position,
                           c.work_history, c.education, c.further_education,
                           c.skills, c.it_skills, c.erp,
                           c.classification_data, c.excluded_companies,
                           ST_Y(c.address_coords::geometry) as lat,
                           ST_X(c.address_coords::geometry) as lng,
                           c.postal_code
                    FROM candidates c
                    WHERE c.hidden = false AND c.deleted_at IS NULL
                      AND (c.availability_status = 'available' OR c.availability_status IS NULL)
                    """
                )
            else:
                # PostGIS Vorfilter
                candidates = await conn.fetch(
                    """
                    SELECT c.id, c.city, c.current_position,
                           c.work_history, c.education, c.further_education,
                           c.skills, c.it_skills, c.erp,
                           c.classification_data, c.excluded_companies,
                           ST_Y(c.address_coords::geometry) as lat,
                           ST_X(c.address_coords::geometry) as lng,
                           c.postal_code
                    FROM candidates c
                    WHERE c.hidden = false AND c.deleted_at IS NULL
                      AND c.address_coords IS NOT NULL
                      AND (c.availability_status = 'available' OR c.availability_status IS NULL)
                      AND ST_DWithin(c.address_coords, $1::geography, $2)
                    """,
                    f"POINT({job['lng']} {job['lat']})",
                    MAX_LUFTLINIE_M,
                )

            # Bestehende Matches fuer diesen Job
            existing = await conn.fetch(
                "SELECT candidate_id FROM matches WHERE job_id = $1",
                UUID(job_id),
            )
            existing_ids = {str(r["candidate_id"]) for r in existing}

            # Filtern
            result_candidates = []
            for c in candidates:
                if str(c["id"]) in existing_ids:
                    continue
                # Excluded companies (Substring-Matching)
                excluded = _parse_jsonb(c["excluded_companies"]) or []
                if _is_company_excluded(job["company_name"], excluded):
                    continue
                # Finance-Rolle Pflicht
                cand_cd = _parse_jsonb(c["classification_data"]) or {}
                cand_primary = cand_cd.get("primary_role", "")
                if not cand_primary or cand_primary not in FINANCE_ROLES:
                    continue
                # Rollen-Kompatibilitaet
                if job_role and not _roles_compatible(cand_primary, job_role):
                    continue
                result_candidates.append(c)

            print(f"=== {len(result_candidates)} PASSENDE KANDIDATEN (noch nicht bewertet) ===\n")
            for i, c in enumerate(result_candidates, 1):
                print(f"--- Kandidat {i} ---")
                print(f"  ID:        {c['id']}")
                print(f"  Stadt:     {c['city'] or 'k.A.'}")
                print(f"  Position:  {c['current_position'] or 'k.A.'}")
                print(f"  Rolle:     {(_parse_jsonb(c['classification_data']) or {}).get('primary_role', 'k.A.')}")
                print(f"  Skills:    {', '.join(c['skills'] or []) or 'k.A.'}")
                print(f"  IT/ERP:    {', '.join(c['it_skills'] or []) or 'k.A.'}")
                if c["work_history"]:
                    wh = c["work_history"]
                    if isinstance(wh, list):
                        for pos in wh:
                            if isinstance(pos, dict):
                                print(f"  Erfahrung: {pos.get('position', '')} bei {pos.get('company', '')} ({pos.get('period', '')})")
                                if pos.get("tasks"):
                                    print(f"             Taetigkeiten: {pos['tasks']}")
                if c["education"]:
                    edu = c["education"]
                    if isinstance(edu, list):
                        for e in edu:
                            if isinstance(e, dict):
                                print(f"  Ausbildung: {e.get('degree', '')} — {e.get('institution', '')} ({e.get('period', '')})")
                if c.get("further_education"):
                    fe = c["further_education"]
                    if isinstance(fe, list):
                        for f in fe:
                            if isinstance(f, dict):
                                print(f"  Weiterbildung: {f.get('title', '')} — {f.get('institution', '')} ({f.get('year', '')})")
                            elif isinstance(f, str):
                                print(f"  Weiterbildung: {f}")
                print()

    finally:
        await pool.close()


# ── CANDIDATE (einzeln) ─────────────────────────────────────


async def cmd_candidate(candidate_id: str):
    """Ein Kandidat mit allen passenden Jobs."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            # Kandidat laden
            cand = await conn.fetchrow(
                """
                SELECT c.id, c.city, c.current_position,
                       c.work_history, c.education, c.further_education,
                       c.skills, c.it_skills, c.erp,
                       c.classification_data, c.excluded_companies,
                       c.desired_positions, c.key_activities,
                       ST_Y(c.address_coords::geometry) as lat,
                       ST_X(c.address_coords::geometry) as lng,
                       c.postal_code
                FROM candidates c
                WHERE c.id = $1
                """,
                UUID(candidate_id),
            )

            if not cand:
                print(f"Kandidat {candidate_id} nicht gefunden.")
                return

            cand_primary = (_parse_jsonb(cand["classification_data"]) or {}).get("primary_role", "")

            print(f"=== KANDIDAT: {cand['current_position'] or 'k.A.'} in {cand['city'] or 'k.A.'} ===")
            print(f"  ID:         {cand['id']}")
            print(f"  Rolle:      {cand_primary or 'k.A.'}")
            print(f"  Skills:     {', '.join(cand['skills'] or []) or 'k.A.'}")
            print(f"  IT/ERP:     {', '.join(cand['it_skills'] or []) or 'k.A.'}")
            if cand["work_history"]:
                wh = cand["work_history"]
                if isinstance(wh, list):
                    for pos in wh:
                        if isinstance(pos, dict):
                            print(f"  Erfahrung:  {pos.get('position', '')} bei {pos.get('company', '')} ({pos.get('period', '')})")
                            if pos.get("tasks"):
                                print(f"              Taetigkeiten: {pos['tasks']}")
            if cand["education"]:
                edu = cand["education"]
                if isinstance(edu, list):
                    for e in edu:
                        if isinstance(e, dict):
                            print(f"  Ausbildung: {e.get('degree', '')} — {e.get('institution', '')} ({e.get('period', '')})")
            if cand.get("further_education"):
                fe = cand["further_education"]
                if isinstance(fe, list):
                    for f in fe:
                        if isinstance(f, dict):
                            print(f"  Weiterbildung: {f.get('title', '')} — {f.get('institution', '')} ({f.get('year', '')})")
                        elif isinstance(f, str):
                            print(f"  Weiterbildung: {f}")
            print()

            # Jobs laden (PostGIS Vorfilter)
            if cand["lat"]:
                jobs = await conn.fetch(
                    """
                    SELECT j.id, j.company_name, j.position, j.city, j.work_location_city,
                           j.work_arrangement, j.classification_data, j.quality_score,
                           j.job_text, j.job_tasks,
                           ST_Y(j.location_coords::geometry) as lat,
                           ST_X(j.location_coords::geometry) as lng,
                           j.postal_code
                    FROM jobs j
                    WHERE j.deleted_at IS NULL
                      AND (j.expires_at IS NULL OR j.expires_at > NOW())
                      AND j.job_text IS NOT NULL AND j.job_text != ''
                      AND (
                          j.work_arrangement = 'remote'
                          OR j.location_coords IS NULL
                          OR ST_DWithin(j.location_coords, $1::geography, $2)
                      )
                    ORDER BY j.created_at DESC
                    """,
                    f"POINT({cand['lng']} {cand['lat']})",
                    MAX_LUFTLINIE_M,
                )
            else:
                jobs = await conn.fetch(
                    """
                    SELECT j.id, j.company_name, j.position, j.city, j.work_location_city,
                           j.work_arrangement, j.classification_data, j.quality_score,
                           j.job_text, j.job_tasks,
                           ST_Y(j.location_coords::geometry) as lat,
                           ST_X(j.location_coords::geometry) as lng,
                           j.postal_code
                    FROM jobs j
                    WHERE j.deleted_at IS NULL
                      AND (j.expires_at IS NULL OR j.expires_at > NOW())
                      AND j.job_text IS NOT NULL AND j.job_text != ''
                    ORDER BY j.created_at DESC
                    """
                )

            # Bestehende Matches
            existing = await conn.fetch(
                "SELECT job_id FROM matches WHERE candidate_id = $1",
                UUID(candidate_id),
            )
            existing_ids = {str(r["job_id"]) for r in existing}

            # Filtern
            excluded = _parse_jsonb(cand["excluded_companies"]) or []
            result_jobs = []
            for j in jobs:
                if str(j["id"]) in existing_ids:
                    continue
                if (j["quality_score"] or "").lower() == "low":
                    continue
                if _is_company_excluded(j["company_name"], excluded):
                    continue
                job_role = (_parse_jsonb(j["classification_data"]) or {}).get("primary_role", "")
                if job_role and not _roles_compatible(cand_primary, job_role):
                    continue
                result_jobs.append(j)

            print(f"=== {len(result_jobs)} PASSENDE JOBS (noch nicht bewertet) ===\n")
            for i, j in enumerate(result_jobs, 1):
                print(f"--- Job {i} ---")
                print(f"  ID:          {j['id']}")
                print(f"  Position:    {j['position']}")
                print(f"  Firma:       {j['company_name']}")
                print(f"  Stadt:       {j['work_location_city'] or j['city']}")
                print(f"  Arrangement: {j['work_arrangement'] or 'k.A.'}")
                print(f"  Rolle:       {(_parse_jsonb(j['classification_data']) or {}).get('primary_role', 'k.A.')}")
                print(f"  Quality:     {j['quality_score'] or 'k.A.'}")
                if j["job_tasks"]:
                    print(f"  Aufgaben:    {j['job_tasks']}")
                print(f"  Job-Text (vollstaendig):")
                print(f"  {j['job_text'] or 'k.A.'}")
                print()

    finally:
        await pool.close()


# ── SAVE ─────────────────────────────────────────────────────


async def cmd_save(json_str: str):
    """Speichert eine oder mehrere Bewertungen in der matches-Tabelle.

    Vor dem Speichern wird die echte Fahrzeit per Google Maps berechnet.
    Wenn BEIDE (Auto + OEPNV) zu lang sind, wird der Match NICHT gespeichert.
    """
    data = json.loads(json_str)

    # Einzelnes Objekt oder Liste
    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        print("Fehler: JSON muss ein Objekt oder eine Liste sein.")
        return

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            saved = 0
            skipped = 0
            skipped_drive = 0
            for item in items:
                job_id = item.get("job_id")
                candidate_id = item.get("candidate_id")
                score = item.get("score", 0)
                empfehlung = item.get("empfehlung", "nicht_passend")
                wow_faktor = item.get("wow_faktor", False)
                wow_grund = item.get("wow_grund")
                ai_explanation = item.get("ai_explanation", "")
                ai_strengths = item.get("ai_strengths", [])
                ai_weaknesses = item.get("ai_weaknesses", [])

                if not job_id or not candidate_id:
                    print(f"  SKIP: job_id oder candidate_id fehlt")
                    skipped += 1
                    continue

                # Score clampen
                score = max(0, min(100, float(score)))

                # Empfehlung validieren
                valid_empfehlungen = ["vorstellen", "beobachten", "nicht_passend"]
                if empfehlung not in valid_empfehlungen:
                    empfehlung = "nicht_passend"

                # Matches unter MIN_SCORE (75%) NICHT speichern
                if score < MIN_SCORE:
                    print(f"  SKIP: {job_id[:8]}...x{candidate_id[:8]}... Score {score} < {MIN_SCORE}% Minimum")
                    skipped += 1
                    continue

                # ── Fahrzeit-Berechnung per Google Maps ──
                drive_car_min = None
                drive_transit_min = None
                distance_km = None

                # Koordinaten von Job und Kandidat laden
                job_row = await conn.fetchrow(
                    """
                    SELECT ST_Y(location_coords::geometry) as lat,
                           ST_X(location_coords::geometry) as lng,
                           postal_code, work_arrangement, job_text
                    FROM jobs WHERE id = $1
                    """,
                    UUID(job_id),
                )
                cand_row = await conn.fetchrow(
                    """
                    SELECT ST_Y(address_coords::geometry) as lat,
                           ST_X(address_coords::geometry) as lng,
                           postal_code
                    FROM candidates WHERE id = $1
                    """,
                    UUID(candidate_id),
                )

                job_is_remote = job_row and job_row["work_arrangement"] == "remote"

                # Fahrzeit nur berechnen wenn NICHT remote und Koordinaten vorhanden
                if (
                    not job_is_remote
                    and job_row and job_row["lat"] and job_row["lng"]
                    and cand_row and cand_row["lat"] and cand_row["lng"]
                ):
                    dt = await _calc_drive_time(
                        cand_row["lat"], cand_row["lng"],
                        job_row["lat"], job_row["lng"],
                    )
                    drive_car_min = dt["car_min"]
                    drive_transit_min = dt["transit_min"]
                    distance_km = dt["car_km"]

                    if drive_car_min is not None and drive_transit_min is not None:
                        # Home-Office Check: bietet der Job min. 2 Tage HO?
                        has_ho = _job_has_homeoffice(job_row["job_text"] or "")
                        max_limit = MAX_DRIVE_TIME_HOMEOFFICE if has_ho else MAX_DRIVE_TIME_DEFAULT

                        # BEIDE muessen ueber dem Limit sein → dann SKIP
                        if drive_car_min > max_limit and drive_transit_min > max_limit:
                            ho_txt = " (mit HO)" if has_ho else ""
                            print(
                                f"  SKIP FAHRZEIT: {job_id[:8]}...x{candidate_id[:8]}... "
                                f"Auto {drive_car_min}min, OEPNV {drive_transit_min}min > {max_limit}min{ho_txt}"
                            )
                            skipped_drive += 1
                            continue
                    elif drive_car_min is not None:
                        # Nur Auto verfuegbar (kein OEPNV-Ergebnis)
                        has_ho = _job_has_homeoffice(job_row["job_text"] or "")
                        max_limit = MAX_DRIVE_TIME_HOMEOFFICE if has_ho else MAX_DRIVE_TIME_DEFAULT
                        if drive_car_min > max_limit:
                            print(
                                f"  SKIP FAHRZEIT: {job_id[:8]}...x{candidate_id[:8]}... "
                                f"Auto {drive_car_min}min > {max_limit}min (kein OEPNV-Ergebnis)"
                            )
                            skipped_drive += 1
                            continue

                try:
                    # UPSERT (bei Duplikat: Update)
                    await conn.execute(
                        """
                        INSERT INTO matches (
                            id, job_id, candidate_id, matching_method,
                            ai_score, v2_score, ai_explanation,
                            ai_strengths, ai_weaknesses,
                            empfehlung, wow_faktor, wow_grund,
                            drive_time_car_min, drive_time_transit_min, distance_km,
                            status, v2_score_breakdown,
                            created_at, updated_at
                        ) VALUES (
                            gen_random_uuid(), $1, $2, 'claude_code',
                            $3, $4, $5,
                            $6, $7,
                            $8, $9, $10,
                            $11, $12, $13,
                            'new', $14,
                            NOW(), NOW()
                        )
                        ON CONFLICT (job_id, candidate_id) DO UPDATE SET
                            matching_method = COALESCE(matches.matching_method, 'claude_code'),
                            ai_score = $3,
                            v2_score = $4,
                            ai_explanation = $5,
                            ai_strengths = $6,
                            ai_weaknesses = $7,
                            empfehlung = $8,
                            wow_faktor = $9,
                            wow_grund = $10,
                            drive_time_car_min = $11,
                            drive_time_transit_min = $12,
                            distance_km = $13,
                            v2_score_breakdown = $14,
                            updated_at = NOW()
                        """,
                        UUID(job_id),
                        UUID(candidate_id),
                        score / 100.0,  # ai_score: 0-1 Skala
                        float(score),   # v2_score: 0-100 Skala
                        ai_explanation,
                        ai_strengths,
                        ai_weaknesses,
                        empfehlung,
                        wow_faktor,
                        wow_grund,
                        drive_car_min,
                        drive_transit_min,
                        distance_km,
                        json.dumps({
                            "scoring_version": "claude_code_v2",
                            "prompt_version": "recruiter_expert_v1",
                            "score": score,
                            "empfehlung": empfehlung,
                            "wow_faktor": wow_faktor,
                            "drive_time_car_min": drive_car_min,
                            "drive_time_transit_min": drive_transit_min,
                        }),
                    )
                    saved += 1
                    emoji = "★" if wow_faktor else "●" if empfehlung == "vorstellen" else "○"
                    drive_info = ""
                    if drive_car_min is not None:
                        drive_info = f" | Auto {drive_car_min}min"
                        if drive_transit_min is not None:
                            drive_info += f", OEPNV {drive_transit_min}min"
                    elif job_is_remote:
                        drive_info = " | Remote"
                    print(f"  {emoji} SAVED: {job_id[:8]}...x{candidate_id[:8]}... → {empfehlung} (Score: {score}{drive_info})")
                except Exception as e:
                    print(f"  FEHLER: {job_id[:8]}...x{candidate_id[:8]}...: {e}")
                    skipped += 1

            print(f"\n=== {saved} gespeichert, {skipped} uebersprungen, {skipped_drive} wegen Fahrzeit rausgefiltert ===")

    finally:
        await pool.close()


# ── STALE-CHECK ──────────────────────────────────────────────


async def cmd_stale_check():
    """Erkennt veraltete Matches (Kandidat/Job hat sich geaendert)."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            # Matches wo Kandidat NACH dem Match aktualisiert wurde
            stale_by_candidate = await conn.fetch(
                """
                SELECT m.id, m.job_id, m.candidate_id, m.created_at,
                       c.updated_at as candidate_updated
                FROM matches m
                JOIN candidates c ON c.id = m.candidate_id
                WHERE m.stale = false
                  AND m.matching_method = 'claude_code'
                  AND c.updated_at > m.created_at
                """
            )

            # Matches wo Job NACH dem Match aktualisiert wurde
            stale_by_job = await conn.fetch(
                """
                SELECT m.id, m.job_id, m.candidate_id, m.created_at,
                       j.updated_at as job_updated
                FROM matches m
                JOIN jobs j ON j.id = m.job_id
                WHERE m.stale = false
                  AND m.matching_method = 'claude_code'
                  AND j.updated_at > m.created_at
                """
            )

            # Matches wo Job abgelaufen ist
            stale_by_expired = await conn.fetch(
                """
                SELECT m.id, m.job_id, m.candidate_id
                FROM matches m
                JOIN jobs j ON j.id = m.job_id
                WHERE m.stale = false
                  AND m.matching_method = 'claude_code'
                  AND j.expires_at IS NOT NULL
                  AND j.expires_at < NOW()
                """
            )

            total_stale = len(stale_by_candidate) + len(stale_by_job) + len(stale_by_expired)

            if total_stale == 0:
                print("Keine veralteten Matches gefunden. Alles aktuell!")
                return

            print(f"=== {total_stale} VERALTETE MATCHES ===\n")

            if stale_by_candidate:
                print(f"  {len(stale_by_candidate)} Matches: Kandidat hat sich geaendert")
            if stale_by_job:
                print(f"  {len(stale_by_job)} Matches: Job hat sich geaendert")
            if stale_by_expired:
                print(f"  {len(stale_by_expired)} Matches: Job abgelaufen")

            # Als stale markieren
            all_stale_ids = set()
            for r in stale_by_candidate:
                all_stale_ids.add(r["id"])
            for r in stale_by_job:
                all_stale_ids.add(r["id"])
            for r in stale_by_expired:
                all_stale_ids.add(r["id"])

            if all_stale_ids:
                await conn.execute(
                    """
                    UPDATE matches SET stale = true, stale_since = NOW(),
                           stale_reason = 'Kandidat oder Job hat sich geaendert'
                    WHERE id = ANY($1)
                    """,
                    list(all_stale_ids),
                )
                print(f"\n{len(all_stale_ids)} Matches als stale markiert.")

    finally:
        await pool.close()


# ── CLEAR OLD MATCHES ────────────────────────────────────────


async def cmd_clear_old_matches():
    """Loescht alle alten (nicht-claude_code) Matches."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM matches WHERE matching_method != 'claude_code' OR matching_method IS NULL"
            )
            if count == 0:
                print("Keine alten Matches zum Loeschen.")
                return

            print(f"  {count} alte Matches gefunden (nicht claude_code).")
            print("  Loesche...")
            await conn.execute(
                "DELETE FROM matches WHERE matching_method != 'claude_code' OR matching_method IS NULL"
            )
            print(f"  {count} alte Matches geloescht.")
    finally:
        await pool.close()


# ── HILFSFUNKTIONEN ──────────────────────────────────────────


async def _calc_drive_time(
    origin_lat: float, origin_lng: float,
    dest_lat: float, dest_lng: float,
) -> dict:
    """Berechnet echte Fahrzeit mit Google Maps Distance Matrix API.

    Returns:
        {"car_min": int|None, "transit_min": int|None, "car_km": float|None, "status": str}
    """
    import httpx
    import time as _time
    from datetime import datetime, timedelta

    if not GOOGLE_MAPS_API_KEY:
        print("  WARNUNG: Kein GOOGLE_MAPS_API_KEY gesetzt — keine Fahrzeit-Berechnung")
        return {"car_min": None, "transit_min": None, "car_km": None, "status": "no_api_key"}

    # Plausibilitaetspruefung: Koordinaten muessen in Deutschland liegen (ca. 47-55 Lat, 5-16 Lng)
    for lat, lng, label in [(origin_lat, origin_lng, "Kandidat"), (dest_lat, dest_lng, "Job")]:
        if lat < 45 or lat > 57 or lng < 4 or lng > 17:
            print(f"  WARNUNG: {label}-Koordinaten ({lat}, {lng}) ausserhalb Deutschlands — ueberspringe Fahrzeit")
            return {"car_min": None, "transit_min": None, "car_km": None, "status": "invalid_coords"}

    origin = f"{origin_lat},{origin_lng}"
    dest = f"{dest_lat},{dest_lng}"

    # OEPNV departure_time: naechster Werktag um 08:00 Uhr (konsistente Ergebnisse)
    now = datetime.now()
    next_day = now + timedelta(days=1)
    # Auf naechsten Werktag springen (Mo=0, Fr=4)
    while next_day.weekday() > 4:  # Sa=5, So=6
        next_day += timedelta(days=1)
    departure_8am = next_day.replace(hour=8, minute=0, second=0, microsecond=0)
    departure_timestamp = str(int(departure_8am.timestamp()))

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Auto-Fahrzeit
        car_min = None
        car_km = None
        try:
            resp = await client.get(DISTANCE_MATRIX_URL, params={
                "origins": origin,
                "destinations": dest,
                "mode": "driving",
                "language": "de",
                "key": GOOGLE_MAPS_API_KEY,
            })
            data = resp.json()
            if data.get("status") == "OK":
                elem = data["rows"][0]["elements"][0]
                if elem["status"] == "OK":
                    car_min = math.ceil(elem["duration"]["value"] / 60)
                    car_km = round(elem.get("distance", {}).get("value", 0) / 1000, 1)
        except Exception as e:
            print(f"  WARNUNG: Google Maps API Fehler (driving): {e}")

        # Rate-Limiting: 200ms Pause zwischen API-Aufrufen
        await asyncio.sleep(0.2)

        # OEPNV-Fahrzeit
        transit_min = None
        try:
            resp = await client.get(DISTANCE_MATRIX_URL, params={
                "origins": origin,
                "destinations": dest,
                "mode": "transit",
                "language": "de",
                "departure_time": departure_timestamp,
                "key": GOOGLE_MAPS_API_KEY,
            })
            data = resp.json()
            if data.get("status") == "OK":
                elem = data["rows"][0]["elements"][0]
                if elem["status"] == "OK":
                    transit_min = math.ceil(elem["duration"]["value"] / 60)
        except Exception as e:
            print(f"  WARNUNG: Google Maps API Fehler (transit): {e}")

    return {
        "car_min": car_min,
        "transit_min": transit_min,
        "car_km": car_km,
        "status": "ok" if car_min is not None else "api_error",
    }


def _job_has_homeoffice(job_text: str) -> bool:
    """Prueft ob der Job-Text mindestens 2 Tage Home-Office/Remote anbietet."""
    if not job_text:
        return False
    text = job_text.lower()

    # Hybrid oder Remote im Arrangement → zaehlt als Home-Office-Angebot
    # Explizite Erwaehnung von 2+ Tagen Home-Office
    import re
    patterns = [
        r"2\s*(?:bis\s*\d\s*)?tage?\s*(?:home[- ]?office|remote|mobil)",
        r"3\s*(?:bis\s*\d\s*)?tage?\s*(?:home[- ]?office|remote|mobil)",
        r"(?:zwei|drei|vier)\s*tage?\s*(?:home[- ]?office|remote|mobil)",
        r"(?:50|60|80)\s*%?\s*(?:home[- ]?office|remote|mobil)",
        r"home[- ]?office.*(?:2|3|zwei|drei)\s*tage",
        r"remote.*(?:2|3|zwei|drei)\s*tage",
        r"(?:mind(?:estens)?\.?\s*)?2\s*(?:x|mal)\s*(?:home[- ]?office|remote)",
    ]
    for pat in patterns:
        if re.search(pat, text):
            return True
    return False


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Berechnet die Entfernung zwischen zwei Punkten in Metern (Haversine)."""
    R = 6_371_000  # Erdradius in Metern
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# Finance-Rollen — NUR diese werden gematcht (alles andere ist kein Finance-Kandidat)
FINANCE_ROLES = {
    "Finanzbuchhalter/in",
    "Senior Finanzbuchhalter/in",
    "Bilanzbuchhalter/in",
    "Senior Bilanzbuchhalter/in",
    "Kreditorenbuchhalter/in",
    "Senior Debitorenbuchhalter/in",
    "Debitorenbuchhalter/in",
    "Lohnbuchhalter/in",
    "Steuerfachangestellte/r / Finanzbuchhalter/in",
    "Head of Finance",
    "Financial Controller",
    "Senior Financial Controller",
    "Leiter Buchhaltung",
    "Leiter Rechnungswesen",
    "Leiter Lohnbuchhaltung",
    "Leiter Debitorenbuchhaltung",
    "Leiter Finanz- und Anlagenbuchhaltung",
    "Teamleiter Finanzbuchhaltung",
    "Teamleiter Kreditorenbuchhaltung",
    "Teamleiter Debitorenbuchhaltung",
    "Teamleiter Lohnbuchhaltung",
    "Teamleiter Rechnungswesen",
    "Teamleiter Buchhaltung",
    "Teamleiter Accounting",
    "Teamleiter Finanzen und Verwaltung",
    "Abteilungsleiter Finanzbuchhaltung",
    "Stv. Abteilungsleiter Finanzbuchhaltung",
    "Abteilungsleiterin Finanzen/Buchhaltung",
    "Stellv. Leitung Buchhaltung",
    "Stellv. Leitung Finanzbuchhaltung",
    "Stellvertretende Leiterin Finance",
    "Hauptbuchhalter/in",
    "Hauptbuchhalterin",
    "Alleinbuchhalterin",
    "Alleinbuchhalter",
    "Anlagenbuchhalterin",
    "Steuerberater/in",
    "Steuerberaterin",
    "Steuerberater",
    "Steuerfachwirtin",
    "Buchhalterin",
    "Selbstständiger Buchhalter",
    "Junior-Buchhalter/in",
    "Group Accountant",
    "Senior Group Accountant",
    "Accountant",
    "Accounting Manager",
    "Manager Accounting",
    "Manager Group Accounting",
    "Financial Accountant",
    "Referent Finanzen und Steuern",
    "Steuerreferentin",
    "Tax Specialist",
    "Tax Officer",
}

# Verwandte Rollen-Matrix — mit echten DB-Rollennamen
# ROLE_COMPAT: Key = Job-Rolle, Value = welche Kandidaten-primary_roles dafuer in Frage kommen
ROLE_COMPAT = {
    # FiBu-Jobs: FiBu + Senior FiBu + StFA Kandidaten
    "Finanzbuchhalter/in": {
        "Finanzbuchhalter/in", "Senior Finanzbuchhalter/in",
        "Steuerfachangestellte/r / Finanzbuchhalter/in",
    },
    "Senior Finanzbuchhalter/in": {
        "Senior Finanzbuchhalter/in",
    },
    # BiBu-Jobs: nur BiBu Kandidaten
    "Bilanzbuchhalter/in": {
        "Bilanzbuchhalter/in", "Senior Bilanzbuchhalter/in",
    },
    "Senior Bilanzbuchhalter/in": {
        "Senior Bilanzbuchhalter/in",
    },
    # Kreditoren: nur Kreditoren
    "Kreditorenbuchhalter/in": {
        "Kreditorenbuchhalter/in",
    },
    # Debitoren: nur Debitoren
    "Debitorenbuchhalter/in": {
        "Debitorenbuchhalter/in",
    },
    # Lohn: nur Lohn
    "Lohnbuchhalter/in": {
        "Lohnbuchhalter/in",
    },
    # StFA: nur StFA
    "Steuerfachangestellte/r / Finanzbuchhalter/in": {
        "Steuerfachangestellte/r / Finanzbuchhalter/in",
    },
    # Controller
    "Financial Controller": {
        "Financial Controller", "Senior Financial Controller",
    },
    "Senior Financial Controller": {
        "Senior Financial Controller",
    },
    # Head of Finance / Leitung
    "Head of Finance": {
        "Head of Finance",
    },
    "Leiter Buchhaltung": {
        "Leiter Buchhaltung",
    },
    # Teamleiter
    "Teamleiter Finanzbuchhaltung": {
        "Teamleiter Finanzbuchhaltung",
    },
    "Teamleiter Kreditorenbuchhaltung": {
        "Teamleiter Kreditorenbuchhaltung",
    },
    "Teamleiter Debitorenbuchhaltung": {
        "Teamleiter Debitorenbuchhaltung",
    },
    "Teamleiter Lohnbuchhaltung": {
        "Teamleiter Lohnbuchhaltung",
    },
    # Steuerberater
    "Steuerberater/in": {
        "Steuerberater/in",
    },
    # Allgemeine Buchhaltung
    "Accountant": {
        "Accountant",
    },
    "Group Accountant": {
        "Group Accountant",
    },
}


def _roles_compatible(cand_primary: str, job_role: str) -> bool:
    """Prueft ob Kandidat-primary_role zum Job passt.
    ROLE_COMPAT[job_role] = Set von Kandidaten-primary_roles die in Frage kommen.
    """
    allowed = ROLE_COMPAT.get(job_role, {job_role})
    return cand_primary in allowed


# ── MAIN ─────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Claude Match Helper — CLI-Tool fuer Claude-basiertes Matching"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="Zeige Status-Uebersicht")
    group.add_argument("--batch", action="store_true", help="Hole naechsten Batch unbewerteter Paare")
    group.add_argument("--job", type=str, help="Zeige Job mit passenden Kandidaten")
    group.add_argument("--candidate", type=str, help="Zeige Kandidat mit passenden Jobs")
    group.add_argument("--save", type=str, help="Speichere Bewertung (JSON)")
    group.add_argument("--stale-check", action="store_true", help="Pruefe auf veraltete Matches")
    group.add_argument("--clear-old-matches", action="store_true", help="Loesche alle nicht-claude_code Matches")

    # Batch-Filter
    parser.add_argument("--city", type=str, help="Filter: Stadt")
    parser.add_argument("--role", type=str, help="Filter: Rolle (z.B. FiBu, BiBu, StFA)")
    parser.add_argument("--limit", type=int, default=10, help="Max Paare pro Batch (default: 10)")

    args = parser.parse_args()

    if args.status:
        asyncio.run(cmd_status())
    elif args.batch:
        asyncio.run(cmd_batch(args.city, args.role, args.limit))
    elif args.job:
        asyncio.run(cmd_job(args.job))
    elif args.candidate:
        asyncio.run(cmd_candidate(args.candidate))
    elif args.save:
        asyncio.run(cmd_save(args.save))
    elif args.stale_check:
        asyncio.run(cmd_stale_check())
    elif args.clear_old_matches:
        asyncio.run(cmd_clear_old_matches())


if __name__ == "__main__":
    main()
