# Akquise-Automatisierung — Recherche-Ergebnisse

> **Stand: 27.02.2026 — 6 Research-Agenten parallel**
> Jeder Agent hat unabhaengig recherchiert. Ergebnisse ungefiltert dokumentiert.

---

## AGENT 1: Datenbank-Modelle & Schema

### Haupt-Entities

**Company** (`app/models/company.py:23-118`)
- `id` (UUID PK), `name` (String 255 NOT NULL), `domain` (String 255)
- `address` (Text), `city` (String 100), `phone` (String 100)
- `location_coords` (PostGIS Geography POINT)
- `employee_count` (String 50), `industry` (String 100)
- `erp_systems` (ARRAY String), `status` (Enum: active/blacklist/laufende_prozesse)
- `notes` (Text), `created_at`, `updated_at`
- Relationships: `jobs` (1:n), `contacts` (1:n), `correspondence` (1:n), `ats_jobs` (1:n)
- Constraint: UniqueConstraint(name, city) fuer Multi-Standorte

**CompanyContact** (`app/models/company_contact.py:13-77`)
- `id` (UUID PK), `company_id` (FK companies CASCADE)
- `salutation` (String 20), `first_name`/`last_name` (String 100)
- `position` (String 255), `email` (String 500), `phone`/`mobile` (String 100)
- `contact_number` (Integer, auto-increment per company), `city` (String 255)
- `notes` (Text), `created_at`, `updated_at`

**Job** (`app/models/job.py:15-153`)
- `id` (UUID PK), `company_name` (String 255), `company_id` (FK companies SET NULL)
- `position` (String 255), `street_address`/`postal_code`/`city`/`work_location_city`
- `location_coords` (PostGIS), `job_url` (String 500), `job_text` (Text)
- `employment_type` (String 100), `industry` (String 100), `company_size` (String 50)
- Hotlist: `hotlist_category`/`hotlist_city`/`hotlist_job_title`/`hotlist_job_titles`
- Classification: `classification_data` (JSONB), `quality_score` (String 20)
- Dedup: `content_hash` (String 64 UNIQUE)
- Lifecycle: `expires_at` (DateTime), `imported_at`, `last_updated_at`
- Embeddings: `embedding` (JSONB), `v2_embedding` (JSONB)

**Match** (`app/models/match.py:47-178`)
- `id` (UUID PK), `job_id` (FK), `candidate_id` (FK)
- `matching_method` (String 50: pre_match/deep_match/smart_match/manual/claude_code)
- Scoring: `v2_score` (Float 0-100), `v2_score_breakdown` (JSONB), `ai_score`
- Fahrzeit: `drive_time_car_min`, `drive_time_transit_min` (Integer)
- Claude v4: `empfehlung` (String 20), `wow_faktor` (Boolean), `wow_grund` (Text)
- Status: `status` (Enum: new/ai_checked/presented/rejected/placed)
- Outreach: `outreach_status`, `outreach_sent_at`, `candidate_feedback`
- Constraint: UniqueConstraint(job_id, candidate_id)

### Akquise-spezifische Felder: NOCH NICHT VORHANDEN
- Keine Felder fuer `acquisition_source`, `anzeigen_id`, `position_id` im Schema
- Erfordert neue Migration (nach 031)

### Duplikaterkennung
- Content-Hash (SHA256): Unternehmen + Position + Stadt + PLZ + URL
- Bei Duplikat: `last_updated_at` + `expires_at` auffrischen, NICHT neu einfuegen
- Feld: `Job.content_hash` (VARCHAR 64, UNIQUE)

### 31 Migrations vorhanden (001 bis 031)
- Letzte: 031 = `rundmail_eligible_from` auf Candidates
- Naechste waere 032 fuer Akquise-Felder

---

## AGENT 2: API Routes & Endpoints

### Router-Registrierung (main.py:350-438)

| Route-Datei | Prefix | Beschreibung |
|---|---|---|
| routes_jobs | /api | Jobs CRUD + CSV-Import + Klassifizierung |
| routes_candidates | /api | Kandidaten CRUD + Klassifizierung |
| routes_companies | /api | Companies CRUD |
| routes_matches | /api | Matches CRUD + Outreach |
| routes_settings | /api | System-Einstellungen |
| routes_matching_v2 | /api/v2 | Profiling, Embeddings, Weights, Scoring |
| routes_claude_matching | /api/v4 | Claude Matching (Stufe 0+1+2) |
| routes_ats_jobs | /api | ATS Job Management |
| routes_ats_pipeline | /api | ATS Pipeline Workflows |
| routes_ats_call_notes | /api | Telefonat-Notizen |
| routes_ats_todos | /api | Aufgaben/TODOs |
| routes_n8n_webhooks | /api | n8n Webhook-Endpoints |
| routes_email | /api | E-Mail Versand |
| routes_email_automation | /api | E-Mail Automatisierung |
| routes_briefing | /api | Morning Briefing |
| routes_search | /api | Globale Suche |
| routes_new_match_center | /match-center | Match Center v4/v5 |
| routes_pages | (keine) | HTML-Seiten (Dashboard, Jobs, etc.) |

### Wichtige Endpoints

**CSV-Import:**
- POST /api/jobs/import — CSV hochladen + Import starten
- POST /api/jobs/import-preview — Stadt-Analyse vor Import

**Companies:**
- GET/POST /api/companies — CRUD
- GET /api/companies/{id} — Detail
- Methode: `get_or_create_by_name()` beim CSV-Import

**Contacts:**
- Kontakte werden ueber Company-Endpoints verwaltet
- Methode: `get_or_create_contact()` beim Import

**ATS:**
- POST /api/ats/call-notes — Telefonat-Notizen speichern
- GET /api/ats/todos — Aufgaben abrufen
- POST /api/ats/todos — Aufgabe erstellen

---

## AGENT 3: Frontend & Templates

### Technologie-Stack
- **Alpine.js 3.x** — Client-seitige State-Management
- **HTMX 1.9.10** — HTML-basierte API-Requests (kein JSON-Frontend)
- **Tailwind CSS + Custom Dark Theme** — Styling via CDN
- **Jinja2** — Server-side Rendering
- **Kein React/Vue** — Reines SSR mit Progressive Enhancement

### 104 Template-Dateien (32 Top-Level + Components + Partials)

**Layout-System:**
- `base.html` (550+ Zeilen): Sticky Top-Nav (48px), Dark-Theme CSS-Variablen, Global Modal+Toast
- Navigation: 13 Nav-Items (Dashboard, Jobs, Kandidaten, Unternehmen, ATS, Hotlisten, Match Center, etc.)
- Active-State via Jinja2 path-check

**Listen-Seiten-Pattern:**
1. Hero Header (Icon + Title + Subtitle)
2. Action Buttons (View Toggle Cards/List, Add, Import)
3. Filter Bar (Search mit HTMX delay:300ms, Dropdowns)
4. Content (Cards ODER List via job_card.html / job_row.html)

**Detail-Seiten-Pattern:**
- Breadcrumb → Header-Card → Meta-Info → Tabs/Accordion → Related Content

**CSV-Import Frontend:**
- Modal mit Drag-Drop Zone
- POST multipart/form-data → Server-Response mit Progress
- Pipeline-Fortschritt: Kategorisierung → Klassifizierung → Geocoding

**Wiederverwendbare Komponenten:**
- `import_dialog.html` — CSV-Import Modal
- `job_row.html` / `job_card.html` — Job-Anzeige
- `quickadd_*.html` — Schnell-Hinzufuegen Modals
- `filter_panel.html`, `pagination.html`, `progress_bar.html`
- Toast-System, Modal-System (global in base.html)

**HTMX-Pattern (typisch):**
```
<input hx-get="/partials/jobs-list"
       hx-trigger="keyup changed delay:300ms"
       hx-target="#jobs-content"
       hx-include="[name='sort_by'],[name='company']">
```

### Fuer Akquise-Seite kopierbar:
- `jobs.html` als Basis (Cards + List Toggle)
- `import_dialog.html` (CSV-Upload Modal)
- `company_detail.html` (Tab-Layout mit Kontakten)
- Alle bestehenden Components (toast, pagination, filter_panel)

---

## AGENT 4: Services & Business Logic

### 46 Service-Dateien gesamt

**CSV-Pipeline (5 Services, sequentiell):**
1. `csv_validator.py` — Header-Check, Encoding, Delimiter, content_hash
2. `csv_import_service.py` — Batch-Insert, Duplikat-Handling, Company get_or_create
3. `categorization_service.py` — FINANCE/ENGINEERING/SONSTIGE Einordnung
4. `finance_classifier_service.py` — GPT-4o-mini: primary_role, sub_level, quality_score
5. `geocoding_service.py` — Nominatim/OSM → PostGIS Koordinaten

**Outreach-Pipeline:**
1. `email_generator_service.py` — GPT-4o-mini personalisierte E-Mails
2. `email_service.py` — Microsoft Graph API (M365, Token-Caching)
3. `job_description_pdf_service.py` — WeasyPrint HTML→PDF
4. `outreach_service.py` — Orchestrierung (Match→PDF→Email→Graph)

**Telefonie:**
- `call_transcription_service.py` — Whisper + GPT-4o-mini (Audio→Transkript→Felder)
- `ats_call_note_service.py` — CallType/CallDirection/Duration/action_items
- **KEIN Webex-Service vorhanden** — Webex-Integration muss NEU gebaut werden

**Matching:**
- `v5_matching_service.py` — AKTUELL: Rollen+Geo Matching (PostGIS + Kompatibilitaet)
- `claude_matching_service.py` — DEPRECATED, re-exportiert nur V5

**Background-Tasks:**
- `job_runner_service.py` — JobRunnerService mit JobType Enum
- IMMER `async_session_maker` verwenden (Railway 30s Timeout!)

### Akquise-Services: NOCH NICHT IMPLEMENTIERT

---

## AGENT 5: CSV Import & Duplikat-Analyse

### Content-Hash Berechnung (csv_validator.py:418-443)
```
Hash-Inputs: Unternehmen + Position + Stadt + PLZ + URL
→ SHA256 → 64-Zeichen Hex-String
→ Gespeichert in Job.content_hash (UNIQUE)
```

### Duplikat-Handling beim Import
1. Alle bestehenden Hashes laden: `Dict[hash → UUID]`
2. Pro CSV-Zeile: Hash berechnen
3. Hash existiert? → `expires_at` + `last_updated_at` auffrischen, NICHT neu importieren
4. Hash neu? → Job einfuegen + Company get_or_create + Contact get_or_create

### Company-Handling
- `get_or_create_by_name(name, city)` — UniqueConstraint(name, city)
- Cache-Key: `{name.lower()}_{city.lower()}`
- Blacklisted Companies werden uebersprungen

### Contact-Handling
- Optional: Nur wenn `ap_vorname` ODER `ap_nachname` vorhanden
- Zuordnung zu Company, NICHT zu Job
- `get_or_create_contact(company_id, first_name, last_name, ...)`

### Vorschau-Funktion
- POST /api/jobs/import-preview
- Stadt-Analyse: Jobs pro Stadt vs. verfuegbare Kandidaten
- Empfehlung: "importieren" wenn Kandidaten vorhanden

### Kritische Erkenntnis fuer Akquise-CSV:
Die bestehende CSV hat ANDERE Spalten als die advertsdata-CSV:
- Aktuell: Position, Strasse, PLZ, Stadt, URL, Beschreibung, etc.
- advertsdata: Position-ID, Anzeigen-ID, AP-Firma (6 Felder), AP-Anzeige (7 Felder), Anzeigen-Text
→ Der Akquise-CSV-Import braucht einen EIGENEN Parser!

---

## AGENT 6: Anforderungs-Analyse (Luecken & Risiken)

### 1. Logische Luecken
1. **Kein Follow-up-Mechanismus** nach E-Mail (Wiedervorlage nach X Tagen fehlt)
2. **Kein Eskalationspfad** (nach 3 Anrufen + 2 Mails → Lead als "tot" markieren?)
3. **Uebergang Qualifizierung → Matching** nicht definiert (Job wird "Stelle" → Claude-Matching?)
4. **Keine Lead-Priorisierung** (363 Zeilen/Tag → nach welchen Kriterien zuerst telefonieren?)
5. **Mehrstufige Kontakte** unklar (AP-Firma UND AP-Anzeige → wer wird angerufen?)

### 2. Technische Risiken
6. **CSV-Qualitaet unberechenbar** (Encoding, fehlende Felder, inkonsistente Telefonnummern)
7. **advertsdata.com kann Format aendern** (kein API-Vertrag, nur CSV-Export)
8. **Webex-Integration fragil** (Browser-Extension oder API, Updates brechen es)
9. **GPT-Rate-Limits** bei Massen-E-Mail-Generierung

### 3. UX-Probleme
10. **17+ Qualifizierungsfragen zu viel fuer Cold-Call** → Minimal-Set (3-5) fuer Erstkontakt
11. **Einzeln E-Mail-Button klicken bei 200+ Leads** → Batch-Funktion noetig
12. **Keine E-Mail-Vorschau** vor dem Senden → GPT-Halluzinationen im Kundenkontakt

### 4. Daten-Integritaet
13. **Firma-Deduplizierung fehlt** ("Mueller GmbH" vs "Müller GmbH" vs "Mueller GmbH & Co. KG")
14. **Kontakt-Deduplizierung nur ueber Name** → gleicher Kontakt, neue E-Mail = Duplikat?
15. **Call-Historie verloren bei Firma-Duplikaten**

### 5. Edge-Cases
16. **Personalvermittler-Anzeigen** in advertsdata (Konkurrenz muss rausgefiltert werden)
17. **Job nach 3 Monaten neu** aber Position-ID + Anzeigen-ID gleich → 3-Monats-Regel greift nicht
18. **Firma mit mehreren Standorten** → ein Kontakt, mehrere Jobs?
19. **Re-Import am selben Tag** mit Aenderungen → Update oder Ignorieren?

### 6. Fehlende Anforderungen
20. **Anrufprotokoll/Logging** (wann, wer, wie lange, Ergebnis)
21. **Wiedervorlage-System** ("Firma X morgen 14 Uhr nochmal anrufen")
22. **Reporting/KPIs** (Conversion-Rate, Aktivitaets-Tracking, Pipeline)
23. **Blacklist** fuer Firmen die keine Zusammenarbeit wuenschen

### 7. E-Mail-Generierung Luecken
24. **Absender-Konfiguration** fehlt (geschaeftliche E-Mail, nicht noreply)
25. **Rechtsgueltige Signatur** fehlt (Impressum, Handelsregister)
26. **Opt-out/Abmelde-Link** fehlt (DSGVO-Pflicht!)
27. **E-Mail-Tracking** fehlt (geoeffnet, geklickt?)
28. **"Passender Kandidat" — woher?** Mini-Matching VOR E-Mail-Versand noetig

### 8. Rueckruf-Zuordnung
29. **Telefonnummer-Matching extrem unzuverlaessig** (Firmenzentrale, Handy, Tage spaeter)
30. **Braucht**: Firma-Telefon + AP-Telefon + Zeitfenster + manuellen Fallback

### 9. DSGVO
31. **B2B-Kaltakquise per E-Mail** nur mit berechtigtem Interesse (Art. 6 Abs. 1 lit. f)
32. **advertsdata-Daten**: Rechtsgrundlage, Aufbewahrungsfrist, Loeschkonzept noetig
33. **Ansprechpartner-Daten sind personenbezogen** → Verarbeitungsverzeichnis Pflicht
34. **Widerspruch muss sofort umgesetzt werden** → Blacklist-Pflicht

---

---

# DEEP-DIVE ERGEBNISSE (27.02.2026)

> **6 Deep-Dive-Agenten haben JEDE Datei im Codebase gelesen.**
> Ergebnisse hier dokumentiert fuer Kontext-Erhaltung ueber Sessions.

---

## DEEP-DIVE 1: ATS-System (Pipeline, Jobs, Todos, Call Notes)

### ATSJob (`app/models/ats_job.py`)
- Eigene Entity NEBEN regulaerem Job: `id` (UUID), `company_id` (FK), `title`, `description`
- **25 Qualifizierungs-Felder** als eigene Spalten (NICHT JSONB):
  - `is_replacement`, `replacement_reason`, `team_size`, `salary_range_min/max`
  - `start_date`, `contract_type`, `remote_policy`, `travel_percentage`
  - `interview_stages`, `decision_maker`, `urgency_level`, `exclusivity`
  - `fee_percentage`, `guarantee_period`, `required_certifications`
  - `nice_to_have_skills`, `deal_breakers`, `company_culture`, `benefits`
  - `candidate_profile_notes`, `internal_notes`
- `manual_overrides` (JSONB) fuer ad-hoc Felder
- `status` Enum: DRAFT → ACTIVE → ON_HOLD → FILLED → CANCELLED
- `source`: ACQUISITION / INBOUND / REFERRAL / REACTIVATION

### ATSPipelineEntry (`app/models/ats_pipeline.py`)
- **10 Pipeline-Stufen:** MATCHED → CONTACTED → INTERESTED → SUBMITTED → INTERVIEW_SCHEDULED → INTERVIEW_DONE → OFFER → ACCEPTED → PLACED → REJECTED
- `entry_type`: MATCH_BASED (aus Matching) oder MANUAL (manuelle Zuordnung)
- `rejection_reason`, `rejection_details` (Text) bei REJECTED
- **Activity-Tracking:** ATSActivity (type, description, metadata JSONB) pro Entry

### ATSTodo (`app/models/ats_todo.py`)
- `title`, `description`, `due_date`, `priority` (1-5, Default 3)
- `related_type` + `related_id`: Polymorphe Verknuepfung (company, candidate, job, pipeline_entry)
- `status`: OPEN → IN_PROGRESS → DONE → CANCELLED
- **Duplikat-Check:** Gleicher Titel + related_id + OPEN = kein neues Todo

### ATSCallNote (`app/models/ats_call_note.py`)
- `call_type`: OUTBOUND / INBOUND / FOLLOW_UP / CALLBACK
- `call_direction`: Redundant zu call_type (Legacy)
- `duration_seconds`, `outcome` (String frei)
- `action_items` (JSONB Array): Automatisch erstellte Todos
- `related_type` + `related_id`: Wie bei Todos
- **Auto-Create-Todos:** Wenn action_items gesetzt → Todos werden automatisch angelegt

### ATSPipelineService (`app/services/ats_pipeline_service.py`)
- `create_entry()`: Match → Pipeline-Entry + Activity + Optional Todo
- `update_stage()`: Validiert erlaubte Uebergaenge, erstellt Activity
- `reject_entry()`: Setzt Stage auf REJECTED + Grund
- `get_pipeline_for_job()`: Alle Entries fuer einen ATSJob
- `get_candidate_pipeline()`: Alle Entries wo Kandidat involviert

### ATSJobService (`app/services/ats_job_service.py`)
- `create_from_qualification()`: Nimmt Qualifizierungs-Daten → ATSJob
- `update_qualification()`: Teilupdate der 25 Felder
- `convert_from_job()`: Normaler Job → ATSJob (Akquise-Uebergang!)
- `get_with_stats()`: ATSJob + Anzahl Pipeline-Entries pro Stage

### RELEVANZ FUER AKQUISE:
- `convert_from_job()` ist der EXAKTE Uebergang Akquise → ATS
- ATSTodo + ATSCallNote koennen 1:1 fuer Akquise-Anrufprotokoll genutzt werden
- ATSActivity fuer Audit-Trail
- 25 Qualifizierungsfelder stimmen ~70% mit den geplanten Fragen ueberein

---

## DEEP-DIVE 2: E-Mail-System (Microsoft Graph, GPT, Outreach)

### MicrosoftGraphClient (`app/services/microsoft_graph_client.py`)
- OAuth2 Client Credentials Flow (NICHT User-Auth)
- `tenant_id`, `client_id`, `client_secret` aus Environment
- **Token-Caching:** In-Memory mit TTL (3600s - 300s Puffer)
- `send_mail(from_email, to_email, subject, body_html, attachments[])`
- `attachments`: Base64-encoded, Content-Type, Filename
- **Retry-Logic:** 3 Versuche mit Exponential Backoff bei 429/5xx

### EmailService (`app/services/email_service.py`)
- **2 E-Mail-Templates:**
  1. `KONTAKTDATEN` — Kandidaten-Kontaktdaten an Firma senden
  2. `STELLENAUSSCHREIBUNG` — Job-Details an Kandidat senden
- Template-Rendering via Jinja2 (HTML-Templates in `app/templates/emails/`)
- `send_email()`: Template rendern → Graph Client → DB-Logging
- **Kein Akquise-Template vorhanden** → Muss NEU erstellt werden

### EmailGeneratorService (`app/services/email_generator_service.py`)
- **Model:** Claude Sonnet (NICHT GPT) fuer Praesentations-E-Mails
- `generate_presentation_email(match, candidate, job, company)`
- Prompt: Personalisierte E-Mail die Kandidat dem Unternehmen vorstellt
- Output: `{subject, body_html, tone, personalization_score}`
- **Fuer Akquise umschreibbar:** Statt Match-Praesentation → Akquise-Anschreiben

### EmailPreparationService (`app/services/email_preparation_service.py`)
- **2x2 Matrix:** Richtung (AN_FIRMA / AN_KANDIDAT) × Variante (KONTAKTDATEN / VORSTELLUNG)
- `prepare_email()`: Laedt alle Daten, generiert via GPT/Claude, gibt Preview zurueck
- `send_prepared_email()`: Preview → Graph Client → Status-Update
- **Preview-Workflow:** Generieren → Anzeigen → Editieren → Senden (KEIN Auto-Send!)

### OutreachService (`app/services/outreach_service.py`)
- Orchestriert: Match laden → Job-PDF generieren → E-Mail generieren → Senden
- `create_outreach(match_id)`: Kompletter Flow in einem Call
- PDF-Generierung via `JobDescriptionPdfService` (WeasyPrint HTML→PDF)
- Outreach-Status auf Match: `outreach_status` (pending/sent/opened/replied/rejected)
- `outreach_sent_at`, `outreach_opened_at` Timestamps

### RELEVANZ FUER AKQUISE:
- MicrosoftGraphClient kann 1:1 wiederverwendet werden
- EmailGeneratorService Prompt muss fuer Akquise angepasst werden
- Preview-Workflow (Generieren → Anzeigen → Editieren → Senden) passt PERFEKT
- Neues Template `AKQUISE_ANSCHREIBEN` noetig
- Anhang: Fiktives Kandidaten-Profil als PDF (WeasyPrint)

---

## DEEP-DIVE 3: Telegram + n8n Integration

### Telegram Bot (`app/services/telegram_bot_service.py`)
- **8 Commands:** /start, /hilfe, /status, /suche, /anruf, /aufgabe, /email, /notiz
- **Intent-Klassifikation:** GPT-4o-mini erkennt 9 Intents aus Freitext:
  - ANRUF_LOG, AUFGABE_ERSTELLEN, PERSON_SUCHEN, STATUS_ABFRAGE
  - EMAIL_SENDEN, NOTIZ_SPEICHERN, KALENDER, HILFE, UNBEKANNT
- **Anruf-Logging:** 13-Feld-Extraktion aus natuerlichem Text:
  - Firma, Kontakt, Typ, Richtung, Dauer, Ergebnis, Notizen, Action-Items, Stimmung
- **Person-Suche:** Sucht in Candidates + Companies + Contacts gleichzeitig
- **Voice-Messages:** Whisper Transkription → Intent-Erkennung → Aktion
- **Kalender-Integration:** Google Calendar API (nicht implementiert, nur Stub)

### N8nNotifyService (`app/services/n8n_notify_service.py`)
- App → n8n: HTTP POST an n8n Webhook-URL
- Events: `new_match`, `csv_import_done`, `outreach_sent`, `pipeline_stage_changed`
- Bearer Token Auth (N8N_WEBHOOK_SECRET in ENV)
- Fire-and-forget mit 5s Timeout + Retry(2)

### n8n → App Webhooks (`app/api/routes_n8n_webhooks.py`)
- POST `/api/n8n/trigger-matching` — Matching starten
- POST `/api/n8n/trigger-classification` — Klassifizierung starten
- POST `/api/n8n/update-job-status` — Job-Status aendern
- POST `/api/n8n/create-todo` — Todo anlegen
- POST `/api/n8n/send-notification` — Telegram-Nachricht senden
- Auth: Bearer Token im Header (`X-N8N-Auth`)

### RELEVANZ FUER AKQUISE:
- N8nNotifyService fuer Wiedervorlagen-Trigger (`acquisition_callback_due`)
- n8n Webhook fuer Cron-basierten CSV-Import (`trigger-acquisition-import`)
- Telegram-Bot erweitern: `/akquise` Command fuer Quick-Status
- Anruf-Logging kann fuer Akquise-Calls adaptiert werden

---

## DEEP-DIVE 4: Company + Contact + Job Services

### CompanyService (`app/services/company_service.py`)
- `get_or_create_by_name(name, city)`:
  1. Exact Match: `WHERE LOWER(name) = LOWER(input) AND LOWER(city) = LOWER(input)`
  2. Kein Match → Neue Company anlegen
  3. **Blacklist-Check:** Wenn `company.status = 'blacklist'` → Skip beim Import
- `update_company()`: Merge-Logik — nur leere Felder ueberschreiben, nie vorhandene
- `search_companies(query)`: ILIKE auf name + city + domain
- **Kein Fuzzy-Matching** ("Mueller" vs "Müller" = 2 Companies)

### CompanyContactService (`app/services/company_contact_service.py`)
- `get_or_create_contact(company_id, first_name, last_name, ...)`:
  1. Match: `WHERE company_id = X AND LOWER(first_name) = Y AND LOWER(last_name) = Z`
  2. Kein Match → Neuer Contact, `contact_number` auto-increment
  3. **Update bei Match:** Nur leere Felder auffuellen (E-Mail, Telefon, Position)
- `list_contacts(company_id)`: Alle Kontakte einer Firma
- **Kein Title-Feld** auf CompanyContact → Muss fuer AP-Anzeige Titel hinzugefuegt werden

### CSV Import Pipeline (`app/services/csv_import_service.py`)
- **Spalten-Mapping:** Hartcodiert (NICHT dynamisch), erwartet bestimmte Header
- **Batch-Size:** 50 Zeilen pro DB-Transaktion (Railway 30s Timeout)
- **`_clean_job_text()`:** Entfernt HTML-Tags, normalisiert Whitespace
- **Content-Hash:** SHA256 ueber (company_name + position + city + postal_code + job_url)
- **Duplikat-Logik:**
  1. Hash existiert → `expires_at` = NOW + 30d, `last_updated_at` = NOW → Skip
  2. Hash neu → Insert Job + get_or_create Company + get_or_create Contact
- **Vorschau:** `import_preview()` → Stadt-Analyse (Jobs vs. Kandidaten pro Stadt)

### CSVValidator (`app/services/csv_validator.py`)
- Encoding-Erkennung (chardet), Delimiter-Erkennung (csv.Sniffer)
- Header-Validierung: Pflichtfelder pruefen
- Zeilen-Limit: `CSV_MAX_ROWS = 10000`
- Content-Hash-Berechnung: `_compute_hash(row_dict)`

### RELEVANZ FUER AKQUISE:
- **EIGENER Parser noetig:** advertsdata CSV hat 29 Spalten vs. Standard-CSV
- `get_or_create_by_name` + `get_or_create_contact` koennen 1:1 wiederverwendet werden
- Blacklist-Check ist bereits eingebaut
- Batch-Size 50 ist bewaehrt fuer Railway
- Neues Feld `title` auf CompanyContact fuer AP-Anzeige (Spalte "Titel")
- Content-Hash-Logik kann auf `anzeigen_id` umgestellt werden (viel zuverlaessiger)

---

## DEEP-DIVE 5: Config + main.py + Hilfsdienste

### Environment Variables (`app/config.py`, 23 Variablen)
- **DB:** DATABASE_URL, DB_POOL_SIZE(5), DB_MAX_OVERFLOW(10), DB_POOL_RECYCLE(300)
- **API-Keys:** OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_MAPS_API_KEY, API_KEY
- **E-Mail:** MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET, MS_FROM_EMAIL
- **n8n:** N8N_WEBHOOK_URL, N8N_WEBHOOK_SECRET
- **Telegram:** TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
- **Limits:** CSV_MAX_ROWS(10000), DEFAULT_RADIUS_KM(25), MAX_CONCURRENT_MATCHING(3)

### Middleware-Stack (`main.py`)
1. **CORSMiddleware** — allow_origins=["*"] (Development)
2. **AuthMiddleware** — API-Key Check (`X-API-Key` Header), Whitelist fuer Health+Pages
3. **SecurityHeadersMiddleware** — X-Frame-Options, CSP, HSTS
4. **RequestIDMiddleware** — UUID pro Request, in Logs verfuegbar

### HTML-Seiten-Routes (`app/api/routes_pages.py`)
- `/` → Dashboard
- `/jobs` → Job-Liste
- `/kandidaten` → Kandidaten-Liste
- `/unternehmen` → Unternehmen-Liste
- `/match-center` → Match Center (v4/v5)
- `/action-board` → Action Board (Claude Matching)
- `/einstellungen` → System-Einstellungen
- **Fuer Akquise:** `/akquise` Route muss hier registriert werden

### CV-Parser (`app/services/cv_parser_service.py`)
- PDF-Upload → Text-Extraktion (pdfplumber) → GPT-4o-mini Parsing
- Output: Strukturierte Kandidaten-Daten (work_history, education, skills)
- **NICHT relevant fuer Akquise** (keine CVs bei Kaltakquise)

### Skill-Configs (`app/config/`)
- `skill_weights.json`: 6 Rollen × Skill-Gewichtungen
- `skill_hierarchy.json`: FiBu, BiBu, LohnBu, StFA Hierarchien
- `role_compatibility.json`: Welche Rollen zusammenpassen
- **NICHT relevant fuer Akquise** (kein Matching in Phase 1)

### RELEVANZ FUER AKQUISE:
- AuthMiddleware Whitelist muss `/akquise` aufnehmen (HTML-Seite)
- Neue Route `/akquise` in routes_pages.py
- Environment: Keine neuen ENV-Variablen noetig (alle Services existieren)
- RequestID nuetzlich fuer Akquise-Audit-Trail

---

## DEEP-DIVE 6: Routes (Matching, Smart Match, Hotlisten, etc.)

### Claude Matching Routes (`app/api/routes_claude_matching.py`, 22 Endpoints)
- POST `/api/v4/claude-match/run` — Background-Task, Concurrent-Lock
- GET `/api/v4/claude-match/status` — Live-Fortschritt (JSON)
- GET `/api/v4/claude-match/daily` — Heutige Ergebnisse
- POST `/api/v4/claude-match/{id}/action` — vorstellen/spaeter/ablehnen
- 8 Debug-Endpoints: match-count, stufe-0-preview, job-health, candidate-health, etc.
- **Pattern:** Background-Task + Status-Polling (NICHT SSE)

### Matching V2 Routes (`app/api/routes_matching_v2.py`, 32 Endpoints)
- Profiling, Embeddings, Scoring, Drive-Time als eigene Endpoint-Gruppen
- Batch-Operationen mit Background-Tasks + Status-Endpoints
- **Drive-Time Backfill:** Eigene Session pro Job (Railway-Pattern)

### Distance Matrix Service (`app/services/distance_matrix_service.py`)
- **Singleton-Pattern:** Einmalig initialisiert beim App-Start
- **PLZ-Cache:** Dict[Tuple[plz_a, plz_b], Tuple[car_min, transit_min]]
- **Batch-API:** Bis zu 25 Origins × 25 Destinations pro Call
- `batch_drive_times(origin_coords, destinations[])` → Dict[dest_id, (car, transit)]

### Call Transcription Service (`app/services/call_transcription_service.py`)
- **2-Stufen-Pipeline:**
  1. Whisper API: Audio → Transkript-Text
  2. GPT-4o-mini: Text → Strukturierte Felder (13 Felder)
- `transcribe_call(audio_file)` → `{transcript, summary, action_items, sentiment, ...}`
- **NICHT fuer Akquise nutzbar** — Akquise hat keine Audio-Aufnahmen (nur Notizen)

### V5 Matching Service (`app/services/v5_matching_service.py`)
- **3-Phasen-Engine:**
  1. PostGIS Vorfilter (Luftlinie ≤ 30km)
  2. Rollen-Kompatibilitaet (primary_role Match)
  3. Claude Bewertung (Score, Empfehlung, WOW-Faktor)
- Ersetzt alle frueheren Versionen (V2-V4)
- **NICHT relevant fuer Akquise Phase 1** (Matching kommt spaeter)

### Smart Match Routes (`app/api/routes_smart_match.py`)
- Ad-hoc Matching fuer einzelne Kandidaten/Jobs
- `POST /api/smart-match/candidate/{id}` → Beste Jobs finden
- `POST /api/smart-match/job/{id}` → Beste Kandidaten finden

### Hotlisten Routes (`app/api/routes_hotlisten.py`)
- Kategorisierte Listen (nach Stadt, Rolle, Titel)
- Auto-generiert aus classification_data
- **Nuetzlich fuer Akquise:** "Passende Kandidaten" pro Job schnell finden

### RELEVANZ FUER AKQUISE:
- Claude Matching Pattern (Background-Task + Status-Polling) als Vorlage fuer Import
- Distance Matrix fuer spaetere Geo-Erweiterung
- Hotlisten fuer "Traum-Kandidat finden" bei E-Mail-Generierung (Phase 2+)
- Smart Match fuer Akquise → ATS Uebergang (qualifizierter Lead → passende Kandidaten)

---

---

# VERTRIEBS-REVIEW (27.02.2026)

> **1 Vertriebsingenieur-Agent (8+ Jahre Kaltakquise-Erfahrung)**
> Bewertet den Plan aus der Perspektive von 50-80 Anrufen pro Tag.

## Bewertung

| Bereich | Note | Kritischste Luecke |
|---------|------|--------------------|
| Fliessband-Modus | 9/10 | "Start"-Button + "Weitermachen wo ich war" fehlt |
| Dispositionen | 7/10 | 3 Szenarien fehlen (Mailbox, AP weg, andere Stelle) |
| Call-Screen | 7/10 | Firma-Kontext + Textbausteine + Call-Historie fehlen |
| E-Mail-Strategie | 8/10 | Follow-up-E-Mail + Timing fehlt |
| Tab-Struktur | 6/10 | "In Bearbeitung" unklar, Firmen-Gruppierung fehlt |
| Priorisierung | 5/10 | Zu simpel, AP-Vorhanden und Senioritaet fehlen |
| Edge Cases | 5/10 | 4 haeufigste nicht abgedeckt |
| Test-Modus | 10/10 | Perfekt |

## Alle Verbesserungen (eingearbeitet in PLAN.md)

1. **13 Dispositionen** statt 10 (+Mailbox, +AP weg, +Andere Stelle, +Weiterverbunden)
2. **Uhrzeit bei Wiedervorlagen** (Pflichtfeld)
3. **Firmen-Gruppierung** mit Batch-Disposition
4. **Bestandskunden-Erkennung** (Badge + Hinweis)
5. **6-Faktoren-Priorisierung** (Branche, AP, Groesse, Alter, Senioritaet, Bekannt)
6. **Call-Screen erweitert** (Firma-Kontext, Historie, Textbausteine, Copy-Button, Neuer-AP)
7. **Frage 2 umformuliert** ("Wie besetzen Sie?" statt "Ext. Dienstleister?")
8. **E-Mail-Delay 2h** statt sofort
9. **Follow-up-E-Mail** nach 5-7 Tagen
10. **Kein Auto-Blacklist** bei Nicht-Erreichen
11. **Tab "Wiedervorlagen"** statt "In Bearbeitung"
12. **"Abtelefonieren starten" Button** + Zustandserhaltung

---

---

---

# MARKETING-REVIEW (27.02.2026)

> **3 Marketing-Review-Agenten parallel:**
> Lifecycle Marketing Manager, Email Marketing Manager, CRM/Automation Manager

---

## LIFECYCLE MARKETING MANAGER — 22 Massnahmen

### Bewertung

| Bereich | Note | Kritischste Luecke |
|---------|------|--------------------|
| Lifecycle-Abdeckung | 6/10 | Nur 2 Touchpoints (Anruf + 1 E-Mail), kein Nurturing |
| Dead-End-Behandlung | 4/10 | blacklist_weich, followup_abgeschlossen = Sackgasse |
| Conversion-Tracking | 5/10 | Keine Reply-Detection, kein Engagement-Score |
| Funnel-Sichtbarkeit | 3/10 | Kein Dashboard fuer Lifecycle-Phasen |
| ATS-Uebergang | 6/10 | Qualifizierungsdaten ja, aber Akquise-Dossier fehlt |

### P1 Massnahmen (eingearbeitet in PLAN.md)

1. **D3 falsche_nummer → "kontakt_fehlt"** statt "verloren" — Lead lebt, nur Kontakt fehlt
2. **Reply-Detection** via Microsoft Graph — Auto-Status "kontaktiert" bei Antwort
3. **Akquise-Dossier bei ATS-Konvertierung** — Alle Calls/E-Mails/Notizen werden uebertragen
4. **Break-up-Email** als 3. Touchpoint (FOMO: "Kandidat hat andere Angebote...")
5. **Abmelde-Endpoint** oeffentlich (DSGVO) → Sofort-Blacklist bei Klick
6. **akquise_status_changed_at** Timestamp fuer Time-to-First-Contact Metrik

### P2 Massnahmen (spaetere Phase)

7. Engagement-Score dynamisch (Call-Versuche, E-Mail-Opens, Replies = hoeher)
8. Nurturing-Sequenz fuer blacklist_weich (nach 180 Tagen: "Wir haben neue Kandidaten...")
9. Funnel-Dashboard: Leads pro Status-Phase als Balkendiagramm
10. Win-back fuer followup_abgeschlossen (nach 90 Tagen: Re-Aktivierung)
11. Retention-Phase: Nach ATS-Konvertierung weiter pflegen (Feedback, Nachbesetzung)
12. Lead-Scoring-Modell: Branche + Firmengroesse + Interaktion = Score

### P3 Massnahmen (Nice-to-have)

13-22. Multi-Channel (LinkedIn), Referral-Programm, Content-Marketing-Trigger, Predictive Scoring, A/B-Testing, Customer-Success-Handoff, NPS nach Placement, Re-Engagement-Kampagnen, Industry-Events-Integration, Competitor-Intelligence

---

## EMAIL MARKETING MANAGER — 17 Empfehlungen

### Bewertung

| Bereich | Note | Kritischste Luecke |
|---------|------|--------------------|
| Zustellbarkeit | 3/10 | Keine SPF/DKIM/DMARC, kein Domain-Warmup |
| E-Mail-Format | 5/10 | HTML statt Plain-Text fuer Kaltakquise |
| Mailbox-Strategie | 4/10 | Keine Zweck-Zuordnung, keine Limits |
| Sequenz-Management | 6/10 | Nur 2 E-Mails, kein Thread-Linking |
| Bounce-Handling | 2/10 | Komplett fehlend |
| Reply-Management | 2/10 | Keine Detection |

### P1 Empfehlungen (eingearbeitet in PLAN.md)

1. **Plain-Text statt HTML** fuer Kaltakquise (hoehere Zustellbarkeit)
2. **SPF/DKIM/DMARC** fuer alle 3 Domains BEVOR Go-Live
3. **Domain-Warmup** 2 Wochen (IONOS: 5→20→50/Tag)
4. **Mailbox-Strategie nach Zweck:**
   - sincirus-karriere.de → Erst-Mails
   - m.hamdard@sincirus-karriere.de → Follow-ups
   - m.hamdard@jobs-sincirus.com → Break-up-Mails
   - hamdard@sincirus.com → Haupt/Antworten
   - hamdard@jobs-sincirus.com → Reserve
5. **Tages-Limit pro Mailbox** (20/Tag IONOS, 100/Tag M365)
6. **Thread-Linking** (In-Reply-To Header bei Follow-ups)
7. **`from_email` + `parent_email_id` + `sequence_position`** auf acquisition_emails
8. **Bounce-Handling** via n8n + Microsoft Graph NDR-Check
9. **Break-up-Email** als 3. E-Mail in Sequenz (Tag 14-17)
10. **Abmelde-Link** mit Token (oeffentlich, kein Auth)

### P2 Empfehlungen (spaetere Phase)

11. A/B-Testing Infrastruktur (Betreff-Varianten)
12. Send-Time-Optimization (ML-basiert)
13. Spam-Score-Check vor Versand (SpamAssassin)
14. E-Mail-Template-Bibliothek (5+ Varianten pro Branche)
15. Engagement-Heatmap (wann werden E-Mails geoeffnet?)
16. Blacklist-Monitoring (MXToolbox API)
17. Sender-Reputation-Dashboard

---

## CRM/AUTOMATION MANAGER — 17 Empfehlungen

### Bewertung

| Bereich | Note | Kritischste Luecke |
|---------|------|--------------------|
| Datenintegritaet | 6/10 | Kein Akquise-Job-Guard im Matching |
| State-Management | 5/10 | Keine State-Machine-Validierung |
| Blacklist-Logik | 6/10 | Keine Cascade bei nie_wieder |
| Re-Import-Logik | 4/10 | Kein Status-aware Re-Import |
| Audit/Compliance | 5/10 | Kein Import-Rollback, recording_consent Default falsch |
| Automatisierung | 7/10 | n8n-Workflows gut geplant |

### P1 Empfehlungen (eingearbeitet in PLAN.md)

1. **Akquise-Job-Guard** im Matching: `WHERE acquisition_source IS NULL`
2. **State-Machine-Validierung** fuer akquise_status Uebergaenge
3. **Blacklist-Cascade** bei nie_wieder: ALLE Stellen der Firma schliessen
4. **Re-Import Status-aware:** blacklist_hart = nie reaktivieren, qualifiziert = nicht zuruecksetzen
5. **recording_consent Default → false** (Ausnahme, nicht Regel)
6. **Import-Batch-ID** fuer Rollback bei fehlerhaftem Import
7. **Abmelde-Endpoint** oeffentlich (Sofort-Blacklist)

### P2 Empfehlungen (spaetere Phase)

8. DSGVO-Loeschendpoint (alle Daten einer Firma inkl. Calls/E-Mails entfernen)
9. Audit-Trail-Tabelle (wer hat wann was geaendert)
10. Contact-Job Direct FK (statt nur ueber Company)
11. Status-History als JSONB Array auf Job (alle Uebergaenge mit Timestamp)
12. Webhook-basierte Eskalation statt Cron-Polling
13. Rate-Limiter fuer E-Mail-Generierung (GPT-Kosten deckeln)
14. Archiv-Retention-Policy (alte Leads nach 1 Jahr loeschen?)
15. Import-Validation-Report als PDF (was wurde importiert, was uebersprungen)
16. Multi-User-Readiness (Felder fuer assigned_to, team)
17. API-Versionierung (/api/akquise/v1/) fuer Zukunftssicherheit

---

## ZUSAMMENFASSUNG: Was wurde in PLAN.md eingearbeitet?

### Aus allen 3 Reviews (P1-Aenderungen):

| Aenderung | Quelle | Phase |
|-----------|--------|-------|
| D3 → kontakt_fehlt statt verloren | Lifecycle | 2 |
| Reply-Detection (Microsoft Graph) | Lifecycle + Email | 5 + 6 |
| Akquise-Dossier bei ATS-Konvertierung | Lifecycle | 2 |
| Break-up-Email (3. Touchpoint) | Lifecycle + Email | 5 |
| Abmelde-Endpoint (oeffentlich) | Lifecycle + CRM | 3 + 5 |
| akquise_status_changed_at | Lifecycle | 1 |
| Plain-Text E-Mails | Email | 5 |
| SPF/DKIM/DMARC + Domain-Warmup | Email | Deployment |
| Multi-Mailbox (5 Postfaecher) + Dropdown | Email + User | 5 + 4 |
| Tages-Limit pro Mailbox | Email | 5 |
| Thread-Linking (In-Reply-To) | Email | 5 |
| from_email + parent_email_id + sequence_position | Email | 1 |
| Bounce-Handling (n8n) | Email | 6 |
| Akquise-Job-Guard (Matching) | CRM | 1 |
| State-Machine-Validierung | CRM | 1 |
| Blacklist-Cascade | CRM | 1 |
| Re-Import Status-aware | CRM | 2 |
| recording_consent Default false | CRM | 1 |
| Import-Batch-ID + Rollback | CRM | 1 + 2 |

---

## NAECHSTE SCHRITTE

Recherche + Deep-Dive + Vertriebs-Review + Marketing-Review abgeschlossen.
Plan ist in PLAN.md (9 Phasen + Pre-Launch Domain-Warmup).
**22 Agenten total:** 6 Research + 6 Review + 6 Deep-Dive + 1 Vertriebsingenieur + 3 Marketing.
Bereit fuer Implementierung ab Phase 0 (Domain-Warmup) / Phase 1 (Migration 032).
