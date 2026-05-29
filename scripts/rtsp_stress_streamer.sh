#!/usr/bin/env bash
# Generate many local RTSP streams from a small set of recorded videos.
#
# Example with bundled/default filenames:
#   cd /path/to/camera_recording
#   ./rtsp_stress_streamer.sh -n 40
#
# Example with custom files:
#   ./rtsp_stress_streamer.sh -n 40 /data/cam1.mp4 /data/cam2.mp4 /data/cam3.mp4 /data/cam4.mp4
#
# Requirements on the streaming machine:
#   - ffmpeg with h264_nvenc support preferred
#   - mediamtx binary in PATH, or ./mediamtx in current directory
#
# The application machine should use URLs printed by this script, for example:
#   rtsp://<streaming-machine-ip>:8554/stress_cam_01

set -euo pipefail

STREAM_COUNT="${STREAM_COUNT:-40}"
FPS="${FPS:-8}"
RTSP_PORT="${RTSP_PORT:-8554}"
STREAM_PREFIX="${STREAM_PREFIX:-stress_cam}"
WORK_DIR="${WORK_DIR:-./rtsp_stress_work}"
BITRATE="${BITRATE:-2500k}"
SCALE="${SCALE:-}"
PRECONVERT="${PRECONVERT:-1}"
USE_NVENC="${USE_NVENC:-1}"
MEDIAMTX_BIN="${MEDIAMTX_BIN:-}"
PUBLISH_HOST="${PUBLISH_HOST:-127.0.0.1}"
PUBLIC_HOST="${PUBLIC_HOST:-}"
RTSP_TRANSPORT="${RTSP_TRANSPORT:-tcp}"
VIDEO_DIR="${VIDEO_DIR:-$PWD}"

DEFAULT_VIDEO_FILES=(
    "camera_026_20260429_183358.mp4"
    "camera_086_20260429_183358.mp4"
    "camera_150_20260429_183359.mp4"
    "camera_153_20260429_183408.mp4"
)

PIDS=()
MEDIAMTX_PID=""

usage() {
    cat <<EOF
Usage:
  $0 [options] [video1 video2 video3 video4 ...]

If no video paths are passed, the script uses these files from VIDEO_DIR
(default VIDEO_DIR is the current working directory):
  ${DEFAULT_VIDEO_FILES[0]}
  ${DEFAULT_VIDEO_FILES[1]}
  ${DEFAULT_VIDEO_FILES[2]}
  ${DEFAULT_VIDEO_FILES[3]}

Options:
  -n, --streams N        Number of RTSP streams to publish. Default: $STREAM_COUNT
  -f, --fps FPS         Output FPS. Default: $FPS
  -p, --port PORT       RTSP server port. Default: $RTSP_PORT
  --prefix NAME         Stream path prefix. Default: $STREAM_PREFIX
  --work-dir DIR        Work/log directory. Default: $WORK_DIR
  --bitrate RATE        Preconverted H264 bitrate. Default: $BITRATE
  --scale WxH           Optional scale, for example 1280x720
  --no-preconvert       Stream source files directly and re-encode each publisher
  --no-nvenc            Use libx264 instead of h264_nvenc

Environment overrides:
  MEDIAMTX_BIN=/path/to/mediamtx
  PUBLIC_HOST=192.168.1.50
  PUBLISH_HOST=127.0.0.1
  VIDEO_DIR=/path/to/camera_recording
  STREAM_COUNT=40 FPS=8 RTSP_PORT=8554

Stop:
  Press Ctrl+C in this terminal.
EOF
}

log() {
    printf '[INFO] %s\n' "$*"
}

warn() {
    printf '[WARN] %s\n' "$*" >&2
}

die() {
    printf '[ERROR] %s\n' "$*" >&2
    exit 1
}

cleanup() {
    if (( ${#PIDS[@]} == 0 )) && [[ -z "${MEDIAMTX_PID:-}" ]]; then
        return
    fi
    log "Stopping RTSP stress streams..."
    for pid in "${PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
    if [[ -n "${MEDIAMTX_PID:-}" ]]; then
        kill "$MEDIAMTX_PID" 2>/dev/null || true
    fi
    wait 2>/dev/null || true
    log "Done."
}
trap cleanup EXIT INT TERM

parse_args() {
    VIDEOS=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -n|--streams)
                STREAM_COUNT="$2"
                shift 2
                ;;
            -f|--fps)
                FPS="$2"
                shift 2
                ;;
            -p|--port)
                RTSP_PORT="$2"
                shift 2
                ;;
            --prefix)
                STREAM_PREFIX="$2"
                shift 2
                ;;
            --work-dir)
                WORK_DIR="$2"
                shift 2
                ;;
            --bitrate)
                BITRATE="$2"
                shift 2
                ;;
            --scale)
                SCALE="$2"
                shift 2
                ;;
            --no-preconvert)
                PRECONVERT=0
                shift
                ;;
            --no-nvenc)
                USE_NVENC=0
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            --)
                shift
                while [[ $# -gt 0 ]]; do
                    VIDEOS+=("$1")
                    shift
                done
                ;;
            -*)
                die "Unknown option: $1"
                ;;
            *)
                VIDEOS+=("$1")
                shift
                ;;
        esac
    done

    if [[ "${#VIDEOS[@]}" -eq 0 ]]; then
        for filename in "${DEFAULT_VIDEO_FILES[@]}"; do
            VIDEOS+=("$VIDEO_DIR/$filename")
        done
    fi

    [[ "$STREAM_COUNT" =~ ^[0-9]+$ ]] || die "Stream count must be an integer."
    [[ "$FPS" =~ ^[0-9]+$ ]] || die "FPS must be an integer."
    [[ "$RTSP_PORT" =~ ^[0-9]+$ ]] || die "RTSP port must be an integer."

    for video in "${VIDEOS[@]}"; do
        [[ -f "$video" ]] || die "Video not found: $video"
    done
}

detect_public_host() {
    if [[ -n "$PUBLIC_HOST" ]]; then
        return
    fi
    PUBLIC_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
    if [[ -z "$PUBLIC_HOST" ]]; then
        PUBLIC_HOST="$PUBLISH_HOST"
    fi
}

find_mediamtx() {
    if [[ -n "$MEDIAMTX_BIN" ]]; then
        [[ -x "$MEDIAMTX_BIN" ]] || die "MEDIAMTX_BIN is not executable: $MEDIAMTX_BIN"
        return
    fi
    if command -v mediamtx >/dev/null 2>&1; then
        MEDIAMTX_BIN="$(command -v mediamtx)"
        return
    fi
    if [[ -x "./mediamtx" ]]; then
        MEDIAMTX_BIN="./mediamtx"
        return
    fi
    die "mediamtx not found. Install it or set MEDIAMTX_BIN=/path/to/mediamtx"
}

check_tools() {
    command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg not found."
    find_mediamtx
    mkdir -p "$WORK_DIR/logs" "$WORK_DIR/converted"
}

port_listening() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$RTSP_PORT" -sTCP:LISTEN >/dev/null 2>&1
        return
    fi
    if command -v ss >/dev/null 2>&1; then
        ss -ltn | awk '{print $4}' | grep -Eq "(:|\\])$RTSP_PORT$"
        return
    fi
    return 1
}

start_mediamtx() {
    if port_listening; then
        log "RTSP port $RTSP_PORT is already listening; using existing server."
        return
    fi

    log "Starting MediaMTX on :$RTSP_PORT"
    MTX_RTSPADDRESS=":$RTSP_PORT" "$MEDIAMTX_BIN" > "$WORK_DIR/logs/mediamtx.log" 2>&1 &
    MEDIAMTX_PID=$!

    for _ in $(seq 1 30); do
        if port_listening; then
            log "MediaMTX ready (PID $MEDIAMTX_PID)"
            return
        fi
        sleep 1
    done

    tail -40 "$WORK_DIR/logs/mediamtx.log" >&2 || true
    die "MediaMTX did not start on port $RTSP_PORT"
}

ffmpeg_has_nvenc() {
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -qw h264_nvenc
}

converted_path_for() {
    local idx="$1"
    printf '%s/converted/source_%02d_%sfps.mp4' "$WORK_DIR" "$idx" "$FPS"
}

build_filter() {
    local vf="fps=$FPS"
    if [[ -n "$SCALE" ]]; then
        vf="$vf,scale=$SCALE"
    fi
    printf '%s,format=yuv420p' "$vf"
}

preconvert_videos() {
    CONVERTED=()
    local encoder_args=()

    if [[ "$USE_NVENC" == "1" ]] && ffmpeg_has_nvenc; then
        log "Using NVIDIA NVENC for one-time 8 FPS conversion."
        encoder_args=(-c:v h264_nvenc -preset p4 -b:v "$BITRATE" -g "$((FPS * 2))")
    else
        warn "Using libx264 for conversion. Install NVENC-enabled ffmpeg for lower CPU."
        encoder_args=(-c:v libx264 -preset veryfast -tune zerolatency -b:v "$BITRATE" -g "$((FPS * 2))")
    fi

    local i=0
    for src in "${VIDEOS[@]}"; do
        i=$((i + 1))
        local out
        out="$(converted_path_for "$i")"
        CONVERTED+=("$out")
        if [[ -s "$out" ]]; then
            log "Using existing converted file: $out"
            continue
        fi
        log "Converting $src -> $out"
        ffmpeg -hide_banner -y \
            -i "$src" \
            -vf "$(build_filter)" \
            "${encoder_args[@]}" \
            -an "$out" \
            > "$WORK_DIR/logs/convert_${i}.log" 2>&1
    done
}

source_for_stream() {
    local stream_idx="$1"
    local source_count="$2"
    local source_idx=$(( (stream_idx - 1) % source_count ))
    if [[ "$PRECONVERT" == "1" ]]; then
        printf '%s' "${CONVERTED[$source_idx]}"
    else
        printf '%s' "${VIDEOS[$source_idx]}"
    fi
}

start_publishers() {
    local source_count="${#VIDEOS[@]}"
    log "Starting $STREAM_COUNT RTSP publishers at $FPS FPS"

    for i in $(seq 1 "$STREAM_COUNT"); do
        local stream_name src url log_file
        stream_name="$(printf '%s_%02d' "$STREAM_PREFIX" "$i")"
        src="$(source_for_stream "$i" "$source_count")"
        url="rtsp://$PUBLISH_HOST:$RTSP_PORT/$stream_name"
        log_file="$WORK_DIR/logs/${stream_name}.log"

        if [[ "$PRECONVERT" == "1" ]]; then
            ffmpeg -hide_banner -nostdin -re -stream_loop -1 \
                -i "$src" \
                -c:v copy -an \
                -f rtsp -rtsp_transport "$RTSP_TRANSPORT" "$url" \
                > "$log_file" 2>&1 &
        else
            local encoder_args=()
            if [[ "$USE_NVENC" == "1" ]] && ffmpeg_has_nvenc; then
                encoder_args=(-c:v h264_nvenc -preset p4 -b:v "$BITRATE" -g "$((FPS * 2))")
            else
                encoder_args=(-c:v libx264 -preset veryfast -tune zerolatency -b:v "$BITRATE" -g "$((FPS * 2))")
            fi
            ffmpeg -hide_banner -nostdin -re -stream_loop -1 \
                -i "$src" \
                -vf "$(build_filter)" \
                "${encoder_args[@]}" -an \
                -f rtsp -rtsp_transport "$RTSP_TRANSPORT" "$url" \
                > "$log_file" 2>&1 &
        fi

        PIDS+=("$!")
        sleep 0.15
    done
}

print_urls() {
    echo
    log "RTSP streams are publishing. Use these URLs from your app machine:"
    for i in $(seq 1 "$STREAM_COUNT"); do
        printf 'rtsp://%s:%s/%s_%02d\n' "$PUBLIC_HOST" "$RTSP_PORT" "$STREAM_PREFIX" "$i"
    done
    echo
    log "Logs: $WORK_DIR/logs"
    log "Press Ctrl+C to stop."
}

main() {
    parse_args "$@"
    detect_public_host
    check_tools
    start_mediamtx
    if [[ "$PRECONVERT" == "1" ]]; then
        preconvert_videos
    fi
    start_publishers
    print_urls

    while true; do
        sleep 5
        local alive=0
        for pid in "${PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                alive=$((alive + 1))
            fi
        done
        log "Publishers alive: $alive/$STREAM_COUNT"
        if (( alive == 0 )); then
            die "All publishers exited. Check $WORK_DIR/logs"
        fi
    done
}

main "$@"
