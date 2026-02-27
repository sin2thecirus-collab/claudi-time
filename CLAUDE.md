# Pulspoint CRM — Matching Tool

> **Stand: 24.02.2026 — v5.0 Claude Code Matching (NEU) + v4.0 Legacy**

---

## NEUES MATCHING-SYSTEM (v5.0 — Claude Code)

**Fuer Matching-Aufgaben: Lies `RECRUITER.md` im Projekt-Root.**

Das neue System ersetzt alle bisherigen Matching-Versionen (V2-V20, Formeln, Scores, ML).
Stattdessen bewertet Claude (Opus 4.6) persoenlich jedes Kandidat-Job-Paar.

**Workflow:**
1. `python claude_match_helper.py --status` → Uebersicht
2. `python claude_match_helper.py --batch` → Unbewertete Paare holen
3. Profile lesen und bewerten
4. `python claude_match_helper.py --save '...'` → In DB schreiben

**Match Center:** `/match-center` (zeigt nur Claude-Code-Matches)

**Regeln:**
- Score < 75% → Match wird NICHT gespeichert, KEINE Fahrzeit — Schrott
- Score >= 75% → Speichern + Google Maps Fahrzeit berechnen
- Luftlinie max 30km (PostGIS Vorfilter)
- Kein taegliches Limit — alle guten Matches auf einmal

**DB-Verbindung:** `postgres://postgres:aG4ddfAgdAbGg3bDBFD12f3GdDGAgcFD@shuttle.proxy.rlwy.net:43640/railway`

---

## PFLICHT VOR JEDER ARBEIT

**Lies IMMER zuerst:** `/Users/miladhamdard/Desktop/IT Projekte/AKTUELLER-STAND.md`

Diese Datei enthaelt:
- Aktuellen Stand aller 13 Phasen (ALLE FERTIG + OPERATIV)
- Milads unantastbare Grundregeln
- Operative Ergebnisse der V2-Pipeline
- Lessons Learned + Gefahren
- Alle drei GPT-Prompts mit Dateipfaden
- Technische Details und Zugangsdaten

**Aktualisiere die Datei SOFORT nach jedem abgeschlossenen Schritt.**

### PFLICHT NACH JEDER KONTEXT-KOMPRIMIERUNG

**Wenn der Chat komprimiert wurde (erkennbar an "continued from a previous conversation" oder fehlender Erinnerung an vorherige Schritte), MUESSEN SOFORT diese Dateien gelesen werden BEVOR irgendeine Arbeit fortgesetzt wird:**

1. `Akquise/MEMORY.md` — Projekt-Status, Architektur-Entscheidungen, Key-Findings
2. `Akquise/PLAN.md` — Aktueller Implementierungsplan (9 Phasen)
3. `Akquise/RECHERCHE.md` — Research + Deep-Dive Ergebnisse
4. `Akquise/REVIEW-TEAM.md` — Review-Team Ergebnisse

**ERST LESEN, DANN ARBEITEN. Keine Ausnahme. Kein "ich erinnere mich noch". Lesen.**

---

## STATUS-UEBERSICHT (20.02.2026)

### Was ist das Matching Tool?
Das MT matcht Finance-Kandidaten (FiBu, BiBu, KrediBu, DebiBu, LohnBu, StFA) automatisch mit Jobangeboten.

### Aktueller Stand — v4.0 Claude Matching DEPLOYED
- **v4.0 Claude Matching** deployed am 20.02.2026:
  - Ersetzt den 7-Komponenten-Algorithmus durch Claude-basiertes Matching
  - 3-Stufen-System: DB-Filter (Stufe 0) → Quick-Check (Stufe 1) → Deep Assessment (Stufe 2)
  - Action Board Dashboard unter `/action-board`
  - Naehe-Matches: Jobs/Kandidaten ohne vollstaendige Daten, <10km, kein Claude-Call
  - CSV-Pipeline gestrafft: stoppt nach Geocoding, Auto-Trigger fuer Claude Matching
  - `expires_at` wird beim Import gesetzt (30 Tage), Duplikate frischen auf
- **Phase 6 Features KOMPLETT:**
  - Regional Insights Endpoint (Kandidaten vs. Jobs pro Stadt)
  - CSV-Import Vorschau mit Stadt-Analyse
  - Auto-Matching nach CSV-Import
  - ATS-Pipeline Integration bei "Vorstellen"
  - Detailliertes Feedback mit Ablehnungsgruenden
  - Action Board mit Regional Insights Section
- **Bugs gefixt:**
  - cv_text Fallback: Kandidaten ohne work_history nutzen jetzt cv_text
  - ai_score * 100 Skala in 5 Templates korrigiert
  - 8 v2 Endpoints als DEPRECATED markiert

### v3.0 Legacy (weiterhin verfuegbar)
- v3.0 Endpoints unter `/api/v2/...` bleiben bestehen (DEPRECATED markiert)
- Match Center zeigt sowohl v3 als auch v4 Matches korrekt an
- Google Maps Fahrzeit weiterhin aktiv

### Was noch offen ist
- Outreach testen (E-Mail + Job-PDF an Test-Kandidaten)
- Morning Briefing mit echten Daten validieren

---

## VERBOTENE AKTIONEN

1. **NIEMALS aus dem Gedaechtnis arbeiten.** Bei Unsicherheit: Datei lesen, nicht raten.
2. **NIEMALS alte Prompts aus vorherigen Sessions verwenden.** Immer den aktuellen Code lesen.
3. **NIEMALS "Position" und "Taetigkeiten" verwechseln.** PRIMARY_ROLE wird NUR durch Taetigkeiten bestimmt.
4. **NIEMALS Code-Snippets zitieren ohne die Datei gelesen zu haben.**
5. **NIEMALS eine Phase als "fertig" markieren ohne den AKTUELLER-STAND.md zu aktualisieren.**
6. **NIEMALS die CSV-Pipeline-Reihenfolge brechen:** Kategorisierung -> Klassifizierung -> Geocoding (v4: stoppt hier)
7. **NIEMALS persoenliche Daten an Claude senden** (Name, Email, Telefon, Adresse, Geburtsdatum) — nur candidate_id + Berufsdaten.
8. **NIEMALS eine DB-Session offen halten waehrend eines API-Calls** (Railway killt nach 30s).
9. **NIEMALS `async_session_factory` schreiben** — der korrekte Name ist `async_session_maker` (aus `app/database.py`).
10. **NIEMALS Background-Task Imports AUSSERHALB von try/except** — Imports muessen innerhalb des try-Blocks liegen, damit Fehler im finally-Block sauber aufgeraeumt werden.
11. **NIEMALS persoenliche Daten an Claude senden** — Nur candidate_id + Berufsdaten. Name, Email, Telefon, Adresse, Geburtsdatum sind TABU.
12. **NIEMALS ORM-Objekte ueber Claude-API-Calls hinweg halten** — Vor dem Claude-Call als Dict extrahieren, DB-Session schliessen.
13. **IMMER ai_score UND v2_score setzen** (Dual-Write) — `v2_score = float(score)`, `ai_score = float(score) / 100.0`
14. **IMMER Claude-JSON validieren** — json.loads in try/except, Score clampen 0-100, Empfehlung validieren.
15. **NIEMALS zweiten Matching-Lauf starten wenn einer laeuft** — `get_status()["running"]` pruefen vor Start.
16. **IMMER prompt_version in v2_score_breakdown speichern** — Fuer Nachvollziehbarkeit welcher Prompt welches Ergebnis erzeugt hat.
17. **NIEMALS Matches mit Score < 75% speichern** — Unter 75% ist Schrott, keine Fahrzeit, nichts.
18. **IMMER Google Maps Fahrzeit fuer Matches >= 75% berechnen** — Jedes gespeicherte Match braucht echte Fahrzeit.
19. **IMMER Luftlinie max 30km als Vorfilter** — 60km Luftlinie kann 100km Fahrweg bedeuten.
20. **NUR Kandidaten mit classification_data matchen** — Ohne Klassifizierung kein Matching. Nicht-Finance-Leute werden so automatisch ignoriert.

---

## DER PLAN

Der komplette 13-Phasen-Plan steht in:
- `/Users/miladhamdard/.claude/plans/dynamic-seeking-storm.md` (1081 Zeilen)
- Backup: `/Users/miladhamdard/Desktop/IT Projekte/Matching-Verbesserung 13-Phasen-Plan (Stand 16.02.2026).md`

---

## V2-PIPELINE — WIE SIE FUNKTIONIERT (Stand 16.02.2026)

### Die Kette (REIHENFOLGE IST VERBINDLICH)

```
1. Klassifizierung (GPT-4o-mini)
   -> Bestimmt PRIMARY_ROLE, sub_level, quality_score
   -> Jobs: POST /api/jobs/maintenance/reclassify-finance
   -> Kandidaten: POST /api/candidates/maintenance/reclassify-finance?force=true

2. Profiling (GPT-4o-mini)
   -> Extrahiert v2_seniority_level, v2_structured_skills, v2_career_trajectory etc.
   -> INPUT HAENGT AB VON KLASSIFIZIERUNG (nutzt hotlist_job_titles)
   -> POST /api/v2/profiles/backfill?entity_type=candidates&force_reprofile=true
   -> POST /api/v2/profiles/backfill?entity_type=jobs&force_reprofile=true

3. Embeddings (OpenAI text-embedding-3-small)
   -> Nutzt v2_current_role_summary + v2_structured_skills vom Profiling
   -> ACHTUNG: Generiert NUR fuer Eintraege wo v2_embedding IS NULL
   -> Wenn nach Re-Profiling: ZUERST Reset -> POST /api/v2/embeddings/reset?entity_type=all
   -> DANN generieren -> POST /api/v2/embeddings/generate?entity_type=all

4. Matching (Python Scoring)
   -> 7 Scoring-Komponenten, TOP_N=50, MIN_SCORE=25.0
   -> POST /api/v2/match/batch?unmatched_only=false

5. Google Maps Fahrzeit (NACH Matching, NUR fuer Score >= Threshold)
   -> Threshold konfigurierbar via /einstellungen (Default: 70)
   -> Wird automatisch bei jedem neuen Match berechnet (wenn Score >= Threshold)
   -> Backfill fuer bestehende Matches: POST /api/v2/drive-times/backfill
```

### KRITISCHE ABHAENGIGKEITEN

```
Klassifizierung aendert -> hotlist_job_titles (z.B. "Bilanzbuchhalter" -> "Finanzbuchhalter")
                       |
Profiling nutzt hotlist_job_titles als Input -> MUSS neu laufen
                       |
Profiling aendert -> v2_current_role_summary, v2_structured_skills
                       |
Embeddings basieren auf Profiling-Output -> MUESSEN neu generiert werden
                       |
ACHTUNG: Embedding-Generator filtert auf v2_embedding IS NULL
         -> ZUERST Reset (POST /api/v2/embeddings/reset), DANN generieren!
                       |
Matching basiert auf Embeddings + classification_data -> MUSS neu laufen
                       |
Google Maps Fahrzeit basiert auf Match-Score -> Berechnet NACH Matching
         -> Threshold aus system_settings Tabelle (key: drive_time_score_threshold)
         -> Aendert man den Slider, betrifft das NUR ZUKUENFTIGE Matches
         -> Bestehende Matches mit Fahrzeit werden NIEMALS ueberschrieben
```

### Status-Endpoints (Live-Fortschritt)

| Was | Status-Endpoint |
|-----|----------------|
| Klassifizierung Jobs | `GET /api/jobs/maintenance/classification-status` |
| Klassifizierung Kandidaten | `GET /api/candidates/maintenance/classification-status` |
| Profiling | `GET /api/v2/profiles/backfill/status` |
| Embeddings | `GET /api/v2/embeddings/status` |
| Matching | `GET /api/v2/match/batch/status` |
| Drive Time Backfill | `GET /api/v2/drive-times/backfill/status` |
| Drive Time Debug | `GET /api/v2/drive-times/debug` |

---

## V4 CLAUDE MATCHING — ARCHITEKTUR (Stand 20.02.2026)

### Wie es funktioniert

```
CSV-Import → Kategorisierung → Klassifizierung → Geocoding → STOP
                                                                |
Action Board klickt "Matching starten" → POST /api/v4/claude-match/run
                                                                |
Stufe 0: Harte DB-Filter (kostenlos)
  → PostGIS: Kandidat <40km zum Job
  → Rollen-Match: classification_data.primary_role stimmt ueberein
  → Quality Gate: Job quality_score IN (high, medium)
  → Duplikate: Kein bestehender Match fuer dieses Paar
  → expires_at: Job nicht abgelaufen (expires_at > NOW() oder NULL)
  → Daten-Qualitaet: Kandidat hat work_history, Job hat job_text
  → Separate Naehe-Matches: <10km, unvollstaendige Daten, KEIN Claude-Call
                                                                |
Stufe 1: Claude Haiku Quick-Check (~$0.001 pro Paar)
  → 1 Satz: "Passt dieser Kandidat zu diesem Job? JA/NEIN"
  → Filtert ~60-70% raus
                                                                |
Stufe 2: Claude Haiku Deep Assessment (~$0.003 pro Paar)
  → Score (0-100), Staerken, Luecken, Empfehlung, WOW-Faktor
  → Empfehlung: vorstellen / beobachten / nicht_passend
  → Matches mit Score < 40 werden NICHT gespeichert
                                                                |
Google Maps Fahrzeit (fuer Top-Matches mit Score >= Threshold)
                                                                |
Action Board zeigt Ergebnisse → Recruiter klickt Aktionen
```

### Privacy-Regeln (UNANTASTBAR)
- **NIEMALS** an Claude senden: Name, Vorname, Email, Telefon, Adresse, PLZ, Geburtsdatum
- **NUR** senden: candidate_id (UUID) + Berufsdaten (work_history, skills, education, Jobtitel)
- Implementiert in: `claude_matching_service.py` → `_extract_candidate_data()`

### API-Endpoints (Prefix: /api/v4)

| Methode | Pfad | Was |
|---------|------|-----|
| POST | `/claude-match/run` | Matching starten (Background-Task) |
| GET | `/claude-match/status` | Live-Fortschritt (Polling) |
| GET | `/claude-match/daily` | Heutige Ergebnisse (Top, WOW, Follow-ups, Naehe) |
| POST | `/claude-match/{id}/action` | Aktion: vorstellen/spaeter/ablehnen |
| POST | `/claude-match/candidate/{id}` | Ad-hoc Matching fuer einzelnen Kandidaten |
| GET | `/claude-match/regional-insights` | Kandidaten vs. Jobs pro Stadt (Top 20) |
| POST | `/claude-match/{id}/detailed-feedback` | Detailliertes Feedback mit Ablehnungsgrund |
| GET | `/debug/match-count` | Statistiken |
| GET | `/debug/stufe-0-preview` | Dry-Run: wieviele Paare wuerden gematcht? |
| GET | `/debug/job-health` | Job-Datenqualitaet |
| GET | `/debug/candidate-health` | Kandidaten-Datenqualitaet |
| GET | `/debug/match/{id}` | Einzelnes Match Detail |
| GET | `/debug/cost-report` | Token/Kosten-Uebersicht |

### Action Board
- **URL:** `/action-board` (navigierbar via Top-Nav + Bottom-Switcher)
- **Technologie:** Alpine.js + Jinja2
- **Sektionen:** Top Matches, Goldene Chancen (WOW), Follow-ups, Naehe-Matches
- **Live-Fortschritt:** 2-Sekunden-Polling waehrend Matching-Lauf

### Empfehlung-Logik
- `vorstellen` → Match wird dem Kunden praesentiert
- `spaeter` → Match verschwindet, erscheint naechsten Tag als Follow-up
- `ablehnen` → Match wird geloescht (DELETE, nicht REJECTED-Status)

### Neue DB-Felder (Migration 023)
- `empfehlung` (String(20)) — vorstellen/beobachten/nicht_passend
- `wow_faktor` (Boolean, default false) — Aussergewoehnliches Match
- `wow_grund` (Text) — Warum WOW

### Neue Dateien
| Datei | Was |
|-------|-----|
| `app/services/claude_matching_service.py` | Kern-Service: Stufe 0 + 1 + 2, Concurrent-Lock, Cost-Tracking |
| `app/api/routes_claude_matching.py` | API-Endpoints + Debug-Endpoints |
| `app/templates/action_board.html` | Action Board Frontend |
| `migrations/versions/023_add_claude_matching_fields.py` | DB-Migration |

### CSV-Pipeline (v4 — GEKUERZT + AUTO-MATCHING)
```
VORHER: Kategorisierung → Klassifizierung → Geocoding → Profiling → Embedding → Matching
JETZT:  Kategorisierung → Klassifizierung → Geocoding → Auto-Trigger Claude Matching
```
- Profiling, Embedding werden NICHT mehr ausgefuehrt
- Claude Matching wird automatisch nach Geocoding getriggert (wenn kein Lauf aktiv)
- Kann auch manuell ueber Action Board gestartet werden
- Vorschau-Endpoint: `POST /api/jobs/import-preview` (Stadt-Analyse vor Import)
- `expires_at` wird beim Import gesetzt: `now + 30 Tage`
- Bei Duplikat-Import: `expires_at` + `last_updated_at` werden aufgefrischt

### Kosten-Schaetzung
- Claude Haiku: $0.80/1M Input, $4.00/1M Output
- Stufe 1 (Quick-Check): ~$0.001 pro Paar
- Stufe 2 (Deep Assessment): ~$0.003 pro Paar
- Bei 2000 Paaren: Stufe 0 filtert auf ~500, Stufe 1 auf ~150, Stufe 2: ~$0.45
- Taeglich: ~$0.50-1.00

### Match Center Kompatibilitaet
- `_effective_score()` in `match_center_service.py` erkennt `claude_match` Method
- Compare-Template zeigt Claude-Breakdown (Staerken/Luecken/Empfehlung) statt 7-Dimensionen-Balken
- Scoring-Version: `v4_claude` in `v2_score_breakdown`

---

## V2-PIPELINE — LEGACY (Stand 16.02.2026, weiterhin verfuegbar)

> **HINWEIS:** Die V2-Pipeline wird von v4 nicht mehr automatisch ausgefuehrt.
> Die Endpoints bleiben aber bestehen fuer manuelle Nutzung.

---

## GEFAHREN + LESSONS LEARNED (20.02.2026)

### 1. Railway idle-in-transaction Timeout (30 Sekunden) — KRITISCHSTE GEFAHR
**Problem:** Railway PostgreSQL killt DB-Connections die > 30s idle sind in einer Transaktion. Wenn ein API-Call (OpenAI, Google Maps) mehrere Sekunden dauert und die DB-Session offen bleibt -> Connection wird gekillt -> alle nachfolgenden DB-Operationen in dieser Session scheitern.

**Loesung:** Pro Entity / pro API-Call eine EIGENE DB-Session oeffnen, den API-Call machen, committen, Session schliessen. NIEMALS eine Session ueber mehrere API-Calls offen halten.

**Muster fuer Background-Tasks mit externen API-Calls:**
```python
from app.database import async_session_maker  # NICHT async_session_factory!

# Schritt 1: Daten laden (eigene Session)
async with async_session_maker() as db:
    data = await db.execute(select(...))
    items = data.all()  # Als reine Dicts/Tuples speichern, NICHT als ORM-Objekte!
# Session hier geschlossen!

# Schritt 2: Pro Item eigene Session -> API-Call -> eigene Session
for item in items:
    async with async_session_maker() as db2:
        # Daten fuer API-Call laden
        coords = await db2.execute(select(...))
    # Session geschlossen BEVOR API-Call!

    # API-Call (KEINE DB-Session offen!)
    result = await external_api_call(...)

    # Ergebnis schreiben (neue Session)
    async with async_session_maker() as db3:
        await db3.execute(update(...).values(...))
        await db3.commit()
    # Session geschlossen!
```

**ACHTUNG:** Der Import heisst `async_session_maker` (NICHT `async_session_factory`). `async_session_factory` existiert NICHT in `app/database.py`. Dieser Fehler hat den Drive Time Backfill zum Absturz gebracht (ImportError ausserhalb try/except -> `running: True` fuer immer).

### 2. Background-Task Imports MUESSEN im try-Block sein
**Problem:** Wenn ein `from app.xyz import abc` in einer async Background-Funktion AUSSERHALB des try/except/finally-Blocks steht und der Import fehlschlaegt, wird der finally-Block NICHT ausgefuehrt. Der Status bleibt dann auf `running: True` haengen.

**Loesung:** Alle Imports innerhalb des try-Blocks platzieren:
```python
async def _run_background_task():
    status = _status_dict
    try:
        from app.database import async_session_maker  # <-- IM try-Block!
        from app.models.match import Match
        # ... Logik ...
    except Exception as e:
        status["errors"] += 1
    finally:
        status["running"] = False  # Wird IMMER ausgefuehrt
```

### 3. SQLAlchemy ORM kann JSONB-Felder nicht zuverlaessig auf NULL setzen
**Problem:** `update(Candidate).values(v2_embedding_current=None)` gibt rowcount > 0 zurueck, aber die Felder sind danach NICHT NULL in der DB.

**Loesung:** Raw SQL verwenden:
```python
await db.execute(text("UPDATE candidates SET v2_embedding_current = NULL WHERE ..."))
await db.commit()
```

### 4. Embedding-Generator filtert auf IS NULL
**Problem:** `generate_candidate_embeddings()` filtert: `WHERE v2_embedding_current IS NULL AND v2_profile_created_at IS NOT NULL`. Wenn Embeddings bereits existieren, werden 0 Entities gefunden.

**Loesung:** VOR dem Generieren einen Reset machen:
```
POST /api/v2/embeddings/reset?entity_type=all
-> DANN: POST /api/v2/embeddings/generate?entity_type=all
```

### 5. Verschiedene Embedding-Feldnamen auf den Models
**ACHTUNG:** Die Feld-Namen sind UNTERSCHIEDLICH:
- **Kandidaten:** `v2_embedding_current` (JSONB) — in `candidate.py`
- **Jobs:** `v2_embedding` (JobJSONB) — in `job.py` (NICHT `v2_embedding_current`!)

### 6. OpenAI Guthaben / Rate Limits
- OpenAI meldet 429 bei BEIDEN Faellen: kein Guthaben UND Rate-Limit
- Unterscheidung: `response.body.error.code` — `"insufficient_quota"` vs `"rate_limit_exceeded"`
- Debug: `GET /api/jobs/debug/openai-test` zeigt Raw-Response
- Auto-Recharge ist AUS -> bei $0.00 Balance geben ALLE KI-Features 429

### 7. Router-Prefix fuer V2-Endpoints
- **KORREKT:** `/api/v2/profiles/backfill`, `/api/v2/embeddings/generate` etc.
- **FALSCH:** `/api/matching/v2/...` (gibt 404)
- Grund: In `main.py:390` steht `app.include_router(matching_v2_router, prefix="/api/v2")`

### 8. Parallelisierung — Semaphore(3) ist der Sweet Spot
- Semaphore(1) = sequentiell = zu langsam
- Semaphore(3) = 3x schneller = kein Rate-Limit-Problem
- Semaphore(5+) = OpenAI 429 Rate-Limit Gefahr bei Bulk-Runs
- 1786 Kandidaten profilen: ~80 Min mit Semaphore(3)

### 9. Google Maps API Key — Achtung bei Zeichen
- **Key liegt in Railway Environment Variables** (`GOOGLE_MAPS_API_KEY`) — NIEMALS in Code/Docs!
- Achtung: kleines `l` vs grosses `I` — immer per Copy-Paste aus Google Cloud Console
- Der Key wird beim App-Start einmalig gelesen (Singleton `DistanceMatrixService`)
- Wenn der Key in Railway geaendert wird, MUSS ein Redeploy getriggert werden!
- Railway redeployed NICHT immer automatisch bei Variable-Aenderung -> leeren Commit pushen:
  `git commit --allow-empty -m "Trigger redeploy" && git push`

### 10. PostGIS Koordinaten-Abfragen
- Die Felder `location_coords` (Job) und `address_coords` (Candidate) sind PostGIS `Geography(POINT, 4326)`
- Zum Extrahieren von lat/lng IMMER: `func.ST_Y(func.ST_GeomFromWKB(Job.location_coords))` fuer lat
- IMMER Column-References verwenden (z.B. `Job.location_coords`), NICHT ORM-Objekt-Attribute (z.B. `job.location_coords`)
- ORM-Objekt-Attribute geben den Python-Wert (WKB Bytes) zurueck, nicht die SQL-Column

### 11. Drive Time Score Threshold — SystemSettings
- Tabelle: `system_settings` (Migration 022)
- Key: `drive_time_score_threshold`, Default: `70`
- Konfigurierbar via `/einstellungen` (Slider)
- Gelesen in: `matching_engine_v2.py` (Phase 10) + `routes_matching_v2.py` (Backfill)
- Helper: `app/api/routes_settings.py` -> `get_drive_time_threshold(db)`
- Aenderung betrifft NUR zukuenftige Matches — bestehende Fahrzeiten werden NIE ueberschrieben

---

## SCORING-GEWICHTUNGEN (DEPLOYED — v3.0, 17.02.2026)

```python
# Datei: app/services/matching_engine_v2.py, Zeile 100-112
DEFAULT_WEIGHTS = {
    "skill_overlap": 15.0,   # Simulation: Korrelation -41.1, optimal 3-15
    "seniority_fit": 25.0,   # Level-Matching
    "job_title_fit": 0.0,    # RAUS — Titel sind zu oft falsch
    "embedding_sim": 21.0,   # Zuverlaessigstes Signal
    "industry_fit": 12.0,    # Branche
    "career_fit": 12.0,      # Karriere-Richtung
    "software_match": 15.0,  # DATEV/SAP — wichtig fuer Lohn/StFA
}
# Summe = 100
# Threshold: skill_overlap < 0.20 → cap 60
# BiBu/FiBu Multiplier: 1.20, KrediBu/DebiBu: 1.15 (bonus-only)
```

---

## GOOGLE MAPS FAHRZEIT — ARCHITEKTUR (Stand 16.02.2026)

### Wie es funktioniert
1. **Bei jedem neuen Match** (in `matching_engine_v2.py`, Phase 10):
   - NACH dem Scoring wird geprueft: Score >= Threshold UND Job hat Koordinaten?
   - Wenn ja: `distance_matrix_service.batch_drive_times()` aufrufen
   - Google Maps Distance Matrix API berechnet Auto + OEPNV Fahrzeit
   - Ergebnis wird auf `Match.drive_time_car_min` + `Match.drive_time_transit_min` gespeichert
   - PLZ-basierter Cache verhindert doppelte API-Calls (gleiche PLZ-Paare)

2. **Backfill fuer bestehende Matches:**
   - `POST /api/v2/drive-times/backfill` -> Background-Task
   - Liest Threshold aus `system_settings` Tabelle
   - Verarbeitet Matches gruppiert nach Job (1 Google Maps API-Call pro Job)
   - **Eigene DB-Session pro Job** (Railway 30s Timeout!)
   - `GET /api/v2/drive-times/backfill/status` -> Live-Fortschritt

3. **Frontend-Anzeige:**
   - 7 Templates zeigen Fahrzeit: `match_center_table.html`, `match_center_candidates.html`, `match_center_group.html`, `match_center_compare.html`, `match_card.html`, `match_result_row.html`, `candidate_matching_jobs.html`
   - Farbcodierung: <=20min gruen, <=40min amber, >40min grau
   - Conditional: `{% if drive_time_car_min is not none %}` -> zeigt nichts wenn keine Fahrzeit

### Wichtige Dateien
| Datei | Was |
|-------|-----|
| `app/services/distance_matrix_service.py` | Singleton-Service, PLZ-Cache, Google Maps API |
| `app/services/matching_engine_v2.py` Zeile 1669-1740 | Phase 10: Fahrzeit-Berechnung nach Scoring |
| `app/api/routes_matching_v2.py` Zeile 2121-2350 | Backfill-Endpoint + Background-Task |
| `app/api/routes_settings.py` | SystemSettings CRUD + Threshold-Helper |
| `app/models/settings.py` | SystemSetting Model (key-value) |
| `app/config.py` Zeile 142 | `google_maps_api_key` aus ENV |

### Kosten
- Google Maps Distance Matrix API: ~$5 pro 1000 Elements
- PLZ-Cache spart ~70-80% der API-Calls bei Bulk-Operationen
- 661 Matches Backfill: ~$0.50-1.00

---

## FUENF PROMPTS — UEBERSICHT (NICHT VERWECHSELN)

### 1. FINANCE_CLASSIFIER_SYSTEM_PROMPT (Kandidaten-Klassifizierung)
- **Datei:** `app/services/finance_classifier_service.py`, Zeile 44-262
- **Zweck:** Kandidaten in 6 Rollen einteilen anhand des GESAMTEN Werdegangs
- **Input:** work_history, education, skills (gesamter Werdegang)
- **Output:** `{is_leadership, roles[], primary_role, sub_level, reasoning}`

### 2. FINANCE_JOB_CLASSIFIER_PROMPT (Job-Klassifizierung)
- **Datei:** `app/services/finance_classifier_service.py`, Zeile 204-304
- **Zweck:** Jobs klassifizieren + Quality Gate (high/medium/low)
- **Input:** Jobtitel + Beschreibung + Aufgaben
- **Output:** `{is_leadership, roles[], primary_role, sub_level, quality_score, quality_reason, ...}`

### 3. CANDIDATE_PROFILE_PROMPT (Kandidaten-Profiling)
- **Datei:** `app/services/profile_engine_service.py`, Zeile 32-226
- **Zweck:** Level (2-6), Skills, Career Trajectory, Industries extrahieren
- **Input:** Strukturierte DB-Felder (work_history, education, skills, hotlist_job_titles)
- **Output:** `{v2_seniority_level, v2_years_experience, v2_structured_skills, v2_career_trajectory, ...}`

### 4. QUICK_CHECK (Claude v4 — Stufe 1)
- **Datei:** `app/services/claude_matching_service.py` — `QUICK_CHECK_SYSTEM` + `QUICK_CHECK_USER`
- **Zweck:** Schnelle JA/NEIN Entscheidung ob Kandidat grundsaetzlich zum Job passt
- **Model:** Claude Haiku
- **Input:** candidate_id + Berufsdaten, Job-Daten (KEINE persoenlichen Daten!)
- **Output:** `{"passt": true/false, "grund": "1 Satz"}`

### 5. DEEP_ASSESSMENT (Claude v4 — Stufe 2)
- **Datei:** `app/services/claude_matching_service.py` — `DEEP_ASSESSMENT_SYSTEM` + `DEEP_ASSESSMENT_USER`
- **Zweck:** Detaillierte Bewertung mit Score, Staerken, Luecken, Empfehlung
- **Model:** Claude Haiku (oder Sonnet fuer hoehere Qualitaet)
- **Input:** candidate_id + Berufsdaten, Job-Daten (KEINE persoenlichen Daten!)
- **Output:** `{score: 0-100, zusammenfassung, staerken[], luecken[], empfehlung, wow_faktor, wow_grund, begruendung}`

---

## TECHNISCHE DETAILS

### Zugangsdaten
- **App URL:** `https://claudi-time-production-46a5.up.railway.app`
- **API-Key Header:** Liegt in Railway Environment Variables (`API_KEY`) — NIEMALS in Code/Docs!
- **n8n URL:** `https://n8n-production-aa9c.up.railway.app`
- **n8n Workflow ID:** `MefdpqcLUZadNJ0Y`
- **Google Maps API Key:** Liegt in Railway Environment Variables (`GOOGLE_MAPS_API_KEY`)
- **Anthropic API Key:** Liegt in Railway Environment Variables (`ANTHROPIC_API_KEY`)
- **GitHub:** `sin2thecirus-collab/claudi-time`, Branch: main
- **WICHTIG:** Alle API-Keys/Secrets gehoeren NUR in Railway ENV oder lokale `.env` — NIEMALS in committed Files!

### Codebase-Pfad
- **Lokal:** `/Users/miladhamdard/Desktop/Claudi Time/matching-tool/`
- **Alle Services:** `app/services/`
- **Alle Routes:** `app/api/`
- **Models:** `app/models/`
- **Templates:** `app/templates/`
- **Configs:** `app/config/`
- **Migrations:** `migrations/versions/`

### Wichtige Dateien
| Datei | Was |
|-------|-----|
| `app/services/claude_matching_service.py` | **V4 Claude Matching (Stufe 0 + 1 + 2)** |
| `app/api/routes_claude_matching.py` | **V4 API-Endpoints + Debug** |
| `app/templates/action_board.html` | **V4 Action Board Dashboard** |
| `app/services/finance_classifier_service.py` | V2 Klassifizierung (Kandidaten + Jobs) |
| `app/services/profile_engine_service.py` | V2 Profiling (GPT-4o-mini) |
| `app/services/matching_engine_v2.py` | V2/V3 Matching + Scoring + Embeddings + Phase 10 Fahrzeit |
| `app/services/distance_matrix_service.py` | Google Maps Fahrzeit (Singleton, PLZ-Cache) |
| `app/services/job_description_pdf_service.py` | Job-PDF fuer Kandidaten-Ansprache |
| `app/services/outreach_service.py` | E-Mail + PDF Versand |
| `app/api/routes_matching_v2.py` | V2 Pipeline-Endpoints (Profiling, Embeddings, Matching, Drive Time) |
| `app/api/routes_settings.py` | System-Einstellungen CRUD (Drive Time Threshold) |
| `app/api/routes_jobs.py` | Job-Endpoints + CSV-Pipeline (v4: stoppt nach Geocoding) |
| `app/api/routes_candidates.py` | Kandidaten-Endpoints + Klassifizierung |
| `app/api/routes_matches.py` | Match-Endpoints + Outreach |
| `app/api/routes_briefing.py` | Morning Briefing Endpoints |
| `app/models/settings.py` | SystemSetting Model (key-value Tabelle) |
| `app/config/skill_weights.json` | Skill-Gewichtungen (6 Rollen) |
| `app/config/skill_hierarchy.json` | Skill-Hierarchie (FiBu, BiBu, LohnBu, StFA) |

### Migrations (chronologisch)
- **019** — `classification_data` (JSONB) + `quality_score` (VARCHAR) auf Jobs
- **020** — `drive_time_car_min` + `drive_time_transit_min` auf Matches
- **021** — 6 Outreach-Felder auf Matches (outreach_status, outreach_sent_at etc.)
- **022** — `system_settings` Tabelle (key-value) mit Default `drive_time_score_threshold = 70`
- **023** — `empfehlung` (String), `wow_faktor` (Boolean), `wow_grund` (Text) auf Matches (Claude v4)

### Wichtige Commits (Session 16.02.2026)
- `fa6eb28` — Phase 1-10: Deep Classification + Quality Gate + Google Maps
- `9fbd230` — Phase 11-13: End-to-End Recruiting Pipeline
- `6306709` — Phase 10 Optimierung: Google Maps nur Score >= 70, Fahrzeit in allen Templates
- `bd5a755` — Fix: Deploy-Crash durch @dataclass Feldreihenfolge
- `77fa71d` — Settings-Slider: Drive Time Score Threshold dynamisch konfigurierbar
- `35d92cd` — Fix: Drive Time Backfill eigene DB-Session pro Job (Railway 30s Timeout)
- `483594a` — Fix: Import async_session_maker statt async_session_factory
