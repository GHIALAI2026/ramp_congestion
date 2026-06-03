#!/usr/bin/env bash
# ==========================================================================
# Port exposure verification (security observation #3)
# ==========================================================================
# Confirms that the sensitive services are NOT bound to a LAN-facing address,
# and reminds you to confirm the same from a separate LAN machine.
#
# Run this on the server AFTER applying the firewall + hardening configs:
#   sudo bash deploy/verify-ports.sh
#
# Exit code is non-zero if any sensitive service is found listening on a
# non-loopback address, so it can be used in CI / pre-go-live checks.
# ==========================================================================
set -uo pipefail

# Services that must NOT be reachable from the LAN.
SENSITIVE_PORTS="8002 8003 5432 6379 1883"
# Ports that MAY face the LAN (allowlisted by the firewall).
LAN_OK_PORTS="443 554 22"

if ! command -v ss >/dev/null 2>&1; then
    echo "ERROR: 'ss' not found (install iproute2)." >&2
    exit 2
fi

echo "=== Listening TCP sockets ==="
ss -tlnH | awk '{print $4}' | sort -u
echo ""

fail=0
echo "=== Sensitive services must be loopback-only ==="
for port in $SENSITIVE_PORTS; do
    # Addresses currently listening on this port.
    addrs=$(ss -tlnH "( sport = :$port )" | awk '{print $4}')
    if [[ -z "$addrs" ]]; then
        echo "  [ -- ] :$port    not listening"
        continue
    fi
    bad=0
    while read -r a; do
        [[ -z "$a" ]] && continue
        # OK if bound to loopback (127.0.0.1 / ::1 / [::1]).
        case "$a" in
            127.0.0.1:*|\[::1\]:*|::1:*) : ;;
            *) bad=1 ;;
        esac
    done <<< "$addrs"
    if [[ $bad -eq 1 ]]; then
        echo "  [FAIL] :$port    LAN-FACING -> $(echo "$addrs" | tr '\n' ' ')"
        fail=1
    else
        echo "  [ OK ] :$port    loopback-only"
    fi
done

echo ""
echo "=== Remote confirmation (run from a SEPARATE machine on the LAN) ==="
echo "  Replace <SERVER_IP> with this host's LAN IP:"
echo "    nmap -Pn -p 443,554,8002,8003,5432,6379,1883 <SERVER_IP>"
echo "  Expected: 443 open (and 554 only if you serve RTSP); all others"
echo "  'filtered' or 'closed'. Anything else means a door is still open."
echo ""

if [[ $fail -ne 0 ]]; then
    echo "RESULT: FAIL — a sensitive service is LAN-facing. Bind it to 127.0.0.1"
    echo "        (see deploy/hardening/) and/or fix the firewall."
    exit 1
fi
echo "RESULT: OK — no sensitive service is LAN-facing on this host."
