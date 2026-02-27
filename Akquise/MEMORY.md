# Akquise-Automatisierung — Memory (Kontexterhaltung)

> **Letzte Aktualisierung: 28.02.2026**
> Diese Datei wird nach JEDER erledigten Aufgabe aktualisiert.

---

## Projekt-Status

| Phase | Status | Beschreibung |
|-------|--------|--------------|
| Recherche | FERTIG | 6 Research + 6 Review + 6 Deep-Dive + 1 Vertriebsingenieur + 3 Marketing abgeschlossen (22 Agenten total) |
| Planung | FERTIG | Plan in PLAN.md (9 Phasen, nach Vertriebs-Review aktualisiert) |
| Migration 032 | FERTIG | `migrations/versions/032_add_acquisition_fields.py` — Job +9, Company +1, Contact +3, acquisition_calls, acquisition_emails, 9 Indizes |
| SQLAlchemy Models | FERTIG | job.py, company.py, company_contact.py erweitert + acquisition_call.py, acquisition_email.py neu |
| Akquise-Job-Guard | FERTIG | `acquisition_source IS NULL` in v5_matching_service.py (Z.438) + matching_engine_v2.py (Z.2245, Z.2398) |
| Backend-Services | FERTIG | AcquisitionImportService, CallService, EmailService, QualifyService |
| API-Endpoints | FERTIG | routes_acquisition.py (19 Endpoints), routes_acquisition_pages.py (4 HTMX-Routes) |
| Auth-Whitelist | FERTIG | `/api/akquise/unsubscribe/` als PUBLIC_PREFIX in auth.py |
| Frontend Hauptseite | FERTIG | akquise_page.html (Dark-Theme, Tabs, KPIs, CSV-Import-Modal, E-Mail-Modal, Call-Screen-Overlay, Rueckruf-Popup) |
| Frontend Tab-Partials | FERTIG | 6 Tab-Templates (heute, neu, wiedervorlagen, nicht_erreicht, qualifiziert, archiv) + company_group + pagination |
| Frontend Call-Screen | FERTIG | 3-Spalten-Layout (Stellentext, Qualifizierung/Kontakt, Aktionen) mit Timer, Textbausteinen, Auto-Advance |
| Frontend Disposition | FERTIG | 13 Dispositionen (D1a-D13) mit Farbcodierung, Wiedervorlage-Datum/Uhrzeit, Confirm bei nie_wieder |
| Frontend E-Mail-Modal | FERTIG | GPT-Generierung, Mailbox-Dropdown (5 Postfaecher), Betreff+Text editierbar, Senden |
| Frontend Formulare | FERTIG | Neuer-AP-Formular (D13), Neue-Stelle-Formular (D12) |
| Frontend Rueckruf-Popup | FERTIG | Popup-Template mit Firma/AP/Lead/letzte Disposition |
| Navigation | FERTIG | /akquise Link in Desktop + Mobile Nav (base.html) |
| DNS-Check | FERTIG | SPF/DKIM/DMARC fuer alle 3 Domains bestaetigt (sincirus.com, sincirus-karriere.de, jobs-sincirus.com) |
| IONOS SMTP Client | OFFEN | Fuer sincirus-karriere.de + jobs-sincirus.com Mailboxes |
| Mailbox-Config | OFFEN | system_settings: 5 Postfaecher, Tages-Limits, Warmup |
| n8n-Workflows | OFFEN | 6 Workflows: Wiedervorlagen, Eskalation, Reporting, Reply-Detection, Bounce-Handling, Follow-up-Reminder |
| Testen + Deploy | OFFEN | Migration ausfuehren, Test-CSV, E2E-Test, Railway Deploy |

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
