# Projekt-Kontext: Matching-Tool für Recruiter

**Erstellt:** 26. Januar 2026
**Aktualisiert:** 26. Januar 2026
**Status:** Phase 6 abgeschlossen

---

## 1. Was ist das Projekt?

Ein automatisiertes Matching-System für Recruiter, das:
- Kandidaten aus Recruit CRM synchronisiert
- Stellenausschreibungen per CSV importiert
- Automatisch passende Kandidaten zu Jobs findet (Distanz ≤25km + fachliche Passung)
- KI-gestützte Bewertung auf Knopfdruck ermöglicht

**Ziel:** Der Recruiter soll nicht mehr manuell vergleichen, sondern nur noch entscheiden.

---

## 2. Technologie-Stack

| Komponente | Technologie |
|------------|-------------|
| Backend | Python 3.11 + FastAPI |
| Datenbank | PostgreSQL 15 + PostGIS |
| Frontend | Jinja2 + HTMX + Tailwind CSS |
| KI | OpenAI API (gpt-4o-mini) |
| Deployment | Railway |

---

## 3. Spezifikations-Dokumente

Alle Spezifikationen liegen im Downloads-Ordner:

| Datei | Inhalt |
|-------|--------|
| `matching_tool_v2_2_final.md` | Hauptspezifikation (Datenbank, API, UI, Services) |
| `8585858585_v2_1_ergaenzungen.md` | CRUD, Statistiken, Alerts |
| `v2_2_ergaenzung_admin_trigger.md` | Background-Jobs mit Cron + manuellen Buttons |
| `implementierungsplan_matching_tool.md` | Detaillierter Implementierungsplan (9 Phasen) |

---

## 4. Kernfunktionen

### 4.1 Datenquellen
- **Kandidaten:** Recruit CRM API (5.000 bestehend, 10-15 neue/Tag)
- **Jobs:** CSV-Import (Tab-getrennt, ~30.000/Woche)

### 4.2 Matching-Logik
- **Hartes Kriterium:** Distanz ≤25km (Haustür-zu-Haustür)
- **Vorfilter:** Keyword-Matching (Skills im Job-Text)
- **KI-Bewertung:** On-Demand, nur wenn User Kandidaten auswählt

### 4.3 Kandidaten-Daten (aus CV extrahiert)
- Vorname, Nachname
- Aktuelle Position
- Alter (aus Geburtsdatum berechnet)
- IT-Kenntnisse (SAP, DATEV, etc.)
- Beruflicher Werdegang (alle Stationen)
- Ausbildung/Abschlüsse
- Wohnadresse (vollständig)

### 4.4 Branchen
- Buchhaltung
- Technische Berufe (Elektriker, Elektrotechnik, Anlagenmechaniker)

---

## 5. Wichtige Entscheidungen

| Entscheidung | Begründung |
|--------------|------------|
| **Kein Redis** | Einzelnutzer, nicht nötig |
| **Kein React/Vue** | HTMX reicht, weniger Komplexität |
| **On-Demand KI** | Batch wäre zu teuer (~$700 vs ~$5/Monat) |
| **Polling statt Webhooks** | Einfacher zu debuggen |
| **PostgreSQL + PostGIS** | Geo-Queries für Distanzberechnung |
| **OpenStreetMap/Nominatim** | Kostenlos für Geokodierung |

---

## 6. Background-Jobs

| Job | Cron (automatisch) | Manuell |
|-----|-------------------|---------|
| Geocoding | 03:00 Uhr täglich | Button in Einstellungen |
| CRM-Sync | 03:30 Uhr täglich | Button in Einstellungen |
| Matching (Vorfilter) | 04:00 Uhr täglich | Button in Einstellungen |
| Cleanup | 05:00 Uhr sonntags | Button in Einstellungen |

---

## 7. Was wurde bisher gemacht?

### Erledigt (Planung)
- [x] Anforderungen gesammelt und dokumentiert
- [x] Recruit CRM API recherchiert
- [x] CSV-Format analysiert
- [x] Technologie-Stack festgelegt
- [x] Datenbank-Schema entworfen
- [x] API-Endpoints spezifiziert
- [x] UI-Mockups erstellt
- [x] Error-Handling definiert
- [x] Implementierungsplan erstellt

### Implementierung
- [x] Phase 1: Projekt-Setup & Datenbank ✅ (26. Jan 2026)
- [x] Phase 2: CSV-Import & Geocoding ✅ (26. Jan 2026)
- [x] Phase 3: CRM-Sync & CV-Parsing ✅ (26. Jan 2026)
- [x] Phase 4: Matching-Logik ✅ (26. Jan 2026)
- [x] Phase 5: API-Endpoints ✅ (26. Jan 2026)
- [x] Phase 6: Frontend ✅ (26. Jan 2026)
- [ ] Phase 7: KI-Integration
- [ ] Phase 8: Statistiken & Alerts
- [ ] Phase 9: Testing & Deployment

---

## 8. Projektstruktur (geplant)

```
matching-tool/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── models/
│   ├── schemas/
│   ├── services/
│   ├── api/
│   └── templates/
├── migrations/
├── tests/
├── docs/
│   └── PROJEKT_KONTEXT.md  ← Diese Datei
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── README.md
```

---

## 9. Credentials (benötigt)

Diese müssen in `.env` eingetragen werden:

```
DATABASE_URL=postgresql+asyncpg://...
OPENAI_API_KEY=sk-...
RECRUIT_CRM_API_KEY=...
RECRUIT_CRM_BASE_URL=https://api.recruitcrm.io/v1/
SECRET_KEY=<random-32-chars>
CRON_SECRET=<random-string>
ENVIRONMENT=development
```

**Hinweis:** API-Keys werden NICHT im Chat geteilt, sondern direkt in die .env eingetragen.

---

## 10. Phase 2: Was wurde implementiert

### Pydantic Schemas (`app/schemas/`)
- `pagination.py` - PaginationParams, PaginatedResponse
- `errors.py` - ErrorCode (Enum), ErrorResponse, ValidationErrorDetail
- `validators.py` - PostalCode, CityName, SearchTerm, BatchDeleteRequest
- `job.py` - JobCreate, JobUpdate, JobResponse, JobImportRow, ImportJobResponse
- `candidate.py` - CandidateCreate, CandidateResponse, CandidateWithMatch, CVParseResult
- `match.py` - MatchResponse, AICheckRequest, AICheckResponse
- `filters.py` - JobFilterParams, CandidateFilterParams, FilterPresetResponse

### Error-Handling (`app/api/`)
- `exception_handlers.py` - AppException, NotFoundException, ConflictException, etc.
- `rate_limiter.py` - InMemoryRateLimiter mit RateLimitTier (STANDARD, WRITE, AI, IMPORT, ADMIN)

### Services (`app/services/`)
- `csv_validator.py` - CSVValidator mit Encoding-Erkennung, Header-/Zeilen-Validierung
- `csv_import_service.py` - CSVImportService mit Duplikaterkennung (content_hash)
- `geocoding_service.py` - GeocodingService mit Nominatim, Rate-Limiting, Caching
- `job_service.py` - JobService mit CRUD, Soft-Delete, Prio-Städte-Sortierung

---

## 11. Phase 3: Was wurde implementiert

### CRM-Integration (`app/services/`)
- `crm_client.py` - RecruitCRMClient mit:
  - Authentifizierung (Bearer Token)
  - Rate-Limiting (60 Requests/Minute)
  - Retry bei Timeouts
  - Kandidaten-Abruf (paginiert + einzeln)
  - Adress-Parsing
  - Fehlerbehandlung (CRMError, CRMRateLimitError, CRMAuthenticationError)

- `crm_sync_service.py` - CRMSyncService mit:
  - Initial-Sync (alle Kandidaten)
  - Incremental-Sync (nur geänderte seit letztem Sync)
  - Einzelner Kandidat-Sync
  - Automatische Erkennung des Sync-Modus
  - SyncResult mit Statistiken

### CV-Parsing (`app/services/`)
- `cv_parser_service.py` - CVParserService mit:
  - PDF-Download von URL
  - Text-Extraktion mit PyMuPDF
  - OpenAI-basiertes Parsing (gpt-4o-mini)
  - Strukturierte Extraktion: Name, Adresse, Skills, Berufserfahrung, Ausbildung
  - Geburtsdatum-Parsing (verschiedene Formate)
  - Fehler-Fallback

### Kandidaten-Service (`app/services/`)
- `candidate_service.py` - CandidateService mit:
  - CRUD-Operationen
  - Filterung (Name, Stadt, Skills, Position)
  - Pagination
  - Hide/Unhide (einzeln + Batch)
  - Kandidaten für Job mit Match-Daten

### OpenAI-Service (`app/services/`)
- `openai_service.py` - OpenAIService mit:
  - Match-Bewertung (Kandidat-Job-Passung)
  - Kosten-Tracking (Token-Verbrauch)
  - Retry bei Timeouts
  - Fallback bei Fehlern (Keyword-basiert)
  - Kosten-Schätzung

---

## 12. Phase 4: Was wurde implementiert

### Keyword-Matching (`app/services/`)
- `keyword_matcher.py` - KeywordMatcher mit:
  - Branchen-spezifische Keyword-Listen (Buchhaltung + Technische Berufe)
  - Software-Keywords (SAP, DATEV, Lexware, etc.)
  - Tätigkeits-Keywords (Buchhaltung, Finanzbuchhaltung, etc.)
  - Qualifikations-Keywords (Bilanzbuchhalter, Elektriker, etc.)
  - Technische Keywords (SPS, Elektrotechnik, SHK, etc.)
  - `extract_keywords_from_text()` - Extrahiert relevante Keywords aus Text
  - `find_matching_keywords()` - Findet übereinstimmende Skills im Job-Text
  - `calculate_score()` - Berechnet Score (0.0-1.0)
  - `match()` - Vollständiges Keyword-Matching
  - `extract_job_requirements()` - Kategorisierte Extraktion (Software, Tasks, Qualifications, Technical)

### Matching-Service (`app/services/`)
- `matching_service.py` - MatchingService mit:
  - `calculate_matches_for_job()` - Findet Kandidaten im 25km Radius mit PostGIS
  - `calculate_matches_for_candidate()` - Findet Jobs für einen Kandidaten
  - `recalculate_all_matches()` - Batch-Matching für alle Jobs (Cron)
  - `get_matches_for_job()` - Matches mit Filter + Pagination
  - `update_match_status()` - Status ändern (NEW, AI_CHECKED, PRESENTED, REJECTED, PLACED)
  - `mark_as_placed()` - Als vermittelt markieren mit Notizen
  - `delete_match()` / `batch_delete_matches()` - Löschen
  - `get_excellent_matches()` - Findet Top-Matches (≤5km + ≥3 Keywords)
  - `cleanup_orphaned_matches()` - Entfernt verwaiste Matches
  - `get_match_statistics()` - Statistiken pro Job

### Datenstrukturen
- `KeywordMatchResult` - Ergebnis eines Keyword-Matchings
- `MatchingResult` - Ergebnis einer Job-Matching-Operation
- `BatchMatchingResult` - Ergebnis einer Batch-Operation

### PostGIS-Integration
- `ST_DWithin()` - Radius-Filter (25km)
- `ST_Distance()` - Exakte Distanzberechnung in Metern
- Spheroid-basierte Berechnung für Genauigkeit

---

## 13. Phase 5: Was wurde implementiert

### Filter Service (`app/services/`)
- `filter_service.py` - FilterService mit:
  - `get_available_cities()` - Alle Städte aus Jobs + Kandidaten
  - `get_available_skills()` - Alle Skills aus Kandidaten
  - `get_available_industries()` - Alle Branchen aus Jobs
  - `get_available_employment_types()` - Alle Beschäftigungsarten
  - `get_priority_cities()` / `add_priority_city()` / `remove_priority_city()` - Prio-Städte-Verwaltung
  - `get_filter_presets()` / `create_filter_preset()` / `delete_filter_preset()` - Filter-Presets
  - `set_default_preset()` - Standard-Preset festlegen

### Job Runner Service (`app/services/`)
- `job_runner_service.py` - JobRunnerService mit:
  - `is_running()` - Prüft ob Job-Typ läuft
  - `start_job()` - Startet einen neuen Background-Job
  - `update_progress()` - Aktualisiert Fortschritt
  - `complete_job()` - Markiert Job als abgeschlossen
  - `fail_job()` - Markiert Job als fehlgeschlagen
  - `get_latest_run()` - Letzter Run eines Job-Typs
  - `get_running_jobs()` - Alle laufenden Jobs
  - `get_job_history()` - Job-Historie

### API Routes (`app/api/`)

#### Jobs API (`routes_jobs.py`)
- `POST /api/jobs/import` - CSV-Import mit Background-Verarbeitung
- `GET /api/jobs` - Jobs auflisten mit Filtern und Pagination
- `GET /api/jobs/{job_id}` - Job-Details
- `PATCH /api/jobs/{job_id}` - Job aktualisieren
- `DELETE /api/jobs/{job_id}` - Job löschen (Soft-Delete)
- `DELETE /api/jobs/batch` - Mehrere Jobs löschen (max 100)
- `GET /api/jobs/{job_id}/candidates` - Kandidaten für Job mit Match-Daten

#### Candidates API (`routes_candidates.py`)
- `POST /api/candidates/sync` - CRM-Sync starten
- `GET /api/candidates` - Kandidaten auflisten mit Filtern
- `GET /api/candidates/{id}` - Kandidaten-Details
- `PATCH /api/candidates/{id}` - Kandidat aktualisieren
- `PUT /api/candidates/{id}/hide` - Kandidat ausblenden
- `PUT /api/candidates/{id}/unhide` - Kandidat wieder einblenden
- `PUT /api/candidates/batch/hide` - Mehrere ausblenden (max 100)
- `PUT /api/candidates/batch/unhide` - Mehrere wieder einblenden
- `POST /api/candidates/{id}/parse-cv` - CV neu parsen
- `GET /api/candidates/{id}/jobs` - Passende Jobs für Kandidat

#### Matches API (`routes_matches.py`)
- `POST /api/matches/ai-check` - KI-Bewertung (max 50 Kandidaten)
- `GET /api/matches/ai-check/estimate` - Kosten-Schätzung
- `GET /api/matches/job/{job_id}` - Matches für einen Job
- `GET /api/matches/{match_id}` - Match-Details
- `PUT /api/matches/{id}/status` - Status ändern
- `PUT /api/matches/{id}/placed` - Als vermittelt markieren
- `DELETE /api/matches/{id}` - Match löschen
- `DELETE /api/matches/batch` - Mehrere löschen
- `GET /api/matches/job/{id}/statistics` - Statistiken
- `GET /api/matches/excellent` - Top-Matches

#### Filters API (`routes_filters.py`)
- `GET /api/filters/options` - Alle Filter-Optionen (Dropdowns)
- `GET /api/filters/cities` - Verfügbare Städte
- `GET /api/filters/skills` - Verfügbare Skills
- `GET /api/filters/industries` - Verfügbare Branchen
- `GET /api/filters/employment-types` - Beschäftigungsarten
- `GET /api/filters/presets` - Filter-Presets
- `GET /api/filters/presets/{id}` - Preset-Details
- `POST /api/filters/presets` - Preset erstellen
- `DELETE /api/filters/presets/{id}` - Preset löschen
- `PUT /api/filters/presets/{id}/default` - Als Standard setzen

#### Settings API (`routes_settings.py`)
- `GET /api/settings/priority-cities` - Prio-Städte auflisten
- `POST /api/settings/priority-cities` - Prio-Stadt hinzufügen
- `PUT /api/settings/priority-cities` - Alle Prio-Städte aktualisieren
- `DELETE /api/settings/priority-cities/{id}` - Prio-Stadt entfernen
- `GET /api/settings/limits` - System-Limits anzeigen

#### Admin API (`routes_admin.py`)
- `POST /api/admin/geocoding/trigger` - Geocoding starten
- `GET /api/admin/geocoding/status` - Geocoding-Status
- `POST /api/admin/crm-sync/trigger` - CRM-Sync starten
- `GET /api/admin/crm-sync/status` - CRM-Sync-Status
- `POST /api/admin/matching/trigger` - Matching starten
- `GET /api/admin/matching/status` - Matching-Status
- `POST /api/admin/cleanup/trigger` - Cleanup starten
- `GET /api/admin/cleanup/status` - Cleanup-Status
- `GET /api/admin/jobs/history` - Job-Historie
- `GET /api/admin/status` - Übersicht aller Jobs

### Rate Limiting
- STANDARD: 100/Min - Lese-Operationen
- WRITE: 30/Min - Schreib-Operationen
- AI: 10/Min - KI-Aufrufe
- IMPORT: 5/Min - Importe
- ADMIN: 20/Min - Admin-Operationen

---

## 14. Phase 6: Was wurde implementiert

### Base Template (`app/templates/base.html`)
- Grundlegendes Layout mit Navigation
- Tailwind CSS via CDN
- HTMX via CDN für partielle Updates
- Alpine.js für leichte Interaktionen
- Toast-Container für Benachrichtigungen
- Modal-Container für Dialoge
- Globale JavaScript-Funktionen

### UI-Komponenten (`app/templates/components/`)
| Komponente | Beschreibung |
|------------|--------------|
| `loading_spinner.html` | Ladeanimation |
| `progress_bar.html` | Fortschrittsbalken für lange Prozesse |
| `skeleton_list.html` | Platzhalter während Listen laden |
| `empty_state.html` | Leere-Daten-Anzeige |
| `toast.html` | Erfolgs-/Fehler-Benachrichtigungen |
| `error_banner.html` | Fehleranzeige in Bereichen |
| `undo_toast.html` | Toast mit Rückgängig-Option |
| `delete_dialog.html` | Lösch-Bestätigungsdialog |
| `alert_banner.html` | Wichtige Hinweise oben auf Seiten |
| `pagination.html` | Seitennavigation |
| `filter_panel.html` | Ausklappbares Filter-UI |
| `job_card.html` | Job-Element in Listen |
| `candidate_row.html` | Kandidaten-Element mit Match-Daten |
| `match_card.html` | KI-Bewertungsergebnis |
| `admin_job_row.html` | Background-Job-Status |
| `import_dialog.html` | CSV-Import-Modal |
| `import_progress.html` | Import-Fortschrittsanzeige |
| `health_indicator.html` | System-Status in Navigation |

### Hauptseiten (`app/templates/`)
| Seite | Route | Beschreibung |
|-------|-------|--------------|
| `dashboard.html` | `/` | Job-Liste mit Filtern, Statistiken, Import |
| `job_detail.html` | `/jobs/{job_id}` | Job-Details mit Kandidatenliste + KI-Check |
| `candidate_detail.html` | `/kandidaten/{id}` | Kandidaten-Profil mit Skills und Jobs |
| `statistics.html` | `/statistiken` | Statistik-Übersicht |
| `settings.html` | `/einstellungen` | Manuelle Aktionen + Prio-Städte |

### Partials für HTMX (`app/templates/partials/`)
| Partial | Beschreibung |
|---------|--------------|
| `job_list.html` | Job-Liste gruppiert nach Prio-Städten |
| `candidate_list.html` | Kandidaten für einen Job |
| `statistics_content.html` | Statistik-Karten |
| `priority_cities.html` | Prio-Städte-Verwaltung |
| `filter_presets.html` | Filter-Presets-Verwaltung |

### Page Routes (`app/api/routes_pages.py`)
- Dashboard: `GET /`
- Job-Detail: `GET /jobs/{job_id}`
- Kandidaten-Detail: `GET /kandidaten/{candidate_id}`
- Statistiken: `GET /statistiken`
- Einstellungen: `GET /einstellungen`
- HTMX-Partials: `/partials/*`

### Features
- **HTMX-Integration:** Partielle Seitenaktualisierungen ohne vollständiges Neuladen
- **Alpine.js:** Leichte Interaktionen (Dropdowns, Toggles)
- **Responsive Design:** Tailwind CSS Mobile-First
- **Deutsche UI:** Alle Texte auf Deutsch
- **Prio-Städte-Sortierung:** Hamburg/München immer oben
- **Batch-Aktionen:** Mehrfachauswahl für Jobs/Kandidaten

---

## 15. Nächster Schritt

**Phase 7: KI-Integration**

Neuen Chat starten mit:
```
cd "/Users/miladhamdard/Desktop/Claudi Time/matching-tool"
claude
```

Dann eingeben:
```
Wir bauen das Matching-Tool für Recruiter.
Phase 6 ist fertig. Starte mit Phase 7: KI-Integration.

Lies zuerst diese Dateien:
1. /Users/miladhamdard/Desktop/Claudi Time/matching-tool/docs/PROJEKT_KONTEXT.md
2. /Users/miladhamdard/Downloads/implementierungsplan_matching_tool.md

Dann schau dir die existierende Projektstruktur an.

WICHTIG: Am Ende der Phase einen Git-Commit machen!
```

---

## 16. Regeln für die Implementierung

### MUSS beachten
1. Jeder API-Endpoint MUSS Error-Handling haben
2. Jede Benutzereingabe MUSS validiert werden
3. Jede Liste MUSS Pagination haben
4. OpenAI wird NUR aufgerufen wenn User klickt
5. Distanz-Filter ist IMMER ≤25km
6. Hamburg und München sind IMMER oben (Prio-Städte)
7. Alle Texte auf DEUTSCH

### NICHT machen
1. KEIN Redis
2. KEIN React/Vue
3. KEINE automatischen OpenAI-Calls
4. KEINE unbegrenzten Listen

---

## 17. Git-Workflow

### Nach jeder größeren Änderung:
```
Bitte mache einen Git-Commit mit aussagekräftiger Message
```

### Commit-Message Format:
```
<typ>: <kurze Beschreibung>

<optionale längere Beschreibung>
```

### Typen:
- `feat:` Neue Funktion
- `fix:` Bugfix
- `docs:` Dokumentation
- `refactor:` Code-Umstrukturierung
- `test:` Tests
- `chore:` Wartung (Dependencies, Config)

### Beispiele:
```
feat: CSV-Import Service implementiert
fix: Geocoding Timeout erhöht auf 10 Sekunden
docs: API-Dokumentation aktualisiert
chore: Dependencies in pyproject.toml hinzugefügt
```

---

## 18. Kontakt-Info

- **CRM:** Recruit CRM
- **Deployment:** Railway (Account vorhanden)
- **Nutzer:** Einzelnutzer (Recruiter)
