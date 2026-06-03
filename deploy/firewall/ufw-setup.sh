#!/usr/bin/env bash
# ==========================================================================
# Host firewall setup — Vehicle Zone Intelligence server
# ==========================================================================
# Implements the host-firewall + source-IP allowlist control for:
#   - observation #3: only TCP/443 (and TCP/554 where required) reachable
#                     from the LAN; 8002/8003/5432/6379/1883 NOT reachable.
#   - observation #4: source-IP allowlisting as the equivalent control when
#                     full VLAN segregation is not yet in place.
#
# This uses ufw (Uncomplicated Firewall), standard on Ubuntu. It sets a
# default-deny posture for INCOMING traffic and then opens ONLY the doors
# that should face the LAN, restricted to the subnets you list below.
#
# Outbound traffic stays allowed, so the server can still dial OUT to the
# cameras (it is the RTSP client — it connects to rtsp://<camera>:554; it does
# NOT need inbound 554 for that). See SERVE_RTSP below.
#
#  ┌──────────────────────────────────────────────────────────────────────┐
#  │  READ THIS FIRST — you can lock yourself out of SSH.                    │
#  │  Set ADMIN_SSH_CIDR correctly and keep a console/out-of-band session    │
#  │  open while you run this, in case a rule is wrong.                       │
#  └──────────────────────────────────────────────────────────────────────┘
#
# Usage:
#   1. Edit the CONFIG block below (replace every __PLACEHOLDER__).
#   2. Review:   sudo bash deploy/firewall/ufw-setup.sh --dry-run
#   3. Apply:    sudo bash deploy/firewall/ufw-setup.sh
#   4. Verify:   sudo bash deploy/verify-ports.sh
# ==========================================================================
set -euo pipefail

# ------------------------------- CONFIG -----------------------------------
# Subnet your administrators SSH in from (KEEP THIS RIGHT — lockout risk).
# Set to the operator network by default; narrow it to your admin subnet if
# SSH should be more restricted than dashboard access.
ADMIN_SSH_CIDR="172.27.0.0/16"               # server is 172.27.6.226

# Subnet of the operator workstations allowed to open the dashboard (443).
OPERATOR_CIDR="172.27.0.0/16"

# Only set to "yes" if THIS host itself serves RTSP on 554 (e.g. a test
# streamer). For normal camera ingestion the server is the CLIENT and this
# must stay "no" — outbound connections to cameras are already allowed.
SERVE_RTSP="no"
CAMERA_CIDR="__CAMERA_CIDR__"                # only used when SERVE_RTSP="yes"

SSH_PORT="22"
# --------------------------------------------------------------------------

DRY_RUN="no"
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN="yes"

run() {
    echo "+ $*"
    [[ "$DRY_RUN" == "yes" ]] || "$@"
}

# Refuse to run with unfilled placeholders — a wrong/empty CIDR could expose
# the dashboard to everyone or lock out SSH.
for v in ADMIN_SSH_CIDR OPERATOR_CIDR; do
    if [[ "${!v}" == *"__"* || -z "${!v}" ]]; then
        echo "ERROR: $v is not set. Edit the CONFIG block first." >&2
        exit 1
    fi
done
if [[ "$SERVE_RTSP" == "yes" && ( "$CAMERA_CIDR" == *"__"* || -z "$CAMERA_CIDR" ) ]]; then
    echo "ERROR: SERVE_RTSP=yes but CAMERA_CIDR is not set." >&2
    exit 1
fi

echo "=== ufw firewall setup (dry-run=$DRY_RUN) ==="

# Baseline: deny incoming, allow outgoing (so we can still reach cameras/DNS).
run ufw default deny incoming
run ufw default allow outgoing

# SSH first, restricted to admins — opened BEFORE enabling, to avoid lockout.
run ufw allow from "$ADMIN_SSH_CIDR" to any port "$SSH_PORT" proto tcp comment "admin SSH"

# Dashboard over HTTPS via Nginx — operators only (observations #3, #4).
run ufw allow from "$OPERATOR_CIDR" to any port 443 proto tcp comment "dashboard HTTPS"

# RTSP inbound only if this host serves streams (uncommon).
if [[ "$SERVE_RTSP" == "yes" ]]; then
    run ufw allow from "$CAMERA_CIDR" to any port 554 proto tcp comment "RTSP ingest"
fi

# Everything else stays denied by the default policy. The sensitive services
# (8002/8003/5432/6379/1883) are additionally bound to loopback (see
# deploy/hardening/*), so they are doubly unreachable from the LAN.

run ufw --force enable
run ufw status verbose

echo "=== done. Now run: sudo bash deploy/verify-ports.sh ==="
