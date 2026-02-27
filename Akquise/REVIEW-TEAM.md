# Akquise-Automatisierung — Review-Team Ergebnisse

> **Stand: 27.02.2026 — 6 Rollen-Agenten parallel**
> Jeder Agent hat unabhaengig den gesamten Kontext geprueft.

---

## 1. SOLUTION ARCHITECT / TECHNICAL LEAD

### 1.1 Backend (MT) vs. n8n — Aufgabenteilung

**MT-Backend (FastAPI):** Alles mit UI, DB-Zugriff, Echtzeit-Interaktion:
- Akquise-CSV-Import (eigener Parser, NICHT der bestehende)
- Akquise-Seite mit Tabs, Filterung, Anruf-Screen
- Disposition + Qualifizierungsfragen
- GPT-E-Mail-Generierung + Vorschau
- Rueckruf-Zuordnung (Telefonnummer-Lookup)
- Uebergang Job zu ATSJob ("Qualifizierung abschliessen")

**n8n:** Zeitgesteuert, asynchron, extern getriggert:
- Taeglicher CSV-Download von advertsdata.com (Cron, 06:00)
- CSV-Upload via Webhook an MT-Backend
- Wiedervorlagen-Reminder (taeglich scannen, Telegram/Push)
- Eskalations-Logik (3x nicht erreicht → Auto-E-Mail)
- E-Mail-Versand via Microsoft Graph (MT generiert, n8n sendet)
- Reporting-Aggregation (Tages-KPIs nach Telegram)

### 1.2 Datenbank — Bestehende Tabellen erweitern + 1 neue Tabelle

**Erweiterungen (Migration 032):**
- **Job:** +`acquisition_source` (String 20), +`position_id` (String 50), +`anzeigen_id` (String 50), +`akquise_status` (Enum), +`akquise_priority` (Integer 1-5), +`first_seen_at`, +`last_seen_at`
- **CompanyContact:** +`source` (String 20), +`contact_role` (String 20: "firma"/"anzeige"), +`phone_normalized` (String 20, E.164, Index)
- **Company:** +`acquisition_status` (String 20: "prospect"/"active_lead"/"customer"/"blacklist")

**NEUE Tabelle: `acquisition_calls`:**
- `id` (UUID PK), `job_id` (FK), `contact_id` (FK), `company_id` (FK)
- `call_type` (Enum: erstanruf/wiedervorlage/rueckruf)
- `disposition` (Enum: erreicht/nicht_erreicht/qualifiziert/kein_bedarf/wiedervorlage)
- `qualification_data` (JSONB — die 17 Fragen als Key-Value)
- `notes` (Text), `duration_seconds` (Integer)
- `follow_up_date` (DateTime), `follow_up_time` (String 5)
- `email_sent` (Boolean), `email_draft_id` (FK)
- `created_at`, `updated_at`

### 1.3 Deduplizierung — Zweistufig

**Primaer: `anzeigen_id` (UNIQUE, WHERE acquisition_source = 'advertsdata')**
- Bei Duplikat: `last_seen_at` + `expires_at` auffrischen, Kontaktdaten updaten
- Content-Hash als Fallback fuer manuelle Imports

**Zeitfenster-Regel:**
- Gleiche `anzeigen_id` seit >90 Tagen nicht gesehen → neuer Import (Reset Status)
- `first_seen_at` bleibt, `last_seen_at` wird aktualisiert

### 1.4 API-Endpoints (Prefix: /api/akquise)

| Methode | Pfad | Funktion |
|---------|------|----------|
| POST | `/import` | CSV-Import (Background-Task) |
| GET | `/import/status` | Import-Fortschritt |
| GET | `/leads` | Akquise-Liste mit Filtern |
| GET | `/leads/{job_id}` | Lead-Detail |
| PATCH | `/leads/{job_id}/status` | Status aendern |
| POST | `/leads/{job_id}/call` | Anruf protokollieren |
| POST | `/leads/{job_id}/email` | GPT-E-Mail generieren (Vorschau) |
| POST | `/leads/{job_id}/email/send` | E-Mail senden |
| POST | `/leads/{job_id}/qualify` | Job → ATSJob konvertieren |
| GET | `/wiedervorlagen` | Heutige Wiedervorlagen |
| GET | `/stats` | Tages-KPIs |
| GET | `/rueckruf/{phone}` | Telefonnummer-Lookup |

### 1.5 ATS-Integration

Wenn `akquise_status = qualifiziert`:
1. ATSJob mit `source = "akquise"`, `source_job_id = job.id` erstellen
2. Qualifizierungsdaten kopieren (Budget, Software, Team etc.)
3. Classification-Pipeline triggern
4. Claude-Matching aktivieren
5. Call-Historie bleibt ueber `acquisition_calls.job_id` erreichbar

### 1.6 Skalierbarkeit bei 1000+ Zeilen/Tag

- CSV: Batch-Insert 50er Chunks, Company-Cache, Upsert → <30s
- Lead-Priorisierung: `akquise_priority` berechnet aus Branche, Groesse, Region
- GPT-E-Mails: Semaphore(3), bei 200/Tag ≈ 10 Min
- DB: Index auf `(acquisition_source, akquise_status, akquise_priority)`

---

## 2. DEVOPS / PLATFORM ENGINEER

### 2.1 Railway DB-Session-Management (RISIKO: HOCH)

- Bestehender CSVImportService haelt eine Session fuer gesamten Import → 30s Timeout!
- **Loesung:** Batch-Insert in 50er Chunks mit eigener Session pro Chunk
- Externe API-Calls (Geocoding, Klassifizierung) MUESSEN das bewiesene Muster nutzen
- Bestehender `pool_size=5, max_overflow=10` reicht

### 2.2 n8n Reliability (RISIKO: MITTEL)

- N8nNotifyService: nur 10s Timeout, kein Retry
- **Loesung:** n8n-seitig Retry-Policy (3x, exponentieller Backoff)
- Idempotenz-Keys mitschicken (event_id = UUID)
- `failed_webhooks`-Tabelle als DLQ, Cron alle 5 Min
- n8n Error-Trigger → Telegram-Alert

### 2.3 DB-Indizes (RISIKO: HOCH bei Phone-Lookup)

Benoetigte neue Indizes:
- `idx_contacts_phone_normalized ON company_contacts (phone_normalized)` — Rueckruf <500ms
- `idx_jobs_anzeigen_id ON jobs (anzeigen_id) WHERE anzeigen_id IS NOT NULL` — Dedup
- `idx_jobs_position_id ON jobs (position_id) WHERE position_id IS NOT NULL`
- `idx_jobs_acquisition_source ON jobs (acquisition_source)` — Filter
- `idx_jobs_expires_at ON jobs (expires_at) WHERE expires_at IS NOT NULL`

### 2.4 Rate-Limits

| Provider | Limit | Empfehlung |
|----------|-------|------------|
| OpenAI Whisper | 50 RPM | Queue + Semaphore(2) |
| OpenAI GPT-4o-mini | 500 RPM | Unkritisch |
| Anthropic Haiku | 4000 RPM | Nicht gleichzeitig mit Matching |
| Google Maps | 3000 Elem/Min | PLZ-Cache nutzen |
| Webex | 500 RPM | Unkritisch |
| Microsoft Graph | 10.000/10 Min | Unkritisch |

Zentraler `RateLimitRegistry`-Service empfohlen.

### 2.5 Logging/Tracing

Strukturiertes JSON-Logging via `python-json-logger`:
- CSV-Import: `{import_id, rows_total/new/duplicate/failed, duration_s}`
- Calls: `{call_id, contact_id, company_id, duration_s, disposition}`
- E-Mails: `{email_id, contact_id, template_type, send_status}`
- Fehler: `{service, operation, entity_id, error_type}`

### 2.6 "2-Sekunden-Popup" bei Rueckruf (RISIKO: HOCH)

Gesamtlatenz realistisch: 500ms-1.5s normal, ABER:
- Railway Cold-Start: +3-5s
- Ohne Phone-Index: >1s Query
- **SLA:** 2s in 80%, 5s in 95% der Faelle
- **Fallback:** Telegram-Notification wenn >3s

### 2.7 Deployment-Strategie (4 Phasen)

- **Phase A:** DB-Migration (nur neue Felder, NULLABLE, kein Code)
- **Phase B:** Backend-Services (hinter `/api/acquisition/`, Feature-Flag)
- **Phase C:** n8n-Workflows (Webex-Webhook, Rueckruf-Lookup)
- **Phase D:** Frontend (Akquise-Seite, nach 1 Woche Testbetrieb)
- **Rollback:** Feature-Flag `acquisition_enabled` in `system_settings`

---

## 3. SECURITY EXPERT

### 3.1 DSGVO — Rechtsgrundlage

- **Art. 6 Abs. 1 lit. f (Berechtigtes Interesse)** fuer advertsdata-Daten
- Daten stammen aus oeffentlichen Stellenanzeigen
- **Pflicht:** Interessenabwaegung dokumentieren, Verarbeitungsverzeichnis
- **Art. 14 DSGVO:** Betroffene bei Erstkontakt informieren (Daten nicht direkt erhoben)

### 3.2 Kaltakquise — Telefon vs. E-Mail

| Kanal | Erlaubt? | Begruendung |
|-------|----------|-------------|
| Telefon (B2B) | JA | § 7 Abs. 2 Nr. 1 UWG, mutmassliche Einwilligung bei sachlichem Bezug |
| E-Mail (B2B) | NUR mit Einwilligung | § 7 Abs. 2 Nr. 3 UWG verlangt ausdrueckliche Einwilligung, auch B2B |

**KRITISCH:** GPT-generierte E-Mails als Kaltmail ohne vorherige Einwilligung = Abmahngefahr!
→ E-Mails NUR senden, wenn im Telefonat zugestimmt ("Schicken Sie mir gerne Infos")
→ Einwilligung im CRM dokumentieren (Disposition-Feld)

### 3.3 Anrufaufzeichnung (§ 201 StGB)

- Aufzeichnung OHNE Einwilligung ALLER Teilnehmer = STRAFBAR (bis 3 Jahre)
- **Pflicht:** Muendliche Einwilligung zu Gespraechsbeginn + Dokumentation im CRM
- **Boolean-Feld:** `recording_consent` — Recording erst NACH Einwilligung aktivieren
- Bei Ablehnung: Recording stoppen, Gespraech trotzdem fuehren

### 3.4 Loeschkette (Recording)

```
Recording → Transkription → Extraktion → Loeschung → Loeschlog
```

- **Retry:** 3 Versuche mit Backoff
- **Dead Letter Queue:** `deletion_queue` Tabelle (pending/failed/completed)
- **Cron:** Alle 15 Min pruefen
- **Max Retention:** 72 Stunden ABSOLUT
- **Alarm:** Nach 3 Fehlern → Datenschutzbeauftragten benachrichtigen
- **Loeschlog:** 12 Monate aufbewahren

### 3.5 Aufbewahrungsfristen

| Datentyp | Frist |
|----------|-------|
| Kontaktdaten (Firma/AP) | Max. 3 Jahre nach letztem Kontakt |
| Call-Transkripte | Max. 6 Monate |
| Audio-Recordings | Max. 72 Stunden |
| E-Mail-Logs | 6 Monate |
| Opt-out/Blacklist | Unbegrenzt (Pflicht!) |

### 3.6 E-Mail-Pflichten

- Vollstaendige Signatur (§ 5 TMG / § 35a GmbHG)
- Abmelde-Link (DSGVO-Pflicht)
- Datenschutzerklaerung-Link
- Abmelde-Link muss technisch in Blacklist schreiben

### 3.7 Audit-Log (revisionssicher, append-only)

Protokollieren: CSV-Import, Anrufe, E-Mails, Loeschungen, Widersprueche, KI-API-Zugriffe

---

## 4. PRODUCT / BUSINESS ANALYST

### 4.1 Lead-Status-Kette

```
neu → anruf_geplant → angerufen → kontaktiert → qualifiziert → stelle_erstellt
                                                → wiedervorlage
                                                → blacklist_weich / blacklist_hart
                                                → verloren
```

### 4.2 Disposition-Matrix (10 Szenarien)

| # | Ergebnis | Status | Auto-Aktion | Naechster Schritt |
|---|----------|--------|-------------|-------------------|
| D1 | Nicht erreicht | angerufen | E-Mail (Template A) | Wiedervorlage +2 Tage |
| D2 | Besetzt/Mailbox | angerufen | Keine E-Mail | Wiedervorlage +1 Tag |
| D3 | Falsche Nummer | verloren | Archivieren | Kein Folgeschritt |
| D4 | Sekretariat blockt | angerufen | E-Mail an info@ | Wiedervorlage +3 Tage |
| D5 | Kein Bedarf aktuell | blacklist_weich | Notiz | Auto-Wiedervorlage +180 Tage |
| D6 | Nie wieder anrufen | blacklist_hart | Sperren | Kein Folgeschritt |
| D7 | Interesse, spaeter | wiedervorlage | Task erstellen | Am Wunschdatum |
| D8 | Will Infos per Mail | email_gesendet | E-Mail (Template C) | Wiedervorlage +5 Tage |
| D9 | Qualifiziert (Erst) | qualifiziert | Zweitkontakt-Task | Terminvereinbarung |
| D10 | Voll qualifiziert | stelle_erstellt | ATSJob konvertieren | Kandidatensuche |

### 4.3 Blacklist-Konzept

| Typ | Bei Import sichtbar? | Erneut kontaktierbar? |
|-----|---------------------|-----------------------|
| Weich | Ja, mit Warnung | Ja, nach 6 Monaten |
| Hart | Nein, uebersprungen | Nie |

### 4.4 Qualifizierungsfragen — Zwei-Stufen-Modell

**Erstkontakt (Cold Call, max 5 Fragen, 3-5 Min):**
1. Stelle noch offen?
2. Arbeiten Sie mit Personalberatern?
3. Groesste Herausforderung bei Besetzung?
4. Timeline?
5. Darf ich passende Profile senden?

**Zweitkontakt (15-20 Min, 12 weitere Fragen):**
Budget, Teamgroesse, Home-Office, Software, Anforderungen, Teamkultur, Entscheidungsprozess, Bewerberlage, Exklusivitaet, Konditionen, Ansprechpartner, naechster Schritt

### 4.5 Lead-Priorisierung (Anrufreihenfolge)

1. Wiedervorlagen faellig heute
2. Rueckrufe vereinbart
3. Follow-ups nach E-Mail
4. Neue Leads (hoher Score: Region <30km, Finance, 50-500 MA)
5. Neue Leads (mittlerer Score)
6. Rest chronologisch

### 4.6 Tages-KPIs

| Metrik | Ziel/Tag |
|--------|---------|
| Anrufe gesamt | 30-50 |
| Entscheider erreicht | 8-12 |
| Qualifiziert | 2-4 |
| E-Mails gesendet | 10-20 |
| Stellen erstellt | 1-2 |
| Conversion Rate | >5% |

### 4.7 Uebergang ATS

Qualifizierung = Statuswechsel, keine Datenmigration. Selber Job-Datensatz mit erweitertem Status + aktiviertem Matching.

---

## 5. UI/UX DESIGNER

### 5.1 Seitenstruktur — Tabs

- **Neue Leads** (Badge) — frisch importiert
- **Heute anrufen** (DEFAULT-Tab) — Wiedervorlagen + ueberfaellige
- **In Bearbeitung** — mindestens 1x angerufen
- **Qualifiziert** — Interesse bestaetigt
- **Abgeschlossen** — kein Handlungsbedarf

### 5.2 Listen-Spalten

Firma (fett) | Jobtitel (40 Zeichen) | Ansprechpartner | Stadt/PLZ | Letzter Kontakt (Disposition-Badge) | Naechste Aktion | Telefon-Icon | Mail-Icon

### 5.3 Call-Screen — Drei-Spalten-Layout

**Trigger:** Klick auf "Anrufen" → volle Seite wechselt in Call-Modus

**Oben:** Call-Bar (gruener Punkt + Firmenname + Timer + Auflegen-Button)

**Linke Spalte (35%):** Stellenausschreibung — Jobtitel als H2, Metadaten, scrollbarer Volltext

**Mittlere Spalte (35%):** Qualifizierung — Kontakt-Card (80px) oben, darunter Qualifizierungsfragen als Checkliste in 3-4 Gruppen

**Rechte Spalte (30%):** Aktionen — Notizen-Textarea (200px, Auto-Save 3s), Disposition-Buttons (4 Stueck, gestapelt, volle Breite), sekundaere Aktionen

### 5.4 Disposition-Flow

Klick "Nicht erreicht" → Button wechselt auf "Gespeichert" → HTMX POST → nach 1.5s Slide-Transition "Naechster Lead" → Weiter-Button → naechster Lead direkt im Call-Screen. **Kein Zurueck-zur-Liste noetig. Fliessband-Modus.**

### 5.5 E-Mail-Vorschau

Slide-Over von rechts (600px): Empfaenger + Betreff (editierbar) → Textarea mit GPT-Text (editierbar) → "Senden" (gruen) + "Abbrechen" + "Neu generieren"

### 5.6 Rueckruf-Popup

Centered Modal (480px, dunkler Overlay): Pulsierender Punkt + "Eingehender Anruf" → Firmenname (H2) → Kontaktperson → Jobtitel + Stadt → Letzte Disposition als Badge → "Annehmen + Call-Screen" (gruen) + "Ignorieren"

### 5.7 Nur Desktop (min. 1280px)

Kaltakquise = Schreibtisch. Drei-Spalten-Layout auf Mobile nicht darstellbar.

### 5.8 Informationshierarchie

**Primaer:** Firmenname + Jobtitel, Ansprechpartner, Disposition-Buttons
**Sekundaer:** Qualifizierungsfragen, Stellenausschreibung, Notizen
**Tertiaer:** Historie, Metadaten, sekundaere Aktionen

---

## 6. FRONTEND DEVELOPER

### 6.1 Template-Struktur

```
app/templates/akquise/
  akquise_page.html          — Hauptseite mit Tab-Navigation
  partials/
    tab_offen.html           — Tab "Offene Leads"
    tab_kontaktiert.html     — Tab "Kontaktiert"
    tab_rueckruf.html        — Tab "Rueckruf"
    tab_gewonnen.html        — Tab "Gewonnen/Abgelehnt"
    lead_row.html            — Einzelne Lead-Zeile
    call_screen.html         — Call-Screen Panel
    csv_import_dialog.html   — Upload + Vorschau
    csv_preview_table.html   — Vorschau nach Upload
    email_modal.html         — GPT-E-Mail Edit + Senden
    disposition_buttons.html — Status-Buttons
    rueckruf_popup.html      — Overlay
    filter_panel.html        — Akquise-Filter
```

### 6.2 Alpine.js Stores

```
akquise: {activeTab, selectedLeadId, callActive, filters, counts}
callScreen: {lead, notizen, fragenChecked, disposition, rueckrufDatum}
```

### 6.3 Kern-Entscheidungen

| Thema | Entscheidung | Begruendung |
|-------|-------------|-------------|
| Tabs | HTMX `hx-get` pro Tab | Performance bei vielen Leads |
| Call-Screen | Eingebettetes Split-Panel (kein Modal) | Persistenter State |
| Autosave | HTMX `delay:2s` Debounce | Kein Datenverlust |
| Telefonie | `tel:` Protocol Handler | Kein SDK-Lock |
| Rueckruf | SSE via HTMX-Extension | Unidirektional, native |
| CSV-Vorschau | Server-side Parse + Cache | Kein JS-Parser noetig |
| Disposition | `hx-post` + OOB-Swap | Liste + Panel synchron |
| Pagination | Server-side, 50/Seite | Kein Virtual Scroll |

### 6.4 Click-to-Call

`tel:` Protocol Handler als Primary (Webex registriert sich als Handler). Kein SDK, kein Backend-Call fuer den Anruf selbst. Nur Protokollierung danach.

### 6.5 Rueckruf-Erkennung

SSE Endpoint `/api/akquise/events` → HTMX SSE-Extension → Toast/Popup. Fallback: Polling alle 60s.

### 6.6 Performance

50 Rows/Seite, Server-side Pagination, HTMX-Search mit delay:300ms. Tab-Counts via HX-Trigger Response-Header. Archivierung nach 30 Tagen.
