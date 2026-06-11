#!/usr/bin/env bash
# One-shot broker repair for this host (Redis 6.0.16 + co-hosted ApexEdge).
# Run once:   sudo bash deploy/fix-brokers.sh
# Fixes:
#   - Redis: the hardening drop-in's `bind ... -::1` uses the `-` optional-bind
#     prefix that only exists in Redis >= 6.2; this box is 6.0.16, so it
#     crash-loops. Replace any `-::1` with `::1`.
#   - Mosquitto: remove our loopback drop-in that collided with ApexEdge's
#     existing `listener 1883` (two listeners on one port -> start failure).
#   - Both: clear the systemd start-limit ("start request repeated too quickly")
#     that blocks plain restarts, then start and verify.
set -uo pipefail

echo "==> Redis: neutralizing unsupported '-::1' bind directive"
sed -i 's/-::1/::1/g' /etc/redis/redis.conf
echo "    bind lines now:"; grep -n '^bind' /etc/redis/redis.conf | sed 's/^/      /'

echo "==> Mosquitto: removing conflicting loopback drop-in (keep ApexEdge's listener)"
rm -f /etc/mosquitto/conf.d/local-only.conf
echo "    conf.d now:"; ls /etc/mosquitto/conf.d/ | sed 's/^/      /'

echo "==> Clearing systemd start-limit and starting both"
systemctl reset-failed redis-server mosquitto
systemctl restart redis-server
systemctl restart mosquitto

# Give them a moment to bind
sleep 2

echo
echo "==================== RESULT ===================="
echo "redis-server : $(systemctl is-active redis-server)    ping: $(redis-cli ping 2>&1)"
echo "mosquitto    : $(systemctl is-active mosquitto)"
echo "listeners    :"
ss -tln | grep -E ':(1883|6379)\b' | sed 's/^/   /' || echo "   (none on 1883/6379)"
echo "================================================"
echo
echo "If either still shows 'failed', the next two lines show why:"
echo "--- redis ---";     journalctl -u redis-server --no-pager -n 3 | tail -3
echo "--- mosquitto ---"; journalctl -u mosquitto     --no-pager -n 3 | tail -3
