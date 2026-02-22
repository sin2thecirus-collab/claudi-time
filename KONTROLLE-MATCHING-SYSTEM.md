# Kontrolle Matching System — Konzept + Umsetzungsplan

> **Erstellt: 22.02.2026**
> **Letzte Aktualisierung: 22.02.2026**
> **Status: Phase 1-4 IMPLEMENTIERT, Phase 5 OFFEN (Testing)**

---

## INHALTSVERZEICHNIS

1. [Wie das V4 System AKTUELL funktioniert](#1-wie-das-v4-system-aktuell-funktioniert)
2. [Was wir aendern und warum](#2-was-wir-aendern-und-warum)
3. [Feature 1: Kontrolliertes Matching (Stufe fuer Stufe)](#3-feature-1-kontrolliertes-matching)
4. [Feature 2: Vergleichs-Button fuer jedes Paar](#4-feature-2-vergleichs-button)
5. [Feature 3: Vorstellen-Button aufteilen](#5-feature-3-vorstellen-button-aufteilen)
6. [Feature 4: E-Mail-Automatisierung mit Varianten](#6-feature-4-e-mail-automatisierung)
7. [Feature 5: PDF-Generierung fuer beide Richtungen](#7-feature-5-pdf-generierung)
8. [Bestehender Code + Endpoints](#8-bestehender-code--endpoints)
9. [Technischer Umsetzungsplan](#9-technischer-umsetzungsplan)
10. [Hinweise + Notizen](#10-hinweise--notizen)

---

## 1. Wie das V4 System AKTUELL funktioniert

### Die 3 Stufen (laufen AUTOMATISCH hintereinander)

```
STUFE 0: Datenbank-Filter + Geo-Kaskade + LLM-Vorfilter
│
│   SCHRITT 1: DB-Basisfilter (keine KI)
│   - Kandidat nicht geloescht, nicht versteckt, klassifiziert?
│   - Kandidat hat Berufserfahrung (work_history ODER cv_text)?
│   - Job nicht geloescht, nicht abgelaufen, klassifiziert?
│   - Job hat Beschreibung (job_text > 50 Zeichen)?
│   - Job Qualitaet mindestens "medium"?
│   - Noch kein Match fuer dieses Paar in der DB?
│
│   SCHRITT 2: Geo-Kaskade (KEIN 2000er-Limit!)
│   Fuer jedes Kandidat-Job-Paar:
│     1. Gleiche PLZ?         → JA → Paar bilden ✓
│     2. Gleiche Stadt?       → JA → Paar bilden ✓
│     3. Luftlinie <= 40km?   → JA → Paar bilden ✓
│     4. Sonst                → Kein Paar ✗
│   Vorteil: Auch bei fehlenden/falschen Geodaten werden Paare
│   mit gleicher PLZ oder Stadt nicht verloren.
│
│   SCHRITT 3: Claude Haiku LLM-Vorfilter
│   - KI bekommt NUR: Aktuelle Position + aktuelle Taetigkeiten des Kandidaten
│     + job_tasks (Taetigkeiten aus der Stellenausschreibung)
│   - KI antwortet mit einer Prozent-Zahl (0-100)
│   - Filter: >= 70% → Paar geht weiter | < 70% → aussortiert
│   - Kosten: ~$0.0003 pro Paar (~$3 fuer 10.000 Paare)
│   Ergebnis: Nur fachlich relevante Paare (KEIN 2000-Limit!)
│
├── STUFE 1: Quick-Check (Claude Haiku KI)
│   "Passt das grob zusammen? JA oder NEIN?"
│   - KI bekommt: Berufserfahrung (alle Positionen), Ausbildung, Zertifikate, Skills, ERP, gewuenschte Positionen
│   - KI antwortet: {"pass": true/false, "reason": "1 Satz"}
│   - ~2 Sekunden pro Paar
│   Ergebnis: Paare die fachlich bestanden haben
│
└── STUFE 2: Deep Assessment (Claude Haiku KI)
    "Passt das WIRKLICH zusammen?"
    - KI bekommt: Berufserfahrung, Ausbildung, Zertifikate, IT-Skills + Jobtitel, Stellenbeschreibung, Entfernung
    - KI schaut sich Werdegang an (wo kommt er her, was hat er gemacht, wo steht er jetzt?) und vergleicht mit Stellenanforderungen
    - Einziger Fach-Hinweis: BiBu braucht Zertifizierung + "eigenstaendige Erstellung" (nicht "Unterstuetzung/Mitwirkung")
    - Bei Score < 75: Kurzantwort {"score": 0, "empfehlung": "nicht_passend"} (spart Tokens)
    - Bei Score >= 75: Volle Bewertung mit Staerken, Luecken, WOW-Faktor
    - Nur Score >= 75 wird gespeichert (kein Muell in der DB)
    Ergebnis: Nur starke Matches in der Datenbank
```

### PROBLEM mit dem aktuellen System

**Alles laeuft automatisch von Stufe 0 bis Stufe 2 durch — OHNE Kontrollmoeglichkeit.**

- Milad sieht ERST die Ergebnisse, wenn ALLES fertig ist
- Keine Moeglichkeit, offensichtlich falsche Paare VOR Stufe 2 rauszuwerfen
- Keine Kostenkontrolle: Stufe 2 kostet am meisten (lange Prompts)
- Keine Moeglichkeit, den Prozess zwischen den Stufen zu pausieren
- Wenn Stufe 2 scheitert (wie passiert: JSON-Truncation-Bug), sind alle Stufe-1-Kosten verloren

### Aktueller Datenfluss

```
POST /api/v4/claude-match/run
    → Startet run_matching() als Background-Task
    → Stufe 0 → Stufe 1 → Stufe 2 → Speichern → Fahrzeit
    → Alles in EINEM Durchlauf, KEINE Pause dazwischen

GET /api/v4/claude-match/status
    → Zeigt Live-Fortschritt (welche Stufe, wie viele verarbeitet)
    → Aber NUR Fortschritt, KEINE Ergebnisse

GET /api/v4/claude-match/daily
    → Zeigt gespeicherte Matches NACH dem Lauf
    → Aber nur fertige Matches, KEINE Zwischenergebnisse
```

### Wichtige technische Details

| Detail | Wert |
|--------|------|
| Claude-Modell Stufe-0 Vorfilter | `claude-haiku-4-5-20251001` |
| Claude-Modell Quick-Check (Stufe 1) | `claude-haiku-4-5-20251001` |
| Claude-Modell Deep Assessment (Stufe 2) | `claude-haiku-4-5-20251001` |
| Max Tokens Stufe-0 Vorfilter | ~5 (nur Prozent-Zahl) |
| Max Tokens Quick-Check | 500 |
| Max Tokens Deep Assessment | 800 |
| Concurrency (parallele Calls) | Semaphore(3) |
| Max Paare pro Lauf | **KEIN LIMIT** (vorher 2000) |
| Stufe-0 Vorfilter Threshold | >= 70% Passung |
| MIN_SCORE_SAVE (Stufe 2) | 75 (vorher 40) |
| Proximity-Matches (ohne KI) | < 10km, ohne Beschreibung |
| Kosten Stufe-0 Vorfilter | ~$0.0003 pro Paar (~$3 fuer 10.000 Paare) |
| Kosten Stufe 1 | ~$0.0004 pro Paar |
| Kosten Stufe 2 | ~$0.002 pro Paar |
| Fahrzeit-Threshold | Score >= 80 (konfigurierbar via /einstellungen) |
| Klassifizierung | GPT-4o (gerade geaendert von gpt-4o-mini) |
| Geocoding | Nominatim/OpenStreetMap (kostenlos) |
| Fahrzeit | Google Maps Distance Matrix API |
| Neues Feld: `job_tasks` | Taetigkeiten extrahiert aus job_text (bei Klassifizierung) |

### Datei-Referenzen (V4 System)

| Datei | Zweck | Wichtige Zeilen |
|-------|-------|-----------------|
| `app/services/claude_matching_service.py` | Kern-Service: 3 Stufen + Vorfilter | Vorfilter-Prompt: Z.132, Stufe 0 (Geo+LLM): Z.382-660, Stufe 1: Z.662+, Stufe 2: Z.750+ |
| `app/api/routes_claude_matching.py` | API-Endpoints | 8 Haupt-Endpoints + 7 Debug |
| `app/templates/action_board.html` | Dashboard UI | Alpine.js, ~500 Zeilen |
| `app/config.py` | API Keys, Settings | anthropic_api_key, google_maps_api_key |
| `app/database.py` | DB-Session Factory | `async_session_maker` (NICHT _factory!) |

---

## 2. Was wir aendern und warum

### Ziel

Milad will die **volle Kontrolle** ueber den Matching-Prozess:

1. **Stufe fuer Stufe**: Jede Stufe einzeln starten, Ergebnisse sehen, pruefen, DANN erst weiter
2. **Paare ausschliessen**: In jeder Stufe einzelne Paare rauswerfen bevor die naechste Stufe startet
3. **Vergleichs-Ansicht**: Fuer jedes Paar sofort sehen: Kandidat vs. Job (bestehender Compare-Button)
4. **Kostenkontrolle**: Nur die Paare in Stufe 2 schicken die wirklich sinnvoll aussehen

### Warum wir das machen

- **Kosten**: Stufe 2 ist der teuerste Schritt (~$3-5 pro Lauf). Wenn Milad vorher 20% der Paare aussortiert, spart das ~$0.60-1.00 pro Lauf
- **Qualitaet**: Milad kennt seine Kandidaten und Jobs. Er kann auf einen Blick sehen ob ein Paar Sinn macht
- **Vertrauen**: Das System ist neu. Milad will es kontrollieren koennen bevor er es "laufen laesst"
- **Fehlerminimierung**: Wenn Stufe 2 einen Bug hat (wie der JSON-Truncation-Bug), sind die Stufe-1-Ergebnisse nicht verloren

### Was sich NICHT aendert

- Die Logik innerhalb jeder Stufe bleibt identisch
- Die Score-Berechnung bleibt identisch
- Die Match-Speicherung bleibt identisch
- Die Fahrzeit-Berechnung bleibt identisch

### Was sich aendert

- `run_matching()` wird in 3 separate Funktionen aufgeteilt
- Zwischenergebnisse werden temporaer gespeichert (In-Memory oder DB)
- Action Board bekommt neue Ansichten fuer Stufe-0 und Stufe-1 Ergebnisse
- Neue Buttons: "Weiter zu Stufe 1", "Weiter zu Stufe 2", "Paar ausschliessen"
- Vergleichs-Button wird in jede Stufen-Ansicht eingebaut

---

## 3. Feature 1: Kontrolliertes Matching

### Neuer Ablauf (Schritt fuer Schritt)

```
┌─────────────────────────────────────────────────────────┐
│  SCHRITT 1: Milad klickt "Matching starten"             │
│  → System fuehrt NUR Stufe 0 aus (DB-Filter)            │
│  → Ergebnis: Liste von ~2000 Kandidat-Job-Paaren        │
│  → Paare werden im Action Board angezeigt                │
│  → Milad kann jedes Paar per Vergleichs-Button pruefen   │
│  → Milad kann einzelne Paare ausschliessen (X-Button)    │
├─────────────────────────────────────────────────────────┤
│  SCHRITT 2: Milad klickt "Weiter zu Stufe 1"            │
│  → System schickt NUR die nicht-ausgeschlossenen Paare   │
│  → Claude Quick-Check (JA/NEIN) fuer jedes Paar          │
│  → Ergebnis: Paare mit JA/NEIN + Begruendung            │
│  → Milad sieht die Ergebnisse (JA-Paare hervorgehoben)  │
│  → Milad kann weitere Paare ausschliessen                │
├─────────────────────────────────────────────────────────┤
│  SCHRITT 3: Milad klickt "Weiter zu Stufe 2"            │
│  → System schickt NUR die genehmigten JA-Paare          │
│  → Claude Deep Assessment (Score + Details)              │
│  → Ergebnis: Fertige Matches mit Score + Staerken etc.   │
│  → Matches werden in DB gespeichert                      │
│  → Milad sieht die fertigen Matches im Action Board      │
│  → Fahrzeit wird berechnet (fuer Score >= 80)            │
└─────────────────────────────────────────────────────────┘
```

### Neue API-Endpoints

#### `POST /api/v4/claude-match/run-stufe-0`
Fuehrt NUR Stufe 0 aus. Gibt die gefilterten Paare zurueck und speichert sie in einer Session.

**Response:**
```json
{
  "session_id": "abc-123",
  "total_pairs": 2084,
  "claude_pairs": 2000,
  "proximity_pairs": 84,
  "pairs": [
    {
      "pair_id": "p-001",
      "candidate_id": "...",
      "candidate_name": "Max Mustermann",
      "candidate_city": "Muenchen",
      "candidate_role": "Finanzbuchhalter",
      "candidate_position": "Senior Buchhalter",
      "job_id": "...",
      "job_position": "Finanzbuchhalter (m/w/d)",
      "job_company": "Firma XYZ",
      "job_city": "Muenchen",
      "distance_km": 12.4,
      "excluded": false
    }
  ]
}
```

#### `POST /api/v4/claude-match/exclude-pairs`
Schliesst einzelne Paare aus der naechsten Stufe aus.

**Request:**
```json
{
  "session_id": "abc-123",
  "pair_ids": ["p-001", "p-007", "p-042"]
}
```

#### `POST /api/v4/claude-match/run-stufe-1`
Fuehrt Stufe 1 (Quick-Check) fuer alle nicht-ausgeschlossenen Paare aus.

**Request:**
```json
{
  "session_id": "abc-123"
}
```

**Response:**
```json
{
  "session_id": "abc-123",
  "total_checked": 1850,
  "passed": 1200,
  "failed": 650,
  "cost_usd": 0.72,
  "pairs": [
    {
      "pair_id": "p-002",
      "candidate_name": "...",
      "job_position": "...",
      "passed": true,
      "reason": "Kandidat hat FiBu-Erfahrung und DATEV-Kenntnisse, passt zum Job.",
      "excluded": false
    }
  ]
}
```

#### `POST /api/v4/claude-match/run-stufe-2`
Fuehrt Stufe 2 (Deep Assessment) fuer alle genehmigten JA-Paare aus.

**Request:**
```json
{
  "session_id": "abc-123"
}
```

**Response:**
```json
{
  "session_id": "abc-123",
  "total_assessed": 1100,
  "saved_matches": 890,
  "top_matches": 45,
  "wow_matches": 8,
  "cost_usd": 3.20,
  "matches": [
    {
      "match_id": "...",
      "candidate_name": "...",
      "job_position": "...",
      "score": 87,
      "empfehlung": "vorstellen",
      "zusammenfassung": "...",
      "staerken": ["DATEV", "BiBu-Qualifikation"],
      "luecken": ["Keine SAP-Erfahrung"],
      "wow_faktor": true
    }
  ]
}
```

### Session-Speicherung

Die Zwischenergebnisse muessen zwischen den Stufen gespeichert werden. Optionen:

**Option A: In-Memory (einfach, aber geht bei Redeploy verloren)**
```python
_matching_sessions: dict[str, dict] = {}
# session_id → { pairs, excluded, stufe_1_results, ... }
```

**Option B: Redis/Cache (persistent, aber braucht Redis)**
- Railway hat Redis als Add-on

**Option C: Datenbank-Tabelle (persistent, kein Redis noetig)**
```sql
CREATE TABLE matching_sessions (
    id UUID PRIMARY KEY,
    created_at TIMESTAMP,
    status VARCHAR(20),  -- stufe_0_done, stufe_1_done, stufe_2_done
    pairs JSONB,
    excluded_pair_ids JSONB,
    stufe_1_results JSONB,
    cost_total FLOAT
);
```

**EMPFEHLUNG:** Option A (In-Memory) fuer den Start. Wenn es funktioniert, spaeter auf Option C upgraden. Grund: Matching-Sessions sind kurzlebig (Minuten bis Stunden), und ein Redeploy ist selten waehrend eines laufenden Matchings.

### Action Board UI-Aenderungen

Das Action Board (`app/templates/action_board.html`) bekommt 3 neue Ansichten:

#### Ansicht 1: Stufe-0-Ergebnisse
- Tabelle mit allen gefundenen Paaren
- Spalten: Kandidat | Position | Job | Firma | Distanz | Vergleich | Ausschliessen
- "Vergleich"-Button oeffnet den bestehenden Compare-Dialog
- "X"-Button schliesst das Paar aus (wird grau/durchgestrichen)
- Unten: "Weiter zu Stufe 1" Button + Kosten-Schaetzung
- Info: "X von Y Paaren ausgeschlossen. ~$Z.ZZ geschaetzte Kosten fuer Stufe 1."

#### Ansicht 2: Stufe-1-Ergebnisse
- Tabelle mit Quick-Check-Ergebnissen
- Spalten: Kandidat | Job | Ergebnis (JA/NEIN) | Begruendung | Vergleich | Ausschliessen
- JA-Paare gruen hervorgehoben, NEIN-Paare grau
- NEIN-Paare sind automatisch ausgeschlossen, koennen aber manuell wieder aufgenommen werden
- Unten: "Weiter zu Stufe 2" Button + Kosten-Schaetzung
- Info: "X Paare bestanden. ~$Z.ZZ geschaetzte Kosten fuer Stufe 2."

#### Ansicht 3: Stufe-2-Ergebnisse (= bisheriges Action Board)
- Die bestehende Ansicht mit Top Matches, WOW-Chancen, etc.
- Keine Aenderung noetig

### Kosten-Schaetzung fuer UI

```python
# Stufe 1 Kosten-Schaetzung (pro Paar)
# Input: ~400 Tokens, Output: ~50 Tokens
# Haiku: $0.80/1M input, $4.00/1M output
COST_PER_QUICK_CHECK = (400 * 0.80 + 50 * 4.00) / 1_000_000  # ~$0.0005

# Stufe 2 Kosten-Schaetzung (pro Paar)
# Input: ~1500 Tokens, Output: ~400 Tokens
COST_PER_DEEP_ASSESSMENT = (1500 * 0.80 + 400 * 4.00) / 1_000_000  # ~$0.0028
```

---

## 4. Feature 2: Vergleichs-Button

### Was bereits existiert

Der Vergleichs-Button existiert bereits im Match Center:

| Komponente | Datei | Zeile |
|-----------|-------|-------|
| Compare-Endpoint | `app/api/routes_match_center.py` | Z.324-347 |
| Compare-Service | `app/services/match_center_service.py` | Z.769-864 |
| Compare-Template | `app/templates/partials/match_center_compare.html` | 505 Zeilen |
| Compare-Datenklasse | `app/services/match_center_service.py` | Z.282-323 (`MatchComparisonData`) |

### Was der Compare-Dialog zeigt

- **Score-Badge** mit Farb-Kodierung
- **Info-Karten**: Distanz, Firma, Position, Adressen (kopierbar)
- **Zwei-Spalten-Layout**:
  - Links: Job-Beschreibung (voller Text)
  - Rechts: Kandidaten-CV (Berufserfahrung, Ausbildung, Zertifikate, IT-Skills, Sprachen)
- **Score-Breakdown**: Scoring-Dimensionen mit Progress-Bars
- **KI-Bewertung**: Erklaerung + Staerken/Schwaechen-Tags
- **Feedback-Buttons**: Gut / Vielleicht / Distanz / Taetigkeiten / Seniority

### Was angepasst werden muss

Der bestehende Compare-Dialog braucht einen **Match-Eintrag in der DB** (er laedt Match + Job + Candidate per Match-ID). Fuer Stufe 0 und Stufe 1 existiert aber noch KEIN Match in der DB — die Paare sind nur temporaer.

**Loesung:** Einen neuen Endpoint bauen der OHNE Match-ID funktioniert:

```
GET /api/v4/claude-match/compare-pair?candidate_id=...&job_id=...
```

Dieser Endpoint:
1. Laedt Kandidat und Job direkt aus der DB
2. Baut ein MatchComparisonData-Objekt (ohne Match-Felder)
3. Gibt das gleiche HTML-Partial zurueck wie der bestehende Compare-Dialog
4. Zeigt: Kandidat links, Job rechts, Distanz, aber OHNE Score/KI-Bewertung (die gibt es ja noch nicht)

**Fuer Stufe-1-Ergebnisse:** Der Compare-Dialog zeigt zusaetzlich die Quick-Check-Begruendung an (JA/NEIN + Reason).

**Fuer Stufe-2-Ergebnisse:** Der bestehende Compare-Dialog funktioniert direkt (Match existiert in DB).

---

## 5. Feature 3: Vorstellen-Button aufteilen

### Aktueller Zustand

Der "Vorstellen"-Button im Action Board macht EINE Sache:
- Setzt `match.user_feedback = "vorstellen"`
- Setzt `match.status = "PRESENTED"`
- Erstellt optional einen ATS-Pipeline-Eintrag

### Neuer Zustand: 2 getrennte Aktionen

#### Aktion A: "Job an Kandidat senden"
> Milad schickt dem Kandidaten den Job-Vorschlag per E-Mail

1. **NEUES** Job-Vorstellungs-PDF wird generiert (NEUER `JobVorstellungPdfService`)
   - Im gleichen Sincirus-Design wie das Kandidaten-Profil-PDF
   - Schoen gestaltet, professionell, NICHT die alte Job-Description
   - Personalisiert fuer den Kandidaten (Anrede, Fahrzeit, fachliche Passung)
2. E-Mail wird vorbereitet mit Job-Details + PDF-Anhang
3. E-Mail-Variante wird automatisch gewaehlt (siehe Feature 4)
4. Milad prueft die E-Mail und klickt "Senden"

**WICHTIG:** Die Job-Detailseite bekommt ebenfalls einen "Job-PDF erstellen"-Button
(analog zum "Profil-PDF erstellen"-Button auf der Kandidaten-Detailseite).
Wenn "Job an Kandidat senden" geklickt wird, wird automatisch das gleiche PDF generiert.

#### Aktion B: "Kandidat beim Kunden vorstellen"
> Milad schickt dem Kunden das Kandidaten-Profil per E-Mail

1. Kandidaten-Profil-PDF wird generiert (bestehender `ProfilePdfService` — GLEICHER Code wie der "Profil-PDF"-Button auf der Kandidaten-Detailseite)
2. Milad waehlt den **Empfaenger** aus:
   - Einen bestehenden **Kontakt** im Unternehmen (aus der Kontakte-Tabelle)
   - Oder ein **Bewerber-Postfach** (allgemeine E-Mail des Unternehmens)
   - Oder eine manuelle E-Mail-Adresse
3. E-Mail wird vorbereitet mit Kandidaten-Highlights + PDF-Anhang
4. E-Mail-Variante wird automatisch gewaehlt (siehe Feature 4)
5. Milad prueft die E-Mail und klickt "Senden"

### UI-Aenderung im Action Board

Statt einem "Vorstellen"-Button gibt es jetzt ein Dropdown oder zwei separate Buttons:

```
┌──────────────────────────────┐
│  [Job an Kandidat senden]    │  ← Generiert Job-PDF + E-Mail an Kandidat
│  [Profil an Kunden senden]   │  ← Nutzt bestehendes Profil-PDF + E-Mail an Kontakt/Postfach
│  [Spaeter]                   │
│  [Ablehnen]                  │
└──────────────────────────────┘
```

### Empfaenger-Auswahl bei "Profil an Kunden senden"

Wenn Milad "Profil an Kunden senden" klickt, oeffnet sich ein Dialog:

```
┌─────────────────────────────────────────┐
│  Kandidat vorstellen bei:               │
│  {Firma XYZ} — {Job Position}           │
│                                         │
│  An wen senden?                         │
│  ┌─────────────────────────────────┐    │
│  │ ○ Max Mueller (HR Manager)      │    │
│  │   max.mueller@firma.de          │    │
│  │ ○ Lisa Schmidt (Abteilungsltr.) │    │
│  │   l.schmidt@firma.de            │    │
│  │ ○ bewerbungen@firma.de          │    │
│  │ ○ Andere E-Mail eingeben...     │    │
│  └─────────────────────────────────┘    │
│                                         │
│  [E-Mail vorbereiten]  [Abbrechen]      │
└─────────────────────────────────────────┘
```

Die Kontakte kommen aus der bestehenden Kontakte-Tabelle des Unternehmens.

### Code-Uebersicht

| Was | Datei | Methode | Neu/Bestehend |
|-----|-------|---------|---------------|
| Kandidaten-Profil-PDF | `app/services/profile_pdf_service.py` | `generate_profile_pdf(candidate_id)` | BESTEHEND (wird wiederverwendet) |
| Job-Vorstellungs-PDF | `app/services/job_vorstellung_pdf_service.py` | `generate_job_vorstellung_pdf(match_id)` | **NEU** (gleiches Design wie Profil-PDF) |
| Job-PDF Button (Job-Detailseite) | `app/api/routes_jobs.py` | `GET /jobs/{id}/vorstellung-pdf` | **NEU** |
| E-Mail an Kandidat | `app/services/outreach_service.py` | `send_to_candidate(match_id)` | BESTEHEND (wird erweitert) |
| E-Mail an Kunden | `app/services/outreach_service.py` | `send_to_customer(match_id, contact_id)` | **NEU** |
| Profil-PDF Endpoint | `app/api/routes_candidates.py` Z.1419 | `GET /{candidate_id}/profile-pdf` | BESTEHEND |

---

## 6. Feature 4: E-Mail-Automatisierung

### Ueberblick: 2 Richtungen x 2 Varianten = 4 E-Mail-Typen

| # | Richtung | Variante | Beschreibung |
|---|----------|----------|-------------|
| 1 | Job → Kandidat | **Erstkontakt** | Kandidat hat noch nie eine Stelle von uns bekommen |
| 2 | Job → Kandidat | **Folgekontakt** | Kandidat kennt uns, hat schon Vorschlaege bekommen |
| 3 | Profil → Kunde | **Erstkontakt** | Neuer Kunde, noch kein Kontakt |
| 4 | Profil → Kunde | **Folgekontakt** | Bestehender Kunde, schon Kandidaten vorgestellt |

### E-Mail-Regeln (ALLE Varianten)

- **Kein HTML** — Normaler Text, aber uebersichtlich formatiert
- **Nicht zu lang, nicht zu kurz** — 5-8 Saetze Kerntext
- **Individuell** — Jede E-Mail ist auf Kandidat+Job zugeschnitten
- **Fachliche Einschaetzung** — In JEDER E-Mail: Was kann der Kandidat? Was will der Job? Wie passen sie zusammen?
- **Uebersichtliche Gegenuberstellung** — Kleine Tabelle oder Liste mit Key Facts
- **Emojis erlaubt** — Dezent, fuer Uebersichtlichkeit (z.B. Haekchen, Stern)
- **PDF-Anhang** — Immer das passende PDF anhaengen

### Variante 1: Job → Kandidat (Erstkontakt)

```
Betreff: Passende Stelle als {job_position} bei {company} in {city}

Hallo {Frau/Herr} {Nachname},

mein Name ist Milad Hamdard von Sincirus und ich bin auf Ihr Profil
aufmerksam geworden. Ich habe eine Stelle, die gut zu Ihrem Hintergrund
als {candidate_position} passen koennte.

{kurze fachliche Einschaetzung: 2-3 Saetze warum der Job zum Kandidaten passt}

Hier die Eckdaten auf einen Blick:

Position:    {job_position}
Unternehmen: {company}, {city}
Gehalt:      {salary_range}
Arbeitszeit: {employment_type}
Fahrzeit:    ca. {drive_time} Minuten von {candidate_city}

Anbei finden Sie die ausfuehrliche Stellenbeschreibung als PDF.

Haetten Sie Interesse an einem kurzen Telefonat dazu?

Beste Gruesse
Milad Hamdard
Sincirus | Finance & Accounting Recruiting
```

### Variante 2: Job → Kandidat (Folgekontakt)

```
Betreff: Neuer Vorschlag: {job_position} bei {company}

Hallo {Frau/Herr} {Nachname},

ich habe eine weitere Stelle gefunden, die zu Ihnen passen koennte.

{kurze fachliche Einschaetzung}

Die Details im Ueberblick:

Position:    {job_position}
Unternehmen: {company}, {city}
Gehalt:      {salary_range}
Fahrzeit:    ca. {drive_time} Minuten

Anbei die ausfuehrliche Beschreibung. Lassen Sie mich gerne wissen,
ob diese Stelle fuer Sie interessant ist.

Beste Gruesse
Milad Hamdard
```

### Variante 3: Profil → Kunde (Erstkontakt)

```
Betreff: Qualifiziertes Profil fuer Ihre Stelle als {job_position}

Guten Tag {Anrede} {Nachname},

mein Name ist Milad Hamdard von Sincirus, wir sind spezialisiert auf
Finance & Accounting Recruiting. Fuer Ihre ausgeschriebene Stelle als
{job_position} habe ich einen passenden Kandidaten fuer Sie.

Kurze Gegenuberstellung:

Ihre Anforderungen          Kandidat
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{requirement_1}             {candidate_strength_1}
{requirement_2}             {candidate_strength_2}
{requirement_3}             {candidate_strength_3}
Software: {job_erp}         {candidate_erp}
Level: {job_level}          {candidate_level}

{fachliche Einschaetzung: 2-3 Saetze}

Anbei das ausfuehrliche Kandidatenprofil als PDF.
Ich freue mich auf Ihre Rueckmeldung.

Beste Gruesse
Milad Hamdard
Sincirus | Finance & Accounting Recruiting
```

### Variante 4: Profil → Kunde (Folgekontakt)

```
Betreff: Weiteres Profil fuer {job_position}

Guten Tag {Anrede} {Nachname},

anbei erhalten Sie ein weiteres Profil fuer Ihre Stelle als {job_position}.

Kurzuebersicht:

{requirement_1}  →  {candidate_strength_1}
{requirement_2}  →  {candidate_strength_2}
{requirement_3}  →  {candidate_strength_3}

{fachliche Einschaetzung}

Das ausfuehrliche Profil finden Sie im Anhang.

Beste Gruesse
Milad Hamdard
```

### Wie erkennt das System die richtige Variante?

**Fuer Kandidaten (Job → Kandidat):**

```python
# Pruefen: Hat der Kandidat schon mal eine E-Mail von uns bekommen?
has_previous_outreach = await db.execute(
    select(Match.id).where(
        Match.candidate_id == candidate_id,
        Match.outreach_status == "sent",
    ).limit(1)
)
is_first_contact = has_previous_outreach.scalar_one_or_none() is None

# ODER: Pruefen ob der Kandidat schon Matches mit Status PRESENTED hat
# ODER: Pruefen ob es einen Anruf-Eintrag gibt (calls Tabelle)
```

**Fuer Kunden (Profil → Kunde):**

```python
# Pruefen: Haben wir diesem Kunden (= dieser Firma fuer diesen Job) schon
# mal einen Kandidaten geschickt?
# Option 1: ATS-Pipeline pruefen
has_previous_presentations = await db.execute(
    select(ATSPipelineEntry.id).where(
        ATSPipelineEntry.ats_job_id == ats_job_id,
    ).limit(1)
)
is_new_customer = has_previous_presentations.scalar_one_or_none() is None

# Option 2: Match-Tabelle pruefen
# Alle Matches fuer diesen Job mit status=PRESENTED zaehlen
```

### Wie realistisch ist die automatische Varianten-Erkennung?

**EINSCHAETZUNG: 85-90% zuverlaessig.**

Die Erkennung basiert auf harten Daten (Hat der Kandidat eine E-Mail bekommen? Ja/Nein). Das ist deterministisch und zuverlaessig.

**Risiken:**
- Kandidat wurde telefonisch kontaktiert aber nicht per E-Mail → System denkt "Erstkontakt"
- Kunde wurde ueber einen ANDEREN Job kontaktiert → System denkt "Erstkontakt" fuer diesen Job
- Daten sind nicht vollstaendig (alte Kontakte vor dem Tool)

**Absicherung:**
- Milad sieht IMMER die vorbereitete E-Mail BEVOR sie gesendet wird
- Er kann die Variante manuell wechseln
- Er kann den Text anpassen bevor er auf "Senden" klickt

### E-Mail-Generierung: GPT vs. Template?

**EMPFEHLUNG: Hybrid-Ansatz**

1. **Grundstruktur** kommt aus einer festen Template (nicht GPT) — damit NIE falsche Daten/Formeln in die E-Mail kommen
2. **Fachliche Einschaetzung** (2-3 Saetze) wird von GPT-4o generiert — die KI kennt Kandidat und Job und kann individuell bewerten
3. **Gegenuberstellungstabelle** wird programmatisch aus den Match-Daten gebaut — keine KI-Halluzination moeglich
4. Milad prueft und kann anpassen

**Warum nicht alles GPT?** Fehler in E-Mails sind in dieser Branche FATAL. Falsche Gehaelter, falsche Firmennamen, falsche Qualifikationen — das verjagt Kunden UND Kandidaten. Die Template-Struktur verhindert das.

---

## 7. Feature 5: PDF-Generierung

### Bestehende PDF-Services

| Service | Datei | Zeilen | Zweck |
|---------|-------|--------|-------|
| `ProfilePdfService` | `app/services/profile_pdf_service.py` | 629 | Kandidaten-Profil-PDF (Sincirus-Design) |
| `JobDescriptionPdfService` | `app/services/job_description_pdf_service.py` | 311 | Job-PDF fuer Kandidaten-Ansprache |
| `ATSJobPdfService` | `app/services/ats_job_pdf_service.py` | 237 | Qualifizierte-Stelle-PDF |

### Was wann generiert wird

| Aktion | PDF | Service | Template | Neu? |
|--------|-----|---------|----------|------|
| Job an Kandidat senden | **Job-Vorstellungs-PDF** (schoen designt) | `JobVorstellungPdfService` **NEU** | `job_vorstellung_sincirus.html` **NEU** | JA |
| Profil an Kunden senden | Kandidaten-Profil (bestehendes Design) | `ProfilePdfService` BESTEHEND | `profile_sincirus_branded.html` | NEIN |
| Button auf Job-Detailseite | **Job-Vorstellungs-PDF** (ohne Kandidat) | `JobVorstellungPdfService` **NEU** | `job_vorstellung_sincirus.html` **NEU** | JA |

### NEUES Job-Vorstellungs-PDF

Das bestehende `JobDescriptionPdfService` erstellt ein einfaches Job-PDF. Das reicht nicht.
Milad will ein **richtig schoen designtes PDF** — im gleichen Sincirus-Stil wie das Kandidaten-Profil.

**Was das neue Job-Vorstellungs-PDF enthaelt:**
- Sincirus-Header mit Logo (gleich wie Profil-PDF)
- Hero-Section: Job-Titel, Firma, Stadt
- Quick Facts: Gehalt, Arbeitszeit, Arbeitsmodell, Branche, Team-Groesse
- Aufgaben-Liste (aus job_text extrahiert)
- Anforderungen-Liste (aus job_text extrahiert)
- Wenn personalisiert fuer einen Kandidaten:
  - Fahrzeit (Auto + OEPNV)
  - Kurze fachliche Einschaetzung warum der Job passt
- Consultant-Info (Milad Hamdard)

**Design-Vorlage:** EXAKT gleiches Layout wie `profile_sincirus_branded.html`:
- Gleiche Farben, Schriften, Abstande
- Gleicher Header/Footer
- Gleiche Section-Struktur (Hero → Quick Facts → Details → Kontakt)

**Technisch:**
```python
# NEUER Service: app/services/job_vorstellung_pdf_service.py
class JobVorstellungPdfService:
    async def generate_job_vorstellung_pdf(
        self,
        job_id: UUID,
        candidate_id: UUID | None = None,  # Optional: fuer personalisierte Version
        match_id: UUID | None = None,       # Optional: fuer Fahrzeit + Score
    ) -> bytes:
        # 1. Job laden
        # 2. Optional: Kandidat + Match laden (fuer Personalisierung)
        # 3. Aufgaben + Anforderungen aus job_text extrahieren
        # 4. Template rendern (job_vorstellung_sincirus.html)
        # 5. WeasyPrint → PDF
        pass
```

**NEUER Endpoint auf Job-Detailseite:**
```
GET /api/jobs/{job_id}/vorstellung-pdf?candidate_id=...&match_id=...
```
- Ohne candidate_id: Generisches Job-PDF (fuer manuelle Weiterleitung)
- Mit candidate_id: Personalisiertes Job-PDF (mit Fahrzeit + Einschaetzung)

### Kandidaten-Profil-PDF (BESTEHEND — wird wiederverwendet)

Fuer "Profil an Kunden senden" wird EXAKT der gleiche Code verwendet wie der bestehende "Profil-PDF"-Button auf der Kandidaten-Detailseite:

```python
# Der gleiche Aufruf wie /api/candidates/{id}/profile-pdf:
from app.services.profile_pdf_service import ProfilePdfService
pdf_service = ProfilePdfService(db)
pdf_bytes = await pdf_service.generate_profile_pdf(candidate_id)
```

Es wird KEIN neuer Profil-PDF-Service erstellt. Der bestehende wird wiederverwendet.

---

## 8. Bestehender Code + Endpoints

### Alle relevanten API-Endpoints

#### V4 Matching (aktuell)
| Methode | Pfad | Datei | Zweck |
|---------|------|-------|-------|
| POST | `/api/v4/claude-match/run` | `routes_claude_matching.py` Z.53 | Matching starten (alle Stufen) |
| GET | `/api/v4/claude-match/status` | `routes_claude_matching.py` Z.84 | Live-Fortschritt |
| GET | `/api/v4/claude-match/daily` | `routes_claude_matching.py` Z.91 | Tages-Ergebnisse |
| POST | `/api/v4/claude-match/{id}/action` | `routes_claude_matching.py` Z.283 | Vorstellen/Spaeter/Ablehnen |
| GET | `/api/v4/claude-match/regional-insights` | `routes_claude_matching.py` Z.802 | Regionale Uebersicht |
| POST | `/api/v4/claude-match/{id}/detailed-feedback` | `routes_claude_matching.py` Z.866 | Feedback |

#### PDF-Generierung
| Methode | Pfad | Datei | Zweck |
|---------|------|-------|-------|
| GET | `/api/candidates/{id}/profile-pdf` | `routes_candidates.py` Z.1419 | Kandidaten-Profil-PDF |
| GET | `/api/matches/{id}/job-pdf` | `routes_matches.py` Z.600 | Job-PDF fuer Kandidat |
| GET | `/api/ats-jobs/{id}/pdf` | `routes_ats_jobs.py` Z.299 | Qualifizierte-Stelle-PDF |

#### Outreach (E-Mail)
| Methode | Pfad | Datei | Zweck |
|---------|------|-------|-------|
| POST | `/api/matches/{id}/send-to-candidate` | `routes_matches.py` Z.634 | E-Mail + PDF an Kandidat |
| POST | `/api/matches/batch-send` | `routes_matches.py` Z.660 | Batch-E-Mail (max 20) |

#### Compare (Vergleich)
| Methode | Pfad | Datei | Zweck |
|---------|------|-------|-------|
| GET | `/api/match-center/compare/{match_id}` | `routes_match_center.py` Z.324 | Vergleichs-Dialog (HTMX) |

#### Debug
| Methode | Pfad | Datei | Zweck |
|---------|------|-------|-------|
| GET | `/api/v4/debug/last-run` | `routes_claude_matching.py` | **WICHTIGSTER DEBUG** — Letzte Session mit Zusammenfassung, failed/passed/error/rejected Samples |
| GET | `/api/v4/debug/match-count` | `routes_claude_matching.py` | Match-Statistiken |
| GET | `/api/v4/debug/stufe-0-preview` | `routes_claude_matching.py` | Dry-Run Stufe 0 Vorschau |
| GET | `/api/v4/debug/job-health` | `routes_claude_matching.py` | Job-Daten-Qualitaet |
| GET | `/api/v4/debug/candidate-health` | `routes_claude_matching.py` | Kandidaten-Daten-Qualitaet |
| GET | `/api/v4/debug/match/{id}` | `routes_claude_matching.py` | Match-Detail mit Claude-Input/Output |
| GET | `/api/v4/debug/cost-report` | `routes_claude_matching.py` | API-Kosten |

#### Session-Debugging
| Methode | Pfad | Datei | Zweck |
|---------|------|-------|-------|
| GET | `/api/v4/claude-match/sessions` | `routes_claude_matching.py` | Alle aktiven Sessions auflisten |
| GET | `/api/v4/claude-match/session/{id}` | `routes_claude_matching.py` | Session-Detail mit allen Debug-Feldern |

**Debug-Felder pro Session (nach Stufe 1):**
- `failed_pairs` — Jedes Paar mit `quick_reason` (Claudes Begruendung warum FAIL)
- `error_pairs` — Parse-Fehler (Claude-Antwort nicht als JSON parsebar)
- `passed_pairs` — Bestandene Paare mit `quick_reason`

**Debug-Felder pro Session (nach Stufe 2):**
- `deep_results` — Gespeicherte Matches mit Score, Empfehlung, Zusammenfassung
- `stufe2_rejected` — Score unter MIN_SCORE_SAVE (75) mit Score + Grund
- `stufe2_errors` — Parse-Fehler in Stufe 2

**TIPP:** `/debug/last-run` im Browser aufrufen (eingeloggt) fuer schnellen Ueberblick.

### Alle relevanten Services

| Service | Datei | Methode | Zweck |
|---------|-------|---------|-------|
| `ClaudeMatchingService` | `claude_matching_service.py` | `run_matching()` | 3-Stufen-Matching |
| `ProfilePdfService` | `profile_pdf_service.py` | `generate_profile_pdf()` | Kandidaten-PDF |
| `JobDescriptionPdfService` | `job_description_pdf_service.py` | `generate_job_pdf()` | Job-PDF |
| `ATSJobPdfService` | `ats_job_pdf_service.py` | `generate_stelle_pdf()` | Stellen-PDF |
| `OutreachService` | `outreach_service.py` | `send_to_candidate()` | E-Mail + PDF senden |
| `MatchCenterService` | `match_center_service.py` | `get_match_comparison()` | Compare-Daten laden |
| `FinanceClassifierService` | `finance_classifier_service.py` | Classify | Klassifizierung (GPT-4o) |
| `GeocodingService` | `geocoding_service.py` | `geocode()` | Nominatim Geocoding |
| `DistanceMatrixService` | `distance_matrix_service.py` | `batch_drive_times()` | Google Maps Fahrzeit |

### Alle relevanten Templates

| Template | Datei | Zweck |
|----------|-------|-------|
| Action Board | `app/templates/action_board.html` | Haupt-Dashboard (Alpine.js) |
| Compare Dialog | `app/templates/partials/match_center_compare.html` | Vergleichs-Modal (505 Zeilen) |
| Match Card | `app/templates/components/match_card.html` | Match-Karte (230 Zeilen) |
| Match Row | `app/templates/partials/match_result_row.html` | Match-Zeile in Ergebnis-Tabelle |
| Profil-PDF | `app/templates/profile_sincirus_branded.html` | Sincirus Kandidaten-PDF |
| Job-PDF | `app/templates/job_description_sincirus.html` | Sincirus Job-PDF |
| Stellen-PDF | `app/templates/ats_job_sincirus.html` | Sincirus Stellen-PDF |

### Technologie-Stack

| Komponente | Technologie |
|-----------|-----------|
| PDF-Rendering | WeasyPrint (HTML → PDF) |
| HTML-Templates | Jinja2 |
| Frontend-Interaktivitaet | Alpine.js |
| HTMX-Partials | HTMX (fuer Compare, Match-Karten) |
| E-Mail-Text-Generierung | GPT-4o-mini (OpenAI) |
| E-Mail-Versand | Microsoft Graph API |
| Cloud-Speicher (PDFs) | Cloudflare R2 |
| Matching-KI | Claude Haiku (Anthropic) |
| Klassifizierung | GPT-4o (OpenAI) |
| Geocoding | Nominatim/OpenStreetMap |
| Fahrzeit | Google Maps Distance Matrix |
| Datenbank | PostgreSQL + PostGIS |

---

## 9. Technischer Umsetzungsplan

### Phase 1: Kontrolliertes Matching (Prio 1) — ✅ DEPLOYED (22.02.2026)

1. ✅ `run_matching()` in 3 Funktionen aufgeteilt (run_stufe_0, run_stufe_1, run_stufe_2)
2. ✅ Session-Storage: In-Memory Dict (`_matching_sessions`)
3. ✅ 3 neue Endpoints: `run-stufe-0`, `run-stufe-1`, `run-stufe-2`
4. ✅ `exclude-pairs` Endpoint
5. ✅ Action Board: Stufe-0/1/2-Ansichten, Ausschliessen-Button, Kosten-Schaetzung
6. ✅ DSGVO: Keine Namen/Emails/Telefon an Claude (nur Kandidat-ID)
7. ✅ Kandidaten-Namen in Stufen-Ansichten sichtbar

### Phase 2: Vergleichs-Button (Prio 2) — ✅ DEPLOYED (22.02.2026)

1. ✅ Endpoint: `GET /api/v4/claude-match/compare-pair?candidate_id=...&job_id=...`
2. ✅ `buildCompareHtml()` zeigt ALLE Felder (Education, Languages, IT-Skills, Zertifikate, Gehalt, Adressen, Fahrzeit, Staerken/Schwaechen)
3. ✅ Vergleichs-Button in Stufe-0 und Stufe-1 Ansichten

### Phase 3: Vorstellen aufteilen + Job-PDF (Prio 3) — ✅ DEPLOYED (22.02.2026)

1. ✅ `JobVorstellungPdfService` (`app/services/job_vorstellung_pdf_service.py`) — Sincirus-Design
2. ✅ Template `job_vorstellung_sincirus.html` — Dark Design wie Profil-PDF
3. ✅ Endpoint: `GET /api/jobs/{id}/vorstellung-pdf`
4. ✅ "Job-PDF" Button auf Job-Detailseite
5. ✅ Action-Endpoint: `job_an_kandidat` + `profil_an_kunden`
6. ✅ Zwei Buttons: "Job an Kandidat" (gruen) + "Profil an Kunden" (accent)
7. ✅ Kontakt-Auswahl-Dialog (Kontakte + manuelle E-Mail)
8. ✅ "Job an Kandidat" oeffnet Job-Vorstellungs-PDF automatisch im neuen Tab
9. ✅ "Profil an Kunden" oeffnet Profil-PDF automatisch im neuen Tab

### Phase 4: E-Mail-Automatisierung (Prio 4) — ✅ IMPLEMENTIERT (22.02.2026)

1. ✅ Varianten-Erkennung implementieren (Erst/Folgekontakt) — `_detect_variant()` in EmailPreparationService
2. ✅ 4 E-Mail-Varianten (inline Templates im neuen EmailPreparationService)
3. ✅ GPT-4o fachliche Einschaetzung generieren (2-3 Saetze) — `_generate_fachliche_einschaetzung()`
4. ✅ E-Mail-Vorschau-Dialog im Frontend (editierbarer Betreff + Text)
5. ✅ Varianten-Wechsel — automatisch erkannt (Erst/Folgekontakt via Match-History)
6. ✅ Senden-Button (Microsoft Graph API via bestehenden MicrosoftGraphClient)
7. ✅ Outreach-Status tracking (outreach_status, outreach_sent_at, presentation_status)

**Neuer Flow:**
- "Job an Kandidat" → Email-Vorschau-Dialog → Bearbeiten → Senden ODER Nur PDF
- "Profil an Kunden" → Kontakt-Auswahl → Email-Vorschau-Dialog → Bearbeiten → Senden ODER Nur PDF
- 3 Buttons im Dialog: Abbrechen / Nur PDF (oeffnet PDF + setzt Status) / Senden (E-Mail + PDF-Anhang)

**Neue Dateien Phase 4:**
- `app/services/email_preparation_service.py` — Vorbereitung + Versand (~420 Zeilen)
- Endpoints: `POST /prepare-email` + `POST /send-email` in routes_claude_matching.py
- Frontend: Email-Vorschau-Modal + `prepareEmail()`, `sendEmail()`, `emailNurPdf()` in action_board.html

### Phase 5: Neue Stufe 0 (Geo-Kaskade + LLM-Vorfilter) — ✅ IMPLEMENTIERT

**Warum:** Das alte 2000-Limit schneidet gute Matches ab, weil nur nach Entfernung sortiert wird.
Neue Stufe 0 prueft ALLE Paare (kein Limit) und filtert mit Claude Haiku nach fachlicher Passung.

1. ✅ `job_tasks` Feld auf Job-Model + DB-Migration 024
2. ✅ Job-Klassifizierung erweitern: GPT extrahiert Taetigkeiten aus job_text → speichert in `job_tasks`
3. ✅ Backfill: `POST /api/jobs/maintenance/backfill-job-tasks` mit Status-Endpoint
4. ✅ Stufe 0 neu: Geo-Kaskade (PLZ → Stadt → 40km) statt nur ST_DWithin + kein 2000-Limit
5. ✅ Stufe 0 neu: Claude Haiku Vorfilter (aktuelle Position + Taetigkeiten vs. job_tasks → Prozent)
6. ✅ Stufe 0 neu: Filter >= 70% Passung → weiter zu Stufe 1
7. ⬜ Testen: Neuer Lauf ab Stufe 0 (braucht erst Backfill)

**Geaenderte Dateien:**
- `migrations/versions/024_add_job_tasks_field.py` — Neues `job_tasks` Text-Feld
- `app/models/job.py` — `job_tasks` Feld hinzugefuegt
- `app/services/finance_classifier_service.py` — Prompt + Datenklasse + apply_to_job erweitert
- `app/api/routes_jobs.py` — Backfill-Endpoint + Status-Endpoint
- `app/services/claude_matching_service.py` — Neue `run_stufe_0()` mit Geo-Kaskade + LLM-Vorfilter (async)
- `app/api/routes_claude_matching.py` — `vorfilter_stats` in Session-Response
- `app/templates/action_board.html` — Neuer `stufe_0_loading` View mit Fortschrittsanzeige

### Phase 6: Testing + Feinschliff (Prio 6) — ⬜ OFFEN

1. Milad legt Test-Unternehmen mit Kontakten an (fuer Kunden-E-Mail-Test)
2. Kompletten Flow testen: Stufe 0 (neu) → 1 → 2 → Vorstellen → E-Mail
3. Test "Job an Kandidat senden" (Job-PDF + E-Mail an Kandidat)
4. Test "Profil an Kunden senden" (Profil-PDF + Kontakt-Auswahl + E-Mail an Kontakt/Postfach)
5. Edge Cases pruefen (leere Ergebnisse, Fehler in einzelnen Stufen, kein Kontakt vorhanden)
6. Kosten tracken und validieren
7. UI polieren

---

## 10. Hinweise + Notizen

### Kritische Hinweise

| # | Hinweis | Grund | Entdeckt |
|---|---------|-------|----------|
| H1 | `async_session_maker` NICHT `async_session_factory` | Import-Name in `app/database.py`. Falscher Name crashed den Service. | 16.02.2026 |
| H2 | Railway 30s idle-in-transaction Timeout | DB-Session MUSS vor jedem API-Call geschlossen werden. Pro API-Call eigene Session. | 16.02.2026 |
| H3 | Imports MUESSEN im try-Block sein | Bei Background-Tasks: Wenn Import ausserhalb try/except fehlschlaegt, wird finally NICHT ausgefuehrt → Status bleibt "running: True" | 16.02.2026 |
| H4 | max_tokens=500 reicht NICHT fuer Deep Assessment | JSON wird abgeschnitten → JSON parse error → alle Stufe-2 Ergebnisse verloren. Fix: max_tokens=800 (vorher 1200, reduziert weil Score<75 nur Kurzantwort). | 22.02.2026 |
| H5 | ai_score ist 0.0-1.0, NICHT 0-100 | Templates muessen `ai_score * 100` rechnen fuer Vergleiche mit Thresholds. 5 Templates waren falsch. | 22.02.2026 |
| H6 | `description` in work_history kann None sein | `work_history[0].get("description", "")` gibt None zurueck wenn key existiert aber Wert None ist. Fix: `(... or "")` | 22.02.2026 |
| H7 | E-Mail-Fehler sind FATAL in der Recruiting-Branche | Falsche Daten, falsche Anrede, falscher Firma-Name → Kandidat/Kunde ist verloren. IMMER Vorschau zeigen. | 22.02.2026 |
| H8 | Google Maps Fahrzeit kostet $5/1000 Elements | PLZ-Cache spart ~70-80% der Calls. Threshold jetzt bei 80 (vorher 70). | 22.02.2026 |
| H9 | Klassifizierung jetzt GPT-4o (vorher mini) | ~10x teurer pro Call aber genauer. Wichtig fuer korrekte Rollen-Zuweisung. | 22.02.2026 |
| H10 | `candidate.city` Label im Query ist `"candidate_city"` | In der Stufe-0-Query wird `Candidate.city.label("candidate_city")` verwendet, aber `_extract_candidate_data()` sucht `row.get("city")`. Ergebnis: city ist immer "Unbekannt". Muss gefixt werden. | 22.02.2026 |
| H11 | Job-Vorstellungs-PDF ist NEU, nicht der alte JobDescriptionPdfService | Milad will ein richtig schoen designtes PDF im gleichen Stil wie das Kandidaten-Profil. Der alte Service reicht dafuer nicht. | 22.02.2026 |
| H12 | E-Mails an Kunden gehen an KONTAKTE, nicht an Firmen | Milad waehlt aus den bestehenden Kontakten im Unternehmen oder gibt ein Bewerber-Postfach ein. Die Kontakte-Tabelle muss existieren. | 22.02.2026 |
| H13 | Stufe-0 und Stufe-1 haben BEIDE: Vergleichs-Button + Ausschliessen-Button | Milad will in JEDER Stufe Paare pruefen (Vergleich) und ausschliessen koennen. Nicht nur in Stufe 0. | 22.02.2026 |
| H14 | Stufe 1: 0 Paare bestanden beim ersten Test | Claude hat nur `activities` (description der letzten Position) bekommen — oft leer ("Nicht verfuegbar"). Fix: Jetzt bekommt Claude den vollen Werdegang (work_history + education + further_education). | 22.02.2026 |
| H15 | Session-/Debug-Endpoints brauchen Login | AuthMiddleware auf allen `/api/v4/` Endpoints. Im Browser eingeloggt aufrufen, NICHT via curl mit API-Key. | 22.02.2026 |
| H16 | Session-Persistence bei Page-Navigation | Wenn Milad die Seite verlaesst und zurueckkommt, muss der laufende/fertige Matching-Lauf wiederhergestellt werden. Frontend `loadStatus()` prueft `progress.session_id` + `progress.stufe` und stellt `stufenView` + `sessionId` wieder her. Keine Parallel-Laeufe moeglich (Server prueft `_matching_status["running"]`). | 22.02.2026 |
| H17 | `/debug/last-run` Internal Server Error | `get_all_sessions()` gibt ein dict zurueck (keys=session_ids), nicht eine Liste. `sorted()` muss ueber `.values()` iterieren, nicht ueber das dict direkt. | 22.02.2026 |
| H18 | 2000-Limit in Stufe 0 schneidet gute Matches ab | Sortierung nach Entfernung + Limit 2000 → ein perfekt passender Kandidat bei 35km wird verworfen, wenn 2000 naehere (aber fachlich schlechtere) Paare existieren. Loesung: Kein Limit, stattdessen LLM-Vorfilter. | 22.02.2026 |
| H19 | Geo-Kaskade: PLZ → Stadt → 40km | Nicht nur ST_DWithin verwenden! Manche Kandidaten/Jobs haben falsche oder fehlende Geodaten. PLZ- und Stadt-Vergleich als Fallback fangen das ab. Reihenfolge: 1. PLZ gleich? 2. Stadt gleich? 3. Luftlinie <= 40km? | 22.02.2026 |
| H20 | `job_tasks` Feld fuer Token-Ersparnis | Taetigkeiten einmal aus job_text extrahieren (bei Klassifizierung), in eigenem Feld speichern. Stufe-0-Vorfilter braucht dann nur ~100 statt ~500 Tokens pro Job. Backfill fuer bestehende Jobs noetig. | 22.02.2026 |

### Offene Fragen

| # | Frage | Status |
|---|-------|--------|
| F1 | Sollen die Session-Daten in der DB oder In-Memory gespeichert werden? | ERLEDIGT: In-Memory (`_matching_sessions` Dict) |
| F2 | Soll die automatische E-Mail-Varianten-Erkennung NUR auf Match-Daten basieren oder auch auf CRM-Daten (Anrufe, Notizen)? | ENTSCHEIDUNG: Match-Daten (outreach_status == "sent"). CRM spaeter. |
| F3 | Soll das "alte" `run_matching()` als Fallback erhalten bleiben (fuer den n8n Morgen-Cron)? | ERLEDIGT: `/api/v4/claude-match/run` existiert weiterhin als Auto-Modus |
| F4 | Braucht der Kunden-E-Mail-Versand einen eigenen Microsoft Graph Zugang oder geht er ueber den gleichen? | ERLEDIGT: Gleicher Graph Account (Milads). MicrosoftGraphClient aus email_service.py wird wiederverwendet. |
| F5 | Soll die Kosten-Anzeige auch historische Kosten zeigen (alle bisherigen Laeufe)? | OFFEN |
| F6 | Gibt es bereits eine Kontakte-Tabelle im System (fuer Unternehmens-Kontakte)? | ERLEDIGT: `CompanyContact` Model existiert (`app/models/company_contact.py`). Kontakt-Endpoint gebaut. |

### Aenderungsprotokoll

| Datum | Was | Grund |
|-------|-----|-------|
| 22.02.2026 | Datei erstellt | Milad will kontrolliertes Matching + E-Mail-Automatisierung |
| 22.02.2026 | GPT-4o statt gpt-4o-mini | Milad wuenscht genauere Klassifizierung |
| 22.02.2026 | Fahrzeit-Threshold 70 → 80 | Milad will Kosten sparen, nur Top-Matches bekommen Fahrzeit |
| 22.02.2026 | Job-PDF korrigiert | Milad will NEUES Job-Vorstellungs-PDF im Profil-Design, nicht den alten JobDescriptionPdfService |
| 22.02.2026 | Empfaenger-Auswahl ergaenzt | Profil an Kunden geht an bestehende Kontakte/Postfaecher, nicht an "die Firma" |
| 22.02.2026 | Phase 1+2 bestaetigt | Vergleichs-Button + Ausschliessen-Button sind in JEDER Stufe (0+1) vorhanden |
| 22.02.2026 | Phase 1 deployed | Kontrolliertes Matching mit 3 Stufen, DSGVO-konform, Kandidaten-Namen sichtbar |
| 22.02.2026 | Phase 2 deployed | buildCompareHtml() zeigt ALLE Felder (Education, Languages, IT-Skills, Gehalt, etc.) |
| 22.02.2026 | Phase 3 deployed | Job-PDF-Service + Template, Buttons aufgeteilt, Kontakt-Dialog, PDF Auto-Oeffnen |
| 22.02.2026 | Phase 4 gestartet | EmailPreparationService, prepare-email + send-email Endpoints, Email-Vorschau-Dialog |
| 22.02.2026 | Phase 4 fertig | 4 E-Mail-Varianten, GPT fachliche Einschaetzung, Email-Vorschau-Dialog mit Bearbeiten, Senden via Graph, Nur-PDF-Option, Button-Flow umgestellt |
| 22.02.2026 | Debug erweitert | Stufe 1+2: Logging pro Paar (PASS/FAIL + Grund), error_pairs/stufe2_rejected/stufe2_errors tracken, neuer `/debug/last-run` + `/debug/claude-input` Endpoint |
| 22.02.2026 | Stufe-1-Prompt gefixt | Claude bekam nur `activities` (oft leer). Jetzt bekommt Stufe 1: work_history (alle Positionen), education, further_education/Zertifikate. Damit kann Claude z.B. IHK BiBu pruefen. |
| 22.02.2026 | Session-Persistence gefixt | `loadStatus()` stellt jetzt bei Page-Reload `sessionId` + `stufenView` aus `progress.session_id` + `progress.stufe` wieder her. Startet Polling wenn Lauf noch laeuft, laedt Session-Daten wenn Lauf fertig. |
| 22.02.2026 | `/debug/last-run` Bug gefixt | `get_all_sessions()` Rueckgabe ist dict, nicht list. `sorted()` iteriert jetzt ueber `.values()`. |
| 22.02.2026 | MIN_SCORE_SAVE: 40 → 75 | Nur noch starke Matches (75+) werden gespeichert. Alles unter 75 ist "nicht_passend" und wird verworfen. Weniger Muell in der DB. |
| 22.02.2026 | Stufe-2-Prompt komplett neu | Kurzer, klarer Prompt: "Schau dir den Werdegang an, schau dir die Stelle an, passt das zusammen?" Einziger Fach-Hinweis: BiBu braucht Zertifizierung + "eigenstaendige Erstellung" vs. "Unterstuetzung/Mitwirkung". Claude bewertet den Rest selbst. Score < 75 → Kurzantwort. |
| 22.02.2026 | Stufe-2-Daten reduziert | Kandidat: nur Berufserfahrung, Ausbildung, Weiterbildung, IT-Skills. Job: nur Titel + Stellenbeschreibung + Entfernung. Weniger Tokens = schneller. max_tokens 1200 → 800. |
| 22.02.2026 | Alles deployed | Session-Persistence, Stufe-2-Prompt, MIN_SCORE 75, Debug-Fix — alles auf Railway deployed. |
| 22.02.2026 | Stufe 0 NEU geplant | 2000-Limit weg. Neue Stufe 0: DB-Basisfilter → Geo-Kaskade (PLZ → Stadt → 40km) → Claude Haiku Vorfilter (aktuelle Position + Taetigkeiten vs. job_tasks → Prozent >= 70%). |
| 22.02.2026 | Neues Feld `job_tasks` geplant | Taetigkeiten aus job_text extrahieren (bei Klassifizierung), in eigenem Feld speichern. Spart Tokens in Stufe 0. Sichtbar + editierbar auf Job-Detailseite. |
| 22.02.2026 | `job_tasks` implementiert | Migration 024, Job-Klassifizierung extrahiert jetzt automatisch Taetigkeiten, Backfill-Endpoint gebaut. |
| 22.02.2026 | Stufe 0 NEU implementiert | Geo-Kaskade (OR: PLZ gleich / Stadt gleich / ≤40km) + Claude Haiku Vorfilter (≥70% Passung). Stufe 0 laeuft jetzt als Background-Task mit Live-Fortschritt. Neuer `stufe_0_loading` View im Frontend. |
