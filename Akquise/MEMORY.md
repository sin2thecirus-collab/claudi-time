# Akquise-Automatisierung — Memory (Kontexterhaltung)

> **Letzte Aktualisierung: 01.03.2026 (Session 2: 7 Gap-Fixes + 3 n8n-Workflows + Migration 033)**
> Diese Datei wird nach JEDER erledigten Aufgabe aktualisiert.

---

## Projekt-Status

| Phase | Status | Beschreibung |
|-------|--------|--------------|
| Recherche | FERTIG | 6 Research + 6 Review + 6 Deep-Dive + 1 Vertriebsingenieur + 3 Marketing abgeschlossen (22 Agenten total) |
| Planung | FERTIG | Plan in PLAN.md (9 Phasen, nach Vertriebs-Review aktualisiert) |
| Migration 032 | DEPLOYED | Manuell auf Railway ausgefuehrt (alembic_version war '033', korrigiert auf '031', dann SQL manuell). Alle Tabellen, Spalten, Indizes bestaetigt. |
| SQLAlchemy Models | FERTIG | job.py, company.py, company_contact.py erweitert + acquisition_call.py, acquisition_email.py neu |
| Akquise-Job-Guard | FERTIG | `acquisition_source IS NULL` in v5_matching_service.py (Z.438) + matching_engine_v2.py (Z.2245, Z.2398) |
| Backend-Services | FERTIG + 6 BUGFIXES | AcquisitionImportService (6 Bugs gefixt, siehe unten), CallService, EmailService, QualifyService |
| API-Endpoints | FERTIG | routes_acquisition.py (19 Endpoints), routes_acquisition_pages.py (4 HTMX-Routes) |
| Auth-Whitelist | FERTIG | `/api/akquise/unsubscribe/` als PUBLIC_PREFIX in auth.py |
| Frontend Hauptseite | FERTIG | akquise_page.html (Dark-Theme, Tabs, KPIs, CSV-Import-Modal, E-Mail-Modal, Call-Screen-Overlay, Rueckruf-Popup) |
| Frontend Tab-Partials | FERTIG | 6 Tab-Templates (heute, neu, wiedervorlagen, nicht_erreicht, qualifiziert, archiv) + company_group + pagination |
| Frontend Call-Screen | FERTIG | 3-Spalten-Layout (Stellentext, Qualifizierung/Kontakt, Aktionen) mit Timer, Textbausteinen, Auto-Advance |
| Frontend Disposition | FERTIG + 2 BUGFIXES | 13 Dispositionen — Scoping-Bug bei D7/D9 gefixt (Follow-up-Datum ging als null), D12 jetzt mit Inline-Formular |
| Frontend E-Mail-Modal | FERTIG | GPT-Generierung, Mailbox-Dropdown (5 Postfaecher), Betreff+Text editierbar, Senden |
| Frontend Formulare | FERTIG + FELDNAMEN-FIX | Neuer-AP-Formular (D13), Neue-Stelle-Formular (D12) — extra_data Feldnamen an Backend angepasst |
| Frontend Rueckruf-Popup | FERTIG + MANUELL-TRIGGER | Popup-Template + manueller Telefonnummer-Lookup im Header (SSE fuer Auto-Erkennung noch offen) |
| Frontend autoSaveNotes | FERTIG | Notizen werden in localStorage gespeichert, bei Disposition an Server gesendet, nach Erfolg geloescht |
| Navigation | FERTIG | /akquise Link in Desktop + Mobile Nav (base.html) |
| DNS-Check | FERTIG | SPF/DKIM/DMARC fuer alle 3 Domains bestaetigt (sincirus.com, sincirus-karriere.de, jobs-sincirus.com) |
| Git Push | FERTIG | Commits: 8221165 (Phase 1-5), 7882872 (IONOS SMTP), 92ca795 (Migration-Fix), bc7204f (n8n Endpoints), 06b9632 (DB-Session-Fix) + 6 Bugfix-Commits |
| IONOS SMTP Client | FERTIG | ionos_smtp_client.py (aiosmtplib, STARTTLS 587), Routing in acquisition_email_service.py |
| Mailbox-Config | FERTIG | config.py: ionos_smtp_password ENV, Email-Routing: IONOS-Domains→SMTP, sincirus.com→Graph |
| Test-CSV | FERTIG | app/testdata/akquise_test_data.csv (10 Firmen, 29 Spalten, Duplikat/Blacklist/Quali-Szenarien) |
| Railway ENV | FERTIG | IONOS_SMTP_PASSWORD auf Claudi-Time Service gesetzt |
| CSV-Import | DEPLOYED + GETESTET | Erster echter Import erfolgreich (advertsdata.com CSV). 6 Bugs gefixt (siehe unten). |
| /akquise Seite | LIVE | Migration 032 deployed, /akquise Seite funktioniert auf Railway, Leads sichtbar |
| SSE Event-Bus | FERTIG (lokal) | acquisition_event_bus.py + SSE-Endpoint + Webhook + Frontend EventSource |
| n8n-Workflows | AKTIV | 6 Workflows erstellt + AKTIVIERT: Morgen-Briefing (BqjDn57PWwVHDpNy), Abend-Report (VazlvT2vuvqLXX9i), Wiedervorlagen-Alarm (CUIhgzyqDpuOQGZq), Follow-up-Erinnerung (nXsYnVBWy9q0Vh7J), Eskalation (YhLwBZewTiKu7qEU), Reply&Bounce Monitor (DIq76xuoQ0QqQMxJ) |
| n8n-Backend-Endpoints | DEPLOYED | 5 Endpoints + DB-Session-Fix (check-inbox 3-Phasen): /n8n/followup-due, /n8n/eskalation-due, /n8n/check-inbox, /n8n/process-reply, /n8n/process-bounce |

---

## Frontend-Audit + Bugfixes (28.02.2026 spaet)

### Systematischer Audit (Backend 100%, Frontend 85%)
- Backend: Alle 4 Services, 19 REST-Endpoints, 4 HTMX-Routes — vollstaendig, keine Stubs, keine TODOs
- Frontend: 5 Probleme gefunden und 4 davon gefixt

### Bugfix 1: Disposition Scoping (KRITISCH)
- **Problem:** D7/D9 Wiedervorlage-Datum ging als `null` an den Server — verschachtelter `x-data` Scope in disposition_buttons.html war isoliert vom `callScreen()` Scope
- **Fix:** `showFollowUp` + `pendingDisposition` in callScreen() verschoben, verschachtelten x-data entfernt, `x-model` direkt auf callScreen-Properties
- **Dateien:** disposition_buttons.html, call_screen.html

### Bugfix 2: D12 Button + Feldnamen
- **Problem:** D12 rief direkt `submitDisposition()` statt Formular anzuzeigen. Beide Formulare (Inline + Standalone) hatten falsche `extra_data` Feldnamen (`new_position` statt `position`)
- **Fix:** D12 zeigt jetzt Inline-Formular in disposition_buttons.html. Feldnamen in beiden Formularen auf Backend-Erwartung (`position`, `employment_type`, `notes`) korrigiert. Backend um employment_type + job_text erweitert.
- **Dateien:** disposition_buttons.html, call_screen.html, new_job_draft_form.html, acquisition_call_service.py

### Bugfix 3: autoSaveNotes() war leer
- **Problem:** Notizen gingen verloren beim Schliessen des Call-Screens
- **Fix:** localStorage-basiertes Autosave (Debounce 2s), Load beim Init, Clear nach erfolgreicher Disposition
- **Dateien:** call_screen.html

### Bugfix 4: Rueckruf-Popup Trigger
- **Problem:** Popup-Template existierte aber wurde nie angezeigt (kein Trigger)
- **Fix:** Telefonnummer-Suchfeld im Header, ruft `/api/akquise/rueckruf/{phone}` auf, zeigt Popup mit Firma/AP/Jobs
- **Dateien:** akquise_page.html

### Phase 7.3: SSE-Endpoint (28.02.2026 spaet)
- **Event-Bus:** `app/services/acquisition_event_bus.py` — In-Memory Pub-Sub mit asyncio.Queue
- **SSE-Stream:** `GET /akquise/events` in routes_acquisition_pages.py — Heartbeat 30s, Auto-Cleanup
- **Webhook:** `POST /api/akquise/events/incoming-call` in routes_acquisition.py — n8n/Webex ruft auf, macht Phone-Lookup, pusht an alle SSE-Clients
- **Frontend:** EventSource in `akquisePage().init()`, `_handleIncomingCall()` zeigt Rueckruf-Popup mit pulsierendem Punkt
- **Refactoring:** `_renderRueckrufPopup()` — gemeinsame Methode fuer manuellen Lookup und SSE-Event (DOM statt innerHTML)

### Phase 6: n8n-Workflows (28.02.2026) — KOMPLETT + AKTIV
- **6 komplett neue Workflows**, unabhaengig von bestehenden n8n-Automationen
- Alle 6 Workflows **AKTIVIERT** am 28.02.2026 — Backend-Endpoints getestet (alle 200 OK), Telegram getestet
- **DB-Session-Fix:** check-inbox Endpoint refactored (3-Phasen: Token→Graph→DB), Commit 06b9632
- **Commits:** bc7204f (5 Endpoints + Doku), 06b9632 (DB-Session-Fix)
- **5 neue Backend-Endpoints** in routes_acquisition.py fuer n8n:
  - `GET /api/akquise/n8n/followup-due` — Follow-up/Break-up faellige Leads
  - `GET /api/akquise/n8n/eskalation-due?apply=true` — Eskalierte Leads + Auto-Update
  - `POST /api/akquise/n8n/check-inbox?minutes=15` — Inbox-Check via Graph API
  - `POST /api/akquise/n8n/process-reply` — Reply manuell verarbeiten
  - `POST /api/akquise/n8n/process-bounce` — Bounce manuell verarbeiten

| # | Workflow | n8n ID | Zeitplan | Endpoint |
|---|----------|--------|----------|----------|
| 1 | Morgen-Briefing | BqjDn57PWwVHDpNy | 08:00 | wiedervorlagen + stats |
| 2 | Abend-Report | VazlvT2vuvqLXX9i | 19:00 | stats |
| 3 | Wiedervorlagen-Alarm | CUIhgzyqDpuOQGZq | 10/12/14/16 | wiedervorlagen |
| 4 | Follow-up-Erinnerung | nXsYnVBWy9q0Vh7J | 09:00 | n8n/followup-due |
| 5 | Eskalation | YhLwBZewTiKu7qEU | 18:00 | n8n/eskalation-due?apply=true |
| 6 | Reply & Bounce Monitor | DIq76xuoQ0QqQMxJ | */15 | n8n/check-inbox |

### Bug-Analyse: Rundmail-Fixes (28.02.2026)
Drei Bugs aus der Rundmail-Automatisierung gegen Akquise-Workflows geprueft:
1. **DB Pool Exhaustion** — Bereits gefixt fuer check-inbox (06b9632). Andere Endpoints sind reine DB-Ops.
2. **Parallele Pfade** — Morgen-Briefing hat parallele GETs, aber kein shared Resource (kein Callback).
3. **IF-Node typeValidation** — Wiedervorlagen-Alarm IF-Node ist sicher (`skip` immer explizit gesetzt).
**Ergebnis:** Kein Handlungsbedarf.

### Security-Fixes (01.03.2026)
1. **Signatur korrigiert:** `EMAIL_SIGNATURE` in acquisition_email_service.py — echte Kontaktdaten, kein "GmbH", konsistent mit bestehenden Signaturen
2. **Tages-Limit Backend-Sperre:** `_check_daily_limit()` in acquisition_email_service.py — 20/Tag IONOS, 100/Tag M365, prueft VOR Versand (nicht nur Frontend)

### Phase 9: E2E-Testmodus (01.03.2026) — FERTIG
- **Test-Helper:** `app/services/acquisition_test_helpers.py` — `is_test_mode()`, `get_test_email()`, `override_email_if_test()`
- **E-Mail-Umleitung:** In `send_email()` — Test-Modus leitet alle E-Mails an Test-Adresse um, fuegt [TEST] Prefix zum Betreff
- **Simulations-Endpoints:** 3 neue Endpoints in routes_acquisition.py:
  - `POST /api/akquise/test/simulate-call/{job_id}` — 8 Szenarien (nicht_erreicht, besetzt, sekretariat, kein_bedarf, interesse, qualifiziert, falsche_nummer, nie_wieder)
  - `POST /api/akquise/test/simulate-callback?phone=...` — Simuliert Rueckruf mit Phone-Lookup + SSE-Event
  - `GET /api/akquise/test/status` — Aktueller Test-Modus-Status
- **Frontend akquise_page.html:** Gelber Test-Modus-Banner + Rueckruf-Simulationsbutton + `testMode` Alpine-Property
- **Frontend call_screen.html:** tel:-Links deaktiviert (durchgestrichen), Simulations-Dropdown mit 8 Szenarien statt echtem Anruf-Button, `simulateCall()` Methode
- **Konfiguration:** `system_settings` Keys `acquisition_test_mode` + `acquisition_test_email`

### Domain-Warmup (01.03.2026) — ERLEDIGT
- Alle 3 Domains (sincirus.com, sincirus-karriere.de, jobs-sincirus.com) sind warm gelaufen

### Backend-Test (01.03.2026) — BESTANDEN (API + E-Mail)
- **Dependency-Fix:** `aiosmtplib>=3.0.0` fehlte im Root `pyproject.toml` (Commit 8cbb66d)
- **Signatur-Fix:** Telefonnummer korrigiert auf +49 40 238 345 320 (Commit 373a320)
- **IONOS_SMTP_PASSWORD:** Auf Railway Claudi-Time Service gesetzt (endet auf ...5765)
- **system_settings:** `acquisition_test_mode=true`, `acquisition_test_email=hamdard@sincirus.com` angelegt
- **Test-CSV Import:** 9 neue Leads importiert, 1 Duplikat korrekt erkannt (batch_id: bf81ee3b)
- **API-Endpoints getestet (alle OK):**
  - `GET /api/akquise/stats` → 358 offene Leads, KPIs korrekt
  - `GET /api/akquise/leads?status=neu` → Firmengruppeierung funktioniert
  - `GET /api/akquise/leads/{id}` → Detail mit Company, Contacts, Job-Text
  - `GET /api/akquise/wiedervorlagen` → Leer (korrekt, keine faelligen)
  - `POST /api/akquise/test/simulate-call/{id}` → Simulation funktioniert
  - `GET /api/akquise/test/status` → test_mode=true, test_email korrekt
  - `GET /api/akquise/n8n/followup-due` → Leer (korrekt)
  - `GET /api/akquise/n8n/eskalation-due` → Leer (korrekt)
- **E-Mail-Flow (End-to-End):**
  - Draft generiert (GPT-4o-mini): Betreff + Body + fiktiver Kandidat
  - Versand via IONOS SMTP: ERFOLGREICH
  - Test-Modus Redirect: E-Mail ging an hamdard@sincirus.com (nicht an Testfirma)
- **Alle 4 IONOS-Mailboxen einzeln getestet + VON MILAD BESTAETIGT:**
  - hamdard@sincirus-karriere.de → ZUGESTELLT
  - m.hamdard@sincirus-karriere.de → ZUGESTELLT
  - m.hamdard@jobs-sincirus.com → ZUGESTELLT
  - hamdard@jobs-sincirus.com → ZUGESTELLT

### Gap-Analyse + Fixes (01.03.2026 — Session 2)

Systematische Analyse aller Akquise-Dateien deckte 7 Luecken auf. Alle gefixt:

**Fix 1: D10 → ATS-Konvertierung** (KRITISCH)
- **Problem:** Frontend rief nie `/qualify` Endpoint — D10 setzte nur Status, kein ATSJob
- **Fix:** In `acquisition_call_service.py` → `record_call()`: Wenn `disposition == "voll_qualifiziert"`, wird automatisch `AcquisitionQualifyService.convert_to_ats()` aufgerufen. Early-Return bei Erfolg, Graceful Fallback bei Fehler.

**Fix 2: Intelligenter Default-Tab**
- **Problem:** Wenn "Heute anrufen" leer war, sah der User eine leere Liste
- **Fix:** In `routes_acquisition_pages.py` → `akquise_page()`: Fallback-Logik durch alle Tabs (neu → wiedervorlagen → nicht_erreicht → qualifiziert)

**Fix 3: Batch-Disposition UI**
- **Problem:** Backend-Endpoint existierte, aber kein Frontend-Trigger
- **Fix:** In `call_screen.html`: Nach erfolgreicher Disposition zeigt `confirm()` Dialog fuer "Gleiche Disposition fuer X weitere Stellen?". Ruft `PATCH /api/akquise/leads/batch-status`. Nur fuer Nicht-Erreicht-Typen (nicht_erreicht, mailbox_besprochen, besetzt, etc.)

**Fix 4: E-Mail 2h Delay** (KOMPLEX)
- **Problem:** E-Mails gingen sofort raus statt mit 2h Verzögerung
- **Migration 033:** `scheduled_send_at` Feld + Partial Index auf `acquisition_emails`
- **Model:** `acquisition_email.py` — Status "scheduled" + `scheduled_send_at` DateTime
- **Service-Refactor:** `acquisition_email_service.py`:
  - `send_email()` scheduled jetzt mit Delay (Test-Modus: sofort)
  - `_do_send()` — extrahierte Sende-Logik
  - `_get_email_delay_minutes()` — liest aus system_settings (Default: 120 Min)
  - `send_scheduled_emails()` — verarbeitet bis zu 20 faellige E-Mails (fuer n8n Cron)
- **2 neue API-Endpoints** in `routes_acquisition.py`:
  - `POST /api/akquise/n8n/send-scheduled-emails` — n8n Cron alle 15 Min
  - `POST /api/akquise/n8n/auto-followup` — n8n Cron taeglich 09:00

**Fix 5-6: n8n Cron-Workflows** (AKTIV)
- `DAbRL5iwkrJfucDD` — "Akquise: Geplante E-Mails versenden" (alle 15 Min) → **AKTIV**
- `InyfM8n9VF3hejgH` — "Akquise: Auto Follow-up + Break-up" (taeglich 09:00) → **AKTIV**
- Telegram-Nodes entfernt (fehlende Credentials), Workflows aktiviert

**Fix 7: Webex n8n-Workflow** (erstellt, INAKTIV)
- `V8rGRTK0gCkqhWvV` — "Akquise: Webex Eingehender Anruf" (Webhook → Backend)
- Bleibt inaktiv bis Webex-Webhook konfiguriert wird (manuell in n8n UI)

### Playwright E2E-Tests (28.02.2026) — 101/101 BESTANDEN (57 Smoke + 17 Deep + 27 Comprehensive)
- **Test-Datei:** `Akquise/playwright_e2e_tests.py` (23 Phasen, 101 Tests, ~2230 Zeilen)
- **Ausfuehren:** `python Akquise/playwright_e2e_tests.py` (braucht .env mit PULSPOINT_EMAIL + PULSPOINT_PASSWORD)
- **Smoke (Phase 1-10):** Login, Hauptseite, 6 Tabs, CSV-Import, Call-Screen, Notizen+Textbausteine, 17 Checkboxen, 14 Dispositionen, Wiedervorlage-Formular, Neue-Stelle-Formular, E-Mail-Modal, Navigation, Rueckruf-Suche
- **Deep Integration (Phase 11-18):** Anruf-Simulation API, Disposition D1a End-to-End (DB-Persistenz + Auto-Wiedervorlage), Notizen-Persistenz (localStorage), GPT E-Mail-Draft (Betreff+Body+Siezen+Links), KPI-Verifizierung (API vs. Frontend), Rueckruf-Simulation API, Abtelefonieren-Flow, Tab-Zustand+Badges
- **Comprehensive (Phase 19-23):** Alle 13 Dispositionen E2E via API (D1a-D13 inkl. Cascade, ATS-Konvertierung, neue Jobs/Contacts), E-Mail Draft+Send (GPT-Generierung + SMTP), 4 n8n-Endpoints, 3 State-Machine-Negativtests (ungueltige Transitionen → 400), Call-History + Batch-Disposition
- **Backend-Bug gefunden + gefixt:** publish() in simulate-callback falsch aufgerufen (HTTP 500)
- **Key-Learning:** D6 (nie_wieder) MUSS als letzter Dispositions-Test laufen — Cascade blacklistet alle Jobs der Firma
- **14 Runs total** — Technische Loesungen dokumentiert in E2E-TEST.md Teil 2B

### Deployment-Status (28.02.2026)
- **Migration 033:** DEPLOYED auf Railway (scheduled_send_at + idx_acq_emails_scheduled)
- **system_settings:** `acquisition_email_delay_minutes = 120` gesetzt
- **Git Commit:** `3c67346` (11 Dateien, +1428 Zeilen) + Playwright-Fixes (noch zu committen)

### Offen
- Phase 7.4: Webex n8n-Workflow aktivieren (braucht Webex-Webhook-URL)
- Manueller UI-Test durch Milad (E2E-TEST.md Teil 3, Checkliste)
- Audit-Log (P2 — nach Go-Live)

---

## Architektur-Entscheidungen (FINAL)

### Datenbank
- **Bestehende Tabellen erweitern:** Job (+9 Felder inkl. status_changed_at, import_batch_id), Company (+1 Feld), CompanyContact (+3 Felder)
- **Neue Tabellen:** `acquisition_calls` (13 Dispositionen, recording_consent Default false) + `acquisition_emails` (E-Mail-Tracking mit from_email, parent_email_id, sequence_position, email_type, unsubscribe_token)
- **Deduplizierung:** `anzeigen_id` als Primary Key (UNIQUE WHERE acquisition_source='advertsdata')
- **Zeitfenster:** Gleiche anzeigen_id nach >90 Tagen nicht gesehen = neuer Import
- **Re-Import-Schutz:** blacklist_hart nie reaktivieren, qualifiziert nicht zuruecksetzen
- **Content-Hash:** Bleibt als Fallback fuer manuelle Imports
- **State-Machine:** Nur erlaubte Status-Uebergaenge (ALLOWED_TRANSITIONS Dict)
- **Blacklist-Cascade:** nie_wieder → ALLE Stellen der Firma schliessen
- **Akquise-Job-Guard:** WHERE acquisition_source IS NULL im Claude-Matching
- **Migration:** 032_add_acquisition_fields.py

### Backend vs. n8n
- **MT-Backend:** UI, DB, Echtzeit (Import, Call-Screen, Disposition, E-Mail-Gen, Rueckruf)
- **n8n:** Timing + externe Systeme (Cron-Import, Wiedervorlagen-Reminder, E-Mail-Versand, Telegram)

### Frontend
- **Stack:** Alpine.js + HTMX + Jinja2 (wie bestehend)
- **Tabs:** HTMX hx-get pro Tab (nicht Alpine x-show)
- **Call-Screen:** Eingebettetes Split-Panel (KEIN Modal), drei Spalten
- **Autosave:** HTMX delay:2s Debounce
- **Click-to-Call:** `tel:` Protocol Handler (Webex registriert sich als Handler)
- **Rueckruf:** SSE via HTMX-Extension
- **Pagination:** Server-side, 50 pro Seite

### API-Endpoints (Prefix: /api/akquise)
- POST /import, GET /import/status
- GET /leads, GET /leads/{id}, PATCH /leads/{id}/status
- POST /leads/{id}/call
- POST /leads/{id}/email, POST /leads/{id}/email/send
- POST /leads/{id}/qualify
- GET /wiedervorlagen, GET /stats, GET /rueckruf/{phone}

### E-Mail-System (Marketing-Review)
- **5 Postfaecher:** hamdard@sincirus.com (Haupt), sincirus-karriere.de (2x), jobs-sincirus.com (2x)
- **Mailbox-Strategie:** Zweck-basiert (Erst-Mail, Follow-up, Break-up, Antworten, Reserve)
- **Plain-Text** fuer Kaltakquise (hoehere Zustellbarkeit)
- **3-E-Mail-Sequenz:** Initial (Tag 0) → Follow-up (Tag 5-7) → Break-up (Tag 14-17)
- **Thread-Linking:** In-Reply-To Header bei Follow-ups
- **Reply-Detection:** n8n alle 15 Min → Microsoft Graph Inbox-Check
- **Bounce-Handling:** n8n alle 30 Min → NDR-Check
- **Tages-Limit:** 20/Tag IONOS (Warmup), 100/Tag M365
- **Domain-Warmup:** 2 Wochen vor Go-Live (IONOS-Domains)
- **SPF/DKIM/DMARC:** Fuer alle 3 Domains

### Security/DSGVO
- E-Mail und Recording: Kein Consent-Gate — Milad entscheidet operativ selbst
- recording_consent Default = false (Ausnahme, nicht Regel)
- Blacklist (hart/weich) auf Firmenebene mit Cascade
- Abmelde-Link in jeder E-Mail (oeffentlicher Endpoint, kein Auth)
- Import-Batch-ID fuer Rollback
- Audit-Log revisionssicher

---

## Deploy + CSV-Import Bugfixes (28.02.2026 abends)

### Migration 032 auf Railway
- Problem: `alembic_version` war '033', aber keine Migration 033 existiert. Migration 032 wurde nie ausgefuehrt.
- Fix: alembic_version auf '031' zurueckgesetzt, Migration 032 SQL manuell ausgefuehrt, Version auf '032' gesetzt.
- Ergebnis: Alle Tabellen (acquisition_calls, acquisition_emails), Spalten (+9 Job, +1 Company, +3 Contact), 9 Indizes bestaetigt.

### CSV-Import — 6 Bugs gefixt:

1. **Keine "Position"-Spalte** (363 Fehler): advertsdata CSV hat keine separate "Position"-Spalte. Jobtitel steht im "Anzeigen-Text".
   - Fix: `_extract_position_from_text()` hinzugefuegt (erkennt (m/w/d), LinkedIn URLs, XING-Marker)
   - Datei: `acquisition_import_service.py`

2. **VARCHAR Truncation** ("value too long for character varying(100)" auf companies.industry): CSV-Werte laenger als DB-Felder.
   - Fix: `_trunc()` Funktion + `_FIELD_LIMITS` Dict fuer alle String-Felder
   - Datei: `acquisition_import_service.py`

3. **greenlet_spawn Error**: `bool(company.jobs)` loest Lazy-Load im Async-Kontext aus.
   - Fix: Ersetzt durch `cache_key in company_cache`
   - Datei: `acquisition_import_service.py`

4. **UniqueViolation auf anzeigen_id**: Zwei Indizes existierten (UNIQUE `ix_jobs_anzeigen_id` + non-unique `idx_jobs_anzeigen_id`). CSV hat Duplikate.
   - Fix: UNIQUE Index auf Railway gedroppt, `existing_jobs` Dict nach jedem Insert aktualisiert
   - Datei: `acquisition_import_service.py` + Railway SQL

5. **"Multiple rows were found"**: `scalar_one_or_none()` in company_service crasht bei mehreren Firmen mit gleichem Namen+Stadt.
   - Fix: `.limit(1).scalars().first()` statt `scalar_one_or_none()`
   - Datei: `app/services/company_service.py` (get_or_create_by_name + get_or_create_contact)

6. **Session-Rollback nach DB-Fehler**: Nachfolgende Zeilen scheiterten nach einem DB-Fehler.
   - Fix: `await self.db.rollback()` im except-Handler nach jedem Fehler
   - Datei: `acquisition_import_service.py`

### Weitere Fixes:
- `COL_ALTERNATIVES` fuer "Firma Telefonnummer" → "company_phone" Mapping
- Error-Details im Frontend angezeigt (erste 10 Fehler sichtbar)
- Import/Preview Endpoints mit try/except umhuellt (kein 500 mehr, stattdessen error_details)

---

## Dateien im Akquise-Ordner

| Datei | Inhalt |
|-------|--------|
| RECHERCHE.md | Research + Deep-Dive Ergebnisse (DB, API, Frontend, Services, CSV, Luecken, ATS, Email, n8n, Config, Routes) |
| REVIEW-TEAM.md | Alle Review-Team-Ergebnisse (Architekt, DevOps, Security, Product, UI/UX, Frontend) |
| PLAN.md | Finaler strukturierter Implementierungsplan |
| MEMORY.md | Diese Datei — Kontexterhaltung ueber Sessions |

---

## CSV-Struktur (advertsdata.com)

363 Zeilen, 29 Spalten, Tab-getrennt:
- Spalten 1-9: Firmendaten (Name, Adresse, PLZ, Ort, E-Mail, Telefon, Domain, Branche, Groesse)
- Spalten 10-15: AP Firma (Anrede, Vorname, Nachname, Funktion, Telefon, E-Mail)
- Spalten 16-20: Anzeigendaten (Link, Position-ID, Anzeigen-ID, Einsatzort, Art)
- Spalte 21: Anzeigen-Text (voller Stellenausschreibungstext)
- Spalten 22-28: AP Anzeige (Anrede, Titel, Vorname, Nachname, Funktion, Telefon, E-Mail)
- Spalte 29: Beschaeftigungsart

---

## Deep-Dive Key-Findings (27.02.2026)

### Wiederverwendbar fuer Akquise (1:1 oder mit kleinen Anpassungen)
- `CompanyService.get_or_create_by_name()` — Blacklist-Check inklusive
- `CompanyContactService.get_or_create_contact()` — Case-insensitive Match
- `MicrosoftGraphClient` — OAuth2 Token-Caching, Retry-Logic
- `EmailPreparationService` — Preview-Workflow (Generieren → Editieren → Senden)
- `ATSTodo` + `ATSCallNote` — Fuer Akquise-Anrufprotokoll
- `ATSJobService.convert_from_job()` — Uebergang Akquise → ATS
- `N8nNotifyService` — Fuer Wiedervorlagen-Trigger
- Background-Task Pattern aus claude_matching (Task + Status-Polling)
- Batch-Size 50 pro DB-Transaktion (bewaehrt fuer Railway)

### Neu zu bauen
- Akquise CSV-Parser (29 Tab-Spalten, eigenes Mapping)
- `acquisition_calls` Tabelle (Disposition + Qualifizierung)
- `acquisition_emails` Tabelle (gesendete E-Mails pro Lead, fuer Tab "Nicht erreicht")
- Akquise-Seite Template (Tab-Layout, Call-Screen, Disposition)
- E-Mail-Template `AKQUISE_ANSCHREIBEN`
- Akquise-GPT-Prompt (Traum-Kandidat aus Stellenausschreibung)
- SSE Endpoint fuer Rueckruf-Popup
- Phone-Normalisierung (E.164)
- `/akquise` Route in routes_pages.py
- AuthMiddleware Whitelist fuer `/akquise`

### Wichtige Dateipfade (fuer Copy-Paste bei Implementierung)
- ATS Models: `app/models/ats_job.py`, `ats_pipeline.py`, `ats_todo.py`, `ats_call_note.py`
- ATS Services: `app/services/ats_pipeline_service.py`, `ats_job_service.py`
- Email: `app/services/email_service.py`, `email_generator_service.py`, `email_preparation_service.py`
- Graph: `app/services/microsoft_graph_client.py`
- CSV: `app/services/csv_import_service.py`, `csv_validator.py`
- Company: `app/services/company_service.py`, `company_contact_service.py`
- n8n: `app/services/n8n_notify_service.py`, `app/api/routes_n8n_webhooks.py`
- Config: `app/config.py` (23 ENV vars)
- Pages: `app/api/routes_pages.py` (neue Route registrieren)
- Middleware: `main.py` Zeile 350-438 (Auth Whitelist)

---

## Vertriebs-Review Aenderungen (27.02.2026)

### Eingearbeitet in PLAN.md:
- **13 statt 10 Dispositionen:** +Mailbox besprochen, +AP nicht mehr da, +Andere Stelle offen, +Weiterverbunden
- **Uhrzeit bei Wiedervorlagen** (Pflichtfeld bei D7, D9)
- **Firmen-Gruppierung:** Leads gleicher Firma zusammengefasst, Batch-Disposition
- **Bestandskunden-Erkennung:** Badge in Lead-Liste + Hinweis im Call-Screen
- **Priorisierung erweitert:** 6-Faktoren-Formel (Branche, AP, Groesse, Alter, Senioritaet, Bekannt)
- **Call-Screen verbessert:** Firma-Kontext, Call-Historie sichtbar, 5 Textbausteine, Copy-Button, Neuer-AP-Formular
- **Frage 2 umformuliert:** "Wie besetzen Sie aktuell?" statt "Ext. Dienstleister?" (weniger defensiv)
- **E-Mail-Delay:** 2h statt sofort (wirkt persoenlicher)
- **Follow-up-E-Mail:** Nach 5-7 Tagen ohne Antwort, kuerzere GPT-Variante
- **Kein Auto-Blacklist** bei Nicht-Erreichen — nur bei expliziter Ablehnung
- **Tab "In Bearbeitung" → "Wiedervorlagen"** (klarer definiert)
- **"Abtelefonieren starten" Button** + "Weitermachen wo ich war" (localStorage)

## Marketing-Review Aenderungen (27.02.2026)

### Eingearbeitet in PLAN.md (19 P1-Massnahmen aus 3 Agenten):
- **D3 → kontakt_fehlt** statt verloren (Lifecycle)
- **Reply-Detection** via Microsoft Graph + n8n (Lifecycle + Email)
- **Akquise-Dossier** bei ATS-Konvertierung (Lifecycle)
- **Break-up-Email** als 3. Touchpoint, Tag 14-17 (Lifecycle + Email)
- **Abmelde-Endpoint** oeffentlich, kein Auth (Lifecycle + CRM)
- **akquise_status_changed_at** Timestamp (Lifecycle)
- **Plain-Text E-Mails** statt HTML (Email)
- **SPF/DKIM/DMARC** fuer alle 3 Domains (Email)
- **Domain-Warmup** 2 Wochen vor Go-Live (Email)
- **Multi-Mailbox** 5 Postfaecher mit Dropdown (Email + User)
- **Tages-Limit** pro Mailbox (Email)
- **Thread-Linking** In-Reply-To Header (Email)
- **from_email + parent_email_id + sequence_position** auf acquisition_emails (Email)
- **Bounce-Handling** via n8n + NDR-Check (Email)
- **Akquise-Job-Guard** WHERE acquisition_source IS NULL (CRM)
- **State-Machine-Validierung** fuer Status-Uebergaenge (CRM)
- **Blacklist-Cascade** bei nie_wieder (CRM)
- **Re-Import Status-aware** (CRM)
- **recording_consent Default false + Import-Batch-ID** (CRM)
- **Intelligenter Default-Tab:** Auto-Weiterleitung wenn "Heute" leer

---

## Bekannte Risiken

1. Railway 30s DB-Timeout — eigene Session pro Batch (50 Zeilen)
2. E-Mail-Kaltakquise ohne Einwilligung = Abmahngefahr (UWG)
3. Anrufaufzeichnung ohne Consent = Straftat (§201 StGB)
4. Firma-Fuzzy-Dedup fehlt ("Mueller" vs "Müller")
5. Webex Click-to-Call — kein SDK noetig, `tel:` reicht
6. Rueckruf-Popup SLA: 2s in 80%, 5s in 95%
7. Phone-Normalisierung (E.164) ist PFLICHT fuer Rueckruf-Lookup

---

## PFLICHT-REGELN (UNANTASTBAR — VERSTOSS = VERBOTEN)

### Regel 1: Nach Kontext-Komprimierung (SOFORT, KEINE AUSNAHME)
- Nach JEDER Komprimierung MUESSEN ALLE folgenden Dateien gelesen werden BEVOR irgendeine Arbeit stattfindet:
  1. `Akquise/MEMORY.md` (diese Datei)
  2. `Akquise/PLAN.md`
  3. `Akquise/RECHERCHE.md`
  4. `Akquise/REVIEW-TEAM.md`
  5. `Akquise/E2E-TEST.md`
  6. Persistente Memory (`~/.claude/projects/.../memory/MEMORY.md`)
- Danach: Aktuelle Aufgabe identifizieren, letzten Stand verstehen, offene Punkte lesen
- ERST wenn der gesamte Kontext zu 100% verstanden ist, darf weitergearbeitet werden
- Diese Regel darf unter GAR KEINEN Umstaenden umgangen werden

### Regel 2: Nach JEDER erledigten Aufgabe (SOFORT aktualisieren)
- ALLE Dateien aktualisieren: Akquise/MEMORY.md + Akquise/PLAN.md + Akquise/E2E-TEST.md + persistente Memory
- Fortschritt festhalten BEVOR mit der naechsten Aufgabe begonnen wird
- Nicht batchen, nicht vergessen — NACH JEDER einzelnen Aufgabe!
- Diese Regel darf unter GAR KEINEN Umstaenden umgangen werden

### Regel 3: 15-Minuten-Regel bei Problemen (STOPP-PFLICHT)
- Wenn ein Problem laenger als 15 Minuten zur Loesung braucht: SOFORT STOPPEN
- Alle Dateien erneut lesen (Regel 1 komplett wiederholen)
- In allen Dateien nach Loesungen/Hinweisen fuer das Problem suchen
- Kontext vollstaendig wiederherstellen
- ERST DANN am Problem weiterarbeiten
- Diese Regel darf unter GAR KEINEN Umstaenden umgangen werden
