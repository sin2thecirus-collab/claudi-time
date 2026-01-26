FROM python:3.11-slim

# Arbeitsverzeichnis setzen
WORKDIR /app

# System-Dependencies für PyMuPDF und asyncpg
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python Dependencies installieren
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# Anwendung kopieren
COPY . .

# Port freigeben
EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health')" || exit 1

# Uvicorn starten
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
