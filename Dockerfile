FROM python:3.11-slim AS base

# Build: 2026-02-04-v1 - Word-CV async + UserInstallation fix
# Arbeitsverzeichnis setzen
WORKDIR /app

# System-Dependencies für PyMuPDF, asyncpg und Word-zu-PDF Konvertierung
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    libreoffice-writer \
    fonts-liberation \
    fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

# Python Dependencies installieren
COPY pyproject.toml README.md ./
COPY app/ ./app/
RUN pip install --no-cache-dir .

# Anwendung kopieren
COPY . .

# Non-root User für Sicherheit
RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /app && \
    mkdir -p /home/appuser/.config/libreoffice && \
    chown -R appuser:appuser /home/appuser

USER appuser

# Port freigeben
EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Umgebungsvariablen
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    HOME=/home/appuser

# Uvicorn starten
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

