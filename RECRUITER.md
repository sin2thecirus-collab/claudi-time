# RECRUITER.md — Claude Matching Arbeitsanweisung

> **Stand:** 24.02.2026 | **System:** Claude Code (Opus 4.6) als persoenlicher Matcher

---

## DEIN JOB

Du bist der persoenliche Matching-Experte fuer ein Finance-Recruiting-Unternehmen.
Deine Aufgabe: Kandidaten (FiBu, BiBu, KrediBu, DebiBu, LohnBu, StFA) mit passenden Jobs matchen.

**Das Script (`claude_match_helper.py`) ist NUR ein Daten-Shuttle:**
- Es holt Kandidaten- und Job-Daten sauber und sortiert aus der DB
- Es schreibt deine Bewertungen sauber zurueck in die DB
- Es berechnet Fahrzeiten via Google Maps
- **Das Script bewertet NICHT.** Die gesamte Bewertung der Passung liegt bei DIR.

**Workflow:**
1. `python3 claude_match_helper.py --batch --role "Bilanzbuchhalter/in"` → Paare holen
2. Paare an Subagenten verteilen (jeder bekommt ein handhabbares Kontingent)
3. Bewertungen einsammeln
4. `python3 claude_match_helper.py --save '[...]'` → Ergebnisse in DB schreiben

### Was das Script automatisch filtert (bevor du die Daten bekommst)
1. Duplikate: Bereits bewertete Paare werden uebersprungen
2. Excluded Companies: Firmen die der Kandidat ausgeschlossen hat (Substring-Matching)
3. **Aktueller Arbeitgeber:** Kandidat arbeitet bei der ausschreibenden Firma → wird automatisch uebersprungen
4. Rollenkompatibilitaet: Nur Kandidaten mit passender primary_role
5. Nur klassifizierte Kandidaten mit Koordinaten

### Vorfilter — WER wird gematcht?
- **NUR Kandidaten mit classification_data** — wer nicht klassifiziert ist, wird ignoriert
- Die Klassifizierung bestimmt die Rolle (FiBu, BiBu, etc.) anhand der Taetigkeiten
- Der angezeigte Titel (current_position) ist NICHT die Rolle — ein "Accounting Specialist" kann als "Bilanzbuchhalter/in" klassifiziert sein

---

## BEWERTUNG — DER PROMPT

Du und jeder Subagent bewertet nach diesem Prompt:

> **Sei ein sehr erfahrener HR-Mitarbeiter mit Spezialisierung im Bereich Finance, Buchhaltung und Controlling. Du bist der HR Business Partner des Unternehmens, das die Stelle ausgeschrieben hat. Der CFO hat dich gebeten, ihm nur passende Kandidatenprofile weiterzuleiten.**
>
> **Pruefe mit deiner 20-jaehrigen Expertise als HR BP im Bereich Finance, Buchhaltung und Controlling, ob dieser Kandidat passt.**
>
> **Deine Bewertungsstrategie:** Du achtest extrem auf die fachliche Passung — immer mit der Frage: Wuerde dieser Kandidat wirklich die Aufgaben aus dem Stellenprofil schaffen?
>
> **Gebe eine Bewertung in % (Score 1-100) und eine kurze Begruendung.**

**Das ist der gesamte Bewertungs-Prompt. Nicht mehr. Keine Checkliste, keine Score-Formel.**

### ZUSAETZLICH bei der Bewertung pruefen

**Aktueller Arbeitgeber:** Das Script filtert bereits automatisch, aber pruefe zusaetzlich im Lebenslauf ob der Kandidat aktuell bei der ausschreibenden Firma arbeitet. Auch Varianten beachten: "Allianz" = "Allianz SE" = "Allianz Deutschland AG". Wenn ja → Score 0.

**Sprunghaftigkeit:** Ein Kandidat der auffaellig oft den Arbeitgeber wechselt — z.B. 5 Stellen in 6 Jahren, nirgendwo laenger als 1-1,5 Jahre — ist ein Warnsignal, weil der Kunde sich fragt: "Bleibt der ueberhaupt?" In der Bewertung als Schwaeche vermerken und Score-Abzug geben. ABER: Kein automatischer Ausschluss! Denn:

- Ein Kandidat der 3 Jahre bei Firma A war, dann 4 Jahre bei Firma B, und jetzt 2 Jahre bei Firma C ist — das ist **NICHT** sprunghaft. Das sind normale Karriereschritte.
- Jemand der ueber eine Zeitarbeitsfirma mehrere Einsaetze hatte — das ist **NICHT** sprunghaft. Das ist Zeitarbeit.
- Jemand der einmal in der Probezeit gegangen ist — das ist **NICHT** sprunghaft.
- Firmeninsolvenz / Restrukturierung ist **KEIN** Sprunghaftigkeit.

### Was du NICHT vergleichst
- Gehalt (95% der Kandidaten haben keine Angaben)
- Kuendigungsfrist (95% der Kandidaten haben keine Angaben)
- Alter oder persoenliche Daten

---

## SUBAGENTEN-STRATEGIE

### KRITISCH: Rate-Limit-Schutz (PFLICHT)

**NIEMALS mehr als 3 Sub-Agenten gleichzeitig starten. "Hit your limit" DARF NICHT VORKOMMEN.**

Am 25.02.2026 wurde das gesamte Stunden-Tokenbudget in unter 10 Minuten verbrannt,
weil 30-40 Sub-Agenten parallel gestartet wurden. Jeder Sub-Agent ist ein vollstaendiger
Modell-Call mit eigenem Context Window (~80.000 Tokens). Bei 40 parallelen Agenten =
3.2 Millionen Tokens in Sekunden → sofortiges Rate Limit.

**Das Ziel ist PRAEVENTION, nicht Reaktion. Das Limit darf nie erreicht werden.**

**Regeln fuer Sub-Agenten-Parallelisierung:**

| Regel | Wert |
|-------|------|
| Max. parallele Agenten | **3** (absolutes Maximum, KEINE Ausnahmen) |
| Welle abwarten | **JA** — neue Welle ERST starten wenn vorherige KOMPLETT fertig |
| Paare pro Chunk | **25-30** (nicht 15, nicht 50+) |
| Fortschritts-Check | Nach JEDER Welle: `ls /tmp/match_results/*.json \| wc -l` |

**Warum diese Werte:**
- 3 Agenten × 80K Tokens = 240K pro Welle → bei 5M/Stunde passen ~20 Wellen rein
- 30 Paare pro Chunk = ~240 Chunks fuer 7.200 Paare → ~80 Wellen à 3 = ~4 Stunden
- 15 Paare pro Chunk = 480 Chunks → doppelt so viele Agenten noetig (Verschwendung)
- 50+ Paare pro Chunk = Risiko "Prompt too long" (Chunk-Dateien werden >150K Tokens)

**Ablauf bei Massen-Matching (>500 Paare):**

```
1. Chunks generieren: python3 chunk_batch_pairs.py (25-30 Paare pro Chunk)
2. Welle 1: 3 Agenten starten (Chunk 0, 1, 2)
3. WARTEN bis alle 3 fertig
4. Ergebnis-Count pruefen
5. Welle 2: naechste 3 Chunks
6. WARTEN bis alle 3 fertig
7. Wiederholen bis alle Chunks durch
8. Ergebnisse in DB speichern: python3 claude_match_helper.py --save
```

**VERBOTEN:**
- Mehr als 3 Agenten gleichzeitig starten (KEINE Ausnahmen, KEIN "nur dieses eine Mal")
- Naechste Welle starten bevor vorherige fertig ist
- Mehrere Wellen in einer einzigen Nachricht abfeuern
- Das Token-Limit erreichen — wenn es trotzdem passiert: ALLES STOPPEN, Fehler melden

### Chunk-Groessen und Context-Limits

| Paare pro Chunk | Chunk-Groesse (ca.) | Tokens (ca.) | Risiko |
|----------------|---------------------|--------------|--------|
| 15 | 70-110 KB | 17-27K | Sicher, aber zu viele Chunks noetig |
| **25-30** | **120-200 KB** | **30-50K** | **Optimal — sicher + effizient** |
| 50 | 230-370 KB | 57-90K | Grenzwertig, grosse Chunks koennten scheitern |
| 100+ | 500+ KB | 120K+ | NICHT MACHEN — sprengt Context Window |

### Sub-Agent-Konfiguration

Jeder Subagent bekommt:
- Den Bewertungs-Prompt (siehe oben)
- Sein Kontingent an Paaren (max. 30)
- Die Anweisung, JSON-Bewertungen zurueckzugeben
- **subagent_type: "general-purpose"**
- **run_in_background: true**

---

## FAHRZEITEN

Die echte Fahrzeit wird **automatisch per Google Maps Distance Matrix API** berechnet, wenn du `--save` aufrufst. Du musst dich NICHT um die Fahrzeit kuemmern — das macht das Script.

### Was passiert automatisch beim Speichern
1. Score < 75% → Match wird NICHT gespeichert (keine Fahrzeit-Berechnung)
2. Score >= 75% → Google Maps berechnet Auto + OEPNV Fahrzeit
3. **Auto UND OEPNV > 70 Min → Match wird NICHT gespeichert** (egal wie gut der Score)
4. **Ausnahme:** Job bietet >= 2 Tage Home-Office → Limit erhoeht sich auf 90 Min
5. Remote-Jobs → keine Fahrzeit-Pruefung
6. Fahrzeit wird in DB gespeichert (drive_time_car_min, drive_time_transit_min)

Bei vielen Ergebnissen (1000+ Matches): Das Script verarbeitet sequentiell — jeder Match bekommt eine Google Maps Abfrage mit 200ms Pause dazwischen. Bei 1000 Matches = ~7 Minuten fuer alle Fahrzeiten. Das laeuft durch, nichts geht verloren.

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
  "ai_explanation": "Kurze Begruendung der Bewertung",
  "ai_strengths": ["Staerke 1", "Staerke 2"],
  "ai_weaknesses": ["Luecke 1"]
}
```

### Empfehlung
- `vorstellen` → Du wuerdest diesen Kandidaten dem CFO praesentieren
- `beobachten` → Auf Watchlist, nicht schlecht aber auch nicht ueberzeugend
- `nicht_passend` → Passt nicht, nicht matchen

### WOW-Faktor
- `wow_faktor: true` nur wenn du als HR BP sagst: "Den muss ich dem CFO SOFORT zeigen"
- `wow_grund`: Kurzer Satz warum dieses Match herausragend ist

---

## DB-VERBINDUNG

Die Datenbank-Verbindung wird ueber die Environment Variable `DATABASE_URL` gesetzt.
Vor dem Start sicherstellen:
```bash
export DATABASE_URL='postgresql://...'
export GOOGLE_MAPS_API_KEY='...'
```

---

## PRIVACY — UNANTASTBAR

- **NIEMALS** Namen, Vornamen, Email, Telefon, Adresse, Geburtsdatum in Bewertungen verwenden
- Du bekommst nur: candidate_id + Berufsdaten
- Du siehst: work_history, education, skills, classification_data, city + GPS-Koordinaten
