#!/bin/sh
# Railway Startup Script
# Build: 2026-01-26-v4

echo "Starting Railway deployment..."
echo "Running alembic migrations..."

# Run migrations (continue even if it fails)
alembic upgrade head || echo "Warning: Alembic migration failed, continuing anyway..."

echo "Starting uvicorn server..."
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
