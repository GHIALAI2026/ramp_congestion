# Security Runbook — Host & Network Hardening

This runbook covers the **server/network** controls that ops applies on the
production host. It currently implements observations **#3, #4, and #9**.

> Other observations: **A–D** (Nginx, app auth/proxy, CORS, DB creds, Metis
> sudo, alert images) are implemented in code already. **#12 (EDR)** is pending
> a separate discussion. **#1 (login)** and **#10 (RTSP credentials)** are
> deferred.

Apply the steps in order. Keep an out-of-band console open during the firewall
step in case of an SSH mistake.

---

## TLS / Domain deployment (Nginx front door)

**Domain:** `ramp-congestion.ghialunifiedapps.in` → **server 172.27.6.226**
**Certificate:** GoDaddy wildcard `*.ghialunifiedapps.in` (expires **2026-12-12**).

1. **DNS** — ensure `ramp-congestion.ghialunifiedapps.in` resolves to
   `172.27.6.226` (A record on the internal DNS). Verify: `nslookup ramp-congestion.ghialunifiedapps.in`.

2. **Build the fullchain** (the issued `.pem` is leaf-only; append GoDaddy's
   intermediate bundle):
   ```bash
   cat 3294a6e444232d0e.pem gd_bundle-g2.crt > fullchain.pem
   ```

3. **Install cert + key on the server** with strict permissions:
   ```bash
   sudo install -o root -g root -m 0644 fullchain.pem \
       /etc/ssl/certs/ramp-congestion.ghialunifiedapps.in.fullchain.pem
   sudo install -o root -g root -m 0640 ramp.key \
       /etc/ssl/private/ramp-congestion.ghialunifiedapps.in.key
   ```
   Then securely delete any temporary copies of the private key.

4. **Set the CORS origin** — create `deploy/.env` from `deploy/.env.example`
   (already pre-filled with `VZI_CORS_ALLOW_ORIGINS=https://ramp-congestion.ghialunifiedapps.in`)
   and set a real `VZI_DB_PASSWORD`.

5. **Install + reload Nginx** (the config is pre-filled for this domain — see
   `deploy/nginx/vehicle-dashboard.conf`):
   ```bash
   sudo cp deploy/nginx/vehicle-dashboard.conf /etc/nginx/sites-available/vehicle-dashboard.conf
   sudo ln -s /etc/nginx/sites-available/vehicle-dashboard.conf /etc/nginx/sites-enabled/
   sudo rm -f /etc/nginx/sites-enabled/default
   sudo nginx -t && sudo systemctl reload nginx
   ```

6. **Verify** from an operator workstation (must be inside `172.27.0.0/16`):
   ```bash
   curl -I https://ramp-congestion.ghialunifiedapps.in/health     # HTTP/2 200, valid cert
   openssl s_client -connect 172.27.6.226:443 \
       -servername ramp-congestion.ghialunifiedapps.in </dev/null  # chain OK, no "local issuer" error
   ```

> **Renewal:** the cert expires **2026-12-12**. In late November, repeat steps
> 2–3 with the renewed files and `sudo systemctl reload nginx`.

---

## #3 — Only the intended ports face the LAN

**In plain terms:** every service listens on a numbered "door" (port). Only the
dashboard (443) and, where required, the camera RTSP port (554) should open
onto the office network. The app (8002), snapshot service (8003), database
(5432), Redis (6379), and MQTT (1883) must be reachable only from *inside* the
server. We achieve this two ways at once (defence in depth): **bind those
services to loopback**, and **block them at the firewall**.

> Note on RTSP/554: the server is the RTSP **client** — it dials *out* to the
> cameras. It does **not** need inbound 554 for normal ingestion. Only open
> inbound 554 if this host itself serves RTSP (e.g. a test streamer).

### 3a. Bind the sensitive services to loopback

**App (8002) and snapshot service (8003):** already done in code — both bind to
`127.0.0.1` (see `start.sh` / `edge_agent/http_server.py`). No action.

**Redis (6379):**
```bash
# Append the hardening directives, then restart.
sudo tee -a /etc/redis/redis.conf < deploy/hardening/redis-hardening.conf
sudo systemctl restart redis-server
```

**Mosquitto (1883):**
```bash
sudo cp deploy/hardening/mosquitto-loopback.conf /etc/mosquitto/conf.d/local-only.conf
sudo systemctl restart mosquitto
```

**PostgreSQL (5432):** ensure it listens on localhost only.
```bash
# In /etc/postgresql/<version>/main/postgresql.conf set:
#     listen_addresses = 'localhost'
# In pg_hba.conf, keep host entries restricted to 127.0.0.1/::1 only.
sudo systemctl restart postgresql
```

### 3b. Firewall the rest

```bash
# Edit the CONFIG block (subnets) first, then:
sudo bash deploy/firewall/ufw-setup.sh --dry-run   # review
sudo bash deploy/firewall/ufw-setup.sh             # apply
```

### 3c. Prove it (don't just assume)

```bash
sudo bash deploy/verify-ports.sh                   # on the server
# then, from a SEPARATE LAN machine:
nmap -Pn -p 443,554,8002,8003,5432,6379,1883 <SERVER_IP>
```
Expected: **443 open** (554 only if you serve RTSP); everything else
**filtered/closed**.

---

## #4 — Network segmentation (or its firewall equivalent)

**In plain terms:** ideally the cameras, the server, and the operator
workstations each live in their own walled-off section of the network (a
**VLAN**), so a problem in one can't spread to the others. If a laptop is
plugged into a camera port, it lands in the camera section and still can't
reach the server or database.

**Preferred — VLAN design** (network team):

| VLAN | Holds | Allowed to talk to |
|------|-------|--------------------|
| Camera VLAN | IP cameras | Server VLAN, RTSP 554 only |
| Server VLAN | This host (app, DB, Redis, MQTT) | Operator VLAN on 443; Camera VLAN on 554 |
| Operator VLAN | Dashboard workstations | Server VLAN on 443 only |

Inter-VLAN rules are enforced on the switch/router so only those flows are
permitted.

**Fallback — if VLANs aren't ready yet:** the **source-IP allowlist** does the
equivalent job on a flat network. It's already built into two places:
- `deploy/firewall/ufw-setup.sh` — restricts 443 to the operator subnet and
  SSH to the admin subnet.
- `deploy/nginx/vehicle-dashboard.conf` — `allow <operator subnet>; deny all;`.

Fill the same subnets into both and the dashboard is reachable only from the
approved range, even without VLANs.

---

## #9 — Upgrade Redis off the end-of-life 5.x line

**In plain terms:** Redis 5 is old and no longer receives security patches —
like a car model with no more recall parts. Move to a current, supported
release (7.x) that still gets fixes. The app talks to Redis the same way, so
no code change is needed.

```bash
redis-server --version          # check what you're on

# Ubuntu: install a current Redis from the official Redis APT repo
sudo apt-get install -y lsb-release curl gpg
curl -fsSL https://packages.redis.io/gpg | sudo gpg --dearmor \
     -o /usr/share/keyrings/redis-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] \
https://packages.redis.io/deb $(lsb_release -cs) main" \
     | sudo tee /etc/apt/sources.list.d/redis.list
sudo apt-get update
sudo apt-get install -y redis

redis-server --version          # confirm 7.x
```

After upgrading, re-apply the loopback hardening from **3a** (the config
directives are version-independent) and restart. The data in DB 1 (used by
this app) is preserved across an in-place upgrade, but take an RDB/AOF backup
first if the instance is shared.

> If you enable `requirepass` (recommended), set
> `VZI_REDIS_URL=redis://:<password>@localhost:6379/1` in `deploy/.env`.

---

## Post-change checklist

- [ ] `sudo bash deploy/verify-ports.sh` → RESULT: OK
- [ ] `nmap` from a LAN machine shows only 443 (and 554 if applicable)
- [ ] `redis-server --version` is 7.x
- [ ] Dashboard reachable over HTTPS from an operator machine; not from outside
      the allowlisted subnet
- [ ] App still starts cleanly (`./start.sh`) and the dashboard shows live data
