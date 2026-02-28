# Akquise-Automatisierung — Finaler Implementierungsplan

> **Stand: 01.03.2026**
> Basierend auf: 6 Research + 6 Review + 6 Deep-Dive + 1 Vertriebsingenieur + 3 Marketing-Review-Agenten
> Quellen: RECHERCHE.md, REVIEW-TEAM.md
>
> **Implementierungs-Status:** Phase 1-9 FERTIG | 7 Gap-Fixes implementiert | Phase 7.4 Webex TEILWEISE FERTIG (Incoming-Call AKTIV, Recording braucht OAuth)
> **Deploy-Status (01.03.):** Migration 032 deployed, CSV-Import funktioniert, /akquise LIVE | Phase 6 n8n-Workflows AKTIV (6/6 + 3 neue), Backend-Endpoints deployed + DB-Session-Fix
> **Gap-Fixes (01.03. Session 2):** D10→ATS auto-trigger, Intelligenter Default-Tab, Batch-Disposition UI, E-Mail 2h Delay (Migration 033), 3 n8n-Workflows (Webex, Scheduled-Emails, Auto-Followup)
> **Backend-Test (01.03.):** API-Endpoints + IONOS-Mailboxen getestet + bestaetigt | E2E-Checkliste (UI-Tests) noch OFFEN

---

## UEBERSICHT

Das Akquise-System automatisiert Milads taeglichen Vertriebsworkflow:
CSV-Import von Vakanzen → Kaltakquise-Anrufe → Qualifizierung → E-Mail-Follow-up → ATS-Pipeline.

**Ziel:** Null manuelles Copy-Paste. Alles wird automatisch im MT eingetragen, dokumentiert und nachverfolgt.

---

## PHASE 1: Datenbank-Migration (032) ✅ FERTIG + DEPLOYED (28.02.2026)
> **Deploy-Hinweis:** alembic_version war falsch auf '033'. Fix: Version auf '031' zurueckgesetzt, SQL manuell ausgefuehrt. UNIQUE Index `ix_jobs_anzeigen_id` gedroppt (CSV hat Duplikat-anzeigen_ids).

### 1.1 Bestehende Tabellen erweitern

**Job-Tabelle (+9 Felder):**
- `acquisition_source` (String 20, nullable) — "advertsdata" oder NULL (bestehende Jobs)
- `position_id` (String 50, nullable) — Externe Position-ID von advertsdata
- `anzeigen_id` (String 50, nullable) — Externe Anzeigen-ID (Primaer-Key fuer Dedup)
- `akquise_status` (String 30, nullable) — neu/angerufen/kontaktiert/qualifiziert/wiedervorlage/email_gesendet/email_followup/kontakt_fehlt/stelle_erstellt/blacklist_weich/blacklist_hart/verloren/followup_abgeschlossen
- `akquise_status_changed_at` (DateTime, nullable) — Letzter Status-Wechsel (fuer Metriken + State-Machine)
- `akquise_priority` (Integer, nullable) — 0 (hoechste) bis 10 (niedrigste), berechnet
- `first_seen_at` (DateTime, nullable) — Erstmalig in CSV gesehen
- `last_seen_at` (DateTime, nullable) — Zuletzt in CSV gesehen
- `import_batch_id` (UUID, nullable) — Batch-ID des Imports (fuer Rollback bei fehlerhaftem Import)

**Company-Tabelle (+1 Feld):**
- `acquisition_status` (String 20, nullable, default "prospect") — prospect/active_lead/customer/blacklist

**CompanyContact-Tabelle (+3 Felder):**
- `source` (String 20, nullable) — "advertsdata" oder "manual"
- `contact_role` (String 20, nullable) — "firma" (AP Firma) oder "anzeige" (AP Anzeige)
- `phone_normalized` (String 20, nullable) — E.164 Format (+491701234567), fuer Rueckruf-Lookup

### 1.2 Neue Tabelle: `acquisition_calls`

Dokumentiert jeden Akquise-Anruf mit Disposition und Qualifizierungsdaten.

| Feld | Typ | Beschreibung |
|------|-----|--------------|
| `id` | UUID PK | Primaerschluessel |
| `job_id` | FK jobs (SET NULL) | Zu welchem Lead/Job gehoert der Call |
| `contact_id` | FK company_contacts (SET NULL) | Ansprechpartner |
| `company_id` | FK companies (SET NULL) | Unternehmen |
| `call_type` | String 20 | erstanruf / wiedervorlage / rueckruf |
| `disposition` | String 30 | nicht_erreicht / besetzt / sekretariat / erreicht_kein_bedarf / erreicht_interesse / erreicht_qualifiziert / falsche_nummer / nie_wieder |
| `qualification_data` | JSONB | Fragen-Antworten als Key-Value |
| `notes` | Text | Freitext-Notizen |
| `duration_seconds` | Integer | Anrufdauer |
| `recording_consent` | Boolean, default **false** | Aufzeichnung aktiv (Ausnahme, nicht Regel — §201 StGB) |
| `follow_up_date` | DateTime | Wiedervorlage-Datum |
| `follow_up_note` | String 500 | Wiedervorlage-Grund |
| `email_sent` | Boolean, default false | E-Mail nach Call gesendet? |
| `email_consent` | Boolean, default true | E-Mail erlaubt |
| `created_at` | DateTime (server_default now()) | Anrufzeitpunkt |

### 1.3 Neue Tabelle: `acquisition_emails`

Speichert alle gesendeten Akquise-E-Mails (zugeordnet zu Job + Contact + Company).

| Feld | Typ | Beschreibung |
|------|-----|--------------|
| `id` | UUID PK | |
| `job_id` | FK jobs (SET NULL) | Zu welchem Lead/Job |
| `contact_id` | FK company_contacts (SET NULL) | An wen gesendet |
| `company_id` | FK companies (SET NULL) | Welche Firma |
| `parent_email_id` | FK acquisition_emails (SET NULL) | Bezugs-E-Mail (fuer Follow-up/Break-up Thread-Linking) |
| `from_email` | String 500 | Absender-Adresse (welches Postfach) |
| `to_email` | String 500 | Empfaenger-Adresse |
| `subject` | String 500 | Betreff |
| `body_html` | Text | Kompletter E-Mail-Text (HTML) — NUR fuer Signatur |
| `body_plain` | Text | Klartext-Version (wird als Content gesendet) |
| `candidate_fiction` | JSONB | Der fiktive Kandidat (Daten die GPT sich ausgedacht hat) |
| `email_type` | String 20 | initial / follow_up / break_up |
| `sequence_position` | Integer, default 1 | 1=Erst-E-Mail, 2=Follow-up, 3=Break-up |
| `status` | String 20 | draft / sent / failed / bounced / replied |
| `sent_at` | DateTime | Wann tatsaechlich versendet |
| `graph_message_id` | String 255 | Microsoft Graph Message-ID (fuer Thread-Linking + Reply-Detection) |
| `unsubscribe_token` | String 64, UNIQUE | Zufaelliger Token fuer Abmelde-Link (pro E-Mail) |
| `created_at` | DateTime (server_default now()) | Entwurf erstellt |

### 1.4 Indizes

- `idx_jobs_anzeigen_id` ON jobs(anzeigen_id) WHERE anzeigen_id IS NOT NULL — Dedup-Lookup
- `idx_jobs_akquise_status` ON jobs(acquisition_source, akquise_status, akquise_priority) — Tab-Queries
- `idx_jobs_batch_id` ON jobs(import_batch_id) WHERE import_batch_id IS NOT NULL — Batch-Rollback
- `idx_contacts_phone_norm` ON company_contacts(phone_normalized) WHERE phone_normalized IS NOT NULL — Rueckruf
- `idx_acq_calls_job` ON acquisition_calls(job_id, created_at DESC) — Call-Historie
- `idx_acq_calls_followup` ON acquisition_calls(follow_up_date) WHERE follow_up_date IS NOT NULL — Wiedervorlagen
- `idx_acq_emails_job` ON acquisition_emails(job_id, created_at DESC) — E-Mail-Historie pro Lead
- `idx_acq_emails_parent` ON acquisition_emails(parent_email_id) WHERE parent_email_id IS NOT NULL — Thread-Verknuepfung
- `idx_acq_emails_unsub` ON acquisition_emails(unsubscribe_token) — Abmelde-Link Lookup

### 1.5 Akquise-Job-Guard (KRITISCH)

Akquise-Jobs duerfen NICHT ins Claude-Matching gelangen. In `claude_matching_service.py` (Stufe 0 Filter) muss hinzugefuegt werden:

```sql
WHERE j.acquisition_source IS NULL  -- Nur regulaere Jobs matchen
```

**Warum:** Akquise-Jobs haben weder Geocoding noch Klassifizierung. Sie wuerden den Matching-Lauf stoeren und falsche Ergebnisse erzeugen. Akquise-Jobs werden erst nach Konvertierung zu ATSJobs fuer Matching freigegeben.

### 1.6 State-Machine fuer akquise_status

Nicht jeder Status-Uebergang ist erlaubt. Validierung im Backend:

```
ERLAUBTE UEBERGAENGE:
neu → angerufen, verloren
angerufen → kontaktiert, wiedervorlage, email_gesendet, kontakt_fehlt, blacklist_weich, blacklist_hart, verloren
kontaktiert → qualifiziert, wiedervorlage, email_gesendet, blacklist_weich, blacklist_hart
kontakt_fehlt → angerufen, verloren  (neuer AP gefunden, oder aufgeben)
email_gesendet → email_followup, qualifiziert, blacklist_weich, blacklist_hart, followup_abgeschlossen, angerufen
email_followup → qualifiziert, blacklist_weich, blacklist_hart, followup_abgeschlossen, angerufen
wiedervorlage → angerufen, kontaktiert, verloren
qualifiziert → stelle_erstellt, verloren
stelle_erstellt → (Endstatus — weiter im ATS)
blacklist_hart → (Endstatus — nie wieder)
blacklist_weich → neu  (nur durch Re-Import nach 180 Tagen)
followup_abgeschlossen → neu  (nur durch Re-Import nach 30 Tagen)
verloren → neu  (nur durch Re-Import)
```

**Implementierung:** `ALLOWED_TRANSITIONS` Dict in `acquisition_call_service.py`, Check bei jeder Disposition.

### 1.7 Blacklist-Cascade

Bei Disposition D6 (nie_wieder):
1. `company.acquisition_status = "blacklist"`
2. **ALLE offenen Stellen** dieser Firma sofort auf `blacklist_hart` setzen
3. `acquisition_calls` Eintrag fuer jede betroffene Stelle (Disposition="nie_wieder", notes="Cascade von [Job-ID]")
4. Toast: "Firma + X weitere Stellen auf Blacklist gesetzt"

**Re-Import-Schutz:** Bei CSV-Import: Wenn `company.acquisition_status = "blacklist"` → Job wird NICHT importiert (bestehendes Verhalten von `get_or_create_by_name()`)

---

## PHASE 2: Backend-Services ✅ FERTIG + 6 BUGFIXES (28.02.2026)
> **Bugfixes beim echten CSV-Import:** (1) _extract_position_from_text(), (2) _trunc() VARCHAR-Limits, (3) greenlet_spawn Fix, (4) Duplikat-Handling, (5) company_service .first(), (6) Session-Rollback. Details in Akquise/MEMORY.md.

### 2.1 AcquisitionImportService (`app/services/acquisition_import_service.py`)

**Zweck:** CSV von advertsdata.com parsen und komplett in DB eintragen.

**Ablauf:**
1. CSV lesen (Tab-getrennt, 29 Spalten, Encoding-Detection)
2. Pro Zeile:
   a. Pruefe `anzeigen_id` gegen DB (Duplikat? → Update `last_seen_at` + `expires_at`)
   b. Pruefe `anzeigen_id` Zeitfenster (>90 Tage nicht gesehen? → Neuer Import)
   c. Company: `get_or_create_by_name(name, city)` mit Blacklist-Check
   d. Contact AP-Firma: `get_or_create_contact()` mit `contact_role="firma"`
   e. Contact AP-Anzeige: `get_or_create_contact()` mit `contact_role="anzeige"`
   f. Job: Erstellen mit `acquisition_source="advertsdata"`, `akquise_status="neu"`
   g. Phone-Normalisierung: Alle Telefonnummern in E.164 umwandeln
3. Batch-Insert (50 Zeilen pro DB-Session, Railway-konform)
4. Priority berechnen (erweiterte Formel, siehe unten)

**Priority-Formel (0-10, hoeher = besser):**
```
P = Branche_Score (0-2)         → Finance=2, Industrie mit FiBu=1, Sonstige=0
  + AP_vorhanden (0-2)          → Durchwahl+Name=2, nur Name=1, nur info@=0
  + Firmengroesse_Score (0-2)   → 50-500 MA=2, 500+=1, <50=0 (zu klein, wenig Provision)
  + Anzeigen_Alter (0-1)        → <14 Tage=1, aelter=0
  + Senioritaet_Score (0-2)     → "Leiter/Head/Senior"=2, "Buchhalter"=1, "Helfer/Praktikant"=0
  + Bekannte_Firma (0-1)        → Company existiert schon mit Matches/Jobs=1, neu=0
```
Sortierung: Hoechster Score zuerst. Gleicher Score: Aelteste `first_seen_at` zuerst.
Gespeichert in `Job.akquise_priority` (Integer 0-10), berechnet beim Import + nach jedem Update.

**Duplikat-Strategie:**
- Primaer: `anzeigen_id` UNIQUE WHERE acquisition_source='advertsdata'
- Bei Match: `last_seen_at` + `expires_at` auffrischen, Kontaktdaten updaten
- Bei anzeigen_id >90 Tage nicht gesehen: als neuer Job importieren, Status reset
- Fallback: content_hash fuer manuelle Imports (wie bestehend)

**Re-Import-Schutz (CRM-Review):**
- `akquise_status = "blacklist_hart"` → Job wird NICHT reaktiviert, NICHT aktualisiert, Skip
- `akquise_status = "blacklist_weich"` → Nur `last_seen_at` auffrischen, Status NICHT aendern
- `akquise_status = "qualifiziert"` / `"stelle_erstellt"` → Nur `last_seen_at`, kein Status-Reset
- Nur `akquise_status IN ("verloren", "followup_abgeschlossen")` → Reset auf "neu" erlaubt

**Import-Batch-ID:**
- Jeder Import bekommt eine UUID als `import_batch_id`
- Alle importierten Jobs dieses Batches bekommen die gleiche ID
- Rollback: `POST /api/akquise/import/rollback/{batch_id}` → DELETE alle Jobs mit dieser Batch-ID (nur wenn Status="neu", also noch nicht bearbeitet)

**KEIN Geocoding, KEINE Klassifizierung** bei Akquise-Import — das passiert erst bei Qualifizierung/ATS-Uebergang.

### 2.2 AcquisitionCallService (`app/services/acquisition_call_service.py`)

**Zweck:** Anrufe protokollieren, Dispositionen verarbeiten, automatische Aktionen ausloesen.

**Funktionen:**
- `record_call(job_id, contact_id, disposition, notes, qualification_data)` → acquisition_call erstellen
- `process_disposition(call)` → Automatische Aktionen je nach Disposition:
  - D1a (nicht_erreicht): akquise_status="angerufen", Wiedervorlage +1 Tag
  - D1b (mailbox_besprochen): akquise_status="angerufen", Wiedervorlage +1 Tag, Notiz "AB besprochen am [Datum]"
  - D2 (besetzt): akquise_status="angerufen", Wiedervorlage +1 Tag
  - D3 (falsche_nummer): akquise_status="kontakt_fehlt" (Lead ist NICHT tot — nur Kontaktdaten falsch. Kann bei Re-Import mit neuem AP reaktiviert werden)
  - D4 (sekretariat): akquise_status="angerufen", Wiedervorlage +1 Tag, optionale Felder: "Durchwahl erhalten" + "Name Sekretariat" → auf Contact speichern
  - D5 (kein_bedarf): akquise_status="blacklist_weich", Wiedervorlage +180 Tage
  - D6 (nie_wieder): akquise_status="blacklist_hart", Company.acquisition_status="blacklist"
  - D7 (interesse_spaeter): akquise_status="wiedervorlage", Task am Wunschdatum + **Uhrzeit** (Pflichtfeld)
  - D8 (will_infos): akquise_status="email_gesendet", E-Mail-Draft generieren (Versand mit 2h Delay)
  - D9 (qualifiziert_erst): akquise_status="qualifiziert", Zweitkontakt-Task am Wunschdatum + **Uhrzeit**
  - D10 (voll_qualifiziert): akquise_status="stelle_erstellt", ATSJob erstellen
  - D11 (ap_nicht_mehr_da): Contact als inaktiv markieren, optionales Feld "Nachfolger?", Lead bleibt aktiv wenn Stelle offen
  - D12 (andere_stelle_offen): Neuen Job-Draft erstellen (Position, grobe Details), gleiche Company + Contact, `acquisition_source="manual"`
  - D13 (weiterverbunden): Neuen Contact erfassen (Name, Funktion, Telefon), als `contact_role="empfehlung"` an Company haengen
- `get_wiedervorlagen(date)` → Faellige Wiedervorlagen fuer heute
- `get_call_history(job_id)` → Alle Calls zu einem Lead
- `lookup_phone(phone)` → Telefonnummer normalisieren + DB-Lookup → Company/Contact/Job

### 2.3 AcquisitionEmailService (`app/services/acquisition_email_service.py`)

**Zweck:** GPT-basierte Akquise-E-Mails generieren und versenden.

**E-Mail-Generierung (GPT-4o-mini):**
- Input: Stellenausschreibung (job_text), Firmenname, Branche, Ansprechpartner
- Output: Personalisierte E-Mail die sich auf die konkrete Vakanz bezieht
- **Kandidat ist FIKTIV** — GPT erfindet den Traumkandidaten basierend auf dem Stellentext
  - GPT analysiert Anforderungen und baut daraus einen plausiblen Kandidaten
  - Kein DB-Lookup, kein Matching — rein aus dem Stellentext abgeleitet
- Prompt-Struktur:
  - Analyse der Stellenausschreibung (Branche, ERP, Anforderungen, Kernkompetenzen)
  - Fiktiven Traumkandidaten beschreiben der exakt auf die Anforderungen passt
  - "Ich stehe aktuell mit einem [Position] aus Ihrer Region in Kontakt, der seit X Jahren..."
  - Bezug auf spezifische Anforderungen (gleiche Branche, ERP, Weiterbildung)
  - "muss wechseln" (nicht "will wechseln" — Dringlichkeit)
  - Call-to-Action ("Gerne wollte ich in Erfahrung bringen, unter welchen Voraussetzungen...")
  - Erreichbarkeit 9-18 Uhr, Terminvorschlag
  - Signatur mit Kontaktdaten

**Ablauf:**
1. `generate_draft(job_id, contact_id, email_type="initial")` → E-Mail-Entwurf generieren (Plain-Text)
2. Frontend zeigt Vorschau (editierbar) + Mailbox-Dropdown (von welchem Postfach senden?)
3. `send_email(draft_id, from_email)` → Via MicrosoftGraphClient (M365) ODER IONOS SMTP versenden
4. `acquisition_emails` Eintrag mit `from_email`, `sequence_position`, `unsubscribe_token` erstellen
5. Disposition + email_sent=true dokumentieren

**WICHTIG:** E-Mail wird NUR automatisch generiert, NIEMALS automatisch gesendet. Milad muss immer den Button klicken.

**Mailbox-Routing:**
- M365 (sincirus.com): Ueber `MicrosoftGraphClient.send_mail()` (bestehend)
- IONOS (sincirus-karriere.de, jobs-sincirus.com): Ueber SMTP (neuer `IonosSmtpClient`)
  - SMTP Server: `smtp.ionos.de`, Port 587, STARTTLS
  - Credentials pro Postfach in `system_settings` oder ENV
  - **Alternative:** Alle IONOS-Mailboxes in M365 als "Send As" konfigurieren (einfacher, ein Client)

**E-Mail-Timing (Vertriebs-Review):**
- E-Mail wird NICHT sofort nach "nicht erreicht" verschickt
- Konfigurierbarer Delay: Default 2 Stunden (wirkt persoenlicher statt automatisiert)
- Alternativ: Gebuendelter Versand um 14:00 (Nachmittags-E-Mails = hoehere Oeffnungsrate)
- Einstellbar via `system_settings`: `acquisition_email_delay_minutes` (Default: 120)
- Im Test-Modus: Delay wird ignoriert, sofortiger Versand an Test-Adresse

**Follow-up-E-Mail (NEU — nach Vertriebs-Review):**
- 5-7 Tage nach Erst-E-Mail ohne Antwort: automatischer Follow-up-Draft
- GPT generiert kuerzere Variante die sich auf die erste E-Mail bezieht:
  - "Bezugnehmend auf meine Nachricht von letzter Woche..."
  - "Der Kandidat ist aktuell noch in Gespraechen, aber ich wollte Ihnen die Gelegenheit nicht vorenthalten..."
- Im Tab "Nicht erreicht": Button **[Follow-up generieren]** erscheint nach 5 Tagen
- Follow-up wird ebenfalls NUR manuell gesendet (Vorschau → Editieren → Button)
- Max 1 Follow-up pro Lead (danach: Anruf oder Abschluss)
- n8n Cron (taeglich 09:00): Prueft `acquisition_emails` WHERE `sent_at < NOW() - 5 Tage` AND kein Follow-up → Telegram: "X Leads warten auf Follow-up-E-Mail"

**Eskalation nach E-Mail + Follow-up (korrigiert):**
- NICHT mehr Auto-Blacklist nach 3 Versuchen
- Stattdessen: Status "followup_abgeschlossen" nach 3 Anrufversuchen post-E-Mail
- Lead kann in 30 Tagen bei neuem CSV-Import reaktiviert werden
- Blacklist NUR bei expliziter Ablehnung (D5, D6), NIEMALS bei Nicht-Erreichen

### 2.4 AcquisitionQualifyService (`app/services/acquisition_qualify_service.py`)

**Zweck:** Lead → ATS-Stelle konvertieren.

**Ablauf:**
1. Job.akquise_status = "stelle_erstellt", akquise_status_changed_at = now()
2. ATSJob erstellen mit `source="akquise"`, `source_job_id=job.id`
3. Qualifizierungsdaten uebertragen (Budget, Software, Team etc.)
4. **Akquise-Dossier uebertragen (Lifecycle-Review):**
   - Alle `acquisition_calls` als ATSCallNotes kopieren (Datum, Disposition, Notizen)
   - Alle `acquisition_emails` als ATSActivity verlinken (Betreff, Datum, Status)
   - Gesamt-Timeline: "Erstkontakt [Datum] → X Anrufe → E-Mail → Qualifiziert"
   - `ats_job.internal_notes` = Zusammenfassung der Akquise-Historie
5. Classification-Pipeline triggern (Kategorisierung → Klassifizierung → Geocoding)
6. Claude-Matching aktivieren (wenn Kandidaten vorhanden)
7. ATSTodo erstellen: "Kandidaten fuer [Stelle] suchen"

---

## PHASE 3: API-Endpoints ✅ FERTIG (28.02.2026)

### 3.1 Route-Datei: `app/api/routes_acquisition.py`

**Prefix: /api/akquise** (registriert in main.py)

**Import-Endpoints:**
- `POST /import` — CSV hochladen, Background-Task starten
- `POST /import/preview` — CSV analysieren ohne Import (Duplikate, bekannte Firmen)
- `GET /import/status` — Import-Fortschritt (Polling)

**Lead-Endpoints:**
- `GET /leads` — Liste mit Filtern (status, stadt, prioritaet, tab), **gruppiert nach Company**
- `GET /leads/{job_id}` — Detail (Company + Contacts + Call-History + Job-Text + weitere Stellen der Firma)
- `PATCH /leads/{job_id}/status` — Status manuell aendern
- `PATCH /leads/batch-status` — Mehrere Leads gleichzeitig (z.B. alle Stellen einer Firma auf "kein_bedarf")

**Call-Endpoints:**
- `POST /leads/{job_id}/call` — Anruf protokollieren (Disposition + Quali-Daten + Notizen)
- `GET /leads/{job_id}/calls` — Call-Historie

**E-Mail-Endpoints:**
- `POST /leads/{job_id}/email/draft` — GPT-E-Mail generieren (Vorschau)
- `POST /leads/{job_id}/email/send` — E-Mail nach Vorschau senden

**Conversion-Endpoints:**
- `POST /leads/{job_id}/qualify` — Lead → ATSJob konvertieren

**Utility-Endpoints:**
- `GET /wiedervorlagen` — Heutige faellige Wiedervorlagen
- `GET /stats` — Tages-KPIs (Anrufe, Erreicht, Qualifiziert, E-Mails, Conversion)
- `GET /rueckruf/{phone}` — Telefonnummer-Lookup (Company/Contact/Jobs)
- `GET /events` — SSE Endpoint fuer Rueckruf-Popups
- `GET /mailboxes` — Verfuegbare Postfaecher mit Tages-Limit-Status
- `GET /unsubscribe/{token}` — **OEFFENTLICH (kein Auth!)** — Abmelde-Link aus E-Mails
- `POST /import/rollback/{batch_id}` — Fehlerhaften Import rueckgaengig machen

### 3.2 HTML-Seite: `app/api/routes_acquisition_pages.py`

- `GET /akquise` — Hauptseite mit Tabs

**Partials (fuer HTMX):**
- `GET /akquise/partials/tab/{tab_name}` — Tab-Inhalt laden
- `GET /akquise/partials/call-screen/{job_id}` — Call-Screen Panel
- `GET /akquise/partials/import-dialog` — CSV-Import Modal
- `GET /akquise/partials/email-modal/{job_id}` — E-Mail Vorschau/Edit Modal

---

## PHASE 4: Frontend-Templates ✅ FERTIG (28.02.2026)

### 4.1 Neue Dateien

```
app/templates/
  akquise/
    akquise_page.html              — Hauptseite (extends base.html, Tab-Navigation)
  partials/
    akquise_tab_heute.html         — Tab "Heute anrufen" (Default, inkl. faellige Wiedervorlagen)
    akquise_tab_neu.html           — Tab "Neue Leads"
    akquise_tab_wiedervorlagen.html — Tab "Wiedervorlagen" (kuenftige, nicht-heute-faellige)
    akquise_tab_nicht_erreicht.html — Tab "Nicht erreicht" (E-Mail gesendet, wartet auf Reaktion)
    akquise_tab_qualifiziert.html  — Tab "Qualifiziert"
    akquise_tab_archiv.html        — Tab "Abgeschlossen"
    akquise_lead_row.html          — Einzelne Lead-Zeile (mit Firmen-Badge wenn mehrere Stellen)
    akquise_company_group.html     — Firmen-Gruppe (aufklappbar, zeigt alle Stellen einer Firma)
    akquise_call_screen.html       — Drei-Spalten Call-Screen
    akquise_import_dialog.html     — CSV-Import mit Vorschau
    akquise_import_preview.html    — Vorschau-Tabelle
    akquise_email_modal.html       — E-Mail Vorschau + Edit
    akquise_disposition.html       — Disposition-Buttons (13 Dispositionen)
    akquise_new_contact.html       — Neuen AP erfassen (bei D13 Weiterverbunden)
    akquise_new_job_draft.html     — Neue Stelle aus Gespraech (bei D12 Andere Stelle)
    akquise_rueckruf_popup.html    — Rueckruf-Overlay
    akquise_stats_widget.html      — Tages-KPIs
```

### 4.2 Firmen-Gruppierung (KRITISCH)

**Problem:** 3 Stellen bei VP GmbH = 3 separate Leads in der Liste. Ohne Gruppierung ruft man die gleiche Firma 3x an.

**Loesung: Gruppierte Lead-Liste**

In der Lead-Liste werden Leads der gleichen Company zusammengefasst:

```
VP Groundforce GmbH, Lehrte (3 Stellen)                    [Aufklappen v]
├── Senior Accountant (FiBu) — neu — Prio 1
├── Lohnbuchhalter/in — neu — Prio 2
└── Teamleiter Finanzbuchhaltung — neu — Prio 1

Mueller Consulting, Hamburg (1 Stelle)
└── Bilanzbuchhalter/in — angerufen — Prio 3
```

**Verhalten:**
- Default: Eingeklappt, zeigt nur Firmenname + Stellenanzahl + hoechste Prioritaet
- Aufklappen: Zeigt alle Stellen mit Status, Prioritaet, letztem Kontakt
- **Im Call-Screen:** Sidebar-Hinweis "Diese Firma hat 2 weitere offene Stellen: [LohnBu] [Teamleiter]"
- **Disposition fuer alle:** Button "Gleiche Disposition fuer alle Stellen" (z.B. "kein Bedarf fuer alle 3")
- **Sortierung:** Nach hoechster Prioritaet innerhalb der Gruppe

**Bestandskunden-Erkennung:**
- Beim CSV-Import: Company-Match gegen bestehende Companies mit `status = "active"` ODER Jobs/Matches
- Badge in der Lead-Liste: **BESTANDSKUNDE** (gruen) wenn Firma schon bekannt
- Im Call-Screen: "Achtung: Diese Firma ist bereits im System. [Details anzeigen]"

**Query fuer gruppierte Liste:**
```sql
SELECT c.id, c.name, c.city, c.status,
       COUNT(j.id) as job_count,
       MIN(j.akquise_priority) as highest_priority,
       MAX(j.last_seen_at) as newest_lead
FROM jobs j
JOIN companies c ON j.company_id = c.id
WHERE j.acquisition_source = 'advertsdata'
  AND j.akquise_status IN (:tab_statuses)
GROUP BY c.id, c.name, c.city, c.status
ORDER BY highest_priority ASC, newest_lead DESC
```

### 4.3 Intelligenter Start + Zustandserhaltung

**"Abtelefonieren starten" Button:**
- Grosser Button auf Tab "Heute anrufen": **[Abtelefonieren starten]**
- Oeffnet automatisch den ersten Lead im Call-Screen (nach Prioritaet sortiert)
- Kein manuelles Anklicken eines Leads noetig

**"Weitermachen wo ich war":**
- `localStorage.setItem('akquise_last_lead_id', jobId)` bei jedem Call-Screen-Wechsel
- Beim naechsten Seitenaufruf: Wenn lastLeadId gesetzt → "Weitermachen bei [Firma]?" Hinweis
- Nach Browser-Refresh oder Tab-Wechsel: Zustand bleibt erhalten

**Intelligenter Default-Tab:**
- Wenn "Heute anrufen" leer und "Neue Leads" hat Eintraege → automatisch auf "Neue Leads" weiterleiten
- Widget oben: "Du hast X Wiedervorlagen und Y neue Leads — [Jetzt starten]"

### 4.4 Alpine.js Stores

**akquiseStore:**
- `activeTab` — Aktueller Tab-Name
- `selectedLeadId` — Aktuell ausgewaehlter Lead
- `lastLeadId` — Letzter bearbeiteter Lead (localStorage-Sync)
- `callActive` — Anruf laeuft?
- `callStartedAt` — Timer-Start
- `filters` — Stadt, Rolle, Quelle
- `counts` — Anzahl pro Tab (Badge-Updates)

**callScreenStore:**
- `lead` — Aktuelles Lead-Objekt
- `siblingJobs` — Weitere Stellen der gleichen Firma
- `notizen` — Textarea-Binding (Autosave via HTMX)
- `fragenChecked` — Array der abgehakten Fragen
- `disposition` — Gewaehlt Disposition
- `rueckrufDatum` — Wiedervorlage-Datum

### 4.5 Call-Screen Layout (verbessert nach Vertriebs-Review)

```
+------------------------------------------------------------------+
| [Call-Bar: ● Calling VP GmbH — 02:34 — [Auflegen]]              |
| ⚠ BESTANDSKUNDE — 2 weitere Stellen: [LohnBu] [Teamleiter]     |
+------------------------------------------------------------------+
| Stellenausschreibung  | Qualifizierung        | Aktionen         |
| (35%, scrollbar)      | (35%, fixed)          | (30%, fixed)     |
|                       |                       |                  |
| [H2] Senior Accountant| [Kontakt-Card]        | [Letzte Calls]   |
| VP GmbH, Lehrte       | Ulrike Sturm — HR     | 25.02. Sekret.   |
| Vollzeit              | +49 170 888 8310 [📋] | "Frau Meyer,     |
| Logistik | 250-500 MA | us@vpgroundforce.de   |  AP im Urlaub    |
| vpgroundforce.de      | Durchwahl: -282       |  bis 03.03."     |
|                       |                       |                  |
| [Voller Text der      | [Qualifizierungsfragen]| [Notizen]        |
|  Stellenausschreibung | ☑ Stelle offen?       | Textarea 200px   |
|  mit Scrollbar]       | ☐ Wie besetzen Sie?   | Auto-Save 3s     |
|                       | ☐ Herausforderung?    |                  |
|                       | ☐ Timeline?           | [Textbausteine]  |
|                       | ☐ Profile senden?     | [AB besprochen]  |
|                       | ...                   | [Sekretariat]    |
|                       |                       | [Termin]         |
|                       | [+ Neuer AP]          | [Im Urlaub bis_] |
|                       | [+ Neue Stelle]       | [Keine Durchwahl] |
|                       |                       |                  |
|                       |                       | [Disposition ▾]  |
|                       |                       | [E-Mail senden]  |
|                       |                       | [Zurueck]        |
+------------------------------------------------------------------+
```

**Verbesserungen gegenueber Original:**

1. **Firma-Kontext:** Branche, MA-Zahl, Domain direkt sichtbar (linke Spalte, unter Stellentitel)
2. **Bestandskunden-Hinweis:** Gelber Banner wenn Firma schon im System
3. **Weitere Stellen:** Links zu anderen offenen Stellen der gleichen Firma
4. **Call-Historie:** Letzte 2-3 Anrufe mit Datum + Disposition + Notiz DIREKT sichtbar (rechte Spalte, oben)
5. **Textbausteine:** 5 klickbare Shortcuts die Text ins Notizfeld einfuegen
6. **Copy-Button:** [📋] neben Telefonnummer kopiert in Zwischenablage
7. **Durchwahl:** Wenn bei frueheren Anruf erhalten, wird sie prominent angezeigt
8. **Neuer AP / Neue Stelle:** Buttons in der mittleren Spalte fuer D12/D13 Szenarien
9. **Frage 2 umformuliert:** "Wie besetzen Sie aktuell?" statt "Arbeiten Sie mit externen Dienstleistern?" (weniger defensiv)

### 4.4 Disposition-Flow (Fliessband)

1. Klick "Nicht erreicht" → Button-Animation "Gespeichert ✓" (500ms)
2. HTMX POST /api/akquise/leads/{id}/call → Disposition + Notizen senden
3. Server antwortet mit naechstem Lead im Call-Screen (auto-advance)
4. HX-Trigger: updateCounts → Tab-Badges aktualisieren
5. OOB-Swap: Lead-Zeile in der Liste aktualisieren/entfernen
6. Toast: "VP GmbH — nicht erreicht (1. Versuch)"

**Kein Zurueck-zur-Liste noetig.** Recruiter bleibt im Call-Screen und arbeitet Lead nach Lead ab.

---

## PHASE 5: E-Mail-System ✅ FERTIG (28.02.2026, inkl. IONOS SMTP + Multi-Mailbox Routing)

### 5.0 Multi-Mailbox-Konfiguration (5 Postfaecher)

**Verfuegbare Postfaecher:**

| # | Adresse | Domain | Typ | Standard-Zweck | Tages-Limit |
|---|---------|--------|-----|----------------|-------------|
| 1 | `hamdard@sincirus.com` | sincirus.com | M365 | Haupt + Antworten | 100/Tag |
| 2 | `hamdard@sincirus-karriere.de` | sincirus-karriere.de | IONOS | Erst-Mails | 20/Tag (Warmup) |
| 3 | `m.hamdard@sincirus-karriere.de` | sincirus-karriere.de | IONOS | Follow-ups | 20/Tag (Warmup) |
| 4 | `m.hamdard@jobs-sincirus.com` | jobs-sincirus.com | IONOS | Break-up-Mails | 20/Tag (Warmup) |
| 5 | `hamdard@jobs-sincirus.com` | jobs-sincirus.com | IONOS | Reserve | 20/Tag (Warmup) |

**Konfiguration in `system_settings`:**

| Key | Value | Beschreibung |
|-----|-------|--------------|
| `acquisition_mailboxes` | JSONB Array | Liste aller Postfaecher mit Limits |
| `acquisition_default_mailbox` | String | Standard-Absender (Default: `hamdard@sincirus-karriere.de`) |

**JSONB-Struktur pro Mailbox:**
```json
{
  "email": "hamdard@sincirus-karriere.de",
  "label": "Sincirus Karriere (Milad)",
  "daily_limit": 20,
  "warmup_phase": true,
  "purpose": "initial",
  "active": true
}
```

**Tages-Limit-Tracking:**
- Query: `SELECT COUNT(*) FROM acquisition_emails WHERE from_email = :email AND sent_at >= CURRENT_DATE AND status = 'sent'`
- Wenn Limit erreicht: Dropdown zeigt "(Limit erreicht)" + Button deaktiviert
- Warmup-Plan: IONOS-Domains starten mit 20/Tag, steigern um 10/Woche bis 50/Tag

**Frontend (E-Mail-Modal):**
```
[Von: ▾ hamdard@sincirus-karriere.de     ]  ← Dropdown mit allen aktiven Mailboxes
[An:  ulrike.sturm@vpgroundforce.de      ]  ← Auto-befuellt aus AP-Daten
[Betreff: ________________________________]
[                                          ]
[  E-Mail-Text (Plain-Text, editierbar)   ]
[                                          ]
[                                          ]
[_____________ Senden _____ Abbrechen ____]
```

### 5.1 GPT-Prompt fuer Akquise-E-Mails

**WICHTIG: Plain-Text statt HTML (Marketing-Review)**

Kaltakquise-E-Mails werden als **Plain-Text** gesendet (NICHT HTML). Gruende:
- Hoehere Zustellbarkeit (weniger Spam-Filter-Trigger)
- Wirkt persoenlicher und handgeschrieben
- IONOS-Domains haben keine Reputation fuer HTML-Mails
- HTML-Signatur wird NUR am Ende angehaengt (Kontaktdaten + Abmelde-Link)

**System-Prompt:**
Du bist ein erfahrener Personalberater im Finance-Bereich. Schreibe eine professionelle Akquise-E-Mail an ein Unternehmen das eine Stelle ausgeschrieben hat. Die E-Mail soll einen passenden Kandidaten anbieten (ohne echten Namen).

**Format: Reiner Fliesstext. KEIN HTML, KEINE Formatierung, KEINE Aufzaehlungen.**

**Regeln:**
- Erfinde einen fiktiven Traumkandidaten der PERFEKT auf die Anforderungen passt
- Analysiere die Stellenausschreibung: Branche, ERP-System, Zertifikate, Erfahrungsjahre, Aufgaben
- Beschreibe den Kandidaten so, dass der Empfaenger denkt: "Das kann nicht wahr sein, der hat genau den Richtigen"
- Gleiche Branche wie das Unternehmen (aus Stellentext ableiten)
- Gleiche ERP-Kenntnisse wenn gefordert (DATEV, SAP, NetSuite etc.)
- Passende Weiterbildung/Zertifikate wenn gefordert (Bilanzbuchhalter, Steuerfachwirt etc.)
- "Kandidat seit X Jahren bei aehnlichem Unternehmen, muss aktuell wechseln" (Dringlichkeit!)
- Region erwaehnen: "aus Ihrer Region" oder "aus dem Raum [Stadt]"
- Abschluss: "Gerne wollte ich in Erfahrung bringen, unter welchen Voraussetzungen Sie sich vorstellen koennen, den Kandidaten einmal kennenzulernen"
- Erreichbarkeit 9-18 Uhr, Terminvorschlag
- KEIN echter Kandidatenname, KEINE Gehaltszahlen, KEINE Firmenname des Kandidaten
- Max 150 Woerter (kurz + praegnant)

**User-Prompt:**
```
Stellenausschreibung: {job_text}
Unternehmen: {company_name}
Branche: {industry}
Position: {position}
Ansprechpartner: {contact_salutation} {contact_last_name}
E-Mail-Typ: {email_type}  (initial / follow_up / break_up)
Vorherige E-Mail: {previous_email_text}  (nur bei follow_up/break_up)
```

### 5.1b Break-up-E-Mail (3. Touchpoint — NEU nach Marketing-Review)

Nach der Follow-up-E-Mail (5-7 Tage nach Erst-Mail) kommt optional eine **Break-up-E-Mail** als letzter Versuch:

**Timing:** 7-10 Tage nach Follow-up (= ca. 14-17 Tage nach Erst-Mail)
**Tonalitaet:** FOMO (Fear of Missing Out) — "Kandidat hat jetzt andere Angebote..."

**GPT-Prompt-Ergaenzung fuer Break-up:**
```
E-Mail-Typ: break_up
Tonalitaet: Freundlich aber mit Dringlichkeit. Der Kandidat hat mittlerweile andere
Gespraeche und die Gelegenheit schliesst sich. Kein Druck, aber klare Deadline.
Beispiel-Formulierung: "Da sich bei dem Kandidaten mittlerweile mehrere Optionen
ergeben haben, wollte ich mich ein letztes Mal melden..."
Max 80 Woerter.
```

**E-Mail-Sequenz komplett:**
```
Tag 0:  Erst-E-Mail (initial) — von sincirus-karriere.de — Traumkandidat vorstellen
Tag 5-7: Follow-up (follow_up) — von m.hamdard@sincirus-karriere.de — "Bezugnehmend auf..."
Tag 14-17: Break-up (break_up) — von m.hamdard@jobs-sincirus.com — "Letzte Gelegenheit..."
```

- Button im Tab "Nicht erreicht": **[Break-up generieren]** erscheint nach 14 Tagen
- Max 1 Break-up pro Lead (danach: endgueltig abgeschlossen)
- `sequence_position`: 1=initial, 2=follow_up, 3=break_up

### 5.2 E-Mail-Signatur + Abmelde-Link

Bestehende Signatur aus `outreach_service.py` wiederverwenden (HTML mit Foto, Kontaktdaten, Website).

**Abmelde-Link (PFLICHT — DSGVO):**
```
---
Sie moechten keine weiteren Nachrichten erhalten?
Hier abmelden: https://claudi-time-production-46a5.up.railway.app/api/akquise/unsubscribe/{token}
```

- Token: Zufaelliger 64-Zeichen Hex-String, pro E-Mail generiert
- Endpoint: `GET /api/akquise/unsubscribe/{token}` — **OEFFENTLICH, KEIN Auth!**
- Aktion: Company → `acquisition_status = "blacklist"`, alle offenen Leads → `blacklist_weich`
- Response: Einfache HTML-Seite "Sie wurden erfolgreich abgemeldet."
- **KEIN Double-Opt-Out** — 1 Klick = sofortige Abmeldung

### 5.3 Thread-Linking (In-Reply-To Header)

Follow-up und Break-up E-Mails muessen im gleichen Thread landen wie die Erst-Mail:

1. Erst-Mail: `graph_message_id` wird nach Versand von Microsoft Graph zurueckgegeben
2. Follow-up: `parent_email_id` referenziert die Erst-Mail
   - Microsoft Graph: `In-Reply-To: <message_id_from_parent>` Header setzen
   - So landet die Follow-up-Mail als Antwort im gleichen Thread beim Empfaenger
3. Break-up: `parent_email_id` referenziert die Follow-up-Mail (oder Erst-Mail)

**Implementierung:** In `MicrosoftGraphClient.send_mail()` optionalen Parameter `in_reply_to` hinzufuegen.

### 5.4 Reply-Detection (NEU — Lifecycle-Review)

**Problem:** Wenn ein Kunde auf die E-Mail antwortet, merkt das System nichts. Der Lead bleibt auf "email_gesendet".

**Loesung: n8n Workflow + Microsoft Graph**

```
n8n Cron (alle 15 Min) → Microsoft Graph API: "Neue E-Mails in Inbox pruefen"
→ Filter: Subject enthaelt Akquise-Betreff ODER In-Reply-To matched graph_message_id
→ Match gefunden:
  → acquisition_emails.status = "replied"
  → Job.akquise_status = "kontaktiert" (auto-upgrade)
  → Telegram: "Antwort von [Firma] auf Akquise-E-Mail! [Link zum Lead]"
```

**Welche Inbox pruefen?** Alle 5 Postfaecher (via Microsoft Graph Subscriptions oder Polling).
**Haupt-Postfach:** Antworten koennen auch an `hamdard@sincirus.com` gehen (Reply-To Header).

### 5.5 E-Mail-Empfaenger-Logik

Prioritaet:
1. AP-Anzeige E-Mail (wenn vorhanden)
2. AP-Firma E-Mail (wenn vorhanden)
3. Bewerber-Postfach (Firmen-E-Mail aus CSV Spalte 5)
4. KEINE E-Mail (wenn kein Kontakt)

### 5.6 Post-Email-Lifecycle (Tab "Nicht erreicht")

**Problem:** Was passiert mit einem Lead NACHDEM die E-Mail rausging?

**Loesung: Neuer Tab "Nicht erreicht"**

Jeder Lead der eine Akquise-E-Mail erhalten hat (Disposition `nicht_erreicht` oder `will_infos` mit anschliessender E-Mail) landet im Tab "Nicht erreicht". Dort ist sichtbar:

| Spalte | Inhalt |
|--------|--------|
| Firma + Job | Company-Name, Position, Stadt |
| AP | Ansprechpartner-Name + Kontaktdaten |
| E-Mail gesendet am | Timestamp der gesendeten E-Mail |
| Tage seit E-Mail | Auto-berechnet (farbig: gruen <3 Tage, gelb 3-7, rot >7) |
| Anrufversuche | Anzahl bisheriger Calls |
| Letzte Disposition | Was beim letzten Anruf passiert ist |
| Aktionen | [Erneut anrufen] [E-Mail anzeigen] [Abschliessen] |

**Lifecycle nach E-Mail-Versand:**

```
E-Mail gesendet
    |
    v
akquise_status = "email_gesendet"  →  Tab "Nicht erreicht"
    |
    +-- Wiedervorlage automatisch: +3 Werktage "Nochmal anrufen nach E-Mail"
    |
    v
Tag 3-5: Wiedervorlage faellig → erscheint AUCH in Tab "Heute anrufen"
    |
    +-- [Erneut anrufen] → Call-Screen oeffnet sich
    |   |
    |   +-- Erreicht + Interesse → akquise_status="qualifiziert" → Tab "Qualifiziert"
    |   +-- Erreicht + kein Bedarf → akquise_status="blacklist_weich" → Tab "Archiv"
    |   +-- Nicht erreicht → akquise_status bleibt "email_gesendet"
    |       → Wiedervorlage +3 Werktage (2. Versuch nach E-Mail)
    |
    +-- [E-Mail anzeigen] → Modal zeigt gesendete E-Mail (Betreff, Text, Datum)
    |
    +-- [Abschliessen] → akquise_status="verloren" → Tab "Archiv"
    |
    v
Nach 3 Anrufversuchen nach E-Mail (konfigurierbar):
    → n8n Eskalation: Auto-Status "blacklist_weich"
    → Telegram: "Lead X nach E-Mail + 3 Versuchen abgeschlossen"
    → Verschwindet aus "Nicht erreicht", geht in "Archiv"
```

**DB-Aenderungen fuer Post-Email-Tracking:**

Auf `acquisition_calls` Tabelle (bereits geplant, keine neuen Felder noetig):
- `email_sent = true` markiert den Call nach dem die E-Mail verschickt wurde
- Die gesendete E-Mail wird separat in einer neuen Tabelle gespeichert:

**Tabelle: `acquisition_emails`** (siehe Phase 1.3 fuer komplettes Schema inkl. `from_email`, `parent_email_id`, `sequence_position`, `email_type`, `unsubscribe_token`)

**Warum eigene Tabelle statt JSONB?**
- E-Mails muessen spaeter angezeigt werden koennen (Betreff, Text, Datum)
- Mehrere E-Mails pro Lead moeglich (Erst-E-Mail + Follow-up nach 7 Tagen)
- DSGVO: Loeschbar pro Lead
- Tracking-Status (gesendet, bounced) muss aktualisierbar sein

**Tab "Nicht erreicht" — Query:**
```sql
SELECT j.*, c.name as company_name, ae.sent_at, ae.subject,
       COUNT(ac.id) as call_count,
       MAX(ac.created_at) as last_call
FROM jobs j
JOIN companies c ON j.company_id = c.id
LEFT JOIN acquisition_emails ae ON ae.job_id = j.id AND ae.status = 'sent'
LEFT JOIN acquisition_calls ac ON ac.job_id = j.id
WHERE j.acquisition_source = 'advertsdata'
  AND j.akquise_status = 'email_gesendet'
GROUP BY j.id, c.name, ae.sent_at, ae.subject
ORDER BY ae.sent_at DESC
```

---

## PHASE 6: n8n-Workflows ✅ FERTIG + AKTIV (01.03.2026)

> **6 komplett neue Workflows**, unabhaengig von bestehenden n8n-Automationen.
> Alle 6 Workflows **AKTIVIERT** am 28.02.2026. Backend-Endpoints getestet (alle 200 OK), Telegram getestet.
> **Commits:** bc7204f (5 Endpoints + Doku), 06b9632 (DB-Session-Fix fuer check-inbox)
> **DB-Session-Fix:** check-inbox refactored zu 3-Phasen-Architektur (Token→Graph→DB) — verhindert Railway 30s Timeout.

### 6.1 Morgen-Briefing (08:00) — n8n ID: `BqjDn57PWwVHDpNy`

```
Schedule Trigger (08:00)
  → Parallel: GET /api/akquise/wiedervorlagen + GET /api/akquise/stats
  → Code: Briefing formatieren (Stats + Wiedervorlagen-Liste)
  → Telegram: Zusammenfassung + Link zur Akquise-Seite
```

### 6.2 Abend-Report (19:00) — n8n ID: `VazlvT2vuvqLXX9i`

```
Schedule Trigger (19:00)
  → GET /api/akquise/stats
  → Code: Tages-Performance mit Emoji-Ampel + Tagesziel-Check
  → Telegram: Tages-KPIs + Motivations-Feedback
```

### 6.3 Wiedervorlagen-Alarm (10/12/14/16 Uhr) — n8n ID: `CUIhgzyqDpuOQGZq`

```
Schedule Trigger (alle 2h, 10-16 Uhr)
  → GET /api/akquise/wiedervorlagen
  → Code: Ueberfaellige + kommende 2h filtern
  → IF: skip=true → Stopp, skip=false → Telegram
  → Telegram: Nur wenn ueberfaellige oder kommende Wiedervorlagen existieren
```

### 6.4 Follow-up-Erinnerung (09:00) — n8n ID: `nXsYnVBWy9q0Vh7J`

```
Schedule Trigger (09:00)
  → GET /api/akquise/n8n/followup-due (NEUER Endpoint)
  → Code: Follow-up-faellig (5-7 Tage) + Break-up-faellig (7-10 Tage) formatieren
  → Telegram: Liste mit Firma/Position/Tage seit Erst-Mail
```

**Neuer Backend-Endpoint:** `GET /api/akquise/n8n/followup-due`
- Sucht Erst-Mails gesendet vor 5-7 Tagen OHNE Follow-up OHNE Reply
- Sucht Follow-up-Mails gesendet vor 7-10 Tagen OHNE Break-up OHNE Reply

### 6.5 Eskalation (18:00) — n8n ID: `YhLwBZewTiKu7qEU`

```
Schedule Trigger (18:00)
  → GET /api/akquise/n8n/eskalation-due?apply=true (NEUER Endpoint)
  → Code: Eskalierte Leads formatieren
  → Telegram: Liste mit Firma/Position/Anzahl Versuche
```

**Neuer Backend-Endpoint:** `GET /api/akquise/n8n/eskalation-due?apply=true/false`
- Sucht Jobs mit Status email_gesendet/email_followup + 3 Anrufe danach ohne Erreichen
- Mit `apply=true`: Setzt Status automatisch auf followup_abgeschlossen
- **WICHTIG:** Blacklist NUR bei expliziter Ablehnung (D5/D6), NICHT bei Nicht-Erreichen

### 6.6 Reply & Bounce Monitor (alle 15 Min) — n8n ID: `DIq76xuoQ0QqQMxJ`

```
Schedule Trigger (*/15)
  → POST /api/akquise/n8n/check-inbox?minutes=15 (NEUER Endpoint)
  → Code: Replies + Bounces formatieren (nur wenn > 0)
  → Telegram: Antworten + Bounces mit Firma/E-Mail/Postfach
```

**Neuer Backend-Endpoint:** `POST /api/akquise/n8n/check-inbox?minutes=15`
- Nutzt MicrosoftGraphClient Token (Backend hat Credentials, n8n nicht)
- Prueft alle 5 Akquise-Mailboxen via Microsoft Graph API
- Reply-Detection: Matcht Absender gegen gesendete acquisition_emails
- Bounce-Detection: Erkennt NDR/MAILER-DAEMON, extrahiert Original-Empfaenger
- Updates: email.status → replied/bounced, job.akquise_status → kontaktiert

### Weitere n8n-Endpoints (Hilfsfunktionen)

| Methode | Pfad | Beschreibung |
|---------|------|------------|
| POST | `/api/akquise/n8n/process-reply` | Einzelne Reply manuell verarbeiten |
| POST | `/api/akquise/n8n/process-bounce` | Einzelnen Bounce manuell verarbeiten |

---

## PHASE 7: Rueckruf-Erkennung ⏳ TEILWEISE FERTIG (7.1 + 7.2 + 7.3 done)

### 7.1 Telefonnummer-Normalisierung ✅ FERTIG

Alle Telefonnummern bei Import in E.164 Format umwandeln:
- "0170 888 8310" → "+491708888310"
- "0511-70050-282" → "+4951170050282"
- "01718141112" → "+491718141112"

Gespeichert in `CompanyContact.phone_normalized` (indexed).
Implementiert in: `acquisition_call_service.py` → `_normalize_phone_simple()`

### 7.2 Lookup-Endpoint ✅ FERTIG + manueller Frontend-Trigger

`GET /api/akquise/rueckruf/{phone}` (phone als E.164)
- Query: `SELECT * FROM company_contacts WHERE phone_normalized = :phone`
- Join: Company + Jobs (WHERE akquise_status NOT IN ('verloren', 'blacklist_hart'))
- Response: `{contact, company, open_jobs[]}`
- **Frontend:** Suchfeld im Header (akquise_page.html), Popup zeigt Firma/AP/offene Stellen

### 7.3 SSE-Endpoint fuer Browser-Popup ✅ FERTIG

- **Event-Bus:** `app/services/acquisition_event_bus.py` — In-Memory asyncio.Queue Pub-Sub
- **SSE-Stream:** `GET /akquise/events` (StreamingResponse, 30s Heartbeat)
- **Webhook:** `POST /api/akquise/events/incoming-call` — n8n/Webex ruft auf, macht Phone-Lookup, pusht SSE-Event
- **Frontend:** EventSource in akquisePage().init(), `_handleIncomingCall()` zeigt Popup mit pulsierendem Punkt
- **Refactoring:** `_renderRueckrufPopup()` — gemeinsame DOM-basierte Methode fuer manuellen Lookup und SSE-Event

### 7.4 Webex-Integration ✅ TEILWEISE FERTIG (01.03.2026 Session 3)

**Incoming-Call-Workflow (V8rGRTK0gCkqhWvV) — AKTIV + GETESTET:**
- Webhook → Set (Phone/Name extrahieren) → Code (Filter <4 Zeichen) → HTTP Request (Backend SSE)
- Webhook-URL: `https://n8n-production-aa9c.up.railway.app/webhook/webex-incoming-call`
- End-to-End getestet: Execution #1040 SUCCESS (200 OK, delivered_to: 3)
- Fixes: webhookId Property, If→Code-Node ersetzt, X-API-Key Header

**Recording-Workflow (w5LTbRw1bFSCIKNP) — DEPLOYED, INAKTIV:**
- 12 Nodes: Webhook → OAuth → Get Recording → Download → Whisper → GPT Extract → If → MT/Unassigned
- Security: process.env in Code-Nodes (keine hardcodierten Keys)
- Braucht: WEBEX_CLIENT_ID, WEBEX_CLIENT_SECRET, WEBEX_REFRESH_TOKEN, OPENAI_API_KEY in n8n ENV

**Backend-Endpoints (unassigned_calls):**
- POST /api/akquise/unassigned-calls — Neuen unassigned Call erstellen
- GET /api/akquise/unassigned-calls — Liste (filter: assigned=true/false)
- PATCH /api/akquise/unassigned-calls/{id}/assign — Zuordnen zu Contact/Company
- DELETE /api/akquise/unassigned-calls/{id} — Loeschen

**Setup-Anleitung:** `Akquise/WEBEX-SETUP.md` (Milad muss OAuth + Webhook manuell einrichten)
**E2E-Tests:** Phase 24 (7 Tests) in playwright_e2e_tests.py

---

## PHASE 8: Qualifizierungs-Checkliste ✅ GROESSTENTEILS FERTIG

### 8.1 Erstkontakt (5 Fragen, Cold-Call)

| # | Frage | Feld |
|---|-------|------|
| 1 | Ist die Stelle noch offen? | stelle_offen (bool) |
| 2 | Wie gehen Sie bei der Besetzung aktuell vor? | besetzung_vorgehen (text) |
| 3 | Was ist die groesste Herausforderung bei der Besetzung? | herausforderung (text) |
| 4 | Wann soll die Stelle besetzt sein? | timeline (text) |
| 5 | Darf ich passende Profile senden? | profile_senden (bool) |

### 8.2 Zweitkontakt (12 Fragen, Termin)

| # | Frage | Feld |
|---|-------|------|
| 6 | Budget/Gehaltsspanne? | budget (text) |
| 7 | Teamgroesse und Struktur? | teamgroesse (text) |
| 8 | Home-Office-Tage? | home_office (text) |
| 9 | Arbeitszeiten, Gleitzeit, Kernzeiten? | arbeitszeiten (text) |
| 10 | Ueberstunden-Handling? | ueberstunden (text) |
| 11 | Software (DATEV, SAP, etc.)? | software (text) |
| 12 | Erfahrung mit der Software? | software_erfahrung (text) |
| 13 | Aeltere Kandidaten willkommen? | aeltere_kandidaten (bool) |
| 14 | Warum ist die Position vakant? | vakanzgrund (text) |
| 15 | Bewerbungsprozess? | bewerbungsprozess (text) |
| 16 | Wer entscheidet? | entscheider (text) |
| 17 | Englisch noetig? Wie eingesetzt? | englisch (text) |

Gespeichert als JSONB in `acquisition_calls.qualification_data`.

---

## PHASE 9: E2E-Testmodus (ohne echte Kunden) ✅ FERTIG (01.03.2026)

### 9.1 Konzept

Ein `test_mode` Flag in `system_settings` schaltet das gesamte Akquise-System in einen Sandbox-Modus:
- **Kein echter Anruf** — `tel:` Links werden deaktiviert, stattdessen Simulations-Button
- **Keine echte E-Mail** — E-Mails gehen an eine Test-Adresse (konfigurierbar) statt an den Kunden
- **Keine echten n8n-Trigger** — Wiedervorlagen/Eskalation nur manuell ausloesbar
- **Sichtbar:** Gelber Banner oben: "TEST-MODUS — Keine echten Anrufe oder E-Mails"

### 9.2 system_settings Eintraege

| Key | Default | Beschreibung |
|-----|---------|--------------|
| `acquisition_test_mode` | `true` | Test-Modus an/aus |
| `acquisition_test_email` | Milads E-Mail | Alle Akquise-E-Mails gehen hierhin statt an Kunden |

### 9.3 Test-CSV (mitgeliefert)

Datei: `app/testdata/akquise_test_data.csv` (Tab-getrennt, gleiche 29 Spalten)

10 fiktive Firmen die verschiedene Szenarien abdecken:

| # | Firma | Szenario |
|---|-------|----------|
| 1 | Testfirma Alpha GmbH, Hannover | Normaler Lead — kompletter Happy Path |
| 2 | Testfirma Beta AG, Hamburg | Hat 2 Ansprechpartner (AP-Firma + AP-Anzeige verschieden) |
| 3 | Testfirma Gamma KG, Muenchen | Telefonnummer fehlt — E-Mail-Only-Flow |
| 4 | Testfirma Delta GmbH, Berlin | Duplikat von #1 (gleiche anzeigen_id) — Dedup testen |
| 5 | Testfirma Epsilon UG, Koeln | Wird Blacklist — "nie_wieder" Disposition testen |
| 6 | Testfirma Zeta GmbH, Frankfurt | Qualifizierung komplett — ATS-Uebergang testen |
| 7 | Testfirma Eta AG, Stuttgart | Wiedervorlage — "interesse_spaeter" in 3 Tagen |
| 8 | Testfirma Theta GmbH, Hannover | Gleiche Stadt wie #1 — Firma-Dedup testen (anderer Name) |
| 9 | Testfirma Iota e.K., Dresden | Sonderzeichen + Umlaute im Anzeigentext |
| 10 | Testfirma Kappa GmbH, Duesseldorf | Rueckruf-Szenario — Telefonnummer fuer Callback-Test |

Jede Zeile hat einen realistischen `Anzeigen-Text` (3-4 Absaetze Stellenausschreibung fuer Finance-Rollen: FiBu, BiBu, LohnBu etc.)

### 9.4 Was im Test-Modus anders ist

**CSV-Import:**
- Funktioniert identisch — Test-CSV wird ganz normal importiert
- Test-Firmen haben `acquisition_source = "advertsdata"` wie echte Daten
- Re-Import der Test-CSV testet Duplikat-Erkennung

**Call-Screen:**
- `tel:` Link wird NICHT gerendert
- Stattdessen: **[Anruf simulieren]** Button mit Dropdown:
  - "Nicht erreicht (Mailbox)" → simuliert 15s Anrufdauer
  - "Besetzt" → simuliert 5s
  - "Sekretariat erreicht" → simuliert 45s
  - "AP erreicht — kein Bedarf" → simuliert 120s
  - "AP erreicht — Interesse" → simuliert 180s
  - "AP erreicht — voll qualifiziert" → simuliert 300s
  - "Falsche Nummer" → simuliert 3s
  - "Nie wieder anrufen" → simuliert 30s
- Nach Klick: Call-Timer laeuft (wie echt), dann Disposition-Auswahl

**Disposition:**
- Funktioniert IDENTISCH — alle 10 Szenarien testbar
- Wiedervorlagen werden real in DB geschrieben
- Status-Wechsel wie in Produktion
- Fliessband-Modus (auto-advance zum naechsten Lead) testbar

**E-Mail:**
- GPT-Generierung laeuft ECHT (Prompt + Antwort, kostet ~$0.002 pro E-Mail)
- Vorschau wird angezeigt (editierbar wie in Produktion)
- Beim Klick auf "Senden":
  - `to_email` wird UEBERSCHRIEBEN mit `acquisition_test_email` aus system_settings
  - Betreff bekommt Prefix: `[TEST] `
  - E-Mail geht real ueber Microsoft Graph raus — aber an DICH, nicht an den Kunden
  - In DB: `acquisition_emails.status = "sent"`, `to_email = Test-Adresse`
- So kannst du die echte E-Mail in deinem Postfach sehen und pruefen

**Rueckruf:**
- Neuer Button auf der Akquise-Seite: **[Rueckruf simulieren]**
- Oeffnet kleines Modal: Telefonnummer eingeben (oder aus Liste waehlen)
- Loest denselben SSE-Event aus wie ein echter Rueckruf
- Popup erscheint mit Firma + AP + letzter Disposition

**Tabs:**
- Alle 6 Tabs funktionieren identisch
- Test-Leads wandern durch die Tabs wie echte Leads
- Tab-Badges (Zaehler) werden aktualisiert

**Stats:**
- Tages-KPIs zaehlen Test-Anrufe und Test-E-Mails mit
- Kein separates Test-Reporting — du siehst exakt was auch in Produktion angezeigt wuerde

### 9.5 Test-Szenarien-Checkliste

Komplett durchspielbar ohne echte Kunden:

**Import-Tests:**
- [ ] Test-CSV importieren → 10 Leads erscheinen in Tab "Neue Leads"
- [ ] Test-CSV erneut importieren → 9 Duplikate erkannt, 0 neue (ausser #4 = Duplikat von #1)
- [ ] Pruefe: Companies, Contacts, Jobs korrekt angelegt

**Call-Screen-Tests:**
- [ ] Lead anklicken → Call-Screen oeffnet sich (3 Spalten)
- [ ] Stellenausschreibung sichtbar und scrollbar
- [ ] Qualifizierungsfragen sichtbar und abhakbar
- [ ] Notizen-Feld funktioniert mit Autosave
- [ ] "Anruf simulieren" → Timer laeuft

**Disposition-Tests (alle 10):**
- [ ] #1: "Nicht erreicht" → Status=angerufen, Wiedervorlage +2 Tage, E-Mail-Flag
- [ ] #2: "Besetzt" → Status=angerufen, Wiedervorlage +1 Tag
- [ ] #3: "Falsche Nummer" → Status=verloren
- [ ] #4: "Sekretariat" → Status=angerufen, Wiedervorlage +3 Tage
- [ ] #5: "Kein Bedarf" → Status=blacklist_weich, Wiedervorlage +180 Tage
- [ ] #5 (Epsilon): "Nie wieder" → Status=blacklist_hart, Company auf Blacklist
- [ ] #7: "Interesse spaeter" → Status=wiedervorlage, Task am Wunschdatum
- [ ] #1 (nach nicht_erreicht): "Will Infos" → E-Mail-Draft generieren
- [ ] #6: "Erst qualifiziert" → Status=qualifiziert, Zweitkontakt-Task
- [ ] #6 (Zweitkontakt): "Voll qualifiziert" → ATSJob erstellt

**Fliessband-Test:**
- [ ] Nach Disposition: naechster Lead erscheint automatisch (kein Zurueck-zur-Liste)
- [ ] Tab-Badges aktualisieren sich
- [ ] Toast-Nachricht zeigt was passiert ist

**E-Mail-Tests:**
- [ ] E-Mail generieren → GPT-Vorschau erscheint
- [ ] Vorschau editieren → Aenderungen bleiben
- [ ] E-Mail senden → kommt in DEINEM Postfach an (nicht beim Kunden)
- [ ] Betreff hat "[TEST]" Prefix
- [ ] Lead wandert in Tab "Nicht erreicht"
- [ ] Gesendete E-Mail ist ueber [E-Mail anzeigen] einsehbar

**Tab "Nicht erreicht" Tests:**
- [ ] Lead mit gesendeter E-Mail erscheint hier
- [ ] "Tage seit E-Mail" Zaehler funktioniert
- [ ] [Erneut anrufen] oeffnet Call-Screen
- [ ] [E-Mail anzeigen] zeigt gesendete E-Mail
- [ ] [Abschliessen] → Lead geht in Archiv

**Wiedervorlage-Tests:**
- [ ] Wiedervorlage fuer heute → erscheint in Tab "Heute anrufen"
- [ ] GET /api/akquise/wiedervorlagen zeigt faellige Leads

**Rueckruf-Test:**
- [ ] "Rueckruf simulieren" → Popup erscheint mit Firma + AP
- [ ] Popup zeigt letzte Disposition + Call-Historie

**Qualifizierung → ATS Test:**
- [ ] #6 voll qualifizieren → ATSJob wird erstellt
- [ ] ATSJob hat Qualifizierungsdaten uebernommen
- [ ] Job erscheint im ATS-Bereich

### 9.6 Test-Modus deaktivieren

```
system_settings: acquisition_test_mode = false
```

Danach:
- `tel:` Links werden echt (Webex oeffnet sich)
- E-Mails gehen an echte Empfaenger (KEIN Prefix, KEINE Umleitung)
- Rueckruf-Simulation verschwindet
- Gelber Banner verschwindet
- Testdaten koennen geloescht werden: `DELETE FROM jobs WHERE company_name LIKE 'Testfirma%'`

### 9.7 Implementierung (Backend)

**Neuer Helper:** `app/services/acquisition_test_helpers.py`

```python
async def is_test_mode(db) -> bool:
    """Prueft ob Akquise-Test-Modus aktiv ist."""
    setting = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "acquisition_test_mode")
    )
    row = setting.scalar_one_or_none()
    return row and row.value == "true"

async def get_test_email(db) -> str:
    """Gibt die Test-E-Mail-Adresse zurueck."""
    setting = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "acquisition_test_email")
    )
    row = setting.scalar_one_or_none()
    return row.value if row else "milad@pulspoint.de"

def override_email_if_test(to_email: str, test_mode: bool, test_email: str) -> tuple[str, str]:
    """Ueberschreibt Empfaenger + Betreff wenn Test-Modus aktiv."""
    if test_mode:
        return test_email, "[TEST] "
    return to_email, ""
```

**Aenderungen an bestehenden Services:**

1. `AcquisitionEmailService.send_email()`:
   - Vor dem Senden: `is_test_mode()` pruefen
   - Wenn ja: `to_email` und Subject-Prefix ueberschreiben

2. `routes_acquisition_pages.py`:
   - `is_test_mode` an jedes Template uebergeben
   - Template zeigt/versteckt Call-Simulation + Rueckruf-Simulation

3. `routes_acquisition.py`:
   - Neuer Endpoint: `POST /api/akquise/test/simulate-call` (nur im Test-Modus)
   - Neuer Endpoint: `POST /api/akquise/test/simulate-callback` (nur im Test-Modus)
   - Beide geben 403 wenn Test-Modus aus

---

## DEPLOYMENT-REIHENFOLGE

| Schritt | Was | Risiko |
|---------|-----|--------|
| 0 | **PRE-LAUNCH:** SPF/DKIM/DMARC fuer alle 3 Domains + Domain-Warmup starten | Keins |
| 1 | Migration 032 deployen (nur Felder, NULLABLE) | Keins — bestehend bleibt unangetastet |
| 2 | Backend-Services + API-Endpoints deployen (hinter Feature-Flag) | Niedrig — eigener Prefix /api/akquise |
| 3 | Frontend: Akquise-Seite + Navigation erweitern | Niedrig — neue Seite, nichts Bestehendes aendern |
| 4 | Test-Modus aktivieren + Test-CSV importieren + E2E-Checkliste durchgehen | Keins — alles im Sandbox |
| 5 | n8n-Workflows konfigurieren (inkl. Reply-Detection + Bounce-Handling) | Niedrig — separate Workflows |
| 6 | **Domain-Warmup abwarten** (2 Wochen, IONOS-Domains) | Keins |
| 7 | Webex-Integration (spaetere Phase) | Mittel — externe Abhaengigkeit |
| 8 | Test-Modus deaktivieren → Produktivbetrieb | - |

**Feature-Flag:** `system_settings` Key: `acquisition_enabled`, Default: `false`
**Rollback:** Flag auf false → Akquise-UI verschwindet, Daten bleiben

### Pre-Launch: Domain-Warmup + DNS (KRITISCH — 2 Wochen vor Go-Live)

**Warum:** Die IONOS-Domains (`sincirus-karriere.de`, `jobs-sincirus.com`) haben keine E-Mail-Reputation. Ohne Warmup landen E-Mails im Spam.

**Schritt 1: DNS-Records setzen (fuer alle 3 Domains)**

| Domain | Record | Wert |
|--------|--------|------|
| sincirus.com | SPF | Bereits vorhanden (M365) — pruefen |
| sincirus-karriere.de | SPF | `v=spf1 include:_spf.perfora.net ~all` (IONOS) |
| sincirus-karriere.de | DKIM | IONOS Auto-Setup |
| sincirus-karriere.de | DMARC | `v=DMARC1; p=none; rua=mailto:hamdard@sincirus.com` |
| jobs-sincirus.com | SPF | `v=spf1 include:_spf.perfora.net ~all` (IONOS) |
| jobs-sincirus.com | DKIM | IONOS Auto-Setup |
| jobs-sincirus.com | DMARC | `v=DMARC1; p=none; rua=mailto:hamdard@sincirus.com` |

**Schritt 2: Warmup-Plan (2 Wochen)**

| Woche | Tages-Limit pro IONOS-Mailbox | Empfaenger |
|-------|-------------------------------|------------|
| Woche 1 | 5-10/Tag | Eigene Adressen + Test-Kontakte |
| Woche 2 | 20/Tag | Echte Kontakte (manuell ausgewaehlt) |
| Woche 3+ | 30-50/Tag | Normaler Betrieb |

**Schritt 3: Validierung**
- `https://mxtoolbox.com/` → SPF/DKIM/DMARC fuer alle Domains pruefen
- Test-Mails an Gmail/Outlook senden → Spam-Ordner kontrollieren
- Erst wenn ALLE 3 Domains sauber: Test-Modus deaktivieren

---

## SICHERHEITS-CHECKLISTE

- [x] SPF/DKIM/DMARC fuer sincirus.com, sincirus-karriere.de, jobs-sincirus.com ✅ (28.02.2026)
- [x] Domain-Warmup 2 Wochen vor Go-Live (IONOS-Domains) ✅ (alle 3 Domains warm gelaufen)
- [x] Abmelde-Link in JEDER E-Mail (oeffentlicher Endpoint, kein Auth) ✅
- [x] Impressum in E-Mail-Signatur ✅ (01.03.2026 — Signatur korrigiert, konsistent mit bestehenden)
- [x] Phone-Normalisierung E.164 bei jedem Import ✅
- [x] Blacklist-Check VOR jedem Anruf und jeder E-Mail ✅
- [x] Blacklist-Cascade: nie_wieder → ALLE Stellen der Firma schliessen ✅
- [x] State-Machine-Validierung: Nur erlaubte Status-Uebergaenge ✅
- [x] Re-Import-Schutz: blacklist_hart darf NICHT reaktiviert werden ✅
- [x] Akquise-Job-Guard: acquisition_source IS NULL im Matching-Filter ✅
- [x] Tages-Limit pro Mailbox (20/Tag fuer IONOS, 100/Tag fuer M365) ✅ (01.03.2026 — Backend-Sperre in send_email())
- [ ] Audit-Log: Import, Calls, E-Mails, Loeschungen, Widersprueche (P2 — spaeter)
- [x] Import-Batch-ID fuer Rollback bei fehlerhaftem Import ✅
- [x] recording_consent Default = false (Aufzeichnung ist Ausnahme) ✅

**Hinweis:** E-Mail-Versand und Anrufaufzeichnung haben KEINE Consent-Gates. Milad entscheidet operativ selbst.

---

## QUALITAETSMETRIKEN (Definition of Done)

- CSV-Import verarbeitet 363+ Zeilen ohne Fehler in <30s
- Duplikat-Erkennung via anzeigen_id funktioniert zuverlaessig
- Re-Import-Schutz: blacklist_hart wird NICHT reaktiviert
- Import-Rollback per Batch-ID funktioniert
- Firma-Deduplizierung via name+city wie bestehend
- Blacklist-Cascade: nie_wieder schliesst ALLE Stellen der Firma
- State-Machine: Ungueltige Status-Uebergaenge werden abgelehnt
- Call-Screen laedt in <1s
- Disposition → naechster Lead in <2s (Fliessband-Modus)
- E-Mail-Generierung (GPT) in <5s (Plain-Text)
- Mailbox-Dropdown zeigt alle 5 Postfaecher mit Limit-Status
- Tages-Limit pro Mailbox wird eingehalten
- Thread-Linking: Follow-up landet im gleichen Thread
- Abmelde-Link funktioniert ohne Login
- Rueckruf-Lookup in <500ms
- Akquise-Jobs erscheinen NICHT im Claude-Matching
- ATS-Konvertierung uebernimmt komplettes Akquise-Dossier
- Alle Daten (Company, Contact, Job, Call, Notes, Emails) korrekt verknuepft
- Keine manuellen Copy-Paste-Schritte fuer den Recruiter
