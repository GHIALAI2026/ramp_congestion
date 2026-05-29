#!/usr/bin/env bash
# ===================================================================
# Add 20 RTSP cameras (stream1–stream20) with full-screen zones
# RTSP base: rtsp://192.168.1.2:8554/stream{1..20}
# ===================================================================
set -euo pipefail

API="http://localhost:8002/api"
EDGE_ID="vehicle-edge-01"

# Full-screen zone polygon (800×500 canvas) — [[x,y], ...] format
FULLSCREEN_POLY='[[0,0],[800,0],[800,500],[0,500]]'

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[FAIL]${NC} $*"; }

echo ""
echo "Adding 20 cameras (rtsp://192.168.1.2:8554/stream1 … stream20)"
echo "Each with a full-screen detection zone"
echo "================================================================"
echo ""

for i in $(seq 1 20); do
    CAM_ID="cam_stream_${i}"
    ZONE_ID="zone_stream_${i}"
    RTSP_URL="rtsp://192.168.1.2:8554/stream${i}"
    CAM_NAME="Stream ${i}"
    ZONE_NAME="Stream ${i} — Full Zone"

    # --- Remove existing camera + zones (idempotent) ---
    curl -s -o /dev/null -X DELETE "${API}/cameras/${CAM_ID}" 2>/dev/null || true

    # --- Create camera ---
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "${API}/cameras" \
        -H "Content-Type: application/json" \
        -d "{
            \"camera_id\": \"${CAM_ID}\",
            \"name\": \"${CAM_NAME}\",
            \"source_url\": \"${RTSP_URL}\",
            \"assigned_edge\": \"${EDGE_ID}\"
        }")

    if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "201" ]]; then
        info "Camera  ${CAM_ID}  (${RTSP_URL})"
    else
        warn "Camera  ${CAM_ID}  HTTP ${HTTP_CODE} (may already exist)"
    fi

    # --- Create zone ---
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "${API}/zones" \
        -H "Content-Type: application/json" \
        -d "{
            \"zone_id\": \"${ZONE_ID}\",
            \"name\": \"${ZONE_NAME}\",
            \"camera_id\": \"${CAM_ID}\",
            \"zone_poly\": ${FULLSCREEN_POLY},
            \"ramp_type\": \"inner\",
            \"max_vehicles\": 20,
            \"max_dwell_time_s\": 900
        }")

    if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "201" ]]; then
        info "Zone    ${ZONE_ID}  (full-screen)"
    else
        warn "Zone    ${ZONE_ID}  HTTP ${HTTP_CODE} (may already exist)"
    fi
done

echo ""
echo "================================================================"
echo -e "${GREEN}Done.${NC} 20 cameras + 20 zones registered."
echo ""
echo "Restart the edge agent to pick up the new cameras:"
echo "  ./start.sh"
echo ""
