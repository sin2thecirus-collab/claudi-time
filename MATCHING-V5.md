# MATCHING V5 — Role + Geography Matching System

> **Stand: 22.02.2026 — Single Source of Truth**
> **Status: IN UMSETZUNG**

---

## Inhaltsverzeichnis

1. [Projektkontext](#1-projektkontext)
2. [Gesamtziel](#2-gesamtziel)
3. [Motivation / Problemstellung](#3-motivation--problemstellung)
4. [Architektur-Uebersicht V5](#4-architektur-uebersicht-v5)
5. [Rollen-Kompatibilitaets-Matrix](#5-rollen-kompatibilitaets-matrix)
6. [Strukturierter Plan (Phasen 1-7)](#6-strukturierter-plan)
7. [Technische Referenzen](#7-technische-referenzen)
8. [Hinweis- und Risiko-Notizblock](#8-hinweis--und-risiko-notizblock)
9. [Debug Raw API Response Dokumentation](#9-debug-raw-api-response-dokumentation)
10. [Aenderungslog](#10-aenderungslog)

---

## 1. Projektkontext

### Was ist das Matching Tool?

Das PulsePoint CRM Matching Tool matcht Finance-Kandidaten (FiBu, BiBu, KrediBu, DebiBu, LohnBu, StFA) automatisch mit Jobangeboten. Die Kandidaten und Jobs werden per CSV importiert, durch GPT-4o klassifiziert (Rollenzuweisung, Quality Gate), geocodiert, und dann gematcht.

### Aktuelle Systemversion (V4 — wird ersetzt)

V4 nutzt ein 3-Stufen Claude-AI-System:
- **Stufe 0:** Harte DB-Filter (PostGIS 40km + LLM-Vorfilter via Claude Haiku)
- **Stufe 1:** Claude Haiku Quick-Check (JA/NEIN pro Paar, ~$0.001)
- **Stufe 2:** Claude Haiku Deep Assessment (Score 0-100, Staerken/Luecken/Empfehlung, ~$0.003)

**Dateien (V4, werden refactored):**

| Datei | Zeilen | Was |
|-------|--------|-----|
| `app/services/claude_matching_service.py` | 2.146 | Kern-Service: Stufe 0+1+2, Prompts, Status, Match-Speicherung |
| `app/api/routes_claude_matching.py` | 1.894 | API-Endpoints, Action Board Backend |
| `app/templates/action_board.html` | 1.325 | Action Board Frontend (Alpine.js) |

### Bestandteile die NICHT geaendert werden

| Datei | Was | Warum unveraendert |
|-------|-----|-------------------|
| `app/services/outreach_service.py` | E-Mail + PDF Versand | Funktioniert, keine Abhaengigkeit zu Matching-Logik |
| `app/services/email_preparation_service.py` | E-Mail-Vorbereitung fuer Action Board | Nutzt nur Match-ID, unabhaengig |
| `app/services/email_service.py` | Microsoft Graph Client | Infrastruktur |
| `app/services/job_description_pdf_service.py` | Job-PDF (WeasyPrint) | Unabhaengig |
| `app/services/profile_pdf_service.py` | Kandidaten-Profil-PDF | Unabhaengig |
| `app/services/distance_matrix_service.py` | Google Maps Fahrzeit | Wird AUFGERUFEN, nicht geaendert |
| `app/services/telegram_bot_service.py` | Telegram Bot | Wird AUFGERUFEN, nicht geaendert |
| `app/services/finance_classifier_service.py` | GPT-Klassifizierung | Laeuft VOR dem Matching |
| `app/models/match.py` | Match-Datenmodell | DB-Schema bleibt, nur neue `matching_method` Werte |
| `app/models/ats_pipeline.py` | ATS Pipeline Model | Unabhaengig |
| `app/models/ats_job.py` | ATS Job Model | Unabhaengig |

---

## 2. Gesamtziel

V4 Claude Matching (teuer, viel Muell) durch ein einfaches, kostenfreies **Rollen + Geographie Matching** ersetzen:

1. **Rollen-Match:** Kandidat `hotlist_job_titles[]` vs. Job `hotlist_job_titles[]` — Ueberlappung + 3 Kompatibilitaetsregeln
2. **Geo-Filter:** PostGIS 27km Radius (statt 40km)
3. **Google Maps Fahrzeit:** Fuer ALLE gematchten Paare (nicht nur Score >= Threshold)
4. **Telegram-Benachrichtigung:** NUR fuer Matches mit Auto ≤ 60 Min UND OEPNV ≤ 30 Min
5. **Optionale manuelle KI-Bewertung:** Recruiter waehlt Matches aus → Claude bewertet mit konfigurierbarem Prompt → Score + Staerken + Luecken

**Kostenvergleich:**

| System | Matching-Kosten pro Lauf | Google Maps | Gesamt |
|--------|--------------------------|-------------|--------|
| V4 (Claude) | ~$0.50-1.00 (Claude Haiku) | ~$0.50-1.00 | ~$1.00-2.00 |
| V5 (Rolle+Geo) | $0.00 (kein AI) | ~$1.00-3.00 | ~$1.00-3.00 |
| V5 + optionale KI | ~$0.10-0.30 (nur ausgewaehlte) | wie oben | ~$1.10-3.30 |

---

## 3. Motivation / Problemstellung

### Warum V4 ersetzt wird

1. **Zu viel Muell:** Claude bewertet JEDES Paar das durch den Geo-Filter kommt, auch offensichtlich unpassende (z.B. Lohnbuchhalter auf BiBu-Stelle). Das erzeugt Matches mit Score 75+ die trotzdem Muell sind.

2. **Zu teuer:** Jeder Matching-Lauf kostet $1-2 fuer Claude API. Bei taeglichem Lauf: ~$30-60/Monat nur fuer Matching.

3. **Klassifizierung ist jetzt sauber:** Die GPT-Klassifizierung wurde am 22.02.2026 repariert:
   - BiBu-Kontext-Pruefung gefixt (Section-Header "AUSBILDUNG:" verursachte false positives)
   - StFA-Rolle praezisiert (nur mit abgeschlossener Ausbildung)
   - Teamleiter Lohnbuchhaltung als eigene Rolle ergaenzt
   - Titel-Felder vereinheitlicht (`hotlist_job_titles` → `manual_job_titles`)

4. **Rollen reichen als Match-Kriterium:** Wenn ein Kandidat als "Finanzbuchhalter/in" klassifiziert ist und ein Job "Finanzbuchhalter/in" sucht — das ist ein Match. Dafuer braucht man keine KI.

### Warum bestimmte Schritte durchgefuehrt werden

| Schritt | Warum |
|---------|-------|
| Rollen-Matrix statt Claude | Deterministische, kostenfreie Entscheidung. Kein Interpretationsspielraum. |
| 27km statt 40km | Milads Vorgabe basierend auf Recruiting-Erfahrung. 40km war zu grosszuegig. |
| Google Maps fuer ALLE Matches | V4 berechnete Fahrzeit nur fuer Score >= 70. V5 hat keinen Score → alle bekommen Fahrzeit. |
| Telegram nur bei Fahrzeit-Limits | Nur wirklich relevante Matches sollen eine Notification ausloesen. |
| KI als manueller Trigger | Recruiter entscheidet WANN und WELCHE Matches bewertet werden. Spart Geld, besserer Input. |

---

## 4. Architektur-Uebersicht V5

### Matching-Flow

```
CSV-Import → Kategorisierung → Klassifizierung → Geocoding → STOP
                                                                |
Auto-Trigger: run_v5_matching() (oder manuell via Action Board)
                                                                |
Phase A: Datenbank-Query
  → PostGIS: Kandidat ≤ 27km zum Job (ST_DWithin)
  → Basis-Filter: Kandidat nicht geloescht/hidden, Job nicht abgelaufen, quality_score high/medium
  → Duplikat-Check: Kein bestehender Match fuer dieses Paar
  → Lade: hotlist_job_titles fuer Kandidat + Job, Koordinaten, PLZ
                                                                |
Phase B: Rollen-Filter (In-Memory, kostenlos)
  → Direkter Vergleich: kandidat.hotlist_job_titles[] ∩ job.hotlist_job_titles[]
  → Plus 3 Kompatibilitaetsregeln (siehe Abschnitt 5)
  → Kein Match → verwerfen
                                                                |
Phase C: Google Maps Fahrzeit (fuer ALLE verbleibenden Paare)
  → Gruppiert nach Job (1 API-Call pro Job-Batch)
  → PLZ-Cache reduziert ~70% der API-Calls
  → Ergebnis: drive_time_car_min + drive_time_transit_min
                                                                |
Phase D: Matches speichern
  → matching_method = "v5_role_geo"
  → status = NEW (kein AI-Score)
  → v2_score_breakdown enthaelt gematchte Rollen
                                                                |
Phase E: Telegram-Benachrichtigung
  → NUR fuer: Auto ≤ 60 Min UND OEPNV ≤ 30 Min
  → Nachricht an Milads Chat-ID (7103040196)
                                                                |
Action Board zeigt Ergebnisse → Recruiter handelt
  → [Optional] KI-Bewertung fuer ausgewaehlte Matches triggern
```

### Feature-Erhalt (V4 → V5)

| Feature | V4 Status | V5 Status | Aenderung |
|---------|-----------|-----------|-----------|
| Action Board Dashboard | `/action-board` | Bleibt | UI-Vereinfachung (WOW/Naehe entfernt) |
| Vorstellen/Spaeter/Ablehnen | `POST /action` | Bleibt | Unveraendert |
| ATS Pipeline Integration | Bei "vorstellen" | Bleibt | Unveraendert |
| Follow-up Logik | "spaeter" → naechster Tag | Bleibt | Unveraendert |
| E-Mail Vorbereitung | `POST /prepare-email` | Bleibt | Unveraendert |
| E-Mail Versand | `POST /send-email` | Bleibt | Unveraendert |
| Job-PDF / Profil-PDF | PDF-Services | Bleibt | Unveraendert |
| Regional Insights | `GET /regional-insights` | Bleibt | Unveraendert |
| Detailliertes Feedback | `POST /detailed-feedback` | Bleibt | Unveraendert |
| Google Maps Fahrzeit | Nur Score >= 70 | Fuer ALLE | Mehr API-Calls |
| WOW-Faktor | `wow_faktor`, `wow_grund` | **ENTFERNT** | Felder bleiben in DB, werden nicht mehr gesetzt |
| Naehe-Matches | `proximity_match` | **ENTFERNT** | Werden nicht mehr erzeugt |
| Claude Scoring | Stufe 1+2 automatisch | **Optional manuell** | Nur auf Knopfdruck |
| Kontrolliertes Matching (Stufen-UI) | Stufe 0/1/2 einzeln | **ENTFERNT** | Vereinfacht zu "Matching starten" |

---

## 5. Rollen-Kompatibilitaets-Matrix

### Grundprinzip

Ein Match entsteht wenn:
1. **Direkte Ueberlappung:** Irgendeine Rolle in `kandidat.hotlist_job_titles[]` kommt auch in `job.hotlist_job_titles[]` vor
2. **ODER Kompatibilitaetsregel:** Eine Kandidaten-Rolle ist kompatibel mit einer Job-Rolle (siehe unten)

### Die 3 Kompatibilitaetsregeln

| # | Kandidaten-Rolle | Kompatibel mit Job-Rolle | Richtung | Begruendung |
|---|------------------|--------------------------|----------|-------------|
| 1 | Steuerfachangestellte/r / Finanzbuchhalter/in | Finanzbuchhalter/in | Bidirektional | StFA macht den Job eines FiBu, FiBu kann auch StFA-Job machen |
| 2 | Senior Finanzbuchhalter/in | Finanzbuchhalter/in | Bidirektional | Senior FiBu kann FiBu, FiBu kann Senior FiBu |
| 3 | Kreditorenbuchhalter/in | Finanzbuchhalter/in | Einseitig (Kandidat→Job) | KrediBu kann FiBu machen, aber FiBu nicht zwingend KrediBu |

### Alle anderen Rollen: NUR direkter Match

| Rolle | Matcht nur mit sich selbst |
|-------|--------------------------|
| Bilanzbuchhalter/in | Ja — eigene Qualifikation noetig |
| Debitorenbuchhalter/in | Ja — eigenes Fachgebiet |
| Lohnbuchhalter/in | Ja — eigenes Fachgebiet |
| Leiter Buchhaltung | Ja — Fuehrungsrolle |
| Teamleiter Lohnbuchhaltung | Ja — Fuehrungsrolle |
| Controller/in | Ja — eigenes Fachgebiet |
| Kaufm. Sachbearbeiter/in | Ja — eigene Kategorie |
| Buchhalter/in (Allgemein) | Ja — Fallback-Rolle |
| Wirtschaftspruefer/in / Steuerberater/in | Ja — eigene Qualifikation |

### Warum die Matrix so einfach ist

Die Intelligenz steckt bereits im Classifier. Wenn ein Debitorenbuchhalter frueher FiBu gemacht hat, gibt ihm der Classifier `hotlist_job_titles: ["Debitorenbuchhalter/in", "Finanzbuchhalter/in"]`. Der direkte Rollen-Vergleich findet den FiBu-Match automatisch. Die Matrix ergaenzt nur die 3 Faelle wo unterschiedliche Rollen-NAMEN fachlich austauschbar sind.

### Implementierung (Python)

```python
# Kompatibilitaets-Map: Kandidaten-Rolle → kompatible Job-Rollen
ROLE_COMPATIBILITY = {
    "Steuerfachangestellte/r / Finanzbuchhalter/in": {"Finanzbuchhalter/in"},
    "Senior Finanzbuchhalter/in": {"Finanzbuchhalter/in"},
    "Kreditorenbuchhalter/in": {"Finanzbuchhalter/in"},
    # Rueckrichtung (bidirektional fuer StFA + Senior FiBu):
    "Finanzbuchhalter/in": {
        "Steuerfachangestellte/r / Finanzbuchhalter/in",
        "Senior Finanzbuchhalter/in",
    },
}

def _roles_match(candidate_roles: list[str], job_roles: list[str]) -> bool:
    """Prueft ob Kandidat und Job ueber Rollen zusammenpassen."""
    cand_set = set(candidate_roles or [])
    job_set = set(job_roles or [])

    # 1. Direkte Ueberlappung
    if cand_set & job_set:
        return True

    # 2. Kompatibilitaetsregeln
    for cand_role in cand_set:
        compat = ROLE_COMPATIBILITY.get(cand_role, set())
        if compat & job_set:
            return True

    return False
```

### ACHTUNG: Rollen-Namen Varianten

Der Classifier gibt die volle Rolle aus: `"Steuerfachangestellte/r / Finanzbuchhalter/in"`. Aber in `hotlist_job_titles` koennte auch die Kurzform `"Steuerfachangestellte/r"` stehen (aus der Kategorisierung). Die Matching-Logik muss BEIDE Varianten erkennen. Loesung: Normalisierung — wenn eine Rolle mit `"Steuerfachangestellte"` beginnt, wird sie als StFA behandelt.

---

## 6. Strukturierter Plan

### Phase 1: Neuen V5 Matching Service erstellen

**Zweck:** Kern-Matching-Logik implementieren (Rolle + Geo + Fahrzeit + Telegram)
**Ziel:** Neuer Service `v5_matching_service.py` der voellig unabhaengig von V4 laeuft
**Erwartetes Ergebnis:** `run_matching()` Funktion die Matches erzeugt und in DB speichert

**Neue Datei:** `app/services/v5_matching_service.py`

**Was rein kommt:**

| Komponente | Herkunft | Aenderung |
|-----------|----------|-----------|
| `_matching_status` Dict | V4 Zeile 54-60 | Vereinfacht (keine Stufen, keine Sessions) |
| `get_status()` | V4 Zeile ~62 | Unveraendert |
| `request_stop()`, `is_stop_requested()`, `clear_stop()` | V4 Zeile ~68-89 | Unveraendert |
| `ROLE_COMPATIBILITY` | **NEU** | Siehe Abschnitt 5 |
| `_roles_match()` | **NEU** | Siehe Abschnitt 5 |
| `_extract_candidate_data()` | V4 Zeile 239-322 | 1:1 kopiert (fuer optionale KI) |
| `_extract_job_data()` | V4 Zeile 325-339 | 1:1 kopiert (fuer optionale KI) |
| `_call_claude()` | V4 Zeile 344-387 | 1:1 kopiert (fuer optionale KI) |
| `run_matching()` | **NEU** | Rolle + Geo + Fahrzeit + Telegram |
| `run_ai_assessment()` | **NEU** | Optionale manuelle KI-Bewertung |

**Was NICHT rein kommt (V4-Code der entfaellt):**

| Komponente | V4 Zeile | Grund |
|-----------|----------|-------|
| `VORFILTER_SYSTEM/USER` Prompts | 147-175 | Kein LLM-Vorfilter mehr |
| `QUICK_CHECK_SYSTEM/USER` Prompts | 182-206 | Keine Stufe 1 |
| `DEEP_ASSESSMENT_SYSTEM/USER` Prompts | 211-234 | Wird durch konfigurierbaren Prompt ersetzt |
| `run_stufe_0()` | 424-1022 | Ersetzt durch `run_matching()` |
| `run_stufe_1()` | 1024-1202 | Entfaellt komplett |
| `run_stufe_2()` | 1204-1583 | Ersetzt durch `run_ai_assessment()` |
| `_matching_sessions` | ~90-142 | Kein Session-System |
| Proximity-Match-Logik | 684-738 | Kein Naehe-Matching |
| `MIN_SCORE_SAVE`, `VORFILTER_MIN_SCORE` | 47, 177 | Kein Score-Threshold |

**`run_matching()` Ablauf im Detail:**

```
Phase A: Daten laden (eigene DB-Session, danach schliessen)
  1. Query: SELECT kandidat + job WHERE
     - ST_DWithin(coords, coords, 27000)  ← 27km
     - Kandidat: nicht geloescht, nicht hidden, classification_data vorhanden
     - Job: nicht geloescht, quality_score IN (high, medium), nicht abgelaufen
     - Kein bestehender Match (UniqueConstraint)
  2. Lade: candidate_id, job_id, hotlist_job_titles (beide), lat/lng/plz (beide), distance_km
  3. Session schliessen, Ergebnisse als List[Dict]

Phase B: Rollen-Filter (rein in-memory)
  1. Fuer jedes Paar: _roles_match(cand_roles, job_roles)
  2. Nicht-passende verwerfen

Phase C: Google Maps Fahrzeit
  1. Paare gruppieren nach job_id
  2. Pro Job-Gruppe: distance_matrix_service.batch_drive_times()
  3. KEINE DB-Session offen waehrend API-Call (Railway 30s Timeout!)
  4. Ergebnis: {candidate_id → DriveTimeResult(car_min, transit_min)}

Phase D: Matches speichern
  1. Pro Match eigene DB-Session (Railway 30s Timeout!)
  2. Match-Felder:
     - matching_method = "v5_role_geo"
     - status = MatchStatus.NEW
     - distance_km = aus Phase A
     - drive_time_car_min, drive_time_transit_min = aus Phase C
     - v2_score = None (kein AI-Score)
     - ai_score = None
     - v2_score_breakdown = {
         "scoring_version": "v5_role_geo",
         "candidate_roles": [...],
         "job_roles": [...],
         "matched_roles": [...],
       }
     - v2_matched_at = now

Phase E: Telegram-Benachrichtigung
  1. Filter: drive_time_car_min <= 60 UND drive_time_transit_min <= 30
  2. Fuer qualifizierte Matches:
     - Lade Kandidaten-Name + Job-Daten (eigene DB-Session)
     - Sende via telegram_bot_service.send_message()
     - Nachricht mit: Kandidat, Rolle, Stelle, Firma, Stadt, Distanz, Fahrzeit
```

**Technische Abhaengigkeiten:**

| Abhaengigkeit | Import | Wo |
|--------------|--------|-----|
| DB-Session | `from app.database import async_session_maker` | IMMER im try-Block! |
| Match Model | `from app.models.match import Match, MatchStatus` | Im try-Block |
| Candidate Model | `from app.models.candidate import Candidate` | Im try-Block |
| Job Model | `from app.models.job import Job` | Im try-Block |
| Distance Matrix | `from app.services.distance_matrix_service import distance_matrix_service` | Im try-Block |
| Telegram | `from app.services.telegram_bot_service import send_message` | Im try-Block |
| SQLAlchemy | `select, and_, func, or_` | Im try-Block |
| PostGIS | `func.ST_DWithin`, `func.ST_Y`, `func.ST_X`, `func.ST_GeomFromWKB` | Im try-Block |
| Anthropic | `from anthropic import Anthropic` | Nur in `run_ai_assessment()` |

**Betroffene Komponenten:** Nur neue Datei, keine bestehende Datei wird geaendert
**Risiko-Einschaetzung:** NIEDRIG — neue Datei, kein bestehender Code betroffen
**Test/Verifikation:**
- `python3 -c "import ast; ast.parse(open('app/services/v5_matching_service.py').read())"`
- Unit-Test: `_roles_match()` mit allen Rollen-Kombinationen

---

### Phase 2: Auto-Trigger umleiten (routes_jobs.py)

**Zweck:** CSV-Import-Pipeline soll V5 statt V4 Matching triggern
**Ziel:** Nach Geocoding wird `v5_matching_service.run_matching()` aufgerufen
**Erwartetes Ergebnis:** Neue Jobs loesen automatisch V5 Matching aus

**Datei:** `app/api/routes_jobs.py`
**Zeilen:** 166-175

**Aenderung:**

```python
# VORHER (V4):
from app.services.claude_matching_service import run_matching, get_status

# NACHHER (V5):
from app.services.v5_matching_service import run_matching, get_status
```

Sonst nichts. Gleiche Logik: `if not get_status()["running"]: asyncio.create_task(run_matching())`

**Technische Abhaengigkeiten:**
- Phase 1 MUSS abgeschlossen sein (v5_matching_service.py muss existieren)
- `run_matching` und `get_status` muessen gleiche Signatur haben wie V4

**Betroffene Komponenten:** Nur `routes_jobs.py` Zeile 168
**Risiko-Einschaetzung:** NIEDRIG — 1 Zeile Import aendern
**Test/Verifikation:** CSV-Import durchfuehren, pruefen ob V5 Matching im Log startet

---

### Phase 3: API-Endpoints anpassen (routes_claude_matching.py)

**Zweck:** Backend-Endpoints auf V5 umstellen, toten Code entfernen
**Ziel:** `/daily` liefert V5 Matches, neue Endpoints fuer KI-Bewertung, alte Stufen-Endpoints weg
**Erwartetes Ergebnis:** Action Board Backend funktioniert mit V5 Matches

**Datei:** `app/api/routes_claude_matching.py`

#### 3.1 Endpoints ENTFERNEN

| Endpoint | Grund |
|----------|-------|
| `POST /claude-match/run-stufe-0` | Kein Stufen-System mehr |
| `POST /claude-match/run-stufe-1` | Kein Stufen-System mehr |
| `POST /claude-match/run-stufe-2` | Kein Stufen-System mehr |
| `POST /claude-match/exclude-pairs` | Kein Session-System mehr |
| `GET /claude-match/session/{session_id}` | Kein Session-System mehr |
| `GET /claude-match/sessions` | Kein Session-System mehr |

#### 3.2 Endpoints AENDERN

**`POST /claude-match/run` (Zeile ~68):**
- Import aendern: `v5_matching_service` statt `claude_matching_service`
- Query-Parameter `model_quick`, `model_deep` entfernen
- Aufruf: `run_matching()` ohne Modell-Parameter

**`POST /claude-match/run-auto` (Zeile ~709):**
- Gleiche Aenderung wie `/run`

**`GET /claude-match/daily` (Zeile 248-437):**
- Top-Matches Query:
  - `Match.matching_method` auf `"v5_role_geo"` aendern (ODER beide akzeptieren fuer Uebergang)
  - `Match.empfehlung == "vorstellen"` Filter ENTFERNEN (V5 hat keine Empfehlung)
  - Stattdessen: `Match.user_feedback.is_(None)` (alle neuen Matches ohne User-Aktion)
  - `Match.created_at >= today_start` bleibt
  - Zusaetzlich laden: `Candidate.first_name`, `Candidate.last_name` (fuer Anzeige)
  - `Match.ai_checked_at` laden (fuer "bereits durch KI gematcht" Badge)
- WOW-Matches Query (Zeile 306-346): **KOMPLETT ENTFERNEN**
- Naehe-Matches Query (Zeile 387-414): **KOMPLETT ENTFERNEN**
- Follow-ups Query (Zeile 348-385):
  - `Match.matching_method` Filter anpassen (beide akzeptieren)
  - Rest bleibt gleich
- Response:
  - `wow_matches` und `proximity_matches` entfernen
  - `summary` anpassen (nur `total_top`, `total_followups`)

**`POST /claude-match/candidate/{candidate_id}` (Zeile ~682):**
- Import aendern auf `v5_matching_service`

**`GET /claude-match/status` (Zeile ~200):**
- Import aendern auf `v5_matching_service`

**`POST /claude-match/stop` (Zeile ~220):**
- Import aendern auf `v5_matching_service`

#### 3.3 Neuer Endpoint: KI-Bewertung

**`POST /claude-match/ai-assessment`**

```python
class AIAssessmentRequest(BaseModel):
    match_ids: list[str]
    custom_prompt: str | None = None

@router.post("/claude-match/ai-assessment")
async def trigger_ai_assessment(body: AIAssessmentRequest, ...):
    # Ruft v5_matching_service.run_ai_assessment() als Background-Task auf
    # Prueft vorher ob schon ein Matching laeuft
    # Gibt {"status": "started", "count": len(match_ids)} zurueck
```

**Technische Abhaengigkeiten:**
- Phase 1 MUSS abgeschlossen sein
- V4-Endpoints die entfernt werden duerfen NICHT aus dem Frontend referenziert werden (Phase 6 prueft das)

**Betroffene Komponenten:** `routes_claude_matching.py`
**Risiko-Einschaetzung:** MITTEL — Aenderungen an bestehendem Code, aber Feature-Erhalt getestet
**Test/Verifikation:**
- `GET /api/v4/claude-match/daily` aufrufen → Response-Struktur pruefen
- `POST /api/v4/claude-match/run` aufrufen → V5 Matching startet
- `GET /api/v4/claude-match/status` → Fortschritt anzeigen
- Entfernte Endpoints geben 404 zurueck

---

### Phase 4: KI-Bewertungs-Prompt in System-Settings

**Zweck:** Konfigurierbarer KI-Bewertungs-Prompt der ueber `/einstellungen` editiert werden kann
**Ziel:** Recruiter (Milad) kann den Prompt jederzeit anpassen ohne Code-Aenderung
**Erwartetes Ergebnis:** Prompt in `system_settings` Tabelle gespeichert, editierbar in UI

**Dateien:**
- `migrations/versions/024_add_ai_assessment_prompt.py` — Neuer Default-Eintrag
- `app/api/routes_settings.py` — Helper-Funktion `get_ai_assessment_prompt()`

**Default-Prompt:**

```
Du bist ein extrem erfahrener Personalberater mit 20 Jahre Berufserfahrung im Bereich Finance und Accounting.

Bewerte bitte nach deiner Meinung und nach deinem Ermessen ob dieser Kandidat fuer diese Stelle geeignet ist.

Achte besonders auf:
- Uebereinstimmung der Taetigkeiten
- Qualifikations-Level
- Branchenerfahrung
- Software-Kenntnisse (DATEV, SAP, etc.)
- Soft Skills und Entwicklungspotenzial

Antworte NUR als JSON:
{
  "score": 0-100,
  "staerken": ["...", "..."],
  "luecken": ["...", "..."]
}
```

**Technische Abhaengigkeiten:**
- `system_settings` Tabelle muss existieren (Migration 022 — bereits deployed)
- `routes_settings.py` muss den Helper exportieren
- Phase 1 (`run_ai_assessment()`) nutzt diesen Prompt

**Betroffene Komponenten:** `routes_settings.py`, neue Migration
**Risiko-Einschaetzung:** NIEDRIG — nur neuer DB-Eintrag + Helper
**Test/Verifikation:** `GET /api/settings/ai_assessment_prompt` → Prompt zurueckgeben

---

### Phase 5: Match Center Kompatibilitaet

**Zweck:** Match Center erkennt V5 Matches korrekt
**Ziel:** `_effective_score()` behandelt `v5_role_geo` Matches
**Erwartetes Ergebnis:** V5 Matches erscheinen im Match Center mit Score 0 (oder AI-Score wenn bewertet)

**Datei:** `app/services/match_center_service.py`
**Zeile:** ~166 (`_effective_score()`)

**Aenderung:** Neuen Case fuer `v5_role_geo` hinzufuegen:

```python
(Match.matching_method == "v5_role_geo",
 func.coalesce(Match.v2_score, literal_column("0")))
```

V5 Matches ohne KI-Bewertung: Score = 0
V5 Matches mit KI-Bewertung: Score = v2_score (aus `run_ai_assessment()`)

**Technische Abhaengigkeiten:** Keine — reine Ergaenzung
**Betroffene Komponenten:** `match_center_service.py`
**Risiko-Einschaetzung:** NIEDRIG — additiver Case, bestehende Cases unveraendert
**Test/Verifikation:** Match Center oeffnen → V5 Matches sichtbar

---

### Phase 6: Action Board Frontend anpassen

**Zweck:** UI auf V5 umstellen, toten Code entfernen, KI-Bewertung integrieren
**Ziel:** Sauberes Action Board mit nur relevanten Sektionen
**Erwartetes Ergebnis:** Top Matches + Follow-ups + KI-Bewertungs-Button

**Datei:** `app/templates/action_board.html`

#### 6.1 ENTFERNEN

| UI-Element | Zeilen (ca.) | Grund |
|-----------|-------------|-------|
| "Kontrolliertes Matching" Button | ~16-29 | Kein Stufen-System |
| Stufe 0 Lade-Ansicht | ~77-118 | Kein Stufen-System |
| Stufe 0 Ergebnis-Tabelle | ~121-166 | Kein Stufen-System |
| Stufe 1 Ergebnisse | ~169-229 | Kein Stufen-System |
| Stufe 2 Ergebnisse | ~232-263 | Kein Stufen-System |
| "Goldene Chancen" Summary-Card | Summary-Bereich | Kein WOW-Faktor |
| "Naehe-Matches" Summary-Card | Summary-Bereich | Kein Naehe-Matching |
| WOW-Badge auf Match-Karten | Karten-Template | Kein WOW-Faktor |

#### 6.2 AENDERN

| UI-Element | Aenderung |
|-----------|-----------|
| Summary-Cards | Nur noch 2: "Neue Matches" + "Follow-ups" |
| Progress-Bar | Vereinfacht: "Rollen-Matching → Fahrzeit-Berechnung → Fertig" |
| Match-Karten | Score-Gradient entfernen, Fahrzeit prominent anzeigen |
| Daten von `/daily` | `wow_matches` + `proximity_matches` nicht mehr erwartet |

#### 6.3 HINZUFUEGEN

| UI-Element | Beschreibung |
|-----------|-------------|
| "KI-Bewertung" Button | Pro Match-Karte — ruft `POST /ai-assessment` auf |
| "Alle bewerten" Button | Bulk-Trigger fuer alle sichtbaren Matches |
| "Bereits durch KI bewertet" Badge | Gruen, wenn `ai_checked_at` gesetzt |
| KI-Score + Staerken/Luecken | Zeigt Ergebnisse der optionalen KI-Bewertung |

**Alpine.js Aenderungen:**

```javascript
// Neue State-Variablen:
aiAssessmentRunning: false,

// Neue Methoden:
async triggerAIAssessment(matchIds) { ... }
async triggerAIAssessmentAll() { ... }

// ENTFERNTE Methoden:
startKontrolliertesMatching()
startStufe1()
startStufe2()
toggleExclude()
excludeFromStufe1()
// Alle stufenView/stufenData/stufe1Data/stufe2Data Variablen
```

**Technische Abhaengigkeiten:**
- Phase 3 MUSS abgeschlossen sein (Backend liefert neues `/daily` Format)
- Phase 1 MUSS abgeschlossen sein (KI-Bewertung Endpoint muss funktionieren)

**Betroffene Komponenten:** `action_board.html`
**Risiko-Einschaetzung:** MITTEL — Grosse UI-Aenderung, aber isoliert in einer Datei
**Test/Verifikation:**
- Action Board laden → keine JS-Fehler in Console
- Match-Karten werden angezeigt
- "Matching starten" → V5 Matching laeuft, Progress wird angezeigt
- "KI-Bewertung" Button → Assessment startet, Badge erscheint

---

### Phase 7: V4 Service als Wrapper + Cleanup + Commit

**Zweck:** Alte V4-Referenzen absichern, sauber aufraumen
**Ziel:** `claude_matching_service.py` wird zum Wrapper, alle Imports funktionieren
**Erwartetes Ergebnis:** System deployt ohne Fehler

**Datei:** `app/services/claude_matching_service.py`

**Aenderung:** Inhalt ersetzen durch:

```python
"""Claude Matching Service v4 — DEPRECATED. Nutze v5_matching_service.py."""
from app.services.v5_matching_service import (
    get_status,
    run_matching,
    request_stop,
    _extract_candidate_data,
    _extract_job_data,
)
```

**Warum nicht loeschen:** n8n Workflows oder andere Stellen koennten `claude_matching_service` importieren. Der Wrapper leitet alles an V5 weiter.

**Technische Abhaengigkeiten:**
- Alle Phasen 1-6 MUESSEN abgeschlossen sein
- Alle Imports aus `claude_matching_service` muessen in `v5_matching_service` existieren

**Vor dem Umbau PFLICHT:** `grep -r "claude_matching_service" app/` ausfuehren und ALLE Import-Stellen identifizieren. Jeden Import im Wrapper sicherstellen.

**Betroffene Komponenten:** `claude_matching_service.py`
**Risiko-Einschaetzung:** HOCH — Wenn ein Import fehlt, crashed der Server beim Start
**Test/Verifikation:**
- `python3 -c "from app.services.claude_matching_service import run_matching, get_status"`
- Server starten → kein ImportError
- `python3 -c "import ast; ast.parse(open('datei').read())"` fuer alle geaenderten .py Dateien

**Abschluss:**
- Syntax-Check aller geaenderten Dateien
- Commit + Push
- Railway Deployment pruefen

---

## 7. Technische Referenzen

### 7.1 API-Endpoints (V5 — nach Umbau)

| Methode | Pfad | Was | Status |
|---------|------|-----|--------|
| POST | `/api/v4/claude-match/run` | V5 Matching starten | GEAENDERT |
| POST | `/api/v4/claude-match/run-auto` | V5 Matching (n8n Cron) | GEAENDERT |
| GET | `/api/v4/claude-match/status` | Live-Fortschritt | GEAENDERT |
| POST | `/api/v4/claude-match/stop` | Matching stoppen | GEAENDERT |
| GET | `/api/v4/claude-match/daily` | Heutige Matches (Top + Follow-ups) | GEAENDERT |
| POST | `/api/v4/claude-match/{id}/action` | Vorstellen/Spaeter/Ablehnen | UNVERAENDERT |
| GET | `/api/v4/claude-match/{id}/contacts` | Kontakte laden | UNVERAENDERT |
| POST | `/api/v4/claude-match/{id}/prepare-email` | E-Mail vorbereiten | UNVERAENDERT |
| POST | `/api/v4/claude-match/{id}/send-email` | E-Mail senden | UNVERAENDERT |
| POST | `/api/v4/claude-match/ai-assessment` | Manuelle KI-Bewertung | **NEU** |
| POST | `/api/v4/claude-match/candidate/{id}` | Ad-hoc Matching | GEAENDERT |
| GET | `/api/v4/claude-match/regional-insights` | Stadt-Statistiken | UNVERAENDERT |
| POST | `/api/v4/claude-match/{id}/detailed-feedback` | Feedback | UNVERAENDERT |
| GET | `/api/v4/debug/match-count` | Statistiken | GEAENDERT |
| GET | `/api/v4/debug/job-health` | Job-Datenqualitaet | UNVERAENDERT |
| GET | `/api/v4/debug/candidate-health` | Kandidaten-Datenqualitaet | UNVERAENDERT |
| GET | `/api/v4/debug/match/{id}` | Match-Detail | UNVERAENDERT |
| GET | `/api/v4/debug/cost-report` | Kosten-Uebersicht | GEAENDERT |

### 7.2 Entfernte Endpoints

| Methode | Pfad | Grund |
|---------|------|-------|
| POST | `/claude-match/run-stufe-0` | Kein Stufen-System |
| POST | `/claude-match/run-stufe-1` | Kein Stufen-System |
| POST | `/claude-match/run-stufe-2` | Kein Stufen-System |
| POST | `/claude-match/exclude-pairs` | Kein Session-System |
| GET | `/claude-match/session/{id}` | Kein Session-System |
| GET | `/claude-match/sessions` | Kein Session-System |

### 7.3 Status-Endpoint (V5)

**`GET /api/v4/claude-match/status`**

Erwartete Response-Struktur:

```json
{
  "running": true,
  "progress": {
    "phase": "geo_filter",
    "geo_pairs_found": 342,
    "role_matches": 87,
    "drive_time_done": 45,
    "drive_time_total": 87,
    "matches_saved": 45,
    "telegram_sent": 12,
    "errors": 0
  },
  "last_run": "2026-02-22T14:30:00Z",
  "last_run_result": {
    "geo_pairs": 342,
    "role_matches": 87,
    "drive_time_calculated": 87,
    "matches_saved": 87,
    "telegram_notifications": 12,
    "duration_seconds": 45
  }
}
```

### 7.4 Datenbank-Felder auf Match

| Feld | Typ | V5 Nutzung |
|------|-----|-----------|
| `matching_method` | String(50) | `"v5_role_geo"` (neu) |
| `status` | Enum | `NEW` (initial), `AI_CHECKED` (nach optionaler KI) |
| `distance_km` | Float | PostGIS Distanz in km |
| `drive_time_car_min` | Integer | Google Maps Auto-Fahrzeit |
| `drive_time_transit_min` | Integer | Google Maps OEPNV-Fahrzeit |
| `v2_score` | Float | NULL (initial), 0-100 (nach KI) |
| `ai_score` | Float | NULL (initial), 0-1 (nach KI) |
| `ai_explanation` | Text | NULL (initial), Zusammenfassung (nach KI) |
| `ai_strengths` | ARRAY(String) | NULL (initial), Staerken (nach KI) |
| `ai_weaknesses` | ARRAY(String) | NULL (initial), Luecken (nach KI) |
| `ai_checked_at` | DateTime | NULL (initial), Zeitpunkt (nach KI) |
| `v2_score_breakdown` | JSONB | `{"scoring_version": "v5_role_geo", ...}` |
| `v2_matched_at` | DateTime | Zeitpunkt der Match-Erstellung |
| `user_feedback` | String(50) | Wie bisher: vorstellen/spaeter/ablehnen/job_an_kandidat/profil_an_kunden |
| `empfehlung` | String(20) | Wird NICHT mehr gesetzt (bleibt NULL) |
| `wow_faktor` | Boolean | Wird NICHT mehr gesetzt (bleibt false) |
| `wow_grund` | Text | Wird NICHT mehr gesetzt (bleibt NULL) |

### 7.5 Rollen-Felder

| Model | Feld | Typ | Beschreibung |
|-------|------|-----|-------------|
| Candidate | `hotlist_job_title` | String(255) | Primaere Rolle |
| Candidate | `hotlist_job_titles` | ARRAY(String) | Alle Rollen |
| Candidate | `classification_data` | JSONB | `{"roles": [...], "primary_role": "...", ...}` |
| Job | `hotlist_job_title` | String(255) | Primaere Rolle |
| Job | `hotlist_job_titles` | ARRAY(String) | Alle Rollen |
| Job | `classification_data` | JSONB | `{"roles": [...], "primary_role": "...", "quality_score": "high", ...}` |

**Fuer V5 Matching werden `hotlist_job_titles` (ARRAY) auf beiden Seiten verwendet.**

### 7.6 Schnittstellen zwischen Komponenten

```
CSV-Import (routes_jobs.py)
    ↓ Auto-Trigger
v5_matching_service.run_matching()
    ↓ Nutzt
    ├── PostGIS (ST_DWithin) → Geo-Filter
    ├── _roles_match() → Rollen-Filter
    ├── distance_matrix_service.batch_drive_times() → Fahrzeit
    ├── telegram_bot_service.send_message() → Benachrichtigung
    └── Match Model → DB-Speicherung
         ↓ Wird gelesen von
         ├── routes_claude_matching.py /daily → Action Board Backend
         ├── action_board.html → Action Board Frontend
         ├── match_center_service.py → Match Center
         └── outreach_service.py → E-Mail Versand

v5_matching_service.run_ai_assessment() [Optional, manuell]
    ↓ Nutzt
    ├── _extract_candidate_data() → Privacy-konforme Daten
    ├── _extract_job_data() → Job-Daten
    ├── Anthropic Claude API → KI-Bewertung
    ├── system_settings.ai_assessment_prompt → Konfigurierbarer Prompt
    └── Match Model → ai_score, ai_strengths, ai_weaknesses aktualisieren
```

---

## 8. Hinweis- und Risiko-Notizblock

### Kritische Hinweise

| # | Hinweis | Betroffene Phase |
|---|---------|-----------------|
| H1 | `async_session_maker` ist der korrekte Import — NICHT `async_session_factory` | Phase 1 |
| H2 | Alle Imports in Background-Tasks MUESSEN im try-Block stehen | Phase 1 |
| H3 | Railway killt DB-Connections nach 30s idle-in-transaction | Phase 1 |
| H4 | NIEMALS persoenliche Daten an Claude senden | Phase 1 (KI-Bewertung) |
| H5 | StFA Rollen-Name hat 2 Varianten (kurz + lang) | Phase 1 |
| H6 | `hotlist_job_titles` kann NULL sein — immer mit `or []` absichern | Phase 1 |
| H7 | UniqueConstraint `uq_match_job_candidate` — Duplikate verhindern | Phase 1 |
| H8 | Google Maps API-Key muss in Railway ENV gesetzt sein | Phase 1 |
| H9 | Telegram Bot Token + Chat ID muessen in Railway ENV gesetzt sein | Phase 1 |
| H10 | V4 Matches in DB bleiben bestehen — `_effective_score()` muss beide erkennen | Phase 5 |

### Kritische Schritte

#### K1: Phase 1 — Google Maps fuer ALLE Matches (Kosten-Risiko)

| Aspekt | Detail |
|--------|--------|
| **Betroffene Abhaengigkeiten** | `distance_matrix_service.py`, Google Maps API, Google Cloud Billing |
| **Moegliche Auswirkungen** | Hoehere Google Maps Kosten als V4 (V4 filterte vorher durch Claude) |
| **Risiko-Level** | MITTEL |
| **Betroffene Systemkomponenten** | V5 Matching Service, Google Maps API |
| **Empfohlene Absicherung** | PLZ-Cache ist aktiv (~70% Ersparnis). Kosten monitoren via `/debug/cost-report`. Rollen-Filter VORHER anwenden (reduziert Paare massiv). |
| **Recovery/Rollback** | Google Maps Kosten sind nicht rueckgaengig machbar, aber gedeckelt durch PLZ-Cache |

#### K2: Phase 3 — `/daily` Endpoint Aenderung

| Aspekt | Detail |
|--------|--------|
| **Betroffene Abhaengigkeiten** | `action_board.html` erwartet bestimmte Response-Struktur |
| **Moegliche Auswirkungen** | Action Board zeigt keine Matches wenn Response-Format nicht passt |
| **Risiko-Level** | MITTEL |
| **Betroffene Systemkomponenten** | Backend (`routes_claude_matching.py`), Frontend (`action_board.html`) |
| **Empfohlene Absicherung** | Phase 3 und Phase 6 zusammen deployen. Oder: leere Arrays temporaer mitliefern. |
| **Recovery/Rollback** | Git revert auf den Commit |

#### K3: Phase 6 — Action Board Frontend Umbau

| Aspekt | Detail |
|--------|--------|
| **Betroffene Abhaengigkeiten** | Alpine.js State, Backend-Endpoints, CSS |
| **Moegliche Auswirkungen** | JS-Fehler wenn Alpine State nicht mit Backend Response uebereinstimmt |
| **Risiko-Level** | MITTEL |
| **Betroffene Systemkomponenten** | `action_board.html` |
| **Empfohlene Absicherung** | Browser Console auf JS-Fehler pruefen. Alle Actions testen. |
| **Recovery/Rollback** | Git revert auf den Template-Commit |

#### K4: Phase 7 — V4 Service als Wrapper

| Aspekt | Detail |
|--------|--------|
| **Betroffene Abhaengigkeiten** | n8n Workflows, andere Services die `claude_matching_service` importieren |
| **Moegliche Auswirkungen** | Server crashed beim Start wenn ein Re-Export fehlt |
| **Risiko-Level** | HOCH |
| **Betroffene Systemkomponenten** | Alle Dateien die `claude_matching_service` importieren |
| **Empfohlene Absicherung** | VOR dem Umbau: `grep -r "claude_matching_service" app/` ausfuehren. |
| **Recovery/Rollback** | Git revert. V4 Service ist im Git-History. |

### Gefaehrliche Aenderungen

| # | Aenderung | Warum gefaehrlich | Absicherung |
|---|-----------|-------------------|-------------|
| G1 | `matching_method` Filter in `/daily` | Falsch → keine Matches im Dashboard | Beide Werte akzeptieren |
| G2 | Entfernen von Stufen-Endpoints | n8n Workflows koennten diese aufrufen | n8n pruefen |
| G3 | `empfehlung` Filter entfernen | V4 Matches erscheinen ploetzlich | `created_at >= today` bleibt |
| G4 | Google Maps fuer alle Matches | Kosten-Explosion bei vielen Matches | PLZ-Cache + Rollen-Filter |

---

## 9. Debug Raw API Response Dokumentation

> **HINWEIS:** Diese Sektion wird WAEHREND der Implementierung gefuellt.
> Fuer jeden relevanten Endpoint wird die tatsaechliche Response dokumentiert.

### 9.1 GET /api/v4/claude-match/daily (V5)

| Feld | Wert |
|------|------|
| **Endpoint** | `GET /api/v4/claude-match/daily` |
| **Query-Parameter** | `limit=20` |
| **Zeitpunkt** | _wird bei Implementierung eingetragen_ |
| **Status-Code** | _wird bei Implementierung eingetragen_ |

Erwartete Response:
```json
{
  "top_matches": [
    {
      "match_id": "uuid",
      "candidate_id": "uuid",
      "job_id": "uuid",
      "ai_score": null,
      "ai_checked_at": null,
      "distance_km": 12.5,
      "drive_time_car_min": 18,
      "drive_time_transit_min": 35,
      "matching_method": "v5_role_geo",
      "created_at": "2026-02-22T14:30:00Z",
      "candidate_city": "Hamburg",
      "candidate_position": "Finanzbuchhalter",
      "candidate_role": "Finanzbuchhalter/in",
      "candidate_first_name": "Max",
      "candidate_last_name": "Mustermann",
      "job_position": "Finanzbuchhalter/in",
      "job_company": "Firma XYZ",
      "job_city": "Hamburg"
    }
  ],
  "follow_ups": [],
  "summary": {
    "total_top": 1,
    "total_followups": 0
  }
}
```

### 9.2 GET /api/v4/claude-match/status (V5)

| Feld | Wert |
|------|------|
| **Endpoint** | `GET /api/v4/claude-match/status` |
| **Zeitpunkt** | _wird bei Implementierung eingetragen_ |

Erwartete Response: Siehe Abschnitt 7.3

### 9.3 POST /api/v4/claude-match/ai-assessment

| Feld | Wert |
|------|------|
| **Endpoint** | `POST /api/v4/claude-match/ai-assessment` |
| **Payload** | `{"match_ids": ["uuid1", "uuid2"], "custom_prompt": null}` |
| **Zeitpunkt** | _wird bei Implementierung eingetragen_ |

Erwartete Response:
```json
{
  "status": "started",
  "count": 2,
  "message": "KI-Bewertung gestartet fuer 2 Matches"
}
```

### 9.4 POST /api/v4/claude-match/run

| Feld | Wert |
|------|------|
| **Endpoint** | `POST /api/v4/claude-match/run` |
| **Zeitpunkt** | _wird bei Implementierung eingetragen_ |

Erwartete Response:
```json
{
  "status": "started",
  "message": "V5 Matching gestartet"
}
```

### 9.5 Telegram Notification

| Feld | Wert |
|------|------|
| **Endpoint** | Telegram Bot API `/sendMessage` |
| **Payload** | `{"chat_id": "7103040196", "text": "...", "parse_mode": "HTML"}` |
| **Zeitpunkt** | _wird bei Implementierung eingetragen_ |

Erwartete Nachricht:
```html
<b>Neuer Match!</b>

Kandidat: Max Mustermann (Finanzbuchhalter/in)
Stelle: Finanzbuchhalter/in bei Firma XYZ, Hamburg
Distanz: 12.5 km
Auto: 18 Min | OEPNV: 25 Min
```

### 9.6 Google Maps Distance Matrix

| Feld | Wert |
|------|------|
| **Endpoint** | `https://maps.googleapis.com/maps/api/distancematrix/json` |
| **Zeitpunkt** | _wird bei Implementierung eingetragen_ |

Erwartete Response:
```json
{
  "rows": [{
    "elements": [{
      "status": "OK",
      "duration": {"value": 1080, "text": "18 mins"},
      "distance": {"value": 12500, "text": "12.5 km"}
    }]
  }]
}
```

---

## 10. Aenderungslog

| Datum | Was geaendert | Warum | Betroffene Komponenten | Abhaengigkeits-Aenderungen | Risiken | Tests anpassen? |
|-------|---------------|-------|----------------------|---------------------------|---------|----------------|
| 22.02.2026 | Initiale Erstellung | V5 Plan angelegt | Alle | Keine (neu) | Keine | Nein |
| 22.02.2026 | Phase 1 abgeschlossen | `v5_matching_service.py` erstellt (853 Zeilen) | Neue Datei | Keine bestehende Datei geaendert | NIEDRIG | Nein |
| 22.02.2026 | Phase 2 abgeschlossen | Import in `routes_jobs.py:168` von `claude_matching_service` auf `v5_matching_service` umgeleitet | `routes_jobs.py` | Auto-Trigger nutzt jetzt V5 | NIEDRIG | Nein |
| 22.02.2026 | Phase 3 abgeschlossen | Stufen-Endpoints entfernt, `/daily` auf V5 umgestellt, `/ai-assessment` NEU, Imports umgeleitet | `routes_claude_matching.py` | Alle Endpoints nutzen V5 Service | MITTEL | Nein |
| 22.02.2026 | Phase 4 abgeschlossen | Migration 025 (ai_assessment_prompt), Helper `get_ai_assessment_prompt()`, value-Spalte auf 5000 Zeichen | `routes_settings.py`, Migration 025 | Prompt ueber `/einstellungen` editierbar | NIEDRIG | Nein |
| 22.02.2026 | Phase 5 abgeschlossen | `_effective_score()` Case fuer `v5_role_geo` hinzugefuegt | `match_center_service.py` | V5 Matches im Match Center sichtbar (Score 0 ohne KI) | NIEDRIG | Nein |
| 22.02.2026 | Phase 6 abgeschlossen | Action Board komplett umgeschrieben: Stufen-UI entfernt, WOW/Proximity entfernt, Summary auf 2 Cards, V5 Progress-Bar, KI-Bewertung Button pro Karte + Bulk, Matched Roles anzeige, Kandidaten-Name prominent | `action_board.html` | Frontend zeigt V5 Matches korrekt, KI-Bewertung triggerbar | MITTEL | Nein |
| 22.02.2026 | Phase 7 abgeschlossen | `claude_matching_service.py` als Wrapper (re-exportiert von V5), Syntax-Check aller 7 Dateien bestanden | `claude_matching_service.py` | n8n/externe Imports weiterhin funktionsfaehig | NIEDRIG | Nein |

---

## Arbeitsregeln (Zusammenfassung)

1. **Dokumentation hat Vorrang.** Bei jeder Plankorrektur oder Problemloesung: ZUERST diese Datei aktualisieren, DANN weiterarbeiten.
2. **Phasen-Dekomposition = Planaenderung.** Wenn eine Phase aufgeteilt wird: Im Aenderungslog dokumentieren BEVOR die Implementierung beginnt.
3. **Konsistenzpruefung bei Planaenderung.** Gesamten Plan lesen, Risiko-Block pruefen, Abhaengigkeiten pruefen.
4. **Langlaufende Problemloesung.** Bei iterativer Fehlersuche: Plan + Risiko-Block erneut lesen BEVOR naechster Schritt beginnt.
5. **Debug Response Dokumentation.** Bei jeder relevanten API-Interaktion: Tatsaechliche Response in Abschnitt 9 dokumentieren.
