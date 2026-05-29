#!/usr/bin/env bash
# ===================================================================
# Remove ALL cameras and zones from the database
# Uses direct PostgreSQL for reliable, atomic deletion.
# ===================================================================
set -euo pipefail

DB_NAME="vehicle_zone"
DB_USER="${VZI_DB_USER:-apexedge}"
DB_PASS="${VZI_DB_PASS:-apexedge}"
DB_HOST="${VZI_DB_HOST:-localhost}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo ""
echo "=== Remove All Cameras & Zones ==="
echo ""

export PGPASSWORD="$DB_PASS"

CAM_COUNT=$(psql -U "$DB_USER" -h "$DB_HOST" -d "$DB_NAME" -tAc "SELECT COUNT(*) FROM cameras;" 2>/dev/null)
ZONE_COUNT=$(psql -U "$DB_USER" -h "$DB_HOST" -d "$DB_NAME" -tAc "SELECT COUNT(*) FROM zones;" 2>/dev/null)

if [[ "$CAM_COUNT" == "0" && "$ZONE_COUNT" == "0" ]]; then
    echo -e "${GREEN}[OK]${NC}   No cameras or zones found — nothing to delete."
    unset PGPASSWORD
    exit 0
fi

echo "Found: ${CAM_COUNT} camera(s), ${ZONE_COUNT} zone(s)"
echo ""

psql -U "$DB_USER" -h "$DB_HOST" -d "$DB_NAME" -c "DELETE FROM zones; DELETE FROM cameras;"

unset PGPASSWORD

echo ""
echo -e "${GREEN}[OK]${NC}   Removed ${CAM_COUNT} camera(s) and ${ZONE_COUNT} zone(s)."
echo ""
