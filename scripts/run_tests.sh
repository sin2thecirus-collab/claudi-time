#!/bin/bash
# Test-Suite mit PostgreSQL + PostGIS ausführen

set -e

echo "=== Starting Test Database ==="
docker-compose -f docker-compose.test.yml up -d

echo "=== Waiting for database to be ready ==="
until docker exec matching-tool-test-db pg_isready -U test -d matching_tool_test; do
    sleep 1
done
echo "Database is ready!"

echo "=== Running Tests ==="
export TEST_DATABASE_URL="postgresql+asyncpg://test:test@localhost:5433/matching_tool_test"

# Argumente durchreichen (z.B. -v, --tb=short, tests/test_geo.py)
python3 -m pytest "$@"

echo "=== Stopping Test Database ==="
docker-compose -f docker-compose.test.yml down

echo "=== Done ==="
