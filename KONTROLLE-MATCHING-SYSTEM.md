# Kontrolle Matching System — Konzept + Umsetzungsplan

> **Erstellt: 22.02.2026**
> **Letzte Aktualisierung: 22.02.2026**
> **Status: KONZEPT — Noch nicht umgesetzt**

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
STUFE 0: Datenbank-Filter (keine KI)
│   "Wer kommt ueberhaupt in Frage?"
│   - Kandidat hat Berufserfahrung (work_history ODER cv_text)?
│   - Job hat Beschreibung (job_text > 50 Zeichen)?
│   - Beide innerhalb 40km (PostGIS ST_DWithin)?
│   - Noch kein Match fuer dieses Paar in der DB?
│   - Job nicht abgelaufen (expires_at)?
│   - Job Qualitaet mindestens "medium"?
│   Ergebnis: ~2000 Kandidat-Job-Paare (Limit 2000)
│
├── STUFE 1: Quick-Check (Claude Haiku KI)
│   "Passt das grob zusammen? JA oder NEIN?"
│   - KI bekommt NUR: Taetigkeiten, Skills, ERP, gewuenschte Positionen
│   - KI antwortet: {"pass": true/false, "reason": "1 Satz"}
│   - ~2 Sekunden pro Paar
│   - ~65% Bestehensrate
│   Ergebnis: ~1300 Paare die weiter duerfen
│
└── STUFE 2: Deep Assessment (Claude Haiku KI)
    "Wie gut passt das genau?"
    - KI bekommt ALLES: Lebenslauf, Stellenbeschreibung, Gehalt, etc.
    - KI gibt zurueck:
      - Score (0-100)
      - Zusammenfassung (1-2 Saetze)
      - Staerken-Liste
      - Luecken-Liste
      - Empfehlung: "vorstellen" / "beobachten" / "nicht_passend"
      - WOW-Faktor: ja/nein + Grund
    - Nur Score >= 40 wird gespeichert
    Ergebnis: Fertige Matches in der Datenbank
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
| Claude-Modell Quick-Check (Stufe 1) | `claude-haiku-4-5-20251001` |
| Claude-Modell Deep Assessment (Stufe 2) | `claude-haiku-4-5-20251001` |
| Max Tokens Quick-Check | 500 |
| Max Tokens Deep Assessment | 1200 |
| Concurrency (parallele Calls) | Semaphore(3) |
| Max Paare pro Lauf | 2000 |
| Proximity-Matches (ohne KI) | < 10km, ohne Beschreibung |
| Kosten Stufe 1 (~2000 Paare) | ~$0.80 |
| Kosten Stufe 2 (~1300 Paare) | ~$3-5 |
| Fahrzeit-Threshold | Score >= 80 (konfigurierbar via /einstellungen) |
| Klassifizierung | GPT-4o (gerade geaendert von gpt-4o-mini) |
| Geocoding | Nominatim/OpenStreetMap (kostenlos) |
| Fahrzeit | Google Maps Distance Matrix API |

### Datei-Referenzen (V4 System)

| Datei | Zweck | Wichtige Zeilen |
|-------|-------|-----------------|
| `app/services/claude_matching_service.py` | Kern-Service: 3 Stufen | Stufe 0: Z.368-531, Stufe 1: Z.551-591, Stufe 2: Z.593-661 |
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
- Die Claude-Prompts bleiben identisch
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

1. Job-PDF wird generiert (bestehender `JobDescriptionPdfService`)
2. E-Mail wird vorbereitet mit Job-Details + PDF-Anhang
3. E-Mail-Variante wird automatisch gewaehlt (siehe Feature 4)
4. Milad prueft die E-Mail und klickt "Senden"

#### Aktion B: "Kandidat beim Kunden vorstellen"
> Milad schickt dem Kunden das Kandidaten-Profil per E-Mail

1. Kandidaten-Profil-PDF wird generiert (bestehender `ProfilePdfService`)
2. E-Mail wird vorbereitet mit Kandidaten-Highlights + PDF-Anhang
3. E-Mail-Variante wird automatisch gewaehlt (siehe Feature 4)
4. Milad prueft die E-Mail und klickt "Senden"

### UI-Aenderung im Action Board

Statt einem "Vorstellen"-Button gibt es jetzt ein Dropdown oder zwei separate Buttons:

```
┌──────────────────────────────┐
│  [Job an Kandidat senden]    │  ← Schickt Job-Vorschlag an Kandidat
│  [Profil an Kunden senden]   │  ← Schickt Kandidaten-Profil an Kunde
│  [Spaeter]                   │
│  [Ablehnen]                  │
└──────────────────────────────┘
```

### Bestehender Code der wiederverwendet wird

| Was | Datei | Methode |
|-----|-------|---------|
| Kandidaten-Profil-PDF | `app/services/profile_pdf_service.py` | `generate_profile_pdf(candidate_id)` |
| Job-PDF fuer Kandidat | `app/services/job_description_pdf_service.py` | `generate_job_pdf(match_id)` |
| E-Mail senden | `app/services/outreach_service.py` | `send_to_candidate(match_id)` |
| Profil-PDF Endpoint | `app/api/routes_candidates.py` Z.1419 | `GET /{candidate_id}/profile-pdf` |
| Job-PDF Endpoint | `app/api/routes_matches.py` Z.600 | `GET /matches/{match_id}/job-pdf` |

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

| Aktion | PDF | Service | Template |
|--------|-----|---------|----------|
| Job an Kandidat senden | Job-Beschreibung (personalisiert) | `JobDescriptionPdfService` | `job_description_sincirus.html` |
| Profil an Kunden senden | Kandidaten-Profil | `ProfilePdfService` | `profile_sincirus_branded.html` |

### WICHTIG: Bestehender Code wird WIEDERVERWENDET

Fuer "Profil an Kunden senden" wird EXAKT der gleiche Code verwendet wie der bestehende "Profil-PDF"-Button auf der Kandidaten-Detailseite:

```python
# Der gleiche Aufruf wie /api/candidates/{id}/profile-pdf:
from app.services.profile_pdf_service import ProfilePdfService
pdf_service = ProfilePdfService(db)
pdf_bytes = await pdf_service.generate_profile_pdf(candidate_id)
```

Es wird KEIN neuer PDF-Service erstellt. Der bestehende wird wiederverwendet.

### Zukuenftig moeglich: Match-spezifisches PDF

Spaeter koennte ein neues PDF erstellt werden, das BEIDES zeigt:
- Links: Kandidaten-Profil (Kurzversion)
- Rechts: Job-Anforderungen
- Unten: Gegenuberstellungstabelle

Das ist aber eine Erweiterung und wird NICHT im ersten Schritt umgesetzt.

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
| GET | `/api/v4/debug/match-count` | `routes_claude_matching.py` Z.386 | Match-Statistiken |
| GET | `/api/v4/debug/stufe-0-preview` | `routes_claude_matching.py` Z.456 | Dry-Run Vorschau |
| GET | `/api/v4/debug/job-health` | `routes_claude_matching.py` Z.529 | Job-Daten-Qualitaet |
| GET | `/api/v4/debug/candidate-health` | `routes_claude_matching.py` Z.606 | Kandidaten-Daten-Qualitaet |
| GET | `/api/v4/debug/match/{id}` | `routes_claude_matching.py` Z.689 | Match-Detail |
| GET | `/api/v4/debug/cost-report` | `routes_claude_matching.py` Z.724 | Kosten-Report |

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

### Phase 1: Kontrolliertes Matching (Prio 1 — HOECHSTE PRIORITAET)

1. `run_matching()` in 3 Funktionen aufteilen:
   - `run_stufe_0()` → Gibt Paare zurueck, speichert in Session
   - `run_stufe_1(session_id)` → Quick-Check, speichert Ergebnisse in Session
   - `run_stufe_2(session_id)` → Deep Assessment, speichert Matches in DB
2. Session-Storage implementieren (In-Memory Dict)
3. 3 neue Endpoints bauen (`run-stufe-0`, `run-stufe-1`, `run-stufe-2`)
4. `exclude-pairs` Endpoint bauen
5. Action Board erweitern:
   - Stufe-0-Ansicht (Paare-Tabelle)
   - Stufe-1-Ansicht (Quick-Check-Ergebnisse)
   - Navigation zwischen Stufen
   - Ausschliessen-Button pro Paar
   - Kosten-Schaetzung pro Stufe

### Phase 2: Vergleichs-Button (Prio 2)

1. Neuer Endpoint: `GET /api/v4/claude-match/compare-pair?candidate_id=...&job_id=...`
2. Compare-Template fuer Paare OHNE Match anpassen
3. Vergleichs-Button in Stufe-0 und Stufe-1 Ansichten einbauen

### Phase 3: Vorstellen aufteilen (Prio 3)

1. Action-Endpoint erweitern: `action = "job_an_kandidat"` und `action = "profil_an_kunden"`
2. UI: Dropdown oder zwei Buttons statt einem
3. PDF-Generierung integrieren (bestehende Services)
4. E-Mail-Vorbereitungs-Dialog bauen

### Phase 4: E-Mail-Automatisierung (Prio 4)

1. Varianten-Erkennung implementieren (Erst/Folgekontakt)
2. 4 E-Mail-Templates als Jinja2-Templates
3. GPT-4o fachliche Einschaetzung generieren
4. E-Mail-Vorschau-Dialog im Frontend
5. Senden-Button (Microsoft Graph API)
6. Outreach-Status tracking erweitern

### Phase 5: Testing + Feinschliff (Prio 5)

1. Kompletten Flow testen: Stufe 0 → 1 → 2 → Vorstellen → E-Mail
2. Edge Cases pruefen (leere Ergebnisse, Fehler in einzelnen Stufen)
3. Kosten tracken und validieren
4. UI polieren

---

## 10. Hinweise + Notizen

### Kritische Hinweise

| # | Hinweis | Grund | Entdeckt |
|---|---------|-------|----------|
| H1 | `async_session_maker` NICHT `async_session_factory` | Import-Name in `app/database.py`. Falscher Name crashed den Service. | 16.02.2026 |
| H2 | Railway 30s idle-in-transaction Timeout | DB-Session MUSS vor jedem API-Call geschlossen werden. Pro API-Call eigene Session. | 16.02.2026 |
| H3 | Imports MUESSEN im try-Block sein | Bei Background-Tasks: Wenn Import ausserhalb try/except fehlschlaegt, wird finally NICHT ausgefuehrt → Status bleibt "running: True" | 16.02.2026 |
| H4 | max_tokens=500 reicht NICHT fuer Deep Assessment | JSON wird abgeschnitten → JSON parse error → alle Stufe-2 Ergebnisse verloren. Fix: max_tokens=1200 | 22.02.2026 |
| H5 | ai_score ist 0.0-1.0, NICHT 0-100 | Templates muessen `ai_score * 100` rechnen fuer Vergleiche mit Thresholds. 5 Templates waren falsch. | 22.02.2026 |
| H6 | `description` in work_history kann None sein | `work_history[0].get("description", "")` gibt None zurueck wenn key existiert aber Wert None ist. Fix: `(... or "")` | 22.02.2026 |
| H7 | E-Mail-Fehler sind FATAL in der Recruiting-Branche | Falsche Daten, falsche Anrede, falscher Firma-Name → Kandidat/Kunde ist verloren. IMMER Vorschau zeigen. | 22.02.2026 |
| H8 | Google Maps Fahrzeit kostet $5/1000 Elements | PLZ-Cache spart ~70-80% der Calls. Threshold jetzt bei 80 (vorher 70). | 22.02.2026 |
| H9 | Klassifizierung jetzt GPT-4o (vorher mini) | ~10x teurer pro Call aber genauer. Wichtig fuer korrekte Rollen-Zuweisung. | 22.02.2026 |
| H10 | `candidate.city` Label im Query ist `"candidate_city"` | In der Stufe-0-Query wird `Candidate.city.label("candidate_city")` verwendet, aber `_extract_candidate_data()` sucht `row.get("city")`. Ergebnis: city ist immer "Unbekannt". Muss gefixt werden. | 22.02.2026 |

### Offene Fragen

| # | Frage | Status |
|---|-------|--------|
| F1 | Sollen die Session-Daten in der DB oder In-Memory gespeichert werden? | ENTSCHEIDUNG: In-Memory (einfach, Matching-Sessions sind kurzlebig) |
| F2 | Soll die automatische E-Mail-Varianten-Erkennung NUR auf Match-Daten basieren oder auch auf CRM-Daten (Anrufe, Notizen)? | OFFEN — CRM-Daten waeren genauer aber komplexer |
| F3 | Soll das "alte" `run_matching()` als Fallback erhalten bleiben (fuer den n8n Morgen-Cron)? | EMPFEHLUNG: Ja, als `/api/v4/claude-match/run-auto` (ohne manuelle Kontrolle) |
| F4 | Braucht der Kunden-E-Mail-Versand einen eigenen Microsoft Graph Zugang oder geht er ueber den gleichen? | PRUEFEN — Aktuell geht alles ueber Milads Graph Account |
| F5 | Soll die Kosten-Anzeige auch historische Kosten zeigen (alle bisherigen Laeufe)? | OFFEN |

### Aenderungsprotokoll

| Datum | Was | Grund |
|-------|-----|-------|
| 22.02.2026 | Datei erstellt | Milad will kontrolliertes Matching + E-Mail-Automatisierung |
| 22.02.2026 | GPT-4o statt gpt-4o-mini | Milad wuenscht genauere Klassifizierung |
| 22.02.2026 | Fahrzeit-Threshold 70 → 80 | Milad will Kosten sparen, nur Top-Matches bekommen Fahrzeit |
