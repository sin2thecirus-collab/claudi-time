# Matching-Tool für Recruiter

Ein intelligentes Matching-Tool, das Jobs aus CSV-Dateien mit Kandidaten aus einem CRM-System verknüpft.

## Features

- **CSV-Import**: Jobs aus Tab-getrennten CSV-Dateien importieren (max. 10.000 Zeilen)
- **CRM-Integration**: Kandidaten aus Recruit CRM synchronisieren
- **CV-Parsing**: Automatische Extraktion von Skills und Berufserfahrung aus PDFs
- **Geo-Matching**: Kandidaten im Umkreis von 25km finden (PostGIS)
- **Keyword-Matching**: Automatischer Abgleich von Skills mit Jobanforderungen
- **KI-Bewertung**: Detaillierte Passung durch OpenAI (gpt-4o-mini)
- **Statistiken**: Übersicht über Matches, Vermittlungen und Kosten
- **Alerts**: Benachrichtigungen bei exzellenten Matches

## Technologie-Stack

- **Backend**: Python 3.11 + FastAPI
- **Datenbank**: PostgreSQL 15 + PostGIS
- **Frontend**: Jinja2 + HTMX + Tailwind CSS
- **KI**: OpenAI API (gpt-4o-mini)
- **Deployment**: Railway

## Installation

### Voraussetzungen

- Python 3.11+
- PostgreSQL 15+ mit PostGIS Extension
- OpenAI API Key
- Recruit CRM API Token

### Lokale Entwicklung

1. Repository klonen:
```bash
git clone <repository-url>
cd matching-tool
```

2. Virtual Environment erstellen:
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# oder
.\venv\Scripts\activate  # Windows
```

3. Dependencies installieren:
```bash
pip install -e ".[dev]"
```

4. Environment-Variablen setzen:
```bash
cp .env.example .env
# .env mit eigenen Werten befüllen
```

5. Datenbank mit Docker starten:
```bash
docker-compose up -d db
```

6. Migrationen ausführen:
```bash
alembic upgrade head
```

7. Server starten:
```bash
uvicorn app.main:app --reload
```

Die Anwendung ist unter http://localhost:8000 erreichbar.

### Mit Docker

```bash
# Alles starten (DB + App)
docker-compose up -d

# Nur DB für lokale Entwicklung
docker-compose up -d db
```

## Tests

### Test-Dependencies installieren

```bash
pip install -e ".[dev]"
pip install aiosqlite  # Für SQLite-basierte Tests
```

### Tests ausführen

```bash
# Alle Tests
pytest

# Mit Coverage
pytest --cov=app --cov-report=html

# Nur Unit-Tests
pytest tests/test_validation.py tests/test_matching.py tests/test_crud.py

# Nur API-Tests
pytest tests/test_api.py

# Nur Integration-Tests
pytest tests/test_integration.py

# Verbose Output
pytest -v
```

### Test-Struktur

```
tests/
├── conftest.py          # Fixtures, Factories, DB-Setup
├── test_validation.py   # Validierungs-Tests (PLZ, Stadt, etc.)
├── test_matching.py     # Keyword-Matching Tests
├── test_crud.py         # Model und Factory Tests
├── test_api.py          # API-Endpoint Tests
└── test_integration.py  # Integration Tests
```

## Deployment auf Railway

### 1. Neues Projekt auf Railway erstellen

1. Gehe zu [railway.app](https://railway.app)
2. Neues Projekt erstellen
3. "Deploy from GitHub" wählen

### 2. PostgreSQL hinzufügen

1. "+ New" klicken
2. "Database" → "PostgreSQL" wählen
3. PostGIS Extension aktivieren:
   - Railway PostgreSQL Settings öffnen
   - Query ausführen: `CREATE EXTENSION IF NOT EXISTS postgis;`

### 3. Environment-Variablen setzen

In Railway unter "Variables":

```
DATABASE_URL=<von Railway bereitgestellt>
OPENAI_API_KEY=sk-...
RECRUIT_CRM_API_KEY=...
RECRUIT_CRM_BASE_URL=https://api.recruitcrm.io/v1
SECRET_KEY=<random-32-chars>
CRON_SECRET=<random-string>
ENVIRONMENT=production
```

### 4. Cron-Jobs einrichten

In Railway unter "Cron":

| Job | Schedule | Command |
|-----|----------|---------|
| Geocoding | `0 3 * * *` | `curl -X POST $RAILWAY_PUBLIC_DOMAIN/api/admin/geocoding/trigger?source=cron` |
| CRM-Sync | `30 3 * * *` | `curl -X POST $RAILWAY_PUBLIC_DOMAIN/api/admin/crm-sync/trigger?source=cron` |
| Matching | `0 4 * * *` | `curl -X POST $RAILWAY_PUBLIC_DOMAIN/api/admin/matching/trigger?source=cron` |
| Cleanup | `0 5 * * 0` | `curl -X POST $RAILWAY_PUBLIC_DOMAIN/api/admin/cleanup/trigger?source=cron` |

## API-Dokumentation

Nach dem Start ist die API-Dokumentation verfügbar unter:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

### Wichtige Endpoints

| Endpoint | Methode | Beschreibung |
|----------|---------|--------------|
| `/api/jobs` | GET | Jobs auflisten |
| `/api/jobs/import` | POST | CSV importieren |
| `/api/candidates` | GET | Kandidaten auflisten |
| `/api/candidates/sync` | POST | CRM-Sync starten |
| `/api/matches/ai-check` | POST | KI-Bewertung starten |
| `/api/statistics/dashboard` | GET | Dashboard-Statistiken |
| `/api/admin/status` | GET | System-Status |

## Projektstruktur

```
matching-tool/
├── app/
│   ├── api/              # API-Routen (12 Dateien)
│   ├── models/           # SQLAlchemy Models (9 Dateien)
│   ├── schemas/          # Pydantic Schemas (8 Dateien)
│   ├── services/         # Business-Logik (16 Dateien)
│   └── templates/        # Jinja2 Templates + Komponenten
├── migrations/           # Alembic Migrationen
├── tests/               # Pytest Tests
├── docs/                # Dokumentation
├── Dockerfile
├── docker-compose.yml
├── railway.json         # Railway Konfiguration
├── railway.toml
└── pyproject.toml
```

## Limits

| Limit | Wert |
|-------|------|
| CSV max. Dateigröße | 50 MB |
| CSV max. Zeilen | 10.000 |
| Batch-Delete max. | 100 |
| KI-Check max. Kandidaten | 50 |
| Filter Multi-Select max. | 20 |
| Matching-Radius | 25 km |

## Lizenz

Proprietär - Alle Rechte vorbehalten
