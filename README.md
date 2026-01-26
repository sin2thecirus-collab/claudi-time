# Matching-Tool für Recruiter

Ein intelligentes Matching-Tool, das Jobs aus CSV-Dateien mit Kandidaten aus einem CRM-System verknüpft.

## Features

- **CSV-Import**: Jobs aus Tab-getrennten CSV-Dateien importieren
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

## API-Dokumentation

Nach dem Start ist die API-Dokumentation verfügbar unter:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Projektstruktur

```
matching-tool/
├── app/
│   ├── api/          # API-Routen
│   ├── models/       # SQLAlchemy Models
│   ├── schemas/      # Pydantic Schemas
│   ├── services/     # Business-Logik
│   └── templates/    # Jinja2 Templates
├── migrations/       # Alembic Migrationen
├── tests/           # Tests
└── docker-compose.yml
```

## Lizenz

Proprietär - Alle Rechte vorbehalten
