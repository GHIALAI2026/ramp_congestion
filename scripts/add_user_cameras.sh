#!/usr/bin/env bash
# ===================================================================
# Add 31 user-specified RTSP cameras with full-screen zones
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

RTSP_URLS=(
    "rtsp://admin:Ap0%28%402024@10.86.95.150/videoStreamId=2"
    "rtsp://admin:Ap0%28%402024@10.86.95.153/videoStreamId=2"
    "rtsp://kspoc:kspoc123@10.64.0.86/stream1"
    "rtsp://kspoc:kspoc123@10.55.5.26/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.17/stream1"
    "rtsp://kspoc:kspoc123@10.55.5.27/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.11/stream1"
    "rtsp://admin:Ap0%28%402024@10.86.95.22/videoStreamId=2"
    "rtsp://kspoc:kspoc123@10.64.0.13/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.14/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.15/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.16/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.30/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.31/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.33/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.34/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.36/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.37/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.38/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.40/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.41/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.42/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.44/stream1"
    "rtsp://kspoc:kspoc123@10.64.0.46/stream1"
    "rtsp://kspoc:kspoc123@10.86.158.163/rtsp/defaultPrimary?streamType=u"
    "rtsp://kspoc:kspoc123@10.86.158.196/rtsp/defaultPrimary?streamType=u"
    "rtsp://kspoc:kspoc123@10.86.158.168/rtsp/defaultPrimary?streamType=u"
    "rtsp://kspoc:kspoc123@10.86.158.140/rtsp/defaultPrimary?streamType=u"
    "rtsp://kspoc:kspoc123@10.86.158.172/rtsp/defaultPrimary?streamType=u"
    "rtsp://kspoc:kspoc123@10.86.158.71:554/videoStreamId=2"
    "rtsp://kspoc:kspoc123@10.86.158.179/rtsp/defaultPrimary?streamType=u"
)

echo ""
echo "Adding 31 cameras with full-screen detection zones"
echo "================================================================"
echo ""

for i in "${!RTSP_URLS[@]}"; do
    SEQ=$((i + 1))
    CAM_ID="cam_${SEQ}"
    ZONE_ID="zone_${SEQ}"
    RTSP_URL="${RTSP_URLS[$i]}"
    CAM_NAME="Camera ${SEQ}"
    ZONE_NAME="Zone ${SEQ} — Full"

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
        warn "Camera  ${CAM_ID}  HTTP ${HTTP_CODE}"
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
        warn "Zone    ${ZONE_ID}  HTTP ${HTTP_CODE}"
    fi
done

echo ""
echo "================================================================"
echo -e "${GREEN}Done.${NC} 31 cameras + 31 zones registered."
echo ""
