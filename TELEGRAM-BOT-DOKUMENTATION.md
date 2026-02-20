# PulsePoint Telegram Bot — Dokumentation

> **Stand: 20.02.2026 — Alle 6 Phasen implementiert, Ready for Deploy**

---

## Uebersicht

Der **@Sincirusbot** ist ein interaktiver Telegram Bot, der als mobile Kommandozentrale fuer PulsePoint CRM dient. Er ermoeglicht Aufgabenverwaltung, Kandidatensuche, Call-Logging mit KI-Extraktion, Stelle-PDF-Generierung und automatische Benachrichtigungen — alles per Chat oder Sprachnachricht.

**Architektur:** Webhook-basiert (nicht Polling), FastAPI-Endpoint, GPT-4o-mini Intent-Klassifizierung, Whisper Spracherkennung, Single-User (Milad).

---

## Was wurde gebaut?

### Phase A — Foundation
| Datei | Aenderung |
|-------|-----------|
| `app/config.py` | 3 neue Settings: `telegram_bot_token`, `telegram_chat_id`, `telegram_webhook_secret` |
| `app/auth.py` | `/api/telegram/` zu PUBLIC_PREFIXES hinzugefuegt (eigene Secret-Token Auth) |

### Phase B — Telegram Bot Core (4 neue Dateien + 1 Aenderung)
| Datei | Beschreibung |
|-------|-------------|
| `app/services/telegram_intent_service.py` | **NEU** — GPT-4o-mini Intent-Klassifizierung + Whisper Transkription |
| `app/services/telegram_bot_service.py` | **NEU** — Kern-Bot: Commands, Free-Text, Voice, Inline-Keyboards |
| `app/api/routes_telegram.py` | **NEU** — Webhook-Endpoint + Webhook-Registrierung bei App-Start |
| `app/main.py` | Router registriert + `register_webhook()` im Lifespan |

### Phase C — n8n Scheduled Workflows (3 neue Workflows)
| Workflow | n8n ID | Schedule | Beschreibung |
|----------|--------|----------|-------------|
| Abend-Zusammenfassung | `OOAHopP2YdOy7oud` | Mo-Fr 18:00 | Tages-Summary mit erledigten + offenen Tasks |
| Wochen-Briefing | `69XsYWe3yy6RI1vO` | Mo 07:30 | Wochen-Uebersicht: Pipeline, Matches, Action Items |
| Aufgaben-Erinnerung | `mYcVUuq2DVEMwp2w` | Alle 5 Min (8-19h, Mo-Fr) | Faellige Tasks als Telegram-Nachricht |

### Phase D — Call Logging & Job-Qualifizierung (1 neue Datei + 1 Aenderung)
| Datei | Beschreibung |
|-------|-------------|
| `app/services/telegram_call_handler.py` | **NEU** — Call-Logging: Kandidaten- und Kunden-Calls, Auto-Erkennung, Job-Quali-Extraktion |
| `app/services/ats_job_service.py` | `update_qualification_fields()` Methode hinzugefuegt |

### Phase E — Stelle-PDF Generation (2 neue Dateien + 2 Aenderungen)
| Datei | Beschreibung |
|-------|-------------|
| `app/services/ats_job_pdf_service.py` | **NEU** — WeasyPrint PDF-Generator fuer ATSJob-Stellen |
| `app/templates/ats_job_sincirus.html` | **NEU** — Sincirus-gebrandetes HTML-Template (Navy/Gruen) |
| `app/api/routes_ats_jobs.py` | `GET /{job_id}/pdf?candidate_id=optional` Endpoint |
| `app/services/telegram_bot_service.py` | `/stellenpdf <id>` Command + PDF-Versand via Telegram |

### Phase F — Job → Stelle Dynamic Sync (1 neue Datei + 3 Aenderungen)
| Datei | Beschreibung |
|-------|-------------|
| `app/services/job_stelle_sync_service.py` | **NEU** — Sync von Job-Import-Feldern zu verknuepfter ATSJob-Stelle |
| `app/models/ats_job.py` | `manual_overrides` JSONB-Spalte hinzugefuegt |
| `app/database.py` | Migration-Entry fuer `ats_jobs.manual_overrides` |
| `app/services/ats_job_service.py` | `update_job()` trackt jetzt manuelle Overrides |

---

## Verfuegbare Bot-Commands

| Command | Beschreibung |
|---------|-------------|
| `/start` | Begruessung |
| `/tasks` | Heutige + ueberfaellige Aufgaben mit Inline-Buttons |
| `/done <id>` | Aufgabe als erledigt markieren |
| `/search <name>` | Kandidat suchen (Name, Ort, Skills) |
| `/briefing` | Tages-Briefing mit Stats |
| `/stats` | Quick-Stats (Stellen, Kandidaten, Matches) |
| `/stellenpdf <id>` | Stelle-PDF generieren und als Datei senden |
| `/help` | Hilfe-Uebersicht |

### Free-Text & Sprachnachrichten
Der Bot versteht auch natuerliche Sprache:
- "Erstelle eine Aufgabe: Kandidat Mueller anrufen morgen 14 Uhr"
- "Suche Bilanzbuchhalter in Muenchen"
- "Briefing fuer heute"
- Sprachnachrichten: Whisper-Transkription → Intent-Erkennung → automatische Aktion

### Call-Logging per Telegram
Stichworte wie "Anruf", "Telefonat", "Call" loesen Call-Logging aus:
- **Kandidaten-Calls:** "Telefonat mit Kandidat Mueller, er ist interessiert an der Stelle..."
- **Kunden-Calls:** "Anruf mit Firma ABC, sie suchen einen Bilanzbuchhalter..."
- **Job-Qualifizierung:** Extrahiert automatisch Team-Groesse, ERP, Home-Office, Gehalt etc.
- **Auto-Erkennung:** GPT unterscheidet Kandidaten- vs. Kunden-Call anhand des Inhalts

---

## API-Endpoints

| Method | Pfad | Beschreibung |
|--------|------|-------------|
| `POST` | `/api/telegram/webhook` | Telegram Webhook (automatisch registriert) |
| `GET` | `/api/telegram/status` | Webhook-Status |
| `GET` | `/api/ats/jobs/{id}/pdf?candidate_id=` | Stelle-PDF Download |

---

## Railway ENV-Variablen (MUESSEN gesetzt werden)

```
TELEGRAM_BOT_TOKEN=8202202934:AAHES-IAIk2MRpZdD1o3NAvCJRd_nkODrIc
TELEGRAM_CHAT_ID=7103040196
TELEGRAM_WEBHOOK_SECRET=<beliebiger 32-Zeichen String generieren>
```

**Wie generieren:**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Wo setzen:** Railway Dashboard → Claudi-Time Service → Variables → Add Variable

---

## n8n Workflows aktivieren

Alle 3 Workflows wurden als **inaktiv** erstellt. Zum Aktivieren:

1. n8n Dashboard oeffnen: `https://n8n-production-aa9c.up.railway.app`
2. Jeden Workflow oeffnen und auf "Active" schalten
3. **Wichtig:** In jedem Workflow den HTTP-Request-Node pruefen:
   - URL muss auf `https://claudi-time-production-46a5.up.railway.app/api/...` zeigen
   - Header `X-API-Key` muss den korrekten API-Key enthalten
4. Telegram-Node: Bot Token + Chat ID muessen konfiguriert sein

### Workflow-IDs
- Abend-Zusammenfassung: `OOAHopP2YdOy7oud`
- Wochen-Briefing: `69XsYWe3yy6RI1vO`
- Aufgaben-Erinnerung: `mYcVUuq2DVEMwp2w`

---

## DB-Migration

Es wird **eine** neue Spalte hinzugefuegt (auto-migriert beim Start):

```sql
ALTER TABLE ats_jobs ADD COLUMN manual_overrides JSONB;
```

Die Migration ist in `app/database.py` registriert und wird beim naechsten Deploy automatisch ausgefuehrt.

---

## Job → Stelle Sync — Wie es funktioniert

### Sync-Richtung
```
Job (CSV-Import) ──sync──> ATSJob (Stelle)
```

### Gesynchte Felder
| Job-Feld | ATSJob-Feld |
|----------|-------------|
| `position` | `title` |
| `job_text` | `description` |
| `work_location_city` | `location_city` |
| `employment_type` | `employment_type` |

### Prioritaets-Schichten (hoechste zuerst)
1. **Manueller Override** — Feld wurde im UI/API manuell bearbeitet → wird NIE ueberschrieben
2. **Call-Qualifizierung** — Feld wurde durch Telefon-Logging befuellt → wird nicht ueberschrieben
3. **Job-Import** — Feld kommt aus dem CSV/Job-Import → wird gesynchte

### manual_overrides JSONB
Wenn ein Feld manuell im UI geaendert wird, speichert `update_job()` automatisch:
```json
{"title": true, "description": true}
```
Der Sync-Service prueft dieses Feld und ueberspringt markierte Felder.

---

## Stelle-PDF — Branding

Das PDF nutzt das **Sincirus-Branding** (identisch zu bestehenden Job-PDFs):
- **Farben:** Navy `#0E1B2E`, Gruen `#16825D`
- **Fonts:** DM Sans (Body), DM Serif Display (Headings)
- **Logo:** `app/static/images/sincirus_logo.png`
- **Optionale Fahrzeit:** Wenn `candidate_id` mitgegeben wird, berechnet Google Maps die Pendelzeit

### PDF-Sektionen
1. Header mit Sincirus-Logo + "Stellenbeschreibung" Badge
2. Positions-Titel (gross)
3. Firmen-Info (Name, Stadt, Branche)
4. Positions-Details (Anstellung, Gehalt, Start)
5. Aufgaben / Taetigkeiten
6. Anforderungen
7. Arbeitsbedingungen (Home Office, Gleitzeit, Urlaub, Kernzeiten)
8. Team & Technik (Team-Groesse, ERP, Digitalisierung)
9. Bewerbungsprozess (Schritte, Feedback-Timeline)
10. Standort + Fahrzeit (wenn Kandidat angegeben)
11. Footer: Berater-Visitenkarte (Milad Hamdard)

---

## Sicherheit

- **Single-User:** Jede Nachricht wird gegen `chat_id == 7103040196` geprueft
- **Webhook Secret:** Telegram sendet `X-Telegram-Bot-Api-Secret-Token` Header, der gegen `TELEGRAM_WEBHOOK_SECRET` verifiziert wird
- **Kein Public Access:** `/api/telegram/` ist zwar in `PUBLIC_PREFIXES` (kein Session-Cookie noetig), hat aber eigene Secret-Token Authentifizierung

---

## Architektur-Entscheidungen

| Entscheidung | Grund |
|-------------|-------|
| Webhook statt Polling | Railway Containers sind ephemeral, Polling wuerde nach Idle-Shutdown stoppen |
| httpx statt python-telegram-bot | Leichtgewichtiger, keine extra Dependency (httpx war bereits vorhanden) |
| Direkte Service-Aufrufe statt HTTP-zu-Self | Kein Overhead, kein Auth-Problem, schneller |
| GPT-4o-mini fuer Intents | Guenstig (<0.01$ pro Nachricht), schnell, ausreichend fuer Intent-Erkennung |
| Imports im try-Block | Railway Background-Task Absturz-Sicherheit (finally-Block laeuft immer) |
| async_session_maker pro Operation | Railway 30s idle-in-transaction Timeout |

---

## Datei-Uebersicht (alle neuen/geaenderten Dateien)

### Neue Dateien (7)
```
app/services/telegram_intent_service.py    — GPT Intent + Whisper
app/services/telegram_bot_service.py       — Bot-Kern (~780 Zeilen)
app/services/telegram_call_handler.py      — Call-Logging Handler
app/services/ats_job_pdf_service.py        — Stelle-PDF Generator
app/services/job_stelle_sync_service.py    — Job→Stelle Sync
app/api/routes_telegram.py                 — Webhook-Endpoint
app/templates/ats_job_sincirus.html        — PDF HTML-Template
```

### Geaenderte Dateien (6)
```
app/config.py                              — 3 Telegram Settings
app/auth.py                                — Public Prefix
app/main.py                                — Router + Webhook-Registrierung
app/models/ats_job.py                      — manual_overrides JSONB
app/database.py                            — Migration-Entry
app/services/ats_job_service.py            — Override-Tracking + Quali-Update
app/api/routes_ats_jobs.py                 — PDF-Endpoint
```

### n8n Workflows (3)
```
Abend-Zusammenfassung (18:00)              — OOAHopP2YdOy7oud
Wochen-Briefing (Mo 07:30)                 — 69XsYWe3yy6RI1vO
Aufgaben-Erinnerung (alle 5 Min)           — mYcVUuq2DVEMwp2w
```

---

## Deploy-Checkliste

- [ ] Railway ENV-Variablen setzen (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_WEBHOOK_SECRET)
- [ ] Git commit + push (triggert Auto-Deploy auf Railway)
- [ ] Nach Deploy: Telegram `/start` senden → Bot sollte antworten
- [ ] Nach Deploy: `/api/telegram/status` pruefen → Webhook registriert?
- [ ] n8n Workflows aktivieren (alle 3)
- [ ] n8n Workflow-Nodes konfigurieren (API-URL, API-Key, Bot-Token)
- [ ] Test: `/tasks` senden
- [ ] Test: `/briefing` senden
- [ ] Test: Sprachnachricht senden
- [ ] Test: `/stellenpdf <job-id>` senden
- [ ] Test: Call-Logging Nachricht senden ("Telefonat mit Kandidat Mueller...")

---

## Troubleshooting

### Bot antwortet nicht
1. Railway Logs pruefen: `railway logs`
2. `/api/telegram/status` aufrufen — Webhook registriert?
3. TELEGRAM_BOT_TOKEN korrekt? → `curl https://api.telegram.org/bot<TOKEN>/getMe`
4. TELEGRAM_WEBHOOK_SECRET stimmt ueberein?

### Webhook nicht registriert
- App braucht `RAILWAY_PUBLIC_DOMAIN` ENV-Variable (Railway setzt diese automatisch)
- Falls nicht vorhanden: manuell setzen auf `claudi-time-production-46a5.up.railway.app`

### PDF-Generierung fehlgeschlagen
- WeasyPrint benoetigt System-Libraries (cairo, pango) — sind im Dockerfile vorhanden
- Font-Dateien muessen unter `app/static/fonts/` liegen

### n8n Workflows laufen nicht
- Workflows muessen manuell aktiviert werden (auf "Active" schalten)
- HTTP-Request-Nodes: Korrekte URL + API-Key pruefen
- Telegram-Node: Bot Token + Chat ID konfiguriert?
