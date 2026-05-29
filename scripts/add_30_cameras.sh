#!/usr/bin/env bash
# ===================================================================
# Add the original 30 RTSP cameras with full-screen zones
#   13 from 192.168.1.221 + 17 from 192.168.1.6
# Idempotent: deletes existing camera+zones before re-creating.
# ===================================================================
set -euo pipefail

API="http://localhost:8002/api"
EDGE_ID="vehicle-edge-01"

# Full-screen zone polygon (800×500 canvas) — [[x,y], ...] format
FULLSCREEN_POLY='[[0,0],[800,0],[800,500],[0,500]]'

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# --- Delete the 20 new cam_stream_* cameras if they exist ---
echo ""
echo "Cleaning up cam_stream_* cameras (if any)..."
for i in $(seq 1 20); do
    curl -s -o /dev/null -X DELETE "${API}/cameras/cam_stream_${i}" 2>/dev/null || true
done

echo ""
echo "Adding 30 cameras (13 × 192.168.1.221 + 17 × 192.168.1.6)"
echo "Each with a full-screen detection zone"
echo "================================================================"
echo ""

# 192.168.1.221 — 13 cameras
CAM_221_IDS=(2 3 4 5 6 7 8 9 10 11 15 17 19)

for n in "${CAM_221_IDS[@]}"; do
    CAM_ID="cam_192_168_1_221_${n}"
    ZONE_ID="zone_192_168_1_221_${n}"
    RTSP_URL="rtsp://192.168.1.221/video0.sdp"

    # Delete existing (idempotent)
    curl -s -o /dev/null -X DELETE "${API}/cameras/${CAM_ID}" 2>/dev/null || true

    # Create camera
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "${API}/cameras" \
        -H "Content-Type: application/json" \
        -d "{
            \"camera_id\": \"${CAM_ID}\",
            \"name\": \"221 Stream ${n}\",
            \"source_url\": \"${RTSP_URL}\",
            \"assigned_edge\": \"${EDGE_ID}\"
        }")

    if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "201" ]]; then
        info "Camera  ${CAM_ID}  (${RTSP_URL})"
    else
        warn "Camera  ${CAM_ID}  HTTP ${HTTP_CODE}"
    fi

    # Create zone
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "${API}/zones" \
        -H "Content-Type: application/json" \
        -d "{
            \"zone_id\": \"${ZONE_ID}\",
            \"name\": \"221-${n} Full Zone\",
            \"camera_id\": \"${CAM_ID}\",
            \"zone_poly\": ${FULLSCREEN_POLY},
            \"ramp_type\": \"inner\",
            \"max_vehicles\": 20,
            \"max_dwell_time_s\": 900
        }")

    if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "201" ]]; then
        info "Zone    ${ZONE_ID}  (full-screen)"
    else
        warn "Zone    ${ZONE_ID}  HTTP ${HTTP_CODE}"
    fi
done

# 192.168.1.6 — 17 cameras
CAM_6_IDS=(1 2 4 5 6 7 8 9 11 12 13 14 16 17 18 19 20)

for n in "${CAM_6_IDS[@]}"; do
    CAM_ID="cam_192_168_1_6_${n}"
    ZONE_ID="zone_192_168_1_6_${n}"
    RTSP_URL="rtsp://192.168.1.6/video0.sdp"

    # Delete existing (idempotent)
    curl -s -o /dev/null -X DELETE "${API}/cameras/${CAM_ID}" 2>/dev/null || true

    # Create camera
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "${API}/cameras" \
        -H "Content-Type: application/json" \
        -d "{
            \"camera_id\": \"${CAM_ID}\",
            \"name\": \"6 Stream ${n}\",
            \"source_url\": \"${RTSP_URL}\",
            \"assigned_edge\": \"${EDGE_ID}\"
        }")

    if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "201" ]]; then
        info "Camera  ${CAM_ID}  (${RTSP_URL})"
    else
        warn "Camera  ${CAM_ID}  HTTP ${HTTP_CODE}"
    fi

    # Create zone
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "${API}/zones" \
        -H "Content-Type: application/json" \
        -d "{
            \"zone_id\": \"${ZONE_ID}\",
            \"name\": \"6-${n} Full Zone\",
            \"camera_id\": \"${CAM_ID}\",
            \"zone_poly\": ${FULLSCREEN_POLY},
            \"ramp_type\": \"inner\",
            \"max_vehicles\": 20,
            \"max_dwell_time_s\": 900
        }")

    if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "201" ]]; then
        info "Zone    ${ZONE_ID}  (full-screen)"
    else
        warn "Zone    ${ZONE_ID}  HTTP ${HTTP_CODE}"
    fi
done

echo ""
echo "================================================================"
echo -e "${GREEN}Done.${NC} 30 cameras + 30 zones registered."
echo ""
echo "Restart the edge agent to pick up the new cameras:"
echo "  ./start.sh"
echo ""
