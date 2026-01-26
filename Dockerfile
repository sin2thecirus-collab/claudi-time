FROM python:3.11-slim AS base

# Arbeitsverzeichnis setzen
WORKDIR /app

# System-Dependencies für PyMuPDF und asyncpg
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python Dependencies installieren
COPY pyproject.toml .
COPY app/ ./app/
RUN pip install --no-cache-dir .

# Anwendung kopieren
COPY . .

# Non-root User für Sicherheit
RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /app
USER appuser

# Port freigeben
EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Umgebungsvariablen
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

# Uvicorn starten
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
