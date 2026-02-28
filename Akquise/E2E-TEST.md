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
| UI-Test (Phase 9 Checkliste) | OFFEN | — |

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
