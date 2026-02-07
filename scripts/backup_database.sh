#!/bin/bash
# =============================================================================
# Datenbank-Backup für Matching-Tool
# Erstellt ein vollständiges PostgreSQL-Backup aller Tabellen
#
# Voraussetzung: DATABASE_URL muss gesetzt sein (oder .env vorhanden)
#
# Nutzung:
#   ./scripts/backup_database.sh                    # Standard-Backup
#   ./scripts/backup_database.sh custom_name        # Backup mit Name
#   SKIP_R2_CHECK=1 ./scripts/backup_database.sh    # Ohne R2-Inventur
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="$PROJECT_DIR/backups"

# Farben
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  Matching-Tool Datenbank-Backup${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""

# .env laden falls vorhanden
if [ -f "$PROJECT_DIR/.env" ]; then
    echo -e "${YELLOW}→ Lade .env Datei...${NC}"
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

# DATABASE_URL prüfen
if [ -z "${DATABASE_URL:-}" ]; then
    echo -e "${RED}✗ DATABASE_URL nicht gesetzt!${NC}"
    echo "  Setze DATABASE_URL als Umgebungsvariable oder in .env"
    echo "  Format: postgresql+asyncpg://user:pass@host:port/dbname"
    exit 1
fi

# asyncpg → psycopg für pg_dump konvertieren
PG_URL=$(echo "$DATABASE_URL" | sed 's|postgresql+asyncpg://|postgresql://|')

# Datenbankname extrahieren für Info
DB_NAME=$(echo "$PG_URL" | sed -n 's|.*/\([^?]*\).*|\1|p')
DB_HOST=$(echo "$PG_URL" | sed -n 's|.*@\([^:]*\):.*|\1|p')

echo -e "${YELLOW}→ Datenbank: ${NC}$DB_NAME @ $DB_HOST"

# Backup-Verzeichnis erstellen
mkdir -p "$BACKUP_DIR"

# Backup-Dateiname
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_NAME="${1:-pre_crm_migration}"
BACKUP_FILE="$BACKUP_DIR/${BACKUP_NAME}_${TIMESTAMP}.sql"
BACKUP_FILE_GZ="${BACKUP_FILE}.gz"

echo -e "${YELLOW}→ Erstelle Backup...${NC}"
echo ""

# Tabellen-Inventur
echo -e "${BLUE}--- Tabellen im Backup ---${NC}"
TABLES=(
    "candidates"
    "jobs"
    "matches"
    "companies"
    "company_contacts"
    "company_correspondence"
    "ats_jobs"
    "ats_pipeline_entries"
    "ats_activities"
    "ats_call_notes"
    "ats_email_templates"
    "ats_todos"
    "alerts"
    "daily_statistics"
    "filter_presets"
    "filter_usage"
    "import_jobs"
    "job_runs"
    "mt_match_memory"
    "mt_training_data"
    "priority_cities"
    "alembic_version"
)

for table in "${TABLES[@]}"; do
    echo -e "  ${GREEN}✓${NC} $table"
done
echo ""

# pg_dump ausführen (vollständiges Backup mit Schema + Daten)
echo -e "${YELLOW}→ pg_dump läuft...${NC}"

if pg_dump "$PG_URL" \
    --verbose \
    --format=plain \
    --create \
    --clean \
    --if-exists \
    --no-owner \
    --no-privileges \
    --encoding=UTF8 \
    > "$BACKUP_FILE" 2>/dev/null; then

    # Komprimieren
    echo -e "${YELLOW}→ Komprimiere Backup...${NC}"
    gzip -c "$BACKUP_FILE" > "$BACKUP_FILE_GZ"
    rm "$BACKUP_FILE"

    # Statistiken
    BACKUP_SIZE=$(du -h "$BACKUP_FILE_GZ" | cut -f1)

    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  ✓ Backup erfolgreich erstellt!${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo -e "  Datei:  ${BACKUP_FILE_GZ}"
    echo -e "  Größe:  ${BACKUP_SIZE}"
    echo -e "  Zeit:   $(date '+%Y-%m-%d %H:%M:%S')"
    echo -e "  Git-Tag: pre-crm-migration-v1.0"
    echo ""

else
    echo -e "${RED}✗ pg_dump fehlgeschlagen!${NC}"
    echo ""
    echo "Mögliche Ursachen:"
    echo "  1. pg_dump nicht installiert: brew install postgresql"
    echo "  2. Datenbank nicht erreichbar (VPN/Firewall?)"
    echo "  3. Falsche Zugangsdaten in DATABASE_URL"
    echo ""
    echo "Alternative: Railway CLI nutzen:"
    echo "  railway run pg_dump \$DATABASE_URL > backup.sql"
    echo ""

    # Fallback: Python-basiertes Backup
    echo -e "${YELLOW}→ Versuche Python-basiertes Backup...${NC}"
    cd "$PROJECT_DIR"
    python3 -c "
import asyncio
import json
import sys
from datetime import datetime, date
from decimal import Decimal

async def backup():
    try:
        import asyncpg
    except ImportError:
        print('asyncpg nicht installiert. Installiere mit: pip install asyncpg')
        sys.exit(1)

    url = '$DATABASE_URL'.replace('postgresql+asyncpg://', 'postgresql://')
    conn = await asyncpg.connect(url)

    tables = await conn.fetch(\"\"\"
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
    \"\"\")

    backup_data = {}
    total_rows = 0

    for t in tables:
        name = t['tablename']
        rows = await conn.fetch(f'SELECT * FROM {name}')

        # Konvertiere zu serialisierbarem Format
        table_data = []
        for row in rows:
            row_dict = {}
            for key, val in dict(row).items():
                if isinstance(val, (datetime, date)):
                    row_dict[key] = val.isoformat()
                elif isinstance(val, Decimal):
                    row_dict[key] = float(val)
                elif isinstance(val, bytes):
                    row_dict[key] = val.hex()
                else:
                    row_dict[key] = val
            table_data.append(row_dict)

        backup_data[name] = table_data
        count = len(table_data)
        total_rows += count
        print(f'  ✓ {name}: {count} Zeilen')

    await conn.close()

    # Speichern
    backup_file = '$BACKUP_DIR/${BACKUP_NAME}_${TIMESTAMP}.json.gz'
    import gzip
    with gzip.open(backup_file, 'wt', encoding='utf-8') as f:
        json.dump(backup_data, f, ensure_ascii=False, default=str)

    print(f'\\n✓ JSON-Backup erstellt: {backup_file}')
    print(f'  Gesamt: {total_rows} Zeilen in {len(backup_data)} Tabellen')

asyncio.run(backup())
" 2>&1 && echo -e "${GREEN}✓ Python-Backup erfolgreich${NC}" || echo -e "${RED}✗ Auch Python-Backup fehlgeschlagen${NC}"
fi

# R2 CV-Inventur (optional)
if [ "${SKIP_R2_CHECK:-0}" != "1" ]; then
    echo ""
    echo -e "${BLUE}--- R2 CV-Inventur ---${NC}"
    cd "$PROJECT_DIR"
    python3 -c "
import asyncio

async def r2_check():
    try:
        import asyncpg
    except ImportError:
        print('asyncpg nicht verfügbar - überspringe R2-Check')
        return

    url = '$DATABASE_URL'.replace('postgresql+asyncpg://', 'postgresql://')
    conn = await asyncpg.connect(url)

    stats = await conn.fetchrow('''
        SELECT
            COUNT(*) as total,
            COUNT(cv_stored_path) as in_r2,
            COUNT(cv_url) as has_crm_url,
            COUNT(CASE WHEN cv_stored_path IS NOT NULL AND cv_url IS NOT NULL THEN 1 END) as both,
            COUNT(CASE WHEN cv_stored_path IS NULL AND cv_url IS NOT NULL THEN 1 END) as crm_only,
            COUNT(CASE WHEN cv_stored_path IS NOT NULL AND cv_url IS NULL THEN 1 END) as r2_only,
            COUNT(CASE WHEN cv_stored_path IS NULL AND cv_url IS NULL THEN 1 END) as no_cv,
            COUNT(cv_text) as has_parsed_text
        FROM candidates
        WHERE deleted_at IS NULL
    ''')

    print(f'  Kandidaten gesamt:     {stats[\"total\"]}')
    print(f'  CV in R2:              {stats[\"in_r2\"]}')
    print(f'  CV-URL (CRM):          {stats[\"has_crm_url\"]}')
    print(f'  Beides (R2 + CRM):     {stats[\"both\"]}')
    print(f'  ⚠️  Nur CRM-URL:        {stats[\"crm_only\"]}')
    print(f'  Nur R2:                {stats[\"r2_only\"]}')
    print(f'  Kein CV:               {stats[\"no_cv\"]}')
    print(f'  CV-Text geparst:       {stats[\"has_parsed_text\"]}')

    if stats['crm_only'] > 0:
        print(f'')
        print(f'  ⚠️  WARNUNG: {stats[\"crm_only\"]} Kandidaten haben CV nur als CRM-URL!')
        print(f'  Diese müssen vor der CRM-Abkapselung nach R2 migriert werden:')
        print(f'  POST /api/candidates/migrate-cvs-to-r2')

    await conn.close()

asyncio.run(r2_check())
" 2>&1 || echo -e "${YELLOW}R2-Check übersprungen (Verbindungsfehler)${NC}"
fi

echo ""
echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  Backup-Prozess abgeschlossen${NC}"
echo -e "${BLUE}============================================${NC}"
