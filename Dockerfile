FROM python:3.11-slim AS base

# Build: 2026-02-20-v2 - Fix: pip install Layer von app/ Code getrennt (Railway Build-Fix)
# Arbeitsverzeichnis setzen
WORKDIR /app

# System-Dependencies für PyMuPDF, asyncpg, Word-zu-PDF und WeasyPrint
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    libreoffice-writer \
    fonts-liberation \
    fontconfig \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

# Python Dependencies installieren (eigener Layer — nur bei pyproject.toml Aenderung)
COPY pyproject.toml README.md ./
RUN mkdir -p app && touch app/__init__.py && \
    pip install --no-cache-dir . && \
    rm -rf app

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

# Start: Migrations + Uvicorn
RUN chmod +x start.sh
CMD ["./start.sh"]

