#!/bin/bash
# =============================================================================
# Rollback-Script für Matching-Tool
# Stellt den exakten Zustand VOR der CRM-Migration wieder her
#
# Was wird zurückgesetzt:
#   1. Git-Code → Tag pre-crm-migration-v1.0 (Commit 6891c51)
#   2. Datenbank → Aus Backup-Datei wiederherstellen
#   3. Railway → Auto-Deploy vom wiederhergestellten Code
#
# Nutzung:
#   ./scripts/rollback.sh                              # Nur Code-Rollback
#   ./scripts/rollback.sh --with-db backup_file.sql.gz # Code + DB-Rollback
#   ./scripts/rollback.sh --dry-run                    # Zeigt nur was passieren würde
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
BOLD='\033[1m'
NC='\033[0m'

# Parameter
DRY_RUN=0
WITH_DB=0
BACKUP_FILE=""
FORCE=0

for arg in "$@"; do
    case $arg in
        --dry-run)
            DRY_RUN=1
            ;;
        --with-db)
            WITH_DB=1
            ;;
        --force)
            FORCE=1
            ;;
        *.sql.gz|*.json.gz)
            BACKUP_FILE="$arg"
            WITH_DB=1
            ;;
    esac
done

echo -e "${RED}============================================${NC}"
echo -e "${RED}  ⚠️  MATCHING-TOOL ROLLBACK${NC}"
echo -e "${RED}============================================${NC}"
echo ""
echo -e "${BOLD}Ziel-Zustand:${NC}"
echo -e "  Git-Tag:  ${GREEN}pre-crm-migration-v1.0${NC}"
echo -e "  Commit:   ${GREEN}6891c51${NC}"
echo -e "  Datum:    $(git -C "$PROJECT_DIR" log --format='%ci' pre-crm-migration-v1.0 -1 2>/dev/null || echo 'Tag nicht gefunden!')"
echo ""

if [ $DRY_RUN -eq 1 ]; then
    echo -e "${YELLOW}🔍 DRY-RUN Modus - keine Änderungen werden durchgeführt${NC}"
    echo ""
fi

# ============================================
# Phase 1: Vorprüfungen
# ============================================
echo -e "${BLUE}--- Phase 1: Vorprüfungen ---${NC}"

# Git-Tag vorhanden?
if git -C "$PROJECT_DIR" rev-parse "pre-crm-migration-v1.0" >/dev/null 2>&1; then
    TAG_COMMIT=$(git -C "$PROJECT_DIR" rev-list -n 1 "pre-crm-migration-v1.0")
    echo -e "  ${GREEN}✓${NC} Git-Tag gefunden: ${TAG_COMMIT:0:7}"
else
    echo -e "  ${RED}✗${NC} Git-Tag 'pre-crm-migration-v1.0' nicht gefunden!"
    echo "    Versuche: git fetch --tags"
    exit 1
fi

# Aktuelle Position
CURRENT_COMMIT=$(git -C "$PROJECT_DIR" rev-parse HEAD)
CURRENT_BRANCH=$(git -C "$PROJECT_DIR" branch --show-current)
echo -e "  ${YELLOW}→${NC} Aktuell: ${CURRENT_BRANCH} @ ${CURRENT_COMMIT:0:7}"

if [ "$CURRENT_COMMIT" = "$TAG_COMMIT" ]; then
    echo -e "  ${GREEN}✓${NC} Code ist bereits auf dem Rollback-Stand!"
fi

# Ungespeicherte Änderungen?
if git -C "$PROJECT_DIR" diff --quiet && git -C "$PROJECT_DIR" diff --cached --quiet; then
    echo -e "  ${GREEN}✓${NC} Keine ungespeicherten Änderungen"
else
    echo -e "  ${YELLOW}⚠️${NC}  Ungespeicherte Änderungen vorhanden!"
    if [ $FORCE -eq 0 ] && [ $DRY_RUN -eq 0 ]; then
        echo "    Nutze --force um trotzdem fortzufahren"
        echo "    WARNUNG: Ungespeicherte Änderungen gehen verloren!"
        exit 1
    fi
fi

# DB-Backup vorhanden?
if [ $WITH_DB -eq 1 ]; then
    if [ -z "$BACKUP_FILE" ]; then
        # Neuestes Backup finden
        BACKUP_FILE=$(ls -t "$BACKUP_DIR"/*.sql.gz "$BACKUP_DIR"/*.json.gz 2>/dev/null | head -1)
        if [ -z "$BACKUP_FILE" ]; then
            echo -e "  ${RED}✗${NC} Kein Backup gefunden in $BACKUP_DIR"
            echo "    Erstelle zuerst ein Backup: ./scripts/backup_database.sh"
            exit 1
        fi
    fi
    echo -e "  ${GREEN}✓${NC} DB-Backup: $(basename "$BACKUP_FILE")"
fi

echo ""

# ============================================
# Phase 2: Sicherheits-Bestätigung
# ============================================
if [ $DRY_RUN -eq 0 ] && [ $FORCE -eq 0 ]; then
    echo -e "${RED}${BOLD}⚠️  WARNUNG: Dies setzt den Code auf den Stand VOR der CRM-Migration zurück!${NC}"
    echo ""
    echo "Folgende Aktionen werden durchgeführt:"
    echo "  1. Git: Neuer Branch 'rollback-$(date +%Y%m%d)' vom Tag"
    echo "  2. Git: Push zum Remote → Railway Auto-Deploy"
    if [ $WITH_DB -eq 1 ]; then
        echo "  3. DB: Datenbank aus Backup wiederherstellen"
    fi
    echo ""
    echo -n "Fortfahren? (ja/nein): "
    read -r CONFIRM
    if [ "$CONFIRM" != "ja" ]; then
        echo "Abgebrochen."
        exit 0
    fi
fi

# ============================================
# Phase 3: Code-Rollback
# ============================================
echo ""
echo -e "${BLUE}--- Phase 3: Code-Rollback ---${NC}"

ROLLBACK_BRANCH="rollback-$(date +%Y%m%d_%H%M%S)"

if [ $DRY_RUN -eq 1 ]; then
    echo -e "  ${YELLOW}[DRY-RUN]${NC} Würde Branch '$ROLLBACK_BRANCH' erstellen"
    echo -e "  ${YELLOW}[DRY-RUN]${NC} Würde zum Tag pre-crm-migration-v1.0 wechseln"
    echo -e "  ${YELLOW}[DRY-RUN]${NC} Würde Branch pushen"
else
    # Aktuellen Stand sichern
    echo -e "  ${YELLOW}→${NC} Erstelle Sicherheits-Tag für aktuellen Stand..."
    git -C "$PROJECT_DIR" tag "pre-rollback-$(date +%Y%m%d_%H%M%S)" HEAD 2>/dev/null || true

    # Neuen Branch vom Tag erstellen
    echo -e "  ${YELLOW}→${NC} Erstelle Branch: $ROLLBACK_BRANCH"
    git -C "$PROJECT_DIR" checkout -b "$ROLLBACK_BRANCH" "pre-crm-migration-v1.0"

    echo -e "  ${GREEN}✓${NC} Branch erstellt und ausgecheckt"

    # Push zum Remote
    echo -e "  ${YELLOW}→${NC} Pushe zum Remote..."
    git -C "$PROJECT_DIR" push -u origin "$ROLLBACK_BRANCH"
    echo -e "  ${GREEN}✓${NC} Branch gepusht"

    echo ""
    echo -e "  ${YELLOW}WICHTIG:${NC} Auf Railway muss der Branch gewechselt werden:"
    echo -e "  Railway Dashboard → Settings → Source → Branch: ${GREEN}$ROLLBACK_BRANCH${NC}"
    echo -e "  Oder direkt main auf diesen Stand setzen:"
    echo -e "  git checkout main && git reset --hard pre-crm-migration-v1.0 && git push --force"
fi

# ============================================
# Phase 4: Datenbank-Rollback (optional)
# ============================================
if [ $WITH_DB -eq 1 ]; then
    echo ""
    echo -e "${BLUE}--- Phase 4: Datenbank-Rollback ---${NC}"

    # .env laden
    if [ -f "$PROJECT_DIR/.env" ]; then
        set -a
        source "$PROJECT_DIR/.env"
        set +a
    fi

    if [ -z "${DATABASE_URL:-}" ]; then
        echo -e "  ${RED}✗${NC} DATABASE_URL nicht gesetzt!"
        exit 1
    fi

    PG_URL=$(echo "$DATABASE_URL" | sed 's|postgresql+asyncpg://|postgresql://|')

    if [ $DRY_RUN -eq 1 ]; then
        echo -e "  ${YELLOW}[DRY-RUN]${NC} Würde Backup wiederherstellen: $(basename "$BACKUP_FILE")"
    else
        if [[ "$BACKUP_FILE" == *.sql.gz ]]; then
            echo -e "  ${YELLOW}→${NC} Stelle SQL-Backup wieder her..."
            gunzip -c "$BACKUP_FILE" | psql "$PG_URL"
            echo -e "  ${GREEN}✓${NC} SQL-Backup wiederhergestellt"

        elif [[ "$BACKUP_FILE" == *.json.gz ]]; then
            echo -e "  ${YELLOW}→${NC} Stelle JSON-Backup wieder her..."
            cd "$PROJECT_DIR"
            python3 -c "
import asyncio
import json
import gzip

async def restore():
    import asyncpg
    url = '$PG_URL'
    conn = await asyncpg.connect(url)

    with gzip.open('$BACKUP_FILE', 'rt', encoding='utf-8') as f:
        data = json.load(f)

    # Alembic-Version zuerst überspringen
    tables = sorted(data.keys(), key=lambda t: (t == 'alembic_version', t))

    for table in tables:
        rows = data[table]
        if not rows:
            continue

        # Tabelle leeren
        await conn.execute(f'TRUNCATE TABLE {table} CASCADE')

        # Daten einfügen
        columns = list(rows[0].keys())
        cols_str = ', '.join(columns)
        placeholders = ', '.join([f'\${i+1}' for i in range(len(columns))])

        for row in rows:
            values = [row.get(col) for col in columns]
            try:
                await conn.execute(
                    f'INSERT INTO {table} ({cols_str}) VALUES ({placeholders})',
                    *values
                )
            except Exception as e:
                print(f'  ⚠️ Fehler bei {table}: {e}')

        print(f'  ✓ {table}: {len(rows)} Zeilen wiederhergestellt')

    await conn.close()
    print('\\n✓ JSON-Backup vollständig wiederhergestellt')

asyncio.run(restore())
"
            echo -e "  ${GREEN}✓${NC} JSON-Backup wiederhergestellt"
        fi
    fi
fi

# ============================================
# Zusammenfassung
# ============================================
echo ""
echo -e "${GREEN}============================================${NC}"
if [ $DRY_RUN -eq 1 ]; then
    echo -e "${YELLOW}  DRY-RUN abgeschlossen (keine Änderungen)${NC}"
else
    echo -e "${GREEN}  ✓ Rollback abgeschlossen!${NC}"
fi
echo -e "${GREEN}============================================${NC}"
echo ""
echo "Nächste Schritte:"
echo "  1. Railway Dashboard prüfen → Deploy-Status"
echo "  2. https://claudi-time-production-46a5.up.railway.app/ testen"
echo "  3. Kandidaten-Suche testen"
echo "  4. CV-Ansicht testen"
echo ""
