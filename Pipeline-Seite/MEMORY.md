# Pipeline-Seite — Memory

## Stand: 17.03.2026

### Was ist die Pipeline?
Die Pipeline-Seite (`/ats`) ist das Kanban-Board des ATS-Systems in Pulspoint CRM. Sie zeigt laufende Recruiting-Prozesse: Welcher Kandidat befindet sich bei welchem Unternehmen/Job in welcher Phase?

### Aktuelle Architektur
- **URL:** `/ats` (routes_ats_pages.py)
- **Template:** `ats_pipeline_overview.html` (1005 Zeilen, komplett neu — Pipeline Command Center)
- **Service:** `ats_pipeline_service.py` (add_candidate, move_stage, reorder, bulk_move, remove_candidate)
- **Model:** `ATSPipelineEntry` (ats_pipeline.py) — Unique Constraint: (ats_job_id, candidate_id)
- **API-Prefix:** `/ats/pipeline/` (routes_ats_pipeline.py)
- **Frontend:** Alpine.js + HTML5 Drag&Drop API (SortableJS entfernt)
- **Neuer Endpoint:** GET /ats/pipeline/search-candidates (Kandidaten-Suche fuer Quick-Add)

### Pipeline-Phasen (PipelineStage Enum)
1. MATCHED → "Gematcht" (blue)
2. SENT → "Vorgestellt" (indigo)
3. FEEDBACK → "Feedback" (amber)
4. INTERVIEW_1 → "Interview 1" (purple)
5. INTERVIEW_2 → "Interview 2" (purple)
6. INTERVIEW_3 → "Interview 3" (purple)
7. OFFER → "Angebot" (emerald)
8. PLACED → "Besetzt" (green)
9. REJECTED → "Abgelehnt" (red) — separate Sektion

### Wie Kandidaten in die Pipeline kommen
1. **Match Center → "Vorstellen":** POST /api/v4/claude-match/{id}/action mit action="vorstellen"
   - Sucht ATSJob mit source_job_id == match.job_id
   - Erstellt ATSPipelineEntry mit stage MATCHED
2. **ATS Job Detail:** POST /ats/pipeline/{job_id}/candidates
3. **Akquise → ATS Konvertierung:** convert_to_ats() erstellt ATSJob mit in_pipeline=true

### Kritische Abhaengigkeiten (NICHT BRECHEN!)
- **ATSJob.in_pipeline** Boolean: Nur Jobs mit in_pipeline=true erscheinen im Kanban
- **ATSJob.source_job_id** FK: Verknuepft ATSJob mit Job (fuer Match → Pipeline)
- **Unique Constraint** (ats_job_id, candidate_id): Verhindert Duplikate
- **n8n Webhook** POST /api/n8n/pipeline/move: Automatische Stage-Moves
- **Drive-Time Daten** kommen aus Match-Records (drive_time_car_min, drive_time_transit_min)
- **ATSActivity** Timeline: Jede Aktion loggt eine Activity (STAGE_CHANGED, CANDIDATE_ADDED, etc.)
- **ClientPresentation** verknuepft Match → Pipeline Entry → Correspondence

### API-Endpoints (NICHT AENDERN!)
| Methode | Pfad | Zweck |
|---------|------|-------|
| GET | /ats/pipeline/{job_id} | Pipeline-Daten laden |
| POST | /ats/pipeline/{job_id}/candidates | Kandidat hinzufuegen |
| PATCH | /ats/pipeline/entries/{id}/stage | Stage wechseln (Drag&Drop) |
| PATCH | /ats/pipeline/entries/{id}/reorder | Sortierung aendern |
| DELETE | /ats/pipeline/entries/{id} | Kandidat entfernen |
| POST | /ats/pipeline/entries/bulk-move | Bulk Stage-Move |

### Geloeste Probleme (17.03.2026 — Redesign)
1. ~~Kein Firmenname sichtbar~~ → Jetzt auf jeder Karte + Sidebar
2. ~~Keine Moeglichkeit, Kandidaten hinzuzufuegen~~ → "Kandidat hinzufuegen" Modal mit Live-Suche
3. ~~Karten zeigen zu wenig Info~~ → Score-Ring, Gehalt, Fahrzeit, ERP, Standort Tags
4. ~~Keine Cross-Job-Uebersicht~~ → "Alle Prozesse" View (activePosition = null)
5. ~~Keine Quick Actions~~ → Kandidat + Kunde Popup-Menues auf jeder Karte
6. ~~Keine erweiterten Filter~~ → Sidebar-Suche + Job-Filter + Attention-Badges
7. ~~Conversion Funnel nimmt zu viel Platz~~ → Kompakte KPI-Bar (4 Metriken)

### Verbleibende offene Punkte
- Match.presentation_status und Pipeline.stage koennen divergieren (kein Sync)
- Activity-Feed Endpoint (letzte N Aktivitaeten) noch nicht implementiert
- Cross-Job Kandidat-View (alle Eintraege eines Kandidaten) noch nicht implementiert

### Gefahren bei Redesign
- Backend-Endpoints MUESSEN stabil bleiben (n8n Webhooks haengen dran)
- PipelineStage Enum NICHT aendern ohne Migration
- ATSPipelineEntry unique constraint beachten
- Drive-Time Daten kommen aus Match, nicht aus Pipeline
- ClientPresentation hat pipeline_entry_id FK
