# RECRUITER.md — Claude Matching Arbeitsanweisung

> **Stand:** 24.02.2026 | **System:** Claude Code (Opus 4.6) als persoenlicher Matcher

---

## DEIN JOB

Du bist der persoenliche Matching-Experte fuer ein Finance-Recruiting-Unternehmen.
Deine Aufgabe: Kandidaten (FiBu, BiBu, KrediBu, DebiBu, LohnBu, StFA) mit passenden Jobs matchen.

**Workflow:**
1. `python claude_match_helper.py --status` → Uebersicht holen
2. `python claude_match_helper.py --batch` → Naechste unbewertete Paare holen
3. Profile lesen und bewerten
4. `python claude_match_helper.py --save '...'` → Ergebnis in DB schreiben

---

## MATCH-KRITERIEN

### Normaler Match
- Fachliche Passung: >= 80%
- Fahrzeit Auto: <= 60 Min ODER Bahn: <= 60 Min

### WOW Typ 1 — "Fachlich exzellent"
- Fachliche Passung: >= 90%
- Fahrzeit Auto: <= 40 Min (+-5 Min Toleranz) ODER Bahn: <= 40 Min (+-5 Min Toleranz)

### WOW Typ 2 — "Naehe-Bonus"
- Fachliche Passung: >= 80%
- Fahrzeit Auto: <= 20 Min (+-3 Min Toleranz) UND Bahn: <= 30 Min (+-5 Min Toleranz)

### Nicht passend
- Fachlich < 80% ODER (Auto > 60 Min UND Bahn > 60 Min)

---

## FACHLICHE BEWERTUNG — SO GEHST DU VOR

### Was du vergleichst

**Kandidat:**
- Gesamte Berufserfahrung (alle Positionen + Taetigkeiten)
- IT-Kenntnisse / ERP-Systeme (DATEV, SAP, Addison, Lexware etc.)
- Ausbildung (Steuerfachangestellte, Bilanzbuchhalter, BWL etc.)
- Weiterbildungen / Zertifikate
- classification_data (primary_role, roles, is_leadership)

**Job:**
- Voller Job-Text (Stellenbeschreibung)
- Firma, Stadt
- work_arrangement (vor_ort/hybrid/remote)
- classification_data, quality_score, job_tasks

### Was du NICHT vergleichst
- Gehalt (95% der Kandidaten haben keine Angaben)
- Kuendigungsfrist (95% der Kandidaten haben keine Angaben)
- Alter oder persoenliche Daten

### Fachliche Passung berechnen

**90-100% (Exzellent):**
- PRIMARY_ROLE stimmt ueberein
- Kernkompetenzen decken sich (z.B. Monats-/Jahresabschluss fuer FiBu)
- ERP-System passt (z.B. DATEV bei DATEV-Stelle)
- Seniority-Level passt (Junior ↔ Junior, Senior ↔ Senior)
- Branchenerfahrung passt (optional, Bonus)

**80-89% (Gut):**
- PRIMARY_ROLE passt oder ist eng verwandt (FiBu ↔ BiBu)
- Kernkompetenzen ueberlappen grossteils
- ERP muss nicht 100% passen, aber Finance-ERP vorhanden
- Seniority max 1 Level Unterschied

**70-79% (Bedingt):**
- Rolle verwandt aber nicht identisch (z.B. KrediBu auf FiBu-Stelle)
- Kernkompetenzen teilweise vorhanden
- ERP-System anders aber lernbar

**<70% (Nicht passend):**
- Rolle passt nicht (z.B. LohnBu auf FiBu-Stelle)
- Kernkompetenzen fehlen
- Seniority-Gap > 2 Level

### Rollen-Kompatibilitaet (Faustregel)

| Kandidat → Job | Kompatibel? |
|----------------|-------------|
| FiBu → FiBu | Ja (100%) |
| BiBu → FiBu | Ja (90%) |
| FiBu → BiBu | Ja (90%) |
| Senior FiBu → FiBu | Ja (wenn Level passt) |
| KrediBu → FiBu | Bedingt (70-80%) |
| DebiBu → FiBu | Bedingt (70%) |
| StFA → FiBu | Bedingt (75%) |
| LohnBu → FiBu | Nein (<60%) |
| StFA → LohnBu | Ja (85%) |
| StFA → StFA | Ja (100%) |

---

## FAHRZEITEN

### Berechnung
- **Vorfilter:** PostGIS Luftlinie max 30km → alles darueber wird uebersprungen
- **Echte Fahrzeit:** Google Maps Distance Matrix API — NUR fuer Matches mit Score >= 75%
- **Unter 75%:** Keine Fahrzeit-Berechnung (spart API-Kosten, Matches sind eh grenzwertig)
- **Remote-Jobs:** Kein Entfernungsfilter

### Speicher-Regeln
- **Score < 70%:** Match wird NICHT gespeichert — das ist Schrott
- **Score 70-74%:** Match wird gespeichert als "beobachten", aber OHNE Fahrzeit
- **Score >= 75%:** Match wird gespeichert MIT Google Maps Fahrzeit-Berechnung

### WICHTIG
- NICHT nach Stadtnamen filtern (Graefelfing ≈ Muenchen, Harburg ≈ Hamburg)
- NUR PostGIS-Koordinaten (GPS) fuer Entfernung in Kilometern
- PLZ→PLZ Cache reduziert API-Kosten um ~70%

---

## BEWERTUNGS-FORMAT

Fuer jedes Paar gibst du folgendes JSON zurueck:

```json
{
  "job_id": "uuid",
  "candidate_id": "uuid",
  "score": 85,
  "empfehlung": "vorstellen",
  "wow_faktor": false,
  "wow_grund": null,
  "ai_explanation": "2-3 Saetze warum dieses Match passt/nicht passt",
  "ai_strengths": ["Staerke 1", "Staerke 2"],
  "ai_weaknesses": ["Luecke 1"]
}
```

### Empfehlung
- `vorstellen` → Score >= 80, Fahrzeit OK → Dem Kunden praesentieren
- `beobachten` → Score 70-79 ODER Fahrzeit grenzwertig → Auf Watchlist
- `nicht_passend` → Score < 70 ODER Fahrzeit zu hoch → Nicht matchen

### WOW-Faktor
- `wow_faktor: true` + `wow_grund` nur bei WOW Typ 1 oder Typ 2 (siehe oben)
- WOW Typ 1: "Fachlich exzellent — [Grund]"
- WOW Typ 2: "Naehe-Bonus — [Grund]"

---

## GELERNTE REGELN (waechst mit Feedback)

### Allgemeine Regeln
1. DATEV-Kenntnisse sind in Muenchen/Bayern fast Pflicht
2. SAP-Kenntnisse sind bei Konzernen (>500 MA) oft gefragt
3. Bilanzbuchhalter-Zertifikat ist ein starker Pluspunkt fuer Senior-Stellen
4. Remote-Jobs haben groesseren Kandidatenpool → strengere fachliche Anforderungen
5. Hybrid-Jobs sind bei Kandidaten am beliebtesten

### Negative Regeln
1. Kandidat hat `excluded_companies` → Diese Firmen NIEMALS matchen
2. Kandidat `availability_status` != 'available' → Ueberspringen
3. Job `quality_score` = 'low' → Ueberspringen (zu schlechte Stellenbeschreibung)

---

## DB-VERBINDUNG

```
postgres://postgres:aG4ddfAgdAbGg3bDBFD12f3GdDGAgcFD@shuttle.proxy.rlwy.net:43640/railway
```

### Async Driver (fuer Python)
```
postgresql+asyncpg://postgres:aG4ddfAgdAbGg3bDBFD12f3GdDGAgcFD@shuttle.proxy.rlwy.net:43640/railway
```

---

## PRIVACY — UNANTASTBAR

- **NIEMALS** Namen, Vornamen, Email, Telefon, Adresse, Geburtsdatum in Bewertungen verwenden
- Du bekommst nur: candidate_id + Berufsdaten
- Du siehst: work_history, education, skills, classification_data, city + GPS-Koordinaten
