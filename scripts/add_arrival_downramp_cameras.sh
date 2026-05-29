#!/usr/bin/env bash
# ===================================================================
# Add 26 Arrival Downramp cameras with full-screen zones
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

# Camera array: (name IP RTSP_URL)
# Note: All special characters in credentials properly URL-encoded
declare -a CAMERAS=(
    "ARRIVAL DOWNRAMP|10.86.94.189|rtsp://admin:Ap0%28%402024@10.86.94.189:554/videoStreamId=2"
    "Arrival - New Ramp|10.86.95.141|rtsp://admin:Ap0%28%402024@10.86.95.141/rtsp/defaultPrimary?streamType=u"
    "Arrival - New Ramp|10.86.95.143|rtsp://admin:Ap0%28%402024@10.86.95.143/videoStreamId=2"
    "Arrival ramp|10.86.95.146|rtsp://admin:Ap0%28%402024@10.86.95.146/videoStreamId=2"
    "Arrival ramp|10.86.95.149|rtsp://admin:Ap0%28%402024@10.86.95.149/videoStreamId=2"
    "Arrival ramp|10.86.95.150|rtsp://admin:Ap0%28%402024@10.86.95.150/videoStreamId=2"
    "Arrival ramp|10.86.95.151|rtsp://admin:Ap0%28%402024@10.86.95.151/videoStreamId=2"
    "Arrival ramp|10.86.95.152|rtsp://admin:Ap0%28%402024@10.86.95.152/videoStreamId=1"
    "Arrival ramp|10.86.95.153|rtsp://admin:Ap0%28%402024@10.86.95.153/videoStreamId=2"
    "Arrival ramp|10.86.95.157|rtsp://admin:Ap0%28%402024@10.86.95.157/videoStreamId=1"
    "Arrival ramp|10.86.95.172|rtsp://admin:Ap0%28%402024@10.86.95.172/videoStreamId=1"
    "Arrival ramp|10.86.95.177|rtsp://admin:Ap0%28%402024@10.86.95.177/videoStreamId=2"
    "Arrival ramp|10.86.95.179|rtsp://admin:Ap0%28%402024@10.86.95.179/videoStreamId=2"
    "Arrival ramp|10.86.95.184|rtsp://admin:Ap0%28%402024@10.86.95.184/videoStreamId=2"
    "Arrival ramp|10.86.95.191|rtsp://admin:Ap0%28%402024@10.86.95.191/stream1"
    "Arrival ramp|10.86.95.192|rtsp://admin:Ap0%28%402024@10.86.95.192/stream1"
    "Arrival - Up Ramp - Que management|10.86.95.195|rtsp://admin:Ap0%28%402024@10.86.95.195/videoStreamId=2"
    "Arrival ramp|10.86.95.204|rtsp://admin:Ap0%28%402024@10.86.95.204/stream1"
    "Arrival ramp|10.86.95.205|rtsp://admin:admin%40123@10.86.95.205/stream1"
    "Arrival Ramp|10.86.95.207|rtsp://admin:Ap0%28%402024@10.86.95.207/stream1"
    "Arrival ramp|10.86.95.209|rtsp://admin:Ap0%28%402024@10.86.95.209/stream1"
    "Arrival ramp|10.86.95.210|rtsp://admin:Ap0%28%402024@10.86.95.210/stream1"
    "Arrival ramp|10.86.95.212|rtsp://admin:admin%40123@10.86.95.212/stream1"
    "Arrival ramp|10.86.95.218|rtsp://admin:admin%40123@10.86.95.218/stream1"
    "Arrival ramp|10.86.95.221|rtsp://admin:Ap0%28%402024@10.86.95.221/stream1"
    "Arrival ramp|10.86.95.222|rtsp://admin:Ap0%28%402024@10.86.95.222/videoStreamId=3"
)

echo ""
echo "Adding 26 Arrival Downramp cameras with full-screen detection zones"
echo "================================================================"
echo ""

for i in "${!CAMERAS[@]}"; do
    SEQ=$((i + 1))
    
    # Parse camera data (name|IP|RTSP_URL)
    IFS='|' read -r CAM_NAME IP RTSP_URL <<< "${CAMERAS[$i]}"
    
    CAM_ID="arrival_cam_${SEQ}"
    ZONE_ID="arrival_zone_${SEQ}"
    # Append last IP octet to keep dropdown labels unique
    LAST_OCTET="${IP##*.}"
    CAM_NAME="${CAM_NAME} .${LAST_OCTET}"
    ZONE_NAME="${CAM_NAME} — Full Zone"

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
        info "Camera  ${CAM_ID}  [${IP}]"
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
echo "✓ Finished adding 26 Arrival Downramp cameras"
echo ""
