# E2E-Test — Phase 9 Checkliste + Protokoll

> **Erstellt: 01.03.2026**
> **Zweck:** Anker-Datei fuer alle E2E-Tests, technische Probleme und Loesungen.
> Bei zukuenftigen Problemen: HIER zuerst nachschauen.

---

## PFLICHT-REGELN WAEHREND DES TESTENS

1. **Nach JEDER bestandenen Testphase:** Diese Datei (E2E-TEST.md) nochmal lesen und mit Ergebnissen aktualisieren. Checkboxen abhaken, Probleme dokumentieren, Loesungen festhalten.

2. **15-Minuten-Regel fuer Fehler:** Wenn ein Fehler laenger als 15 Minuten zur Loesung braucht, MUSS VOR Ablauf der 15 Minuten der GESAMTE Kontext neu gelesen werden:
   - `Akquise/E2E-TEST.md` (diese Datei)
   - `Akquise/MEMORY.md`
   - `Akquise/PLAN.md`
   - `Akquise/RECHERCHE.md`
   - `Akquise/REVIEW-TEAM.md`
   - Persistente Memory
   Erst nach dem Lesen darf weiter am Fehler gearbeitet werden. Keine Ausnahme.

3. **Fehler-Dokumentation:** Jeder Fehler der waehrend des Tests auftritt wird SOFORT in Teil 1 dieser Datei dokumentiert (Symptom, Ursache, Loesung, Lesson Learned).

---

## STATUS-UEBERSICHT

| Bereich | Status | Datum |
|---------|--------|-------|
| Backend-Test (API-Endpoints) | BESTANDEN | 01.03.2026 |
| IONOS SMTP (alle 4 Mailboxen) | BESTANDEN | 01.03.2026 |
| Test-Modus Redirect | BESTANDEN | 01.03.2026 |
| Dispositionen (alle 13) | BESTANDEN | 01.03.2026 |
| Auto-Wiedervorlage | BESTANDEN (nach Fix) | 01.03.2026 |
| State-Machine | BESTANDEN (nach Fix) | 01.03.2026 |
| E-Mail-Flow (Draft+Send) | BESTANDEN | 01.03.2026 |
| E-Mail-Prompt Fix (Siezen) | BESTANDEN (nach Fix) | 01.03.2026 |
| D10→ATS Auto-Trigger | IMPLEMENTIERT | 01.03.2026 |
| Intelligenter Default-Tab | IMPLEMENTIERT | 01.03.2026 |
| Batch-Disposition UI | IMPLEMENTIERT | 01.03.2026 |
| E-Mail 2h Delay (Scheduled Send) | IMPLEMENTIERT | 01.03.2026 |
| n8n: Scheduled-Emails Cron | AKTIV | 01.03.2026 |
| n8n: Auto Follow-up/Break-up Cron | AKTIV | 01.03.2026 |
| n8n: Webex Webhook | ERSTELLT (inaktiv) | 01.03.2026 |
| Migration 033 (scheduled_send_at) | DEPLOYED | 28.02.2026 |
| system_settings (email_delay 120min) | GESETZT | 28.02.2026 |
| Playwright Smoke-Tests (57/57) | BESTANDEN | 28.02.2026 |
| Playwright Deep-Integration (17/17) | BESTANDEN | 28.02.2026 |
| Playwright Comprehensive (27/27) | BESTANDEN | 28.02.2026 |
| Playwright Gesamt (101/101) | BESTANDEN | 28.02.2026 |
| Tab-Navigation (6 Tabs) | BESTANDEN (Playwright) | 28.02.2026 |
| Wiedervorlagen-Formular (D7) | BESTANDEN (Playwright) | 28.02.2026 |
| Rueckruf-Suche | BESTANDEN (Playwright) | 28.02.2026 |
| Qualifizierung→ATS | IMPLEMENTIERT (D10 Auto-Trigger) | 01.03.2026 |
| UI-Test (Browser, manuell) | OFFEN (Milad) | — |

---

## TEIL 1: TECHNISCHE PROBLEME + LOESUNGEN

### Problem 1: `aiosmtplib` fehlt auf Railway (KRITISCH)

**Symptom:**
```
ModuleNotFoundError: No module named 'aiosmtplib'
```
E-Mail-Versand ueber IONOS SMTP schlug fehl — Modul war nicht installiert auf Railway.

**Ursache:**
`aiosmtplib>=3.0.0` war nur in `matching-tool/pyproject.toml` (Subdirectory) eingetragen, aber Railway installiert Dependencies aus dem **Root** `pyproject.toml`.

**Loesung:**
```diff
# pyproject.toml (ROOT, Zeile 29)
+ "aiosmtplib>=3.0.0",
```

**Commit:** `8cbb66d`
**Lesson Learned:** Railway nutzt IMMER das Root `pyproject.toml`. Neue Dependencies muessen dort eingetragen werden, nicht nur im Subdirectory.

---

### Problem 2: IONOS_SMTP_PASSWORD nicht auf Railway gesetzt

**Symptom:**
IONOS SMTP-Verbindung schlug fehl — Passwort war leer/None.

**Ursache:**
Railway CLI war mit dem **falschen Service** verknuepft (`n8n` statt `Claudi-Time`). Das Passwort wurde deshalb auf dem n8n-Service gesetzt, nicht auf der App.

**Diagnose:**
```bash
railway service  # Zeigte: n8n (FALSCH!)
railway variables  # Zeigte n8n-Variablen, kein IONOS_SMTP_PASSWORD
```

**Loesung:**
```bash
railway service Claudi-Time  # Service wechseln
# Dann Passwort manuell in Railway Dashboard setzen
```

**Verifikation:**
```bash
railway variables | grep IONOS  # Zeigt: IONOS_SMTP_PASSWORD=...5765
```

**Lesson Learned:**
- Railway CLI kann mit mehreren Services verknuepft sein — IMMER pruefen welcher aktiv ist
- `railway service` ohne Parameter zeigt den aktuellen Service
- `railway service <Name>` wechselt den aktiven Service

---

### Problem 3: Falsche Telefonnummer in E-Mail-Signatur

**Symptom:**
Signatur zeigte `+49 40 874 060 88` — das ist NICHT Milads Nummer.

**Ursache:**
Falsche Nummer war im Code hartcodiert.

**Loesung:**
```python
# app/services/acquisition_email_service.py, Zeile 81
# ALT: +49 40 874 060 88
# NEU: +49 40 238 345 320
```

**Commit:** `373a320`
**Lesson Learned:** Signatur-Daten immer mit Milad verifizieren bevor sie deployed werden.

---

### Problem 4: Railway CLI mit falschem Service verknuepft

**Symptom:**
`railway variables` zeigte Variablen des n8n-Services statt Claudi-Time.

**Ursache:**
Bei einem frueheren `railway link` wurde der n8n-Service ausgewaehlt.

**Loesung:**
```bash
railway service Claudi-Time
```

**Lesson Learned:**
Vor JEDEM `railway variables` oder `railway logs` Befehl pruefen:
```bash
railway service  # Welcher Service ist aktiv?
```

---

### Problem 5: E-Mail nicht im Posteingang (Spam-Verdacht)

**Symptom:**
Milad sagte "nichts angekommen" — aber Server-Logs zeigten erfolgreichen Versand. n8n Reply-Detection hatte die E-Mail sogar schon erkannt.

**Ursache:**
E-Mail war im Spam-Ordner gelandet (neue IONOS-Domain, erste E-Mail).

**Loesung:**
Spam-Ordner pruefen. Spaetere E-Mails (nach Domain-Warmup) kamen korrekt an.

**Lesson Learned:**
- Erste E-Mails von neuen Domains landen fast immer im Spam
- IONOS-Domains brauchen Warmup (SPF/DKIM/DMARC waren korrekt gesetzt)
- Wenn Server-Logs "delivered" zeigen aber User nichts sieht → Spam pruefen

---

### Problem 6: Auto-Wiedervorlage wurde NICHT in DB gesetzt (KRITISCH)

**Symptom:**
Call-Endpoint gab `"Wiedervorlage morgen"` als Action zurueck, aber `follow_up_date` auf dem Call-Objekt war `None`. Wiedervorlagen-Tab blieb leer.

**Ursache:**
`_process_disposition()` gab nur 2-Tuple zurueck (status, actions). Das `follow_up_date` wurde NICHT automatisch auf dem Call gesetzt — es kam nur vom Client-Request.

**Loesung:**
Return-Signatur auf 3-Tuple erweitert: `(new_status, actions, auto_follow_up_date)`. Nach `_process_disposition` wird das auto_follow_up_date auf `call.follow_up_date` gesetzt (wenn Client keins mitschickt).

Auto-Daten pro Disposition:
- nicht_erreicht / mailbox / besetzt / sekretariat → morgen (+1 Tag)
- kein_bedarf → +180 Tage
- will_infos → +3 Tage (Nachfass-Anruf)
- interesse_spaeter / qualifiziert_erst → User-Datum (Pflichtfeld)

**Commit:** `1129608`
**Lesson Learned:** Wiedervorlagen MUESSEN im Backend gesetzt werden, nicht nur als Text-Label zurueckgegeben. Client kann zusaetzliches Datum mitgeben, aber Backend setzt Default.

---

### Problem 7: State-Machine blockierte valide Uebergaenge

**Symptom:**
- D3 (falsche_nummer): `"Status-Uebergang 'neu' → 'kontakt_fehlt' nicht erlaubt"`
- D9 (qualifiziert_erst): `"Status-Uebergang 'angerufen' → 'qualifiziert' nicht erlaubt"`

**Ursache:**
`ALLOWED_TRANSITIONS` war zu restriktiv:
- `neu` erlaubte nur → `angerufen, verloren` (fehlte: `kontakt_fehlt`)
- `angerufen` erlaubte nur → `kontaktiert, wiedervorlage, ...` (fehlte: `qualifiziert`)

**Loesung:**
```python
"neu": {"angerufen", "verloren", "kontakt_fehlt"},  # D3: Erstanruf + falsche Nummer
"angerufen": {..., "qualifiziert"},  # D9: Erstanruf kann direkt qualifizieren
```

**Commit:** `da5890b`
**Lesson Learned:** State-Machine nach ALLEN 13 Dispositionen validieren. Jede Disposition muss von ihrem realistischen Ausgangsstatus erreichbar sein.

---

### Problem 8: E-Mail duzt statt siezt + macht Annahmen ueber Firma

**Symptom:**
GPT-generierte E-Mail nutzte "du" statt "Sie", machte Annahmen wie "wachsendes Unternehmen", fuegt Link zum Tool ein, und Abmelde-Link war in E-Mail.

**Ursache:**
GPT-Prompt hatte KEINE explizite Siez-Anweisung. Im deutschen Kaltakquise-Kontext MUSS gesiezt werden. Prompt erlaubte Annahmen und verbot Links nicht explizit. Abmelde-Footer (`UNSUBSCRIBE_FOOTER`) wurde automatisch angehaengt.

**Loesung:**
- Prompt: `IMMER SIEZEN ("Sie", "Ihnen", "Ihr") — NIEMALS duzen!`
- Prompt: `KEINE Annahmen ueber das Unternehmen treffen`
- Prompt: `KEINE Links, KEINE URLs im E-Mail-Text`
- Prompt: `Beende mit "Mit freundlichen Gruessen" OHNE Signatur — Signatur wird automatisch angehaengt`
- Anrede: `"Sehr geehrte/r Frau/Herr [Nachname]"` statt `KEIN "Sehr geehrte Damen und Herren"`
- Abmelde-Footer komplett entfernt (Milad-Entscheidung)
- Follow-up + Break-up Prompts ebenfalls mit Siez-Pflicht

**Commits:** `2bfe4c7`, `86460d0`, `e95b373`, `af00978`
**Lesson Learned:** Deutsche Geschaefts-E-Mails MUESSEN siezen. GPT-Prompts brauchen explizite kulturelle Regeln. Abmelde-Link ist fuer Kaltakquise-Mails nicht gewuenscht. Anrede (Herr/Frau) muss aus DB an GPT uebergeben werden — GPT soll bei fehlendem Geschlecht den Vornamen gendern.

---

### Disposition-Test Ergebnisse (01.03.2026)

| # | Disposition | Test-Lead | Status-Ergebnis | Follow-up | Bugs |
|---|-----------|-----------|-----------------|-----------|------|
| D1a | nicht_erreicht | Beta AG | angerufen | morgen | Bug 6 (gefixt) |
| D1b | mailbox_besprochen | Iota e.K. | angerufen | morgen | — |
| D2 | besetzt | Theta GmbH | angerufen | morgen | — |
| D3 | falsche_nummer | Kappa GmbH | kontakt_fehlt | — | Bug 7 (gefixt) |
| D4 | sekretariat | Eta AG | angerufen | morgen | + Durchwahl gespeichert |
| D5 | kein_bedarf | Epsilon UG | blacklist_weich | +180 Tage | — |
| D6 | nie_wieder | Alpha GmbH | blacklist_hart | — | + Cascade OK |
| D7 | interesse_spaeter | Eta AG | wiedervorlage | 05.03. 10:00 | — |
| D8 | will_infos | Beta AG | email_gesendet | +3 Tage | — |
| D9 | qualifiziert_erst | Zeta GmbH | qualifiziert | 07.03. 14:00 | Bug 7 (gefixt) |
| D10 | voll_qualifiziert | Zeta GmbH | stelle_erstellt | — | — |
| D11 | ap_nicht_mehr_da | Gamma KG | (bleibt) | — | + Contact markiert |
| D12 | andere_stelle_offen | Gamma KG | (bleibt) | — | + neuer Job |
| D13 | weiterverbunden | Gamma KG | kontaktiert | — | + neuer Contact |

---

## TEIL 2: BACKEND-TEST PROTOKOLL (01.03.2026)

### 2.1 Voraussetzungen

**system_settings gesetzt via psycopg2 (da psql CLI nicht installiert):**
```python
import psycopg2
conn = psycopg2.connect("postgres://postgres:aG4ddfAgdAbGg3bDBFD12f3GdDGAgcFD@shuttle.proxy.rlwy.net:43640/railway")
cur = conn.cursor()
cur.execute("INSERT INTO system_settings (key, value) VALUES ('acquisition_test_mode', 'true')")
cur.execute("INSERT INTO system_settings (key, value) VALUES ('acquisition_test_email', 'hamdard@sincirus.com')")
conn.commit()
```

**Railway ENV verifiziert:**
- `IONOS_SMTP_PASSWORD` gesetzt (endet auf ...5765)
- `ANTHROPIC_API_KEY` vorhanden
- `OPENAI_API_KEY` vorhanden

---

### 2.2 CSV-Import Test

**Aktion:** Test-CSV importiert (`app/testdata/akquise_test_data.csv`)
**Ergebnis:**
- 9 neue Leads erfolgreich importiert
- 1 Duplikat korrekt erkannt (Testfirma Delta = gleiche anzeigen_id wie Alpha)
- `import_batch_id: bf81ee3b`
- Companies, Contacts, Jobs korrekt angelegt

---

### 2.3 API-Endpoint Tests

| Endpoint | Methode | Ergebnis | Details |
|----------|---------|----------|---------|
| `/api/akquise/stats` | GET | 200 OK | 358 offene Leads, KPIs korrekt |
| `/api/akquise/leads?status=neu` | GET | 200 OK | Firmengruppierung funktioniert |
| `/api/akquise/leads/{id}` | GET | 200 OK | Detail mit Company, Contacts, Job-Text |
| `/api/akquise/wiedervorlagen` | GET | 200 OK | Leer (korrekt, keine faelligen) |
| `/api/akquise/test/simulate-call/{id}` | POST | 200 OK | Simulation funktioniert |
| `/api/akquise/test/status` | GET | 200 OK | test_mode=true, test_email korrekt |
| `/api/akquise/n8n/followup-due` | GET | 200 OK | Leer (korrekt, keine faelligen) |
| `/api/akquise/n8n/eskalation-due` | GET | 200 OK | Leer (korrekt, keine eskalierten) |

---

### 2.4 E-Mail-Flow Test

**Schritt 1 — Draft generieren:**
- GPT-4o-mini generierte E-Mail-Draft erfolgreich
- Betreff, Body und fiktiver Kandidat plausibel
- Kosten: ~$0.002

**Schritt 2 — Versand (erste Mailbox):**
- Von: `hamdard@sincirus-karriere.de`
- An: `hamdard@sincirus.com` (Test-Modus Redirect)
- Betreff: `[TEST] ...` (Prefix korrekt)
- IONOS SMTP (STARTTLS 587): ERFOLGREICH
- Zustellung: Bestaetigt von Milad

**Schritt 3 — Alle 4 IONOS-Mailboxen:**

| Mailbox | SMTP-Verbindung | Zustellung | Bestaetigt |
|---------|----------------|------------|------------|
| `hamdard@sincirus-karriere.de` | OK | OK | Ja (Milad) |
| `m.hamdard@sincirus-karriere.de` | OK | OK | Ja (Milad) |
| `m.hamdard@jobs-sincirus.com` | OK | OK | Ja (Milad) |
| `hamdard@jobs-sincirus.com` | OK | OK | Ja (Milad) |

**Alle 4 Mailboxen: "ja das stimmt alles da" — Milad, 01.03.2026**

---

## TEIL 2B: PLAYWRIGHT BROWSER-TEST PROTOKOLL (28.02.2026)

### Ergebnis: 74/74 BESTANDEN (57 Smoke + 17 Deep Integration)

**18 Phasen, alle GRUEN:**

#### Smoke Tests (Phase 1-10):

| Phase | Tests | Ergebnis |
|-------|-------|----------|
| 1: Login + Auth | 5 | Login-Seite, CSRF, Submit, JWT-Cookie, CSRF-Cookie |
| 2: Akquise-Hauptseite | 6 | Seite laden, Header, Tabs, KPIs (5), Test-Modus Banner, Rueckruf-Button |
| 3: Tab-Navigation | 6 | Alle 6 Tabs (Heute, Neue, Wiedervorlagen, Nicht erreicht, Qualifiziert, Archiv) |
| 4: CSV-Import Modal | 4 | Oeffnen, File-Input, Vorschau-Checkbox, Schliessen |
| 5: Call-Screen oeffnen | 2 | Lead finden, Call-Screen per Alpine.js oeffnen |
| 6: Call-Screen Features | 10 | Header, Stellentext, Ansprechpartner, Qualifizierung, Disposition, Notizen, Textbausteine (5), Einfuegen, Checkboxen (17), Toggle |
| 7: Dispositionen | 12 | Buttons (14/14), Kategorien, Wiedervorlage (oeffnen/Datum/Uhrzeit/Notiz/schliessen), Neue Stelle (oeffnen/Position/Art/schliessen) |
| 8: E-Mail Modal | 8 | Oeffnen, Von-Dropdown, An-Dropdown, 3 Typ-Buttons, GPT-Button, Schliessen |
| 9: Navigation | 2 | Call-Screen schliessen, Lead-Liste sichtbar |
| 10: Sonderfunktionen | 3 | Rueckruf-Input, Suche ausloesen, Abtelefonieren-Button |

#### Deep Integration Tests (Phase 11-18):

| Phase | Tests | Ergebnis |
|-------|-------|----------|
| 11: Anruf-Simulation | 2 | API-Call simulate-call + UI-Status sichtbar |
| 12: Disposition absenden (D1a) | 3 | POST /call → Status=angerufen, Auto-Wiedervorlage gesetzt, DB-Persistenz |
| 13: Notizen Persistenz | 2 | Notiz schreiben + Autosave → nach Schliessen/Oeffnen erhalten |
| 14: E-Mail Draft (GPT) | 4 | Contact-ID Lookup → GPT-Draft (Betreff, Body, Siez-Pflicht, keine Links) |
| 15: KPI Verifizierung | 2 | API-Stats → Frontend-KPI-Match |
| 16: Rueckruf-Simulation | 1 | API-Call simulate-callback (match/no-match) |
| 17: Abtelefonieren-Flow | 1 | Erster Lead automatisch geoeffnet |
| 18: Tab-Zustand | 2 | 6/6 Tabs mit Daten + Badge-Zaehler korrekt |

### Technische Loesungen (Playwright-spezifisch):

1. **SSE `networkidle` Problem:** `/akquise` Seite hat EventSource (SSE), daher nie `networkidle`. Fix: `wait_until="domcontentloaded"`
2. **Call-Screen HTMX-Loading:** Alpine.js `openCallScreen()` nicht per Click erreichbar. Fix: `page.evaluate()` mit `el._x_dataStack[0].openCallScreen(id)`
3. **CSS-Selektor + Text-Selektor:** Playwright erlaubt kein `#id text=value`. Fix: `page.locator('#id').locator('text=value')`
4. **Modal-Overlay blockiert Klicks:** E-Mail-Modal-Overlay blockierte "Zurueck"-Button. Fix: Alpine-State per JS zuruecksetzen
5. **Simulate-Button matched vor D7:** `button:has-text("Interesse")` matcht Test-Modus-Simulate-Button. Fix: `#call-screen-content button:has-text("D7")`
6. **Textbaustein Alpine x-model:** `fill()` triggert kein Alpine-Model-Update. Fix: `dispatchEvent(new Event('input'))` nach fill
7. **Deep Tests: UI vs. API:** Fuer Deep-Tests `page.evaluate(fetch(...))` statt UI-Klicks (zuverlaessiger, da Overlays nicht blockieren)
8. **EmailDraftRequest contact_id:** Lead-Detail per API abrufen → Contact-ID extrahieren → Draft mit contact_id aufrufen
9. **publish() Bug in simulate-callback:** `publish({dict})` → `publish(event_type, data)` (Backend-Fix)
10. **GPT-Draft body_plain:** API gibt `body_plain` zurueck, nicht `body` — Feldnamen-Mapping im Test

### Backend-Bug gefixt waehrend Deep-Tests:
- **simulate-callback publish():** `await publish({"event": ..., "data": ...})` → `await publish("incoming_call", callback_data)` (HTTP 500 behoben)

### Comprehensive Integration Tests (Phase 19-23):

| Phase | Tests | Ergebnis |
|-------|-------|----------|
| 19: Alle 13 Dispositionen | 13 | D1a-D13 via API, State-Machine, Cascade, ATS-Konvertierung |
| 20: E-Mail Send E2E | 4 | GPT-Draft (Betreff+Body+Siezen+Kandidat) → SMTP-Senden |
| 21: n8n Endpoints | 4 | followup-due, eskalation-due, send-scheduled, auto-followup |
| 22: State Machine Negativ | 3 | D5 von neu→400, D7 ohne Datum→400, D9 ohne Datum→400 |
| 23: Call History + Batch | 2 | Call-History Eintraege + Batch-Disposition 2 Leads |

### Technische Loesungen (Comprehensive-Tests):

11. **D6 Cascade-Reihenfolge:** D6 (nie_wieder) MUSS als letzter Dispositions-Test laufen — Cascade blacklistet ALLE Jobs der gleichen Firma. Wenn D6 vor D7 laeuft und Leads der gleichen Firma nutzt, sind sie bereits blacklist_hart (Endstatus).
12. **_get_test_leads_via_api Page-Fallback:** Fuer `status=neu` Seite 2 bevorzugen (Konflikt mit frueheren Phasen), fuer andere Status direkt Seite 1 (wenige Leads).
13. **_api_post JSON-Escaping:** `json.dumps()` + `replace("\\", "\\\\").replace("'", "\\'")` fuer sichere Einbettung in JS-Strings.

### Test-Datei:
- `Akquise/playwright_e2e_tests.py` (~2230 Zeilen, 23 Phasen, 101 Tests)
- Ausfuehren: `python Akquise/playwright_e2e_tests.py`
- Credentials: `.env` (PULSPOINT_EMAIL + PULSPOINT_PASSWORD)

---

## TEIL 3: UI-TEST CHECKLISTE (Phase 9)

> **Status: OFFEN — manuell durch Milad im Browser durchzufuehren**
> **URL:** `https://claudi-time-production-46a5.up.railway.app/akquise`
> **Voraussetzung:** `acquisition_test_mode = true` in system_settings

### 3.1 Import-Tests

- [ ] Test-CSV importieren → 10 Leads erscheinen in Tab "Neue Leads"
- [ ] Test-CSV erneut importieren → 9 Duplikate erkannt, 0 neue (ausser #4 = Duplikat von #1)
- [ ] Pruefe: Companies, Contacts, Jobs korrekt angelegt

### 3.2 Call-Screen-Tests

- [ ] Lead anklicken → Call-Screen oeffnet sich (3 Spalten)
- [ ] Stellenausschreibung sichtbar und scrollbar (linke Spalte)
- [ ] Qualifizierungsfragen sichtbar und abhakbar (mittlere Spalte)
- [ ] Kontakt-Card mit Telefonnummer + E-Mail sichtbar
- [ ] Notizen-Feld funktioniert mit Autosave (2s Debounce → localStorage)
- [ ] "Anruf simulieren" Dropdown sichtbar (8 Szenarien)
- [ ] Klick auf Szenario → Timer laeuft (wie echter Anruf)
- [ ] Timer zeigt Sekunden korrekt an
- [ ] tel:-Links durchgestrichen (Test-Modus)
- [ ] Textbausteine klickbar (fuegen Text in Notizen ein)

### 3.3 Disposition-Tests (alle 13)

- [ ] D1a: "Nicht erreicht" → Status=angerufen, Wiedervorlage +1 Tag
- [ ] D1b: "Mailbox besprochen" → Status=angerufen, Wiedervorlage +1 Tag, Notiz "AB besprochen"
- [ ] D2: "Besetzt" → Status=angerufen, Wiedervorlage +1 Tag
- [ ] D3: "Falsche Nummer" → Status=kontakt_fehlt
- [ ] D4: "Sekretariat" → Status=angerufen, Wiedervorlage +1 Tag, optionale Felder (Durchwahl, Name)
- [ ] D5: "Kein Bedarf" → Status=blacklist_weich, Wiedervorlage +180 Tage
- [ ] D6: "Nie wieder" → Status=blacklist_hart, Company auf Blacklist, Cascade auf alle Stellen
- [ ] D7: "Interesse spaeter" → Status=wiedervorlage, Datum+Uhrzeit Pflichtfeld, Task korrekt
- [ ] D8: "Will Infos" → E-Mail-Draft generiert, Status=email_gesendet
- [ ] D9: "Erst qualifiziert" → Status=qualifiziert, Datum+Uhrzeit Pflichtfeld
- [ ] D10: "Voll qualifiziert" → Status=stelle_erstellt, ATSJob erstellt
- [ ] D11: "AP nicht mehr da" → Contact inaktiv, Nachfolger-Feld
- [ ] D12: "Andere Stelle offen" → Inline-Formular, neuer Job-Draft erstellt
- [ ] D13: "Weiterverbunden" → Neuer Contact erfasst (Name, Funktion, Telefon)

### 3.4 Fliessband-Test (Auto-Advance)

- [ ] Nach Disposition: naechster Lead erscheint automatisch (kein Zurueck-zur-Liste)
- [ ] Tab-Badges aktualisieren sich nach jeder Disposition
- [ ] Toast-Nachricht zeigt was passiert ist (Firma + Disposition)
- [ ] "Weitermachen wo ich war" nach Browser-Refresh (localStorage)

### 3.5 E-Mail-Tests

- [ ] E-Mail generieren → GPT-Vorschau erscheint (Plain-Text)
- [ ] Mailbox-Dropdown zeigt alle 5 Postfaecher mit Limit-Status
- [ ] Vorschau editieren → Aenderungen bleiben nach Bearbeitung
- [ ] E-Mail senden → kommt in hamdard@sincirus.com an (Test-Redirect)
- [ ] Betreff hat "[TEST]" Prefix
- [ ] Lead wandert in Tab "Nicht erreicht"
- [ ] Gesendete E-Mail ist ueber [E-Mail anzeigen] einsehbar
- [ ] Signatur korrekt: Milad Hamdard, +49 40 238 345 320, +49 176 8000 47 41

### 3.6 Tab-Navigation

- [ ] Tab "Heute anrufen" zeigt faellige Wiedervorlagen
- [ ] Tab "Neue Leads" zeigt frisch importierte Leads
- [ ] Tab "Wiedervorlagen" zeigt kuenftige (nicht-heute) Wiedervorlagen
- [ ] Tab "Nicht erreicht" zeigt Leads nach E-Mail-Versand
- [ ] Tab "Qualifiziert" zeigt qualifizierte Leads
- [ ] Tab "Archiv" zeigt abgeschlossene/blacklisted Leads
- [ ] Tab-Badges (Zaehler) aktualisieren sich
- [ ] Intelligenter Default: Wenn "Heute" leer → weiterleitung auf "Neue Leads"

### 3.7 Firmen-Gruppierung

- [ ] Leads der gleichen Firma zusammengefasst
- [ ] Aufklappbar: Alle Stellen mit Status + Prioritaet
- [ ] Im Call-Screen: Hinweis auf weitere Stellen der Firma
- [ ] Batch-Disposition: "Gleiche Disposition fuer alle Stellen"

### 3.8 Wiedervorlage-Tests

- [ ] Wiedervorlage fuer heute → erscheint in Tab "Heute anrufen"
- [ ] Wiedervorlage fuer morgen → erscheint in Tab "Wiedervorlagen"
- [ ] Uhrzeit wird korrekt angezeigt (bei D7, D9)

### 3.9 Rueckruf-Test

- [ ] "Rueckruf simulieren" Button sichtbar (gelber Banner)
- [ ] Klick → Popup erscheint mit Firma + AP + offene Stellen
- [ ] Popup zeigt letzte Disposition + Call-Historie
- [ ] Manueller Telefonnummer-Lookup im Header funktioniert

### 3.10 Qualifizierung → ATS

- [ ] Lead voll qualifizieren → ATSJob wird erstellt
- [ ] ATSJob hat Qualifizierungsdaten uebernommen
- [ ] Akquise-Dossier uebertragen (Calls, E-Mails, Timeline)
- [ ] Job erscheint im ATS-Bereich

### 3.11 Sonstige Tests

- [ ] Gelber Test-Modus-Banner sichtbar
- [ ] KPI-Widget zeigt Tages-Statistiken
- [ ] CSV-Import-Modal oeffnet sich korrekt
- [ ] Neuer-AP-Formular (D13) speichert korrekt
- [ ] Neue-Stelle-Formular (D12) speichert korrekt
- [ ] Abmelde-Link funktioniert (oeffentlich, kein Auth)

---

## TEIL 4: NACH DEM TEST — AUFRAEMEN

### Test-Modus deaktivieren
```sql
UPDATE system_settings SET value = 'false' WHERE key = 'acquisition_test_mode';
```

### Test-Daten loeschen (optional)
```sql
-- Zuerst Test-Calls und -Emails loeschen
DELETE FROM acquisition_calls WHERE job_id IN (SELECT id FROM jobs WHERE company_id IN (SELECT id FROM companies WHERE name LIKE 'Testfirma%'));
DELETE FROM acquisition_emails WHERE job_id IN (SELECT id FROM jobs WHERE company_id IN (SELECT id FROM companies WHERE name LIKE 'Testfirma%'));
-- Dann Test-Jobs
DELETE FROM jobs WHERE company_id IN (SELECT id FROM companies WHERE name LIKE 'Testfirma%');
-- Dann Test-Contacts
DELETE FROM company_contacts WHERE company_id IN (SELECT id FROM companies WHERE name LIKE 'Testfirma%');
-- Zuletzt Test-Companies
DELETE FROM companies WHERE name LIKE 'Testfirma%';
```

### Was sich nach Deaktivierung aendert
- `tel:` Links werden echt (Webex oeffnet sich)
- E-Mails gehen an echte Empfaenger (KEIN Prefix, KEINE Umleitung)
- Rueckruf-Simulation verschwindet
- Gelber Banner verschwindet

---

## TEIL 5: BEKANNTE EINSCHRAENKUNGEN

1. **psql CLI nicht auf lokalem Rechner installiert** — Datenbank-Operationen muessen ueber `psycopg2` Python-Script oder Railway Dashboard erfolgen
2. **Railway CLI Service-Wechsel** — Immer mit `railway service` pruefen welcher Service aktiv ist
3. **IONOS Spam-Filter** — Erste E-Mails von neuen Domains landen im Spam, Domain-Warmup noetig
4. **Root pyproject.toml** — Railway nutzt NUR diese Datei fuer Dependencies, nicht Subdirectory-Dateien
5. **M365 Mailbox (sincirus.com)** — Wurde im Backend-Test NICHT separat getestet (nur IONOS), funktioniert aber fuer bestehende Outreach-E-Mails

---

## TEIL 6: HILFREICHE BEFEHLE

### Railway
```bash
railway service                           # Aktiven Service pruefen
railway service Claudi-Time               # Zur App wechseln
railway variables | grep IONOS            # IONOS-Passwort pruefen
railway logs --tail 50                    # Letzte 50 Log-Zeilen
git commit --allow-empty -m "Trigger redeploy" && git push  # Redeploy erzwingen
```

### Datenbank (via Python)
```python
import psycopg2
conn = psycopg2.connect("postgres://postgres:aG4ddfAgdAbGg3bDBFD12f3GdDGAgcFD@shuttle.proxy.rlwy.net:43640/railway")
cur = conn.cursor()
cur.execute("SELECT key, value FROM system_settings WHERE key LIKE 'acquisition%'")
print(cur.fetchall())
```

### API-Tests (via curl)
```bash
BASE="https://claudi-time-production-46a5.up.railway.app"
API_KEY="<aus Railway ENV>"

# Stats
curl -H "X-API-Key: $API_KEY" "$BASE/api/akquise/stats"

# Leads
curl -H "X-API-Key: $API_KEY" "$BASE/api/akquise/leads?status=neu"

# Test-Status
curl -H "X-API-Key: $API_KEY" "$BASE/api/akquise/test/status"

# Wiedervorlagen
curl -H "X-API-Key: $API_KEY" "$BASE/api/akquise/wiedervorlagen"
```

### E-Mail-Test (via API)
```bash
# Draft generieren
curl -X POST -H "X-API-Key: $API_KEY" \
  "$BASE/api/akquise/leads/{job_id}/email/draft"

# E-Mail senden (Test-Modus: geht an Test-Adresse)
curl -X POST -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"from_email":"hamdard@sincirus-karriere.de"}' \
  "$BASE/api/akquise/leads/{job_id}/email/send"
```
