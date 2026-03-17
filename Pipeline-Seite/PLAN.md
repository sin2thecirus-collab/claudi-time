# Pipeline-Seite — Redesign Plan

## Stand: 17.03.2026

---

## PHASE 1: Analyse + Design (FERTIG)
- [x] Backend-Analyse (Models, Services, Routes, Abhaengigkeiten)
- [x] Frontend-Analyse (Templates, Alpine.js, SortableJS)
- [x] Live-Seite inspiziert
- [x] Probleme identifiziert
- [x] HTML-Preview erstellt (Pipeline-Seite/preview.html)
- [x] Milad-Feedback eingeholt (3 Iterationen: Sidebar, Action-Buttons)
- [x] Design finalisiert (17.03.2026 — von Milad abgenommen)

## PHASE 2: Backend-Erweiterungen (FERTIG — 17.03.2026)
- [x] Match-Daten laden (Score, Fahrzeit, WOW, Empfehlung) in ats_main() — direkt in bestehenden Endpoint integriert
- [x] Neuer Endpoint: GET /ats/pipeline/search-candidates (Kandidaten-Suche fuer Quick-Add)
- [x] Erweiterte Card-Daten: company, job_title, job_id, location, score, drive_time_car, wow, empfehlung, matching_method
- [ ] Neuer Endpoint: GET /ats/pipeline/candidate/{id}/all-entries (alle Pipeline-Eintraege eines Kandidaten) — optional
- [ ] Activity-Feed Endpoint (letzte N Aktivitaeten ueber alle Jobs) — optional

## PHASE 3: Frontend-Redesign (FERTIG — 17.03.2026)
- [x] Neues Template: Pipeline Command Center (1005 Zeilen, ats_pipeline_overview.html komplett ersetzt)
- [x] Smart Sidebar: Job-Liste mit Suche, Attention-Badges, Kandidaten-Count
- [x] Haupt-Board: Reichere Kanban-Karten (Score-Ring, ERP/Gehalt/Fahrzeit Tags, Action-Popups)
- [x] Detail-Panel: Vollstaendige Kandidaten-Info (Slide-In rechts)
- [x] Multi-View: Board + Tabelle (Toggle)
- [x] Erweiterte Filter + Suche (Sidebar-Filter + Cross-Job-View)
- [x] Drag&Drop mit HTML5 API (PATCH /api/ats/pipeline/entries/{id}/stage)
- [x] KPI-Bar: In Pipeline, Platziert, Ø Tage, Conversion Rate
- [x] "Kandidat hinzufuegen" Modal mit Live-Suche
- [x] Kandidat + Kunde Action-Popup-Menues auf jeder Karte
- [x] Dark Theme mit Glass Morphism, Glow Effects, Gradient Avatars

## PHASE 4: Integration + Test (geplant)
- [ ] Kandidaten-Detailseite: Pipeline-Eintraege anzeigen
- [ ] Match Center → Pipeline Sync verbessern
- [ ] n8n Webhook Kompatibilitaet sicherstellen
- [ ] E2E Tests

---

## Design-Konzept: "Pipeline Command Center"

### Layout (3-Spalten):
```
+------------------+----------------------------------------+------------------+
| SIDEBAR          | MAIN BOARD                             | DETAIL PANEL     |
| Company > Job    | Kanban Columns                         | (on click)       |
| Navigator        | + KPI Bar                              | Kandidat-Profil  |
| + Filter         | + Filter Bar                           | + Activity       |
| + Quick Stats    | + Multi-View Toggle                    | + Quick Actions  |
+------------------+----------------------------------------+------------------+
```

### Sidebar Features:
- Baumstruktur: Firma → Jobs (mit Kandidaten-Count)
- "Alle Prozesse" View (Cross-Job)
- Suchfeld
- Quick-Filter (nur aktive, nur mit Kandidaten)
- Mini-Stats pro Firma

### Board Features:
- Reichere Karten: Avatar, Name, Rolle, Firma, Job, Score, Tage, Fahrzeit, naechste Aktion
- Farbcodierung nach Phase
- Quick-Action Buttons auf Hover
- Drag&Drop mit Animations
- Confirmation-Dialog bei kritischen Moves (Platziert, Abgesagt)

### Detail Panel Features:
- Vollstaendiges Kandidaten-Profil
- Alle Pipeline-Eintraege (Cross-Job)
- Activity Timeline
- Notizen (inline editierbar)
- Quick Actions: Email, Anruf, ToDo erstellen
- Match-Score + Staerken/Luecken

### KPI Bar:
- In Pipeline (aktive Kandidaten)
- Platziert (diesen Monat)
- Ø Tage pro Phase
- Conversion Rate (mit Trend-Pfeil)
- Revenue Pipeline (wenn Gehalt bekannt)

---

## Abhaengigkeiten-Matrix

| Aenderung | Betrifft | Risiko |
|-----------|----------|--------|
| Template austauschen | NUR Frontend | NIEDRIG |
| Neue Endpoints | Backend + Frontend | MITTEL |
| Stage Enum aendern | DB + Backend + Frontend + n8n | HOCH |
| Pipeline Entry Model aendern | DB + Backend + Frontend | HOCH |
| Sidebar Navigation | NUR Frontend | NIEDRIG |
| KPI berechnen | Backend (neuer Endpoint) | NIEDRIG |

---

## Wichtige Regeln
1. Backend-Endpoints NICHT umbenennen (n8n Webhooks!)
2. PipelineStage Enum NICHT aendern ohne Migration
3. ATSPipelineEntry Schema NICHT brechen
4. Drive-Time Daten kommen aus Match — Pipeline hat keine eigenen
5. in_pipeline Boolean auf ATSJob ist der Visibility-Switch
6. IMMER Activity loggen bei Stage-Changes
