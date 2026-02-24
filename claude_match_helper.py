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
DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:aG4ddfAgdAbGg3bDBFD12f3GdDGAgcFD@shuttle.proxy.rlwy.net:43640/railway",
)

# asyncpg braucht postgresql:// (nicht postgresql+asyncpg://)
ASYNCPG_URL = DB_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
    "postgres://", "postgresql://"
)

# Max Luftlinie in Metern fuer PostGIS-Vorfilter
MAX_LUFTLINIE_M = 30_000  # 30 km

# Minimum-Score: Matches unter diesem Wert werden NICHT gespeichert + keine Fahrzeit
MIN_SCORE = 75  # Unter 75% = nicht speichern, keine Fahrzeit, nichts


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

            # Kandidaten laden (verfuegbar, nicht geloescht, mit Koordinaten)
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
                if job["quality_score"] == "low":
                    continue  # Schlechte Job-Qualitaet ueberspringen

                job_is_remote = job["work_arrangement"] == "remote"

                for cand in candidates:
                    # Bereits bewertet?
                    if (str(job["id"]), str(cand["id"])) in existing_set:
                        continue

                    # Excluded Companies pruefen
                    excluded = _parse_jsonb(cand["excluded_companies"]) or []
                    if isinstance(excluded, list) and job["company_name"]:
                        if job["company_name"].lower() in [e.lower() for e in excluded if isinstance(e, str)]:
                            continue

                    # Rollen-Kompatibilitaet (grober Vorfilter)
                    job_role = (_parse_jsonb(job["classification_data"]) or {}).get("primary_role", "")
                    cand_roles = (_parse_jsonb(cand["classification_data"]) or {}).get("roles", [])
                    cand_primary = (_parse_jsonb(cand["classification_data"]) or {}).get("primary_role", "")

                    # Rollen-Kompatibilitaet (nur wenn beide klassifiziert sind)
                    if job_role and cand_primary:
                        compatible = _roles_compatible(cand_primary, cand_roles, job_role)
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
                    print(f"  Aufgaben:    {j['job_tasks'][:200]}...")
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
                        for pos in wh[:3]:
                            if isinstance(pos, dict):
                                print(f"  Erfahrung:   {pos.get('position', '')} bei {pos.get('company', '')} ({pos.get('period', '')})")
                    elif isinstance(wh, dict):
                        for key, val in list(wh.items())[:3]:
                            print(f"  Erfahrung:   {key}: {str(val)[:100]}")
                if c["education"]:
                    edu = c["education"]
                    if isinstance(edu, list):
                        for e in edu[:2]:
                            if isinstance(e, dict):
                                print(f"  Ausbildung:  {e.get('degree', '')} — {e.get('institution', '')} ({e.get('period', '')})")
                    elif isinstance(edu, dict):
                        for key, val in list(edu.items())[:2]:
                            print(f"  Ausbildung:  {key}: {str(val)[:100]}")
                print()
                print(f"  Job-Text (Auszug):")
                print(f"  {(j['job_text'] or '')[:500]}")
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
            print(f"  Job-Text:")
            print(f"  {(job['job_text'] or '')[:1000]}")
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
                # Excluded companies
                excluded = _parse_jsonb(c["excluded_companies"]) or []
                if isinstance(excluded, list) and job["company_name"]:
                    if job["company_name"].lower() in [e.lower() for e in excluded if isinstance(e, str)]:
                        continue
                # Rollen-Check
                cand_primary = (_parse_jsonb(c["classification_data"]) or {}).get("primary_role", "")
                cand_roles = (_parse_jsonb(c["classification_data"]) or {}).get("roles", [])
                if job_role and cand_primary and not _roles_compatible(cand_primary, cand_roles, job_role):
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
                        for pos in wh[:3]:
                            if isinstance(pos, dict):
                                print(f"  Erfahrung: {pos.get('position', '')} bei {pos.get('company', '')} ({pos.get('period', '')})")
                if c["education"]:
                    edu = c["education"]
                    if isinstance(edu, list):
                        for e in edu[:2]:
                            if isinstance(e, dict):
                                print(f"  Ausbildung: {e.get('degree', '')} — {e.get('institution', '')} ({e.get('period', '')})")
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
            cand_roles = (_parse_jsonb(cand["classification_data"]) or {}).get("roles", [])

            print(f"=== KANDIDAT: {cand['current_position'] or 'k.A.'} in {cand['city'] or 'k.A.'} ===")
            print(f"  ID:         {cand['id']}")
            print(f"  Rolle:      {cand_primary or 'k.A.'}")
            print(f"  Skills:     {', '.join(cand['skills'] or []) or 'k.A.'}")
            print(f"  IT/ERP:     {', '.join(cand['it_skills'] or []) or 'k.A.'}")
            if cand["work_history"]:
                wh = cand["work_history"]
                if isinstance(wh, list):
                    for pos in wh[:5]:
                        if isinstance(pos, dict):
                            print(f"  Erfahrung:  {pos.get('position', '')} bei {pos.get('company', '')} ({pos.get('period', '')})")
            if cand["education"]:
                edu = cand["education"]
                if isinstance(edu, list):
                    for e in edu[:3]:
                        if isinstance(e, dict):
                            print(f"  Ausbildung: {e.get('degree', '')} — {e.get('institution', '')} ({e.get('period', '')})")
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
                if j["quality_score"] == "low":
                    continue
                if isinstance(excluded, list) and j["company_name"]:
                    if j["company_name"].lower() in [e.lower() for e in excluded if isinstance(e, str)]:
                        continue
                job_role = (_parse_jsonb(j["classification_data"]) or {}).get("primary_role", "")
                if job_role and cand_primary and not _roles_compatible(cand_primary, cand_roles, job_role):
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
                    print(f"  Aufgaben:    {j['job_tasks'][:200]}")
                print(f"  Job-Text:    {(j['job_text'] or '')[:300]}...")
                print()

    finally:
        await pool.close()


# ── SAVE ─────────────────────────────────────────────────────


async def cmd_save(json_str: str):
    """Speichert eine oder mehrere Bewertungen in der matches-Tabelle."""
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

                # Matches unter MIN_SCORE (75%) NICHT speichern — Schrott
                if score < MIN_SCORE:
                    print(f"  SKIP: {job_id[:8]}...x{candidate_id[:8]}... Score {score} < {MIN_SCORE}% Minimum")
                    skipped += 1
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
                            status, v2_score_breakdown,
                            created_at, updated_at
                        ) VALUES (
                            gen_random_uuid(), $1, $2, 'claude_code',
                            $3, $4, $5,
                            $6, $7,
                            $8, $9, $10,
                            'new', $11,
                            NOW(), NOW()
                        )
                        ON CONFLICT (job_id, candidate_id) DO UPDATE SET
                            matching_method = 'claude_code',
                            ai_score = $3,
                            v2_score = $4,
                            ai_explanation = $5,
                            ai_strengths = $6,
                            ai_weaknesses = $7,
                            empfehlung = $8,
                            wow_faktor = $9,
                            wow_grund = $10,
                            v2_score_breakdown = $11,
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
                        json.dumps({
                            "scoring_version": "claude_code_v1",
                            "prompt_version": "claude_code_v1",
                            "score": score,
                            "empfehlung": empfehlung,
                            "wow_faktor": wow_faktor,
                        }),
                    )
                    saved += 1
                    emoji = "★" if wow_faktor else "●" if empfehlung == "vorstellen" else "○"
                    print(f"  {emoji} SAVED: {job_id[:8]}...x{candidate_id[:8]}... → {empfehlung} (Score: {score})")
                except Exception as e:
                    print(f"  FEHLER: {job_id[:8]}...x{candidate_id[:8]}...: {e}")
                    skipped += 1

            print(f"\n=== {saved} gespeichert, {skipped} uebersprungen ===")

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



def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Berechnet die Entfernung zwischen zwei Punkten in Metern (Haversine)."""
    R = 6_371_000  # Erdradius in Metern
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# Verwandte Rollen-Matrix
ROLE_COMPAT = {
    "FiBu": {"FiBu", "BiBu", "Senior FiBu"},
    "BiBu": {"BiBu", "FiBu", "Senior FiBu"},
    "Senior FiBu": {"Senior FiBu", "FiBu", "BiBu"},
    "KrediBu": {"KrediBu", "FiBu", "BiBu"},
    "DebiBu": {"DebiBu", "KrediBu", "FiBu"},
    "LohnBu": {"LohnBu", "StFA"},
    "StFA": {"StFA", "LohnBu"},
}


def _roles_compatible(cand_primary: str, cand_roles: list[str], job_role: str) -> bool:
    """Prueft ob Kandidat-Rollen mit Job-Rolle kompatibel sind."""
    # Direkte Uebereinstimmung
    if cand_primary == job_role:
        return True
    if job_role in (cand_roles or []):
        return True

    # Verwandte Rollen pruefen
    compat = ROLE_COMPAT.get(cand_primary, set())
    if job_role in compat:
        return True

    # Alle Kandidat-Rollen pruefen
    for role in (cand_roles or []):
        compat = ROLE_COMPAT.get(role, set())
        if job_role in compat:
            return True

    return False


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
