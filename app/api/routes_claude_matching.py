"""V5 Matching — API Routes.

Matching:
  POST /claude-match/run               — V5 Matching starten (Rollen+Geo, Background-Task)
  POST /claude-match/run-auto          — Alias fuer /run (n8n Cron)
  GET  /claude-match/status             — Live-Fortschritt
  POST /claude-match/stop              — Matching stoppen
  GET  /claude-match/daily              — Heutige Top-Matches fuer Action Board
  POST /claude-match/{match_id}/action  — Vorstellen/Spaeter/Ablehnen
  POST /claude-match/candidate/{id}     — Ad-hoc: Jobs fuer einen Kandidaten finden
  POST /claude-match/ai-assessment      — Manuelle KI-Bewertung fuer ausgewaehlte Matches

Debug:
  GET  /debug/match-count               — Match-Statistiken
  GET  /debug/stufe-0-preview           — Dry-Run: Daten-Uebersicht
  GET  /debug/job-health                — Job-Daten Gesundheitscheck
  GET  /debug/candidate-health          — Kandidaten-Daten Gesundheitscheck
  GET  /debug/match/{match_id}          — Match-Detail
  GET  /debug/cost-report               — API-Kosten
"""

import logging
from datetime import datetime, date, timezone, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import sqlalchemy as sa
from sqlalchemy import select, func, and_, case, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.match import Match, MatchStatus
from app.models.candidate import Candidate
from app.models.job import Job

logger = logging.getLogger(__name__)

router = APIRouter(tags=["V5 Matching"])


# ══════════════════════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════════════════════

class ActionRequest(BaseModel):
    """Request Body fuer Match-Aktionen."""
    action: str  # vorstellen, spaeter, ablehnen
    note: str | None = None


class AIAssessmentRequest(BaseModel):
    """Request Body fuer manuelle KI-Bewertung."""
    match_ids: list[str]
    custom_prompt: str | None = None


# ══════════════════════════════════════════════════════════════
# Matching-Endpoints
# ══════════════════════════════════════════════════════════════

@router.post("/claude-match/run")
async def start_matching():
    """Startet das V5 Matching (Rollen + Geo + Fahrzeit)."""
    from app.services.v5_matching_service import get_status, run_matching

    status = get_status()
    if status["running"]:
        return {
            "status": "already_running",
            "message": "Matching laeuft bereits. Fortschritt unter /status abrufbar.",
            "progress": status["progress"],
        }

    result = await run_matching()
    return result


@router.get("/claude-match/status")
async def matching_status():
    """Gibt den aktuellen Matching-Status zurueck (Live-Fortschritt)."""
    from app.services.v5_matching_service import get_status
    return get_status()


@router.get("/claude-match/live")
async def matching_live():
    """Live-Status-Seite fuer V5 Matching — direkt im Browser aufrufbar."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>V5 Matching Live-Status</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 960px; margin: 40px auto; padding: 0 20px; background: #0f1117; color: #e4e4e7; }
  h1 { font-size: 20px; margin-bottom: 24px; }
  h2 { font-size: 15px; margin: 20px 0 10px; color: #a1a1aa; }
  .card { background: #1a1b23; border: 1px solid #2a2b35; border-radius: 12px; padding: 20px; margin-bottom: 16px; }
  .row { display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 13px; }
  .label { color: #a1a1aa; }
  .value { font-weight: 600; }
  .green { color: #10b981; }
  .red { color: #ef4444; }
  .amber { color: #f59e0b; }
  .blue { color: #6366f1; }
  .bar-bg { background: #2a2b35; border-radius: 6px; height: 8px; margin: 12px 0; overflow: hidden; }
  .bar { height: 100%; background: #6366f1; border-radius: 6px; transition: width 0.5s; }
  button { padding: 12px 20px; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; background: #6366f1; color: #fff; margin-bottom: 8px; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-full { width: 100%; }
  .btn-red { background: #ef4444; }
  .btn-green { background: #10b981; }
  .btn-sm { padding: 4px 10px; font-size: 11px; border-radius: 6px; margin: 0; }
  .status-text { text-align: center; font-size: 13px; color: #a1a1aa; margin-top: 12px; }
  a { color: #6366f1; text-decoration: none; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
  th { text-align: left; color: #a1a1aa; font-weight: 600; padding: 6px 8px; border-bottom: 1px solid #2a2b35; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
  td { padding: 5px 8px; border-bottom: 1px solid #1f2029; color: #e4e4e7; }
  tr:hover td { background: #1f2029; }
  .tag { display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 4px; margin: 1px 2px; }
  .tag-green { background: rgba(16,185,129,0.15); color: #10b981; }
  .tag-blue { background: rgba(99,102,241,0.15); color: #6366f1; }
  .phase-badge { display: inline-block; font-size: 11px; font-weight: 600; padding: 3px 10px; border-radius: 50px; margin-left: 8px; }
  .phase-active { background: rgba(245,158,11,0.15); color: #f59e0b; }
  .phase-done { background: rgba(16,185,129,0.15); color: #10b981; }
  .phase-waiting { background: rgba(99,102,241,0.15); color: #6366f1; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
  .continue-box { text-align: center; padding: 16px; margin: 12px 0; background: rgba(99,102,241,0.08); border: 1px solid rgba(99,102,241,0.3); border-radius: 10px; }
  .continue-box p { font-size: 13px; color: #a1a1aa; margin-bottom: 10px; }
  /* Modal */
  .modal-bg { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.7); z-index: 100; display: flex; align-items: center; justify-content: center; }
  .modal { background: #1a1b23; border: 1px solid #2a2b35; border-radius: 14px; padding: 24px; max-width: 700px; width: 90%; max-height: 80vh; overflow-y: auto; }
  .modal h3 { font-size: 16px; margin-bottom: 16px; }
  .modal .close { float: right; cursor: pointer; font-size: 18px; color: #a1a1aa; background: none; border: none; padding: 0; }
  .detail-row { display: flex; gap: 8px; margin-bottom: 6px; font-size: 12px; }
  .detail-label { color: #a1a1aa; min-width: 120px; }
  .detail-value { color: #e4e4e7; flex: 1; }
</style>
</head>
<body>
<h1>V5 Matching Live-Status</h1>
<div class="card" id="info">Lade...</div>
<div id="buttons">
  <button id="startBtn" class="btn-full" onclick="startMatching()">V5 Matching starten</button>
  <button id="stopBtn" class="btn-full btn-red" onclick="stopMatching()" style="display:none;">Lauf stoppen</button>
</div>
<div class="status-text" id="msg"></div>

<div id="continue-area"></div>
<div id="phase-results"></div>
<div id="modal-container"></div>

<p style="margin-top:24px;font-size:12px;"><a href="/action-board">&larr; Zurueck zum Action Board</a></p>

<script>
let polling = null;
const esc = s => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };

function cmpBtn(cid, jid) {
  return '<button class="btn-sm" onclick="showCompare(\\''+cid+'\\',\\''+jid+'\\')">Vergleich</button>';
}

async function loadStatus() {
  try {
    const r = await fetch('/api/v4/claude-match/status');
    const d = await r.json();
    const p = d.progress || {};
    const pr = p.phase_results || {};
    const running = d.running || false;
    const phase = p.phase || '';
    const waiting = p.waiting_for_continue || false;

    // ── Status-Card ──
    let html = '';
    if (waiting) {
      html += row('Status', '<span class="blue">Pausiert — warte auf Weiter</span>');
    } else {
      html += row('Status', running ? '<span class="amber">Laeuft</span>' : '<span class="green">Bereit</span>');
    }
    html += row('Phase', phase || '-');
    if (p.cleanup_deleted > 0) html += row('Alte Matches geloescht', '<span class="amber">' + p.cleanup_deleted + '</span>');
    if (p.geo_pairs_found > 0) html += row('Geo-Paare (27km)', '<span class="blue">' + p.geo_pairs_found + '</span>');
    if (p.role_matches > 0) html += row('Rollen-Matches', '<span class="green">' + p.role_matches + '</span>');
    if (p.drive_time_total > 0) {
      const pct = Math.round((p.drive_time_done || 0) / p.drive_time_total * 100);
      html += row('Fahrzeit', (p.drive_time_done || 0) + ' / ' + p.drive_time_total + ' (' + pct + '%)');
      html += '<div class="bar-bg"><div class="bar" style="width:' + pct + '%"></div></div>';
    }
    if (p.matches_saved > 0) html += row('Gespeichert', '<span class="green">' + p.matches_saved + '</span>');
    if (p.telegram_sent > 0) html += row('Telegram', '<span class="blue">' + p.telegram_sent + '</span>');
    if (p.errors > 0) html += row('Fehler', '<span class="red">' + p.errors + '</span>');

    if (!running && d.last_run_result) {
      const lr = d.last_run_result;
      html += '<hr style="border-color:#2a2b35;margin:10px 0;">';
      html += row('Letzter Lauf', d.last_run || '-');
      html += row('Dauer', lr.duration_seconds + 's');
    }
    document.getElementById('info').innerHTML = html;
    document.getElementById('startBtn').style.display = running ? 'none' : 'block';
    document.getElementById('stopBtn').style.display = running ? 'block' : 'none';

    // ── Weiter-Button ──
    const phaseNames = {
      cleanup_done: 'Cleanup abgeschlossen',
      geo_filter_done: 'Geo-Filter abgeschlossen',
      role_filter_done: 'Rollen-Filter abgeschlossen',
      drive_time_done: 'Fahrzeit-Berechnung abgeschlossen',
      saving_done: 'Matches gespeichert',
    };
    if (waiting) {
      const phaseName = phaseNames[phase] || phase;
      document.getElementById('continue-area').innerHTML =
        '<div class="continue-box"><p>' + esc(phaseName) + ' — Ergebnisse pruefen, dann weiter.</p>' +
        '<button class="btn-green" onclick="continueMatching()">Weiter &rarr; Naechste Phase</button></div>';
    } else {
      document.getElementById('continue-area').innerHTML = '';
    }

    // ── Phase-Ergebnisse ──
    let phaseHtml = '';

    // Cleanup
    if (pr.cleanup) {
      phaseHtml += phaseSection('Phase 0: Cleanup', pr.cleanup.message || 'Fertig', phaseState('cleanup', phase));
    }

    // Geo-Filter
    if (pr.geo_filter && pr.geo_filter.length > 0) {
      const st = phaseState('geo_filter', phase);
      phaseHtml += '<h2>Phase A: Geo-Filter (27km) <span class="phase-badge phase-' + st + '">' + p.geo_pairs_found + ' Paare</span></h2>';
      phaseHtml += '<div class="card"><table><thead><tr><th>Kandidat</th><th>Rolle</th><th>Job</th><th>Firma</th><th>km</th><th></th></tr></thead><tbody>';
      pr.geo_filter.forEach(r => {
        phaseHtml += '<tr><td>' + esc(r.kandidat) + '</td><td>' + esc(r.kandidat_rolle) + '</td><td>' + esc(r.job) + '</td><td>' + esc(r.firma) + ', ' + esc(r.job_stadt) + '</td><td>' + (r.distanz_km || '?') + '</td><td>' + cmpBtn(r.candidate_id, r.job_id) + '</td></tr>';
      });
      if (p.geo_pairs_found > 30) phaseHtml += '<tr><td colspan="6" style="color:#a1a1aa;text-align:center;">... und ' + (p.geo_pairs_found - 30) + ' weitere</td></tr>';
      phaseHtml += '</tbody></table></div>';
    }

    // Rollen-Filter
    if (pr.role_filter && pr.role_filter.length > 0) {
      const st = phaseState('role_filter', phase);
      phaseHtml += '<h2>Phase B: Rollen-Filter <span class="phase-badge phase-' + st + '">' + p.role_matches + ' Matches</span></h2>';
      phaseHtml += '<div class="card"><table><thead><tr><th>Kandidat</th><th>Kand. Rollen</th><th>Job</th><th>Gematchte Rollen</th><th>km</th><th></th></tr></thead><tbody>';
      pr.role_filter.forEach(r => {
        const krTags = (r.kandidat_rollen || []).map(x => '<span class="tag tag-blue">' + esc(x) + '</span>').join('');
        const mrTags = (r.gematchte_rollen || []).map(x => '<span class="tag tag-green">' + esc(x) + '</span>').join('');
        phaseHtml += '<tr><td>' + esc(r.kandidat) + '</td><td>' + krTags + '</td><td>' + esc(r.job) + ' (' + esc(r.firma) + ')</td><td>' + mrTags + '</td><td>' + (r.distanz_km || '?') + '</td><td>' + cmpBtn(r.candidate_id, r.job_id) + '</td></tr>';
      });
      if (p.role_matches > 30) phaseHtml += '<tr><td colspan="6" style="color:#a1a1aa;text-align:center;">... und ' + (p.role_matches - 30) + ' weitere</td></tr>';
      phaseHtml += '</tbody></table></div>';
    }

    // Fahrzeit
    if (pr.drive_time && pr.drive_time.length > 0) {
      const st = phaseState('drive_time', phase);
      phaseHtml += '<h2>Phase C: Fahrzeit <span class="phase-badge phase-' + st + '">' + (p.drive_time_done || 0) + '/' + (p.drive_time_total || 0) + '</span></h2>';
      phaseHtml += '<div class="card"><table><thead><tr><th>Kandidat</th><th>Job</th><th>km</th><th>Auto</th><th>OEPNV</th><th></th></tr></thead><tbody>';
      pr.drive_time.forEach(r => {
        const carColor = r.auto_min != null ? (r.auto_min <= 20 ? 'green' : r.auto_min <= 40 ? 'amber' : 'red') : '';
        phaseHtml += '<tr><td>' + esc(r.kandidat) + '</td><td>' + esc(r.job) + '</td><td>' + (r.distanz_km || '?') + '</td><td class="' + carColor + '">' + (r.auto_min != null ? r.auto_min + ' Min' : '-') + '</td><td>' + (r.oepnv_min != null ? r.oepnv_min + ' Min' : '-') + '</td><td>' + cmpBtn(r.candidate_id, r.job_id) + '</td></tr>';
      });
      phaseHtml += '</tbody></table></div>';
    }

    // Gespeichert
    if (pr.saved) {
      phaseHtml += phaseSection('Phase D: Matches gespeichert', pr.saved.message || (pr.saved.count + ' Matches'), phaseState('saving', phase));
    }

    // Telegram
    if (pr.telegram && pr.telegram.length > 0) {
      phaseHtml += '<h2>Phase E: Telegram <span class="phase-badge phase-done">' + p.telegram_sent + ' gesendet</span></h2>';
      phaseHtml += '<div class="card"><table><thead><tr><th>Kandidat</th><th>Rolle</th><th>Job</th><th>Auto</th><th>OEPNV</th></tr></thead><tbody>';
      pr.telegram.forEach(r => {
        phaseHtml += '<tr><td>' + esc(r.kandidat) + '</td><td>' + esc(r.rolle) + '</td><td>' + esc(r.job) + '</td><td class="green">' + r.auto_min + ' Min</td><td>' + r.oepnv_min + ' Min</td></tr>';
      });
      phaseHtml += '</tbody></table></div>';
    }

    document.getElementById('phase-results').innerHTML = phaseHtml;

    if (running && !polling) polling = setInterval(loadStatus, 2000);
    if (!running && polling) { clearInterval(polling); polling = null; }
  } catch(e) {
    document.getElementById('info').textContent = 'Fehler: ' + e.message;
  }
}

function row(label, value) {
  return '<div class="row"><span class="label">' + label + '</span><span class="value">' + value + '</span></div>';
}
function phaseSection(title, msg, state) {
  return '<h2>' + title + ' <span class="phase-badge phase-' + state + '">' + esc(msg) + '</span></h2>';
}
function phaseState(phaseName, currentPhase) {
  if (currentPhase === phaseName) return 'active';
  if (currentPhase === phaseName + '_done') return 'waiting';
  return 'done';
}

async function startMatching() {
  document.getElementById('msg').textContent = 'Starte V5 Matching...';
  const r = await fetch('/api/v4/claude-match/run', {method:'POST'});
  const d = await r.json();
  document.getElementById('msg').textContent = d.message || d.error || JSON.stringify(d);
  loadStatus();
  if (!polling) polling = setInterval(loadStatus, 2000);
}

async function stopMatching() {
  document.getElementById('msg').textContent = 'Wird gestoppt...';
  const r = await fetch('/api/v4/claude-match/stop', {method:'POST'});
  const d = await r.json();
  document.getElementById('msg').textContent = d.message || d.error || JSON.stringify(d);
  loadStatus();
}

async function continueMatching() {
  document.getElementById('msg').textContent = 'Naechste Phase wird gestartet...';
  document.getElementById('continue-area').innerHTML = '';
  const r = await fetch('/api/v4/claude-match/continue', {method:'POST'});
  const d = await r.json();
  document.getElementById('msg').textContent = d.message || '';
  loadStatus();
}

async function showCompare(candidateId, jobId) {
  document.getElementById('modal-container').innerHTML = '<div class="modal-bg" onclick="closeModal(event)"><div class="modal"><p>Lade Vergleich...</p></div></div>';
  try {
    const r = await fetch('/api/v4/claude-match/compare-pair?candidate_id=' + candidateId + '&job_id=' + jobId);
    const d = await r.json();
    const data = d.data || {};
    let html = '<div class="modal-bg" onclick="closeModal(event)"><div class="modal" onclick="event.stopPropagation()">';
    html += '<button class="close" onclick="closeModal()">&times;</button>';
    html += '<h3>Vergleich: Kandidat vs. Job</h3>';
    html += '<h2 style="margin:12px 0 8px;font-size:14px;">Kandidat</h2>';
    html += detailRow('Name', data.candidate_name || '—');
    html += detailRow('Stadt', data.candidate_city || '—');
    html += detailRow('Position', data.candidate_current_position || '—');
    html += detailRow('Rolle', data.candidate_role || '—');
    html += detailRow('Gehalt', data.candidate_salary || '—');
    if (data.skills && data.skills.length > 0) html += detailRow('Skills', data.skills.join(', '));
    if (data.it_skills && data.it_skills.length > 0) html += detailRow('IT-Skills', data.it_skills.join(', '));
    // Werdegang
    if (data.work_history && data.work_history.length > 0) {
      let whHtml = '';
      data.work_history.slice(0, 5).forEach(e => {
        if (typeof e === 'object') {
          whHtml += '<div style="margin-bottom:6px;padding:6px;background:#0f1117;border-radius:6px;font-size:11px;">';
          whHtml += '<b>' + esc(e.position || '—') + '</b> bei ' + esc(e.company || '—');
          if (e.description) whHtml += '<br><span style="color:#a1a1aa;">' + esc((e.description || '').substring(0, 200)) + '</span>';
          whHtml += '</div>';
        }
      });
      html += detailRow('Werdegang', whHtml);
    }
    html += '<hr style="border-color:#2a2b35;margin:14px 0;">';
    html += '<h2 style="margin:0 0 8px;font-size:14px;">Job</h2>';
    html += detailRow('Position', data.job_position || '—');
    html += detailRow('Firma', data.job_company_name || '—');
    html += detailRow('Stadt', data.job_city || '—');
    if (data.job_text) html += detailRow('Beschreibung', '<div style="max-height:200px;overflow-y:auto;font-size:11px;white-space:pre-wrap;">' + esc((data.job_text || '').substring(0, 1000)) + '</div>');
    // Match-Info
    if (data.distance_km != null || data.drive_time_car_min != null) {
      html += '<hr style="border-color:#2a2b35;margin:14px 0;">';
      html += '<h2 style="margin:0 0 8px;font-size:14px;">Match-Daten</h2>';
      if (data.distance_km != null) html += detailRow('Distanz', data.distance_km + ' km');
      if (data.drive_time_car_min != null) html += detailRow('Auto', data.drive_time_car_min + ' Min');
      if (data.drive_time_transit_min != null) html += detailRow('OEPNV', data.drive_time_transit_min + ' Min');
    }
    html += '</div></div>';
    document.getElementById('modal-container').innerHTML = html;
  } catch(e) {
    document.getElementById('modal-container').innerHTML = '<div class="modal-bg" onclick="closeModal(event)"><div class="modal"><button class="close" onclick="closeModal()">&times;</button><p class="red">Fehler: ' + esc(e.message) + '</p></div></div>';
  }
}

function detailRow(label, value) {
  return '<div class="detail-row"><span class="detail-label">' + esc(label) + '</span><span class="detail-value">' + value + '</span></div>';
}

function closeModal(event) {
  if (!event || event.target.classList.contains('modal-bg')) {
    document.getElementById('modal-container').innerHTML = '';
  }
}

loadStatus();
if (!polling) polling = setInterval(loadStatus, 2000);
</script>
</body>
</html>""")


@router.post("/claude-match/continue")
async def continue_matching():
    """Setzt den pausierten Matching-Prozess fort (naechste Phase starten)."""
    from app.services.v5_matching_service import request_continue
    return request_continue()


@router.post("/claude-match/stop")
async def stop_matching():
    """Stoppt den aktuell laufenden Matching-Prozess."""
    from app.services.v5_matching_service import request_stop
    return request_stop()


@router.get("/claude-match/daily")
async def daily_matches(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    include_followups: bool = Query(default=True),
):
    """Holt heutige Top-Matches fuer das Action Board (V5)."""
    today = date.today()
    today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    # ── Top-Matches: V5 Rollen+Geo, heute erstellt, kein Feedback ──
    top_query = (
        select(
            Match.id.label("match_id"),
            Match.candidate_id,
            Match.job_id,
            Match.v2_score,
            Match.ai_strengths,
            Match.ai_weaknesses,
            Match.ai_explanation,
            Match.ai_checked_at,
            Match.distance_km,
            Match.drive_time_car_min,
            Match.drive_time_transit_min,
            Match.matching_method,
            Match.v2_score_breakdown,
            Match.created_at,
            # Kandidaten-Info
            Candidate.first_name.label("candidate_first_name"),
            Candidate.last_name.label("candidate_last_name"),
            Candidate.city.label("candidate_city"),
            Candidate.current_position.label("candidate_position"),
            Candidate.salary.label("candidate_salary"),
            Candidate.hotlist_job_title.label("candidate_role"),
            # Job-Info
            Job.position.label("job_position"),
            Job.company_name.label("job_company"),
            Job.city.label("job_city"),
        )
        .outerjoin(Candidate, Match.candidate_id == Candidate.id)
        .outerjoin(Job, Match.job_id == Job.id)
        .where(
            and_(
                Match.matching_method.in_(["v5_role_geo", "claude_match"]),
                Match.user_feedback.is_(None),
                Match.created_at >= today_start,
            )
        )
        .order_by(Match.distance_km.asc())
        .limit(limit)
    )

    result = await db.execute(top_query)
    top_matches = [dict(row._mapping) for row in result.all()]

    # ── Follow-ups (Spaeter von gestern/vorgestern) ──
    follow_ups = []
    if include_followups:
        followup_query = (
            select(
                Match.id.label("match_id"),
                Match.candidate_id,
                Match.job_id,
                Match.v2_score,
                Match.ai_strengths,
                Match.ai_weaknesses,
                Match.ai_explanation,
                Match.ai_checked_at,
                Match.distance_km,
                Match.drive_time_car_min,
                Match.drive_time_transit_min,
                Match.matching_method,
                Match.feedback_at,
                Candidate.first_name.label("candidate_first_name"),
                Candidate.last_name.label("candidate_last_name"),
                Candidate.city.label("candidate_city"),
                Candidate.current_position.label("candidate_position"),
                Candidate.hotlist_job_title.label("candidate_role"),
                Job.position.label("job_position"),
                Job.company_name.label("job_company"),
                Job.city.label("job_city"),
            )
            .outerjoin(Candidate, Match.candidate_id == Candidate.id)
            .outerjoin(Job, Match.job_id == Job.id)
            .where(
                and_(
                    Match.matching_method.in_(["v5_role_geo", "claude_match"]),
                    Match.user_feedback == "spaeter",
                    Match.feedback_at < today_start,
                )
            )
            .order_by(Match.distance_km.asc())
            .limit(10)
        )
        result = await db.execute(followup_query)
        follow_ups = [dict(row._mapping) for row in result.all()]

    # Alle UUIDs und datetimes serialisierbar machen
    def _serialize(matches: list[dict]) -> list[dict]:
        for m in matches:
            for k, v in m.items():
                if isinstance(v, UUID):
                    m[k] = str(v)
                elif isinstance(v, datetime):
                    m[k] = v.isoformat()
        return matches

    return {
        "top_matches": _serialize(top_matches),
        "follow_ups": _serialize(follow_ups),
        "summary": {
            "total_top": len(top_matches),
            "total_followups": len(follow_ups),
        },
    }


@router.post("/claude-match/{match_id}/action")
async def match_action(
    match_id: UUID,
    body: ActionRequest,
    db: AsyncSession = Depends(get_db),
):
    """Verarbeitet Dashboard-Aktionen: vorstellen, spaeter, ablehnen."""
    match = await db.get(Match, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="Match nicht gefunden")

    now = datetime.now(timezone.utc)

    if body.action == "vorstellen":
        match.user_feedback = "vorstellen"
        match.feedback_at = now
        match.status = MatchStatus.PRESENTED
        match.presentation_status = "prepared"
        if body.note:
            match.feedback_note = body.note

        # ATS Integration: Kandidat in Pipeline einfuegen
        try:
            from app.models.ats_job import ATSJob
            from app.models.ats_pipeline import ATSPipelineEntry, PipelineStage

            # ATSJob fuer diesen Job suchen
            ats_job_result = await db.execute(
                select(ATSJob).where(ATSJob.source_job_id == match.job_id)
            )
            ats_job = ats_job_result.scalar_one_or_none()

            if ats_job:
                # Pruefen ob Kandidat schon in Pipeline
                existing = await db.execute(
                    select(ATSPipelineEntry).where(
                        ATSPipelineEntry.ats_job_id == ats_job.id,
                        ATSPipelineEntry.candidate_id == match.candidate_id,
                    )
                )
                if not existing.scalar_one_or_none():
                    entry = ATSPipelineEntry(
                        ats_job_id=ats_job.id,
                        candidate_id=match.candidate_id,
                        stage=PipelineStage.MATCHED,
                    )
                    db.add(entry)
                    logger.info("ATS Pipeline Entry erstellt fuer Match %s", match_id)
        except Exception as e:
            logger.warning("ATS Integration fuer Match %s: %s", match_id, e)

    elif body.action == "spaeter":
        match.user_feedback = "spaeter"
        match.feedback_at = now
        if body.note:
            match.feedback_note = body.note

    elif body.action == "job_an_kandidat":
        match.user_feedback = "job_an_kandidat"
        match.feedback_at = now
        match.status = MatchStatus.PRESENTED
        match.presentation_status = "prepared"
        if body.note:
            match.feedback_note = body.note

    elif body.action == "profil_an_kunden":
        match.user_feedback = "profil_an_kunden"
        match.feedback_at = now
        match.status = MatchStatus.PRESENTED
        match.presentation_status = "prepared"
        if body.note:
            match.feedback_note = body.note

        # ATS Integration: Kandidat in Pipeline einfuegen
        try:
            from app.models.ats_job import ATSJob
            from app.models.ats_pipeline import ATSPipelineEntry, PipelineStage

            ats_job_result = await db.execute(
                select(ATSJob).where(ATSJob.source_job_id == match.job_id)
            )
            ats_job = ats_job_result.scalar_one_or_none()

            if ats_job:
                existing = await db.execute(
                    select(ATSPipelineEntry).where(
                        ATSPipelineEntry.ats_job_id == ats_job.id,
                        ATSPipelineEntry.candidate_id == match.candidate_id,
                    )
                )
                if not existing.scalar_one_or_none():
                    entry = ATSPipelineEntry(
                        ats_job_id=ats_job.id,
                        candidate_id=match.candidate_id,
                        stage=PipelineStage.MATCHED,
                    )
                    db.add(entry)
                    logger.info("ATS Pipeline Entry erstellt fuer Match %s (profil_an_kunden)", match_id)
        except Exception as e:
            logger.warning("ATS Integration fuer Match %s: %s", match_id, e)

    elif body.action == "ablehnen":
        match.user_feedback = "ablehnen"
        match.feedback_at = now
        match.status = MatchStatus.REJECTED
        if body.note:
            match.feedback_note = body.note
            match.rejection_reason = body.note[:50]

    else:
        raise HTTPException(status_code=400, detail=f"Unbekannte Aktion: {body.action}")

    await db.commit()

    return {"success": True, "match_id": str(match_id), "action": body.action}


@router.get("/claude-match/{match_id}/contacts")
async def get_match_contacts(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Laedt Kontakte des Unternehmens fuer die Empfaenger-Auswahl bei 'Profil an Kunden'.

    Gibt alle CompanyContacts fuer die Firma des Jobs zurueck.
    """
    match = await db.get(Match, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="Match nicht gefunden")

    # Job laden um company_id zu bekommen
    job = await db.execute(select(Job).where(Job.id == match.job_id))
    job_obj = job.scalar_one_or_none()
    if not job_obj:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")

    contacts = []
    company_name = job_obj.company_name or "Unbekannt"

    # CompanyContacts laden wenn company_id vorhanden
    if job_obj.company_id:
        try:
            from app.models.company_contact import CompanyContact

            contact_result = await db.execute(
                select(CompanyContact).where(
                    CompanyContact.company_id == job_obj.company_id
                )
            )
            for c in contact_result.scalars().all():
                name_parts = []
                if c.first_name:
                    name_parts.append(c.first_name)
                if c.last_name:
                    name_parts.append(c.last_name)
                contacts.append({
                    "contact_id": str(c.id),
                    "name": " ".join(name_parts) or "Unbekannt",
                    "position": c.position or "",
                    "email": c.email or "",
                    "phone": c.phone or c.mobile or "",
                })
        except Exception as e:
            logger.warning("Kontakte laden fuer Match %s: %s", match_id, e)

    return {
        "match_id": str(match_id),
        "job_id": str(match.job_id),
        "job_position": job_obj.position or "",
        "company_name": company_name,
        "contacts": contacts,
    }


@router.post("/claude-match/{match_id}/prepare-email")
async def prepare_email(
    match_id: UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """Bereitet eine E-Mail vor (generiert Text via GPT, sendet NICHT).

    Request body:
        direction: "job_an_kandidat" oder "profil_an_kunden"
        contact_email: E-Mail des Empfaengers (nur bei profil_an_kunden)
    """
    direction = body.get("direction", "")
    if direction not in ("job_an_kandidat", "profil_an_kunden"):
        raise HTTPException(status_code=400, detail="direction muss 'job_an_kandidat' oder 'profil_an_kunden' sein")

    from app.services.email_preparation_service import EmailPreparationService
    service = EmailPreparationService(db)
    result = await service.prepare_email(
        match_id=match_id,
        direction=direction,
        contact_email=body.get("contact_email"),
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.post("/claude-match/{match_id}/send-email")
async def send_email(
    match_id: UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """Sendet vorbereitete E-Mail mit PDF-Anhang via Microsoft Graph.

    Request body:
        direction: "job_an_kandidat" oder "profil_an_kunden"
        recipient_email: Empfaenger-E-Mail
        subject: Betreff (ggf. editiert)
        body_text: E-Mail-Text (ggf. editiert)
    """
    direction = body.get("direction", "")
    recipient_email = body.get("recipient_email", "")
    subject = body.get("subject", "")
    body_text = body.get("body_text", "")

    if not all([direction, recipient_email, subject, body_text]):
        raise HTTPException(status_code=400, detail="direction, recipient_email, subject und body_text sind erforderlich")

    from app.services.email_preparation_service import EmailPreparationService
    service = EmailPreparationService(db)
    result = await service.send_email(
        match_id=match_id,
        direction=direction,
        recipient_email=recipient_email,
        subject=subject,
        body_text=body_text,
    )

    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("message", "E-Mail-Versand fehlgeschlagen"))

    return result


@router.post("/claude-match/candidate/{candidate_id}")
async def match_for_candidate(
    candidate_id: UUID,
):
    """Ad-hoc: Finde passende Jobs fuer einen bestimmten Kandidaten."""
    from app.services.v5_matching_service import get_status, run_matching

    status = get_status()
    if status["running"]:
        return {
            "status": "already_running",
            "message": "Matching laeuft bereits.",
        }

    result = await run_matching(candidate_id=str(candidate_id))
    return result


# Alias fuer n8n Cron
@router.post("/claude-match/run-auto")
async def start_matching_auto():
    """Alias fuer /run — fuer n8n Morgen-Cron."""
    from app.services.v5_matching_service import get_status, run_matching

    status = get_status()
    if status["running"]:
        return {"status": "already_running", "message": "Matching laeuft bereits."}

    result = await run_matching()
    return result


@router.post("/claude-match/ai-assessment")
async def trigger_ai_assessment(body: AIAssessmentRequest):
    """Manuelle KI-Bewertung fuer ausgewaehlte Matches starten."""
    from app.services.v5_matching_service import run_ai_assessment, get_status

    status = get_status()
    if status["running"]:
        return {
            "status": "already_running",
            "message": "Ein Matching/Assessment laeuft bereits.",
        }

    if not body.match_ids:
        raise HTTPException(status_code=400, detail="Keine Match-IDs angegeben")

    result = await run_ai_assessment(
        match_ids=body.match_ids,
        custom_prompt=body.custom_prompt,
    )
    return result




# ══════════════════════════════════════════════════════════════
# Vergleichs-Endpoint fuer Paare OHNE Match
# ══════════════════════════════════════════════════════════════

@router.get("/claude-match/compare-pair")
async def compare_pair(
    candidate_id: UUID = Query(...),
    job_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Vergleichs-Daten fuer ein Kandidat-Job-Paar (auch ohne bestehenden Match).

    Wird verwendet fuer den Vergleichs-Button in Stufe 0 und Stufe 1.
    """
    # Zuerst pruefen ob ein Match existiert
    existing_match = await db.execute(
        select(Match.id).where(
            and_(Match.candidate_id == candidate_id, Match.job_id == job_id)
        )
    )
    match_row = existing_match.scalar_one_or_none()

    if match_row:
        # Match existiert — normalen Compare-Endpoint verwenden
        from app.services.match_center_service import MatchCenterService
        service = MatchCenterService(db)
        comparison = await service.get_match_comparison(match_row)
        if comparison:
            return {
                "has_match": True,
                "match_id": str(match_row),
                "data": comparison.__dict__ if hasattr(comparison, "__dict__") else comparison,
            }

    # Kein Match — Daten direkt aus DB laden
    candidate = await db.execute(
        select(
            Candidate.id,
            Candidate.first_name,
            Candidate.last_name,
            Candidate.city,
            Candidate.postal_code,
            Candidate.street_address,
            Candidate.current_position,
            Candidate.current_company,
            Candidate.work_history,
            Candidate.education,
            Candidate.further_education,
            Candidate.languages,
            Candidate.it_skills,
            Candidate.skills,
            Candidate.hotlist_job_title,
            Candidate.salary,
        ).where(Candidate.id == candidate_id)
    )
    cand = candidate.one_or_none()
    if not cand:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    job = await db.execute(
        select(
            Job.id,
            Job.position,
            Job.company_name,
            Job.city,
            Job.postal_code,
            Job.street_address,
            Job.job_text,
        ).where(Job.id == job_id)
    )
    j = job.one_or_none()
    if not j:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")

    name_parts = []
    if cand.first_name:
        name_parts.append(cand.first_name)
    if cand.last_name:
        name_parts.append(cand.last_name)

    return {
        "has_match": False,
        "match_id": None,
        "data": {
            "candidate_id": str(cand.id),
            "candidate_name": " ".join(name_parts) or "Unbekannt",
            "candidate_city": cand.city or "",
            "candidate_postal_code": cand.postal_code or "",
            "candidate_street_address": cand.street_address or "",
            "candidate_current_position": cand.current_position or "",
            "candidate_current_company": cand.current_company or "",
            "candidate_role": cand.hotlist_job_title or "",
            "candidate_salary": cand.salary or "",
            "work_history": cand.work_history,
            "education": cand.education,
            "further_education": cand.further_education,
            "languages": cand.languages,
            "it_skills": cand.it_skills,
            "skills": cand.skills,
            "job_id": str(j.id),
            "job_position": j.position or "",
            "job_company_name": j.company_name or "",
            "job_city": j.city or "",
            "job_postal_code": j.postal_code or "",
            "job_street_address": j.street_address or "",
            "job_text": j.job_text or "",
            "ai_score": None,
            "ai_explanation": None,
            "ai_strengths": None,
            "ai_weaknesses": None,
            "distance_km": None,
            "drive_time_car_min": None,
            "drive_time_transit_min": None,
        },
    }


# ══════════════════════════════════════════════════════════════
# Debug-Endpoints
# ══════════════════════════════════════════════════════════════

@router.get("/debug/last-run")
async def debug_last_run():
    """Zeigt den letzten V5 Matching-Lauf mit Ergebnissen."""
    from app.services.v5_matching_service import get_status
    status = get_status()
    return {
        "running": status["running"],
        "last_run": status.get("last_run"),
        "last_run_result": status.get("last_run_result"),
        "current_progress": status.get("progress"),
    }


@router.get("/debug/match-count")
async def debug_match_count(db: AsyncSession = Depends(get_db)):
    """Match-Statistiken (V5 + Legacy)."""
    today = date.today()
    today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    week_start = today_start - timedelta(days=7)

    # By matching_method
    method_query = await db.execute(
        select(Match.matching_method, func.count())
        .group_by(Match.matching_method)
    )
    by_method = {row[0] or "unknown": row[1] for row in method_query.all()}

    # By status
    status_query = await db.execute(
        select(Match.status, func.count())
        .group_by(Match.status)
    )
    by_status = {row[0].value if hasattr(row[0], "value") else str(row[0]): row[1] for row in status_query.all()}

    # Today (V5)
    today_q = await db.execute(
        select(func.count()).where(
            and_(Match.matching_method == "v5_role_geo", Match.created_at >= today_start)
        )
    )
    today_v5 = today_q.scalar() or 0

    # This week (V5)
    week_q = await db.execute(
        select(func.count()).where(
            and_(Match.matching_method == "v5_role_geo", Match.created_at >= week_start)
        )
    )
    week_v5 = week_q.scalar() or 0

    # AI assessed
    ai_q = await db.execute(
        select(func.count()).where(Match.ai_checked_at.isnot(None))
    )
    ai_assessed = ai_q.scalar() or 0

    return {
        "by_method": by_method,
        "by_status": by_status,
        "today_v5": today_v5,
        "this_week_v5": week_v5,
        "ai_assessed_total": ai_assessed,
    }


@router.get("/debug/stufe-0-preview")
async def debug_stufe_0_preview(db: AsyncSession = Depends(get_db)):
    """Zeigt was Stufe 0 liefern WUERDE ohne Claude-Calls (Dry-Run)."""

    # Aktive Kandidaten
    cand_count = await db.execute(
        select(func.count()).where(
            and_(
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.classification_data.isnot(None),
            )
        )
    )
    total_candidates = cand_count.scalar() or 0

    # Kandidaten mit Daten fuer Claude
    cand_with_data = await db.execute(
        select(func.count()).where(
            and_(
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.classification_data.isnot(None),
                (Candidate.work_history.isnot(None)) | (Candidate.cv_text.isnot(None)),
            )
        )
    )
    candidates_with_data = cand_with_data.scalar() or 0

    # Aktive Jobs
    job_count = await db.execute(
        select(func.count()).where(
            and_(
                Job.deleted_at.is_(None),
                Job.quality_score.in_(["high", "medium"]),
                Job.classification_data.isnot(None),
                (Job.expires_at.is_(None)) | (Job.expires_at > func.now()),
            )
        )
    )
    active_jobs = job_count.scalar() or 0

    # Jobs mit job_text
    jobs_with_text = await db.execute(
        select(func.count()).where(
            and_(
                Job.deleted_at.is_(None),
                Job.quality_score.in_(["high", "medium"]),
                Job.job_text.isnot(None),
                func.length(Job.job_text) > 50,
                (Job.expires_at.is_(None)) | (Job.expires_at > func.now()),
            )
        )
    )
    jobs_with_text_count = jobs_with_text.scalar() or 0

    # Existierende Matches (werden uebersprungen)
    existing = await db.execute(select(func.count()).select_from(Match))
    existing_matches = existing.scalar() or 0

    return {
        "total_candidates": total_candidates,
        "candidates_with_data": candidates_with_data,
        "candidates_without_data": total_candidates - candidates_with_data,
        "active_jobs": active_jobs,
        "jobs_with_text": jobs_with_text_count,
        "jobs_without_text": active_jobs - jobs_with_text_count,
        "existing_matches": existing_matches,
        "potential_pairs": total_candidates * active_jobs,
        "note": "Tatsaechliche Paare nach Distanzfilter sind deutlich weniger",
    }


@router.get("/debug/job-health")
async def debug_job_health(db: AsyncSession = Depends(get_db)):
    """Gesundheitscheck der Job-Daten."""
    now = datetime.now(timezone.utc)

    total = await db.execute(
        select(func.count()).where(Job.deleted_at.is_(None))
    )
    total_count = total.scalar() or 0

    # Aktiv (nicht abgelaufen)
    active = await db.execute(
        select(func.count()).where(
            and_(
                Job.deleted_at.is_(None),
                (Job.expires_at.is_(None)) | (Job.expires_at > func.now()),
            )
        )
    )
    active_count = active.scalar() or 0

    # Ohne job_text
    no_text = await db.execute(
        select(func.count()).where(
            and_(
                Job.deleted_at.is_(None),
                (Job.job_text.is_(None)) | (func.length(func.coalesce(Job.job_text, "")) <= 50),
            )
        )
    )
    no_text_count = no_text.scalar() or 0

    # Ohne Koordinaten
    no_coords = await db.execute(
        select(func.count()).where(
            and_(Job.deleted_at.is_(None), Job.location_coords.is_(None))
        )
    )
    no_coords_count = no_coords.scalar() or 0

    # Ohne Classification
    no_class = await db.execute(
        select(func.count()).where(
            and_(Job.deleted_at.is_(None), Job.classification_data.is_(None))
        )
    )
    no_class_count = no_class.scalar() or 0

    # By city (Top 10)
    city_query = await db.execute(
        select(
            func.coalesce(Job.city, Job.work_location_city, "Unbekannt").label("city"),
            func.count().label("cnt"),
        )
        .where(
            and_(
                Job.deleted_at.is_(None),
                (Job.expires_at.is_(None)) | (Job.expires_at > func.now()),
            )
        )
        .group_by("city")
        .order_by(text("cnt DESC"))
        .limit(10)
    )
    by_city = {row[0]: row[1] for row in city_query.all()}

    return {
        "total_jobs": total_count,
        "active_jobs": active_count,
        "expired_jobs": total_count - active_count,
        "no_job_text": no_text_count,
        "no_coordinates": no_coords_count,
        "no_classification": no_class_count,
        "by_city_top10": by_city,
    }


@router.get("/debug/candidate-health")
async def debug_candidate_health(db: AsyncSession = Depends(get_db)):
    """Gesundheitscheck der Kandidaten-Daten."""
    total = await db.execute(
        select(func.count()).where(
            and_(Candidate.deleted_at.is_(None), Candidate.hidden == False)
        )
    )
    total_count = total.scalar() or 0

    # Ohne work_history UND ohne cv_text
    no_data = await db.execute(
        select(func.count()).where(
            and_(
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.work_history.is_(None),
                Candidate.cv_text.is_(None),
            )
        )
    )
    no_data_count = no_data.scalar() or 0

    # Ohne Koordinaten
    no_coords = await db.execute(
        select(func.count()).where(
            and_(
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.address_coords.is_(None),
            )
        )
    )
    no_coords_count = no_coords.scalar() or 0

    # Ohne Classification
    no_class = await db.execute(
        select(func.count()).where(
            and_(
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.classification_data.is_(None),
            )
        )
    )
    no_class_count = no_class.scalar() or 0

    # By city (Top 10)
    city_query = await db.execute(
        select(
            func.coalesce(Candidate.city, "Unbekannt").label("city"),
            func.count().label("cnt"),
        )
        .where(and_(Candidate.deleted_at.is_(None), Candidate.hidden == False))
        .group_by("city")
        .order_by(text("cnt DESC"))
        .limit(10)
    )
    by_city = {row[0]: row[1] for row in city_query.all()}

    # By role (Top 10)
    role_query = await db.execute(
        select(
            func.coalesce(Candidate.hotlist_job_title, "Unklassifiziert").label("role"),
            func.count().label("cnt"),
        )
        .where(and_(Candidate.deleted_at.is_(None), Candidate.hidden == False))
        .group_by("role")
        .order_by(text("cnt DESC"))
        .limit(10)
    )
    by_role = {row[0]: row[1] for row in role_query.all()}

    return {
        "total_candidates": total_count,
        "no_work_data": no_data_count,
        "no_coordinates": no_coords_count,
        "no_classification": no_class_count,
        "by_city_top10": by_city,
        "by_role_top10": by_role,
    }


@router.get("/debug/match/{match_id}")
async def debug_match_detail(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Detail-Ansicht eines einzelnen Matches mit Claude-Input/Output."""
    match = await db.get(Match, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="Match nicht gefunden")

    return {
        "match_id": str(match.id),
        "candidate_id": str(match.candidate_id) if match.candidate_id else None,
        "job_id": str(match.job_id) if match.job_id else None,
        "matching_method": match.matching_method,
        "status": match.status.value if hasattr(match.status, "value") else str(match.status),
        "v2_score": match.v2_score,
        "ai_score": match.ai_score,
        "ai_explanation": match.ai_explanation,
        "ai_strengths": match.ai_strengths,
        "ai_weaknesses": match.ai_weaknesses,
        "empfehlung": match.empfehlung,
        "wow_faktor": match.wow_faktor,
        "wow_grund": match.wow_grund,
        "distance_km": match.distance_km,
        "drive_time_car_min": match.drive_time_car_min,
        "drive_time_transit_min": match.drive_time_transit_min,
        "user_feedback": match.user_feedback,
        "feedback_note": match.feedback_note,
        "v2_score_breakdown": match.v2_score_breakdown,
        "created_at": match.created_at.isoformat() if match.created_at else None,
        "quick_reason": match.quick_reason,
    }


@router.get("/debug/cost-report")
async def debug_cost_report(db: AsyncSession = Depends(get_db)):
    """Kosten-Uebersicht: V5 Matching ist kostenfrei, nur optionale KI kostet."""
    from app.services.v5_matching_service import get_status

    today = date.today()
    today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    week_start = today_start - timedelta(days=7)
    month_start = today_start.replace(day=1)

    status = get_status()

    # V5 Matches (kostenfrei)
    v5_today = await db.execute(
        select(func.count()).where(
            and_(Match.matching_method == "v5_role_geo", Match.created_at >= today_start)
        )
    )
    v5_week = await db.execute(
        select(func.count()).where(
            and_(Match.matching_method == "v5_role_geo", Match.created_at >= week_start)
        )
    )
    v5_month = await db.execute(
        select(func.count()).where(
            and_(Match.matching_method == "v5_role_geo", Match.created_at >= month_start)
        )
    )

    # KI-Assessments (kostenpflichtig)
    ai_today = await db.execute(
        select(func.count()).where(
            and_(Match.ai_checked_at.isnot(None), Match.ai_checked_at >= today_start)
        )
    )
    ai_month = await db.execute(
        select(func.count()).where(
            and_(Match.ai_checked_at.isnot(None), Match.ai_checked_at >= month_start)
        )
    )

    return {
        "v5_matches_today": v5_today.scalar() or 0,
        "v5_matches_week": v5_week.scalar() or 0,
        "v5_matches_month": v5_month.scalar() or 0,
        "v5_matching_cost_usd": 0.00,
        "ai_assessments_today": ai_today.scalar() or 0,
        "ai_assessments_month": ai_month.scalar() or 0,
        "ai_estimated_cost_per_assessment_usd": 0.003,
        "last_run": status.get("last_run"),
        "last_run_result": status.get("last_run_result"),
    }


# ══════════════════════════════════════════════════════════════
# Regional Insights (Phase 6)
# ══════════════════════════════════════════════════════════════

@router.get("/claude-match/regional-insights")
async def get_regional_insights(db: AsyncSession = Depends(get_db)):
    """Regionale Uebersicht: Kandidaten vs. Jobs pro Stadt."""
    # Kandidaten pro Stadt
    cand_query = (
        select(Candidate.city, func.count(Candidate.id))
        .where(
            Candidate.deleted_at.is_(None),
            Candidate.hidden == False,
            Candidate.city.isnot(None),
            Candidate.city != "",
        )
        .group_by(Candidate.city)
    )

    # Aktive Jobs pro Stadt
    job_query = (
        select(Job.city, func.count(Job.id))
        .where(
            Job.deleted_at.is_(None),
            (Job.expires_at.is_(None)) | (Job.expires_at > func.now()),
            Job.city.isnot(None),
            Job.city != "",
        )
        .group_by(Job.city)
    )

    cand_result = await db.execute(cand_query)
    job_result = await db.execute(job_query)

    cand_by_city = {row[0]: row[1] for row in cand_result}
    jobs_by_city = {row[0]: row[1] for row in job_result}

    all_cities = set(cand_by_city.keys()) | set(jobs_by_city.keys())
    regions = []
    for city in sorted(
        all_cities,
        key=lambda c: cand_by_city.get(c, 0) + jobs_by_city.get(c, 0),
        reverse=True,
    ):
        c = cand_by_city.get(city, 0)
        j = jobs_by_city.get(city, 0)
        if c > 5 and j > 3:
            status = "gut_abgedeckt"
        elif c > 0 and j > 0:
            status = "ausbaufaehig"
        elif j > 3 and c == 0:
            status = "sourcing_chance"
        else:
            status = "keine_abdeckung"
        regions.append({
            "city": city,
            "candidate_count": c,
            "job_count": j,
            "status": status,
        })

    return {"regions": regions[:20]}


# ══════════════════════════════════════════════════════════════
# Detailliertes Feedback (Phase 6)
# ══════════════════════════════════════════════════════════════

@router.post("/claude-match/{match_id}/detailed-feedback")
async def submit_detailed_feedback(
    match_id: UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """Detailliertes Feedback fuer Matching-Verbesserung."""
    match = await db.get(Match, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="Match nicht gefunden")

    match.user_feedback = body.get("feedback", "neutral")
    match.feedback_note = body.get("note", "")
    match.rejection_reason = body.get("rejection_reason")
    match.feedback_at = datetime.now(timezone.utc)

    await db.commit()

    # Feedback-Statistiken aggregieren
    stats_query = (
        select(Match.user_feedback, func.count(Match.id))
        .where(
            Match.matching_method.in_(["v5_role_geo", "claude_match"]),
            Match.user_feedback.isnot(None),
        )
        .group_by(Match.user_feedback)
    )
    stats = await db.execute(stats_query)
    feedback_stats = {row[0]: row[1] for row in stats}

    return {"success": True, "feedback_stats": feedback_stats}


# ══════════════════════════════════════════════════════════════
# Klassifizierungs-Pruefseite
# ══════════════════════════════════════════════════════════════

@router.get("/debug/classification-check")
async def classification_check_page(
    entity: str = Query("candidates", description="candidates oder jobs"),
    db: AsyncSession = Depends(get_db),
):
    """HTML-Seite: 100 zufaellige Kandidaten/Jobs mit Werdegang + Klassifizierung nebeneinander."""
    from fastapi.responses import HTMLResponse

    if entity == "jobs":
        query = (
            select(
                Job.id,
                Job.position,
                Job.company_name,
                Job.city,
                Job.job_text,
                Job.job_tasks,
                Job.classification_data,
                Job.quality_score,
            )
            .where(
                Job.deleted_at.is_(None),
                Job.classification_data.isnot(None),
            )
            .order_by(func.random())
            .limit(100)
        )
        rows = await db.execute(query)
        items = rows.all()

        cards_html = ""
        for i, row in enumerate(items, 1):
            cls = row.classification_data or {}
            primary = cls.get("primary_role", "—")
            roles = ", ".join(cls.get("roles", []))
            sub_level = cls.get("sub_level", "—")
            quality = row.quality_score or "—"
            reasoning = cls.get("reasoning", "—")
            tasks = (row.job_tasks or "—")[:500]
            job_text = (row.job_text or "")[:800].replace("<", "&lt;").replace(">", "&gt;")

            cards_html += f"""
            <div style="border:1px solid #ddd;border-radius:12px;margin-bottom:16px;overflow:hidden;">
                <div style="background:#f0f0f0;padding:12px 16px;display:flex;justify-content:space-between;align-items:center;">
                    <span style="font-weight:700;">#{i} — {(row.position or 'Kein Titel')[:60]}</span>
                    <span style="font-size:12px;color:#666;">{row.company_name or ''} | {row.city or ''} | Quality: {quality}</span>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:0;">
                    <div style="padding:16px;border-right:1px solid #eee;">
                        <h4 style="margin:0 0 8px;font-size:13px;color:#888;">JOBBESCHREIBUNG</h4>
                        <div style="font-size:12px;white-space:pre-wrap;max-height:300px;overflow-y:auto;">{job_text}</div>
                        <h4 style="margin:12px 0 4px;font-size:13px;color:#888;">EXTRAHIERTE AUFGABEN</h4>
                        <div style="font-size:12px;color:#333;">{tasks}</div>
                    </div>
                    <div style="padding:16px;background:#f9fafb;">
                        <h4 style="margin:0 0 8px;font-size:13px;color:#888;">KLASSIFIZIERUNG</h4>
                        <div style="margin-bottom:8px;">
                            <span style="background:#3b82f6;color:#fff;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:600;">{primary}</span>
                        </div>
                        <div style="font-size:12px;margin-bottom:4px;"><b>Alle Rollen:</b> {roles or '—'}</div>
                        <div style="font-size:12px;margin-bottom:4px;"><b>Sub-Level:</b> {sub_level}</div>
                        <div style="font-size:12px;margin-bottom:4px;"><b>Begruendung:</b></div>
                        <div style="font-size:11px;color:#555;background:#fff;padding:8px;border-radius:6px;max-height:150px;overflow-y:auto;">{reasoning}</div>
                    </div>
                </div>
            </div>"""

    else:
        query = (
            select(
                Candidate.id,
                Candidate.first_name,
                Candidate.last_name,
                Candidate.current_position,
                Candidate.city,
                Candidate.work_history,
                Candidate.education,
                Candidate.further_education,
                Candidate.skills,
                Candidate.it_skills,
                Candidate.hotlist_job_title,
                Candidate.classification_data,
            )
            .where(
                Candidate.deleted_at.is_(None),
                Candidate.classification_data.isnot(None),
            )
            .order_by(func.random())
            .limit(100)
        )
        rows = await db.execute(query)
        items = rows.all()

        cards_html = ""
        for i, row in enumerate(items, 1):
            cls = row.classification_data or {}
            primary = cls.get("primary_role", "—")
            roles = ", ".join(cls.get("roles", []))
            sub_level = cls.get("sub_level", "—")
            is_leadership = cls.get("is_leadership", False)
            reasoning = cls.get("reasoning", "—")

            # Werdegang aufbereiten
            wh = row.work_history or []
            wh_html = ""
            if isinstance(wh, list):
                for entry in wh[:6]:
                    if isinstance(entry, dict):
                        pos = entry.get("position", "—")
                        comp = entry.get("company", "")
                        period = entry.get("period", entry.get("duration", ""))
                        desc = (entry.get("description", "") or "")[:200]
                        wh_html += f"""<div style="margin-bottom:8px;padding:6px;background:#fff;border-radius:4px;">
                            <div style="font-size:12px;font-weight:600;">{pos}</div>
                            <div style="font-size:11px;color:#666;">{comp} | {period}</div>
                            <div style="font-size:11px;color:#444;margin-top:2px;">{desc}</div>
                        </div>"""
            if not wh_html:
                wh_html = '<div style="font-size:12px;color:#999;">Kein Werdegang vorhanden</div>'

            # Ausbildung
            edu = row.education or []
            edu_html = ""
            if isinstance(edu, list):
                for entry in edu[:3]:
                    if isinstance(entry, dict):
                        degree = entry.get("degree", entry.get("qualification", "—"))
                        inst = entry.get("institution", entry.get("school", ""))
                        edu_html += f'<div style="font-size:11px;">{degree} — {inst}</div>'
            if not edu_html:
                edu_html = '<span style="font-size:11px;color:#999;">—</span>'

            # IT Skills
            it = row.it_skills or []
            it_html = ", ".join(it[:10]) if it else "—"

            # Skills
            sk = row.skills or []
            sk_html = ", ".join(sk[:10]) if sk else "—"

            name = f"{row.first_name or ''} {row.last_name or ''}".strip() or "Unbekannt"
            leadership_badge = ' <span style="background:#f59e0b;color:#fff;padding:2px 8px;border-radius:10px;font-size:10px;">LEADERSHIP</span>' if is_leadership else ""

            cards_html += f"""
            <div style="border:1px solid #ddd;border-radius:12px;margin-bottom:16px;overflow:hidden;">
                <div style="background:#f0f0f0;padding:12px 16px;display:flex;justify-content:space-between;align-items:center;">
                    <span style="font-weight:700;">#{i} — {name}</span>
                    <span style="font-size:12px;color:#666;">{row.current_position or '—'} | {row.city or '—'}</span>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:0;">
                    <div style="padding:16px;border-right:1px solid #eee;max-height:400px;overflow-y:auto;">
                        <h4 style="margin:0 0 8px;font-size:13px;color:#888;">BERUFLICHER WERDEGANG</h4>
                        {wh_html}
                        <h4 style="margin:12px 0 4px;font-size:13px;color:#888;">AUSBILDUNG</h4>
                        {edu_html}
                        <h4 style="margin:12px 0 4px;font-size:13px;color:#888;">IT-SKILLS</h4>
                        <div style="font-size:11px;">{it_html}</div>
                        <h4 style="margin:12px 0 4px;font-size:13px;color:#888;">SKILLS</h4>
                        <div style="font-size:11px;">{sk_html}</div>
                    </div>
                    <div style="padding:16px;background:#f9fafb;">
                        <h4 style="margin:0 0 8px;font-size:13px;color:#888;">KLASSIFIZIERUNG</h4>
                        <div style="margin-bottom:8px;">
                            <span style="background:#3b82f6;color:#fff;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:600;">{primary}</span>
                            {leadership_badge}
                        </div>
                        <div style="font-size:12px;margin-bottom:4px;"><b>Alle Rollen:</b> {roles or '—'}</div>
                        <div style="font-size:12px;margin-bottom:4px;"><b>Sub-Level:</b> {sub_level}</div>
                        <div style="font-size:12px;margin-bottom:4px;"><b>Hotlist-Titel:</b> {row.hotlist_job_title or '—'}</div>
                        <div style="font-size:12px;margin-bottom:8px;"><b>Begruendung:</b></div>
                        <div style="font-size:11px;color:#555;background:#fff;padding:8px;border-radius:6px;max-height:200px;overflow-y:auto;">{reasoning}</div>
                    </div>
                </div>
            </div>"""

    title = "Job-Klassifizierung" if entity == "jobs" else "Kandidaten-Klassifizierung"
    count = len(items)
    toggle_link = "jobs" if entity == "candidates" else "candidates"
    toggle_text = "Jobs pruefen" if entity == "candidates" else "Kandidaten pruefen"

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>{title} — Pruefung</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f5f5f5; padding: 24px; }}
</style>
</head><body>
<div style="max-width:1200px;margin:0 auto;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;">
        <div>
            <h1 style="font-size:22px;">{title} — Pruefung</h1>
            <p style="font-size:13px;color:#666;margin-top:4px;">{count} zufaellige Eintraege. Seite neu laden = neue Stichprobe.</p>
        </div>
        <div style="display:flex;gap:8px;">
            <a href="?entity={toggle_link}" style="padding:8px 16px;background:#3b82f6;color:#fff;border-radius:8px;text-decoration:none;font-size:13px;">{toggle_text}</a>
            <a href="?entity={entity}" style="padding:8px 16px;background:#10b981;color:#fff;border-radius:8px;text-decoration:none;font-size:13px;">Neue Stichprobe</a>
        </div>
    </div>
    {cards_html}
</div>
</body></html>"""

    return HTMLResponse(content=html)
