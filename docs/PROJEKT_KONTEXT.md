# Projekt-Kontext: Matching-Tool für Recruiter

**Erstellt:** 26. Januar 2026
**Status:** Planung abgeschlossen, Implementierung steht bevor

---

## 1. Was ist das Projekt?

Ein automatisiertes Matching-System für Recruiter, das:
- Kandidaten aus Recruit CRM synchronisiert
- Stellenausschreibungen per CSV importiert
- Automatisch passende Kandidaten zu Jobs findet (Distanz ≤25km + fachliche Passung)
- KI-gestützte Bewertung auf Knopfdruck ermöglicht

**Ziel:** Der Recruiter soll nicht mehr manuell vergleichen, sondern nur noch entscheiden.

---

## 2. Technologie-Stack

| Komponente | Technologie |
|------------|-------------|
| Backend | Python 3.11 + FastAPI |
| Datenbank | PostgreSQL 15 + PostGIS |
| Frontend | Jinja2 + HTMX + Tailwind CSS |
| KI | OpenAI API (gpt-4o-mini) |
| Deployment | Railway |

---

## 3. Spezifikations-Dokumente

Alle Spezifikationen liegen im Downloads-Ordner:

| Datei | Inhalt |
|-------|--------|
| `matching_tool_v2_2_final.md` | Hauptspezifikation (Datenbank, API, UI, Services) |
| `8585858585_v2_1_ergaenzungen.md` | CRUD, Statistiken, Alerts |
| `v2_2_ergaenzung_admin_trigger.md` | Background-Jobs mit Cron + manuellen Buttons |
| `implementierungsplan_matching_tool.md` | Detaillierter Implementierungsplan (9 Phasen) |

---

## 4. Kernfunktionen

### 4.1 Datenquellen
- **Kandidaten:** Recruit CRM API (5.000 bestehend, 10-15 neue/Tag)
- **Jobs:** CSV-Import (Tab-getrennt, ~30.000/Woche)

### 4.2 Matching-Logik
- **Hartes Kriterium:** Distanz ≤25km (Haustür-zu-Haustür)
- **Vorfilter:** Keyword-Matching (Skills im Job-Text)
- **KI-Bewertung:** On-Demand, nur wenn User Kandidaten auswählt

### 4.3 Kandidaten-Daten (aus CV extrahiert)
- Vorname, Nachname
- Aktuelle Position
- Alter (aus Geburtsdatum berechnet)
- IT-Kenntnisse (SAP, DATEV, etc.)
- Beruflicher Werdegang (alle Stationen)
- Ausbildung/Abschlüsse
- Wohnadresse (vollständig)

### 4.4 Branchen
- Buchhaltung
- Technische Berufe (Elektriker, Elektrotechnik, Anlagenmechaniker)

---

## 5. Wichtige Entscheidungen

| Entscheidung | Begründung |
|--------------|------------|
| **Kein Redis** | Einzelnutzer, nicht nötig |
| **Kein React/Vue** | HTMX reicht, weniger Komplexität |
| **On-Demand KI** | Batch wäre zu teuer (~$700 vs ~$5/Monat) |
| **Polling statt Webhooks** | Einfacher zu debuggen |
| **PostgreSQL + PostGIS** | Geo-Queries für Distanzberechnung |
| **OpenStreetMap/Nominatim** | Kostenlos für Geokodierung |

---

## 6. Background-Jobs

| Job | Cron (automatisch) | Manuell |
|-----|-------------------|---------|
| Geocoding | 03:00 Uhr täglich | Button in Einstellungen |
| CRM-Sync | 03:30 Uhr täglich | Button in Einstellungen |
| Matching (Vorfilter) | 04:00 Uhr täglich | Button in Einstellungen |
| Cleanup | 05:00 Uhr sonntags | Button in Einstellungen |

---

## 7. Was wurde bisher gemacht?

### Erledigt (Planung)
- [x] Anforderungen gesammelt und dokumentiert
- [x] Recruit CRM API recherchiert
- [x] CSV-Format analysiert
- [x] Technologie-Stack festgelegt
- [x] Datenbank-Schema entworfen
- [x] API-Endpoints spezifiziert
- [x] UI-Mockups erstellt
- [x] Error-Handling definiert
- [x] Implementierungsplan erstellt

### Implementierung
- [x] Phase 1: Projekt-Setup & Datenbank ✅ (26. Jan 2026)
- [ ] Phase 2: CSV-Import & Geocoding
- [ ] Phase 3: CRM-Sync & CV-Parsing
- [ ] Phase 4: Matching-Logik
- [ ] Phase 5: API-Endpoints
- [ ] Phase 6: Frontend
- [ ] Phase 7: KI-Integration
- [ ] Phase 8: Statistiken & Alerts
- [ ] Phase 9: Testing & Deployment

---

## 8. Projektstruktur (geplant)

```
matching-tool/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── models/
│   ├── schemas/
│   ├── services/
│   ├── api/
│   └── templates/
├── migrations/
├── tests/
├── docs/
│   └── PROJEKT_KONTEXT.md  ← Diese Datei
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── README.md
```

---

## 9. Credentials (benötigt)

Diese müssen in `.env` eingetragen werden:

```
DATABASE_URL=postgresql+asyncpg://...
OPENAI_API_KEY=sk-...
RECRUIT_CRM_API_KEY=...
RECRUIT_CRM_BASE_URL=https://api.recruitcrm.io/v1/
SECRET_KEY=<random-32-chars>
CRON_SECRET=<random-string>
ENVIRONMENT=development
```

**Hinweis:** API-Keys werden NICHT im Chat geteilt, sondern direkt in die .env eingetragen.

---

## 10. Nächster Schritt

**Phase 2: CSV-Import & Geocoding**

Neuen Chat starten mit:
```
cd "/Users/miladhamdard/Desktop/Claudi Time/matching-tool"
claude
```

Dann eingeben:
```
Wir bauen das Matching-Tool für Recruiter.
Phase 1 ist fertig. Starte mit Phase 2: CSV-Import & Geocoding.

Lies zuerst diese Dateien:
1. /Users/miladhamdard/Desktop/Claudi Time/matching-tool/docs/PROJEKT_KONTEXT.md
2. /Users/miladhamdard/Downloads/implementierungsplan_matching_tool.md

Dann schau dir die existierende Projektstruktur an.
```

---

## 11. Regeln für die Implementierung

### MUSS beachten
1. Jeder API-Endpoint MUSS Error-Handling haben
2. Jede Benutzereingabe MUSS validiert werden
3. Jede Liste MUSS Pagination haben
4. OpenAI wird NUR aufgerufen wenn User klickt
5. Distanz-Filter ist IMMER ≤25km
6. Hamburg und München sind IMMER oben (Prio-Städte)
7. Alle Texte auf DEUTSCH

### NICHT machen
1. KEIN Redis
2. KEIN React/Vue
3. KEINE automatischen OpenAI-Calls
4. KEINE unbegrenzten Listen

---

## 12. Git-Workflow

### Nach jeder größeren Änderung:
```
Bitte mache einen Git-Commit mit aussagekräftiger Message
```

### Commit-Message Format:
```
<typ>: <kurze Beschreibung>

<optionale längere Beschreibung>
```

### Typen:
- `feat:` Neue Funktion
- `fix:` Bugfix
- `docs:` Dokumentation
- `refactor:` Code-Umstrukturierung
- `test:` Tests
- `chore:` Wartung (Dependencies, Config)

### Beispiele:
```
feat: CSV-Import Service implementiert
fix: Geocoding Timeout erhöht auf 10 Sekunden
docs: API-Dokumentation aktualisiert
chore: Dependencies in pyproject.toml hinzugefügt
```

---

## 13. Kontakt-Info

- **CRM:** Recruit CRM
- **Deployment:** Railway (Account vorhanden)
- **Nutzer:** Einzelnutzer (Recruiter)
