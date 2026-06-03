# Server Setup — Vehicle Zone Intelligence

Step-by-step deployment for the production host. Follow the sections **in
order**. Every command runs **on the server** (`172.27.6.226`).

### Assumptions

- OS: Ubuntu / Debian, and you have `sudo`.
- This repository is checked out on the server. Set a shortcut for its path:
  ```bash
  export APP_DIR=/opt/vehicle-detection      # adjust to where you cloned it
  cd "$APP_DIR"
  ```
- **The TLS certificate and key are already in place** with these exact names:
  - `/etc/ssl/certs/ramp-congestion.ghialunifiedapps.in.fullchain.pem`  (mode `0644`, root)
  - `/etc/ssl/private/ramp-congestion.ghialunifiedapps.in.key`          (mode `0640`, root)
- DNS: `ramp-congestion.ghialunifiedapps.in` resolves to `172.27.6.226`.
  Confirm before starting:
  ```bash
  nslookup ramp-congestion.ghialunifiedapps.in        # must return 172.27.6.226
  ```

### What you'll end up with

Only **HTTPS/443** (the dashboard via Nginx) is reachable from the LAN, limited
to `172.27.0.0/16`. The app, database, cache, and message broker are all locked
to the server itself. No default passwords.

---

## 1. Install system packages

```bash
sudo apt update
sudo apt install -y nginx ufw mosquitto mosquitto-clients \
    postgresql-16 redis-server python3-venv python3-pip iproute2 lsof
```

> TimescaleDB (the time-series add-on for PostgreSQL) is installed from its own
> APT repo — see the project README "Installation" if it isn't already present.
> Redis is upgraded to a supported version in **Step 3**.

---

## 2. PostgreSQL — database + least-privilege user (observation #5)

Create a dedicated application role and database. The app user is **not** a
superuser and can only touch its own database.

```bash
# Pick a strong, unique password and use the SAME value in Step 5 (.env).
sudo -u postgres psql <<'SQL'
CREATE ROLE vzi_app LOGIN PASSWORD 'REPLACE_WITH_STRONG_DB_PASSWORD';
CREATE DATABASE vehicle_zone OWNER vzi_app;
\c vehicle_zone
CREATE EXTENSION IF NOT EXISTS timescaledb;
SQL
```

If `CREATE EXTENSION timescaledb` reports it must be preloaded, add it (next
step also locks PostgreSQL to localhost), then re-run the `CREATE EXTENSION`
line above:

```bash
sudo tee /etc/postgresql/16/main/conf.d/zz-vehicle.conf >/dev/null <<'EOF'
listen_addresses = 'localhost'
shared_preload_libraries = 'timescaledb'
EOF
sudo systemctl restart postgresql      # adjust "16" to your PostgreSQL version
```

Load the schema (vzi_app owns the DB, so it may create the tables):

```bash
PGPASSWORD='REPLACE_WITH_STRONG_DB_PASSWORD' \
  psql -U vzi_app -h localhost -d vehicle_zone -f "$APP_DIR/schema.sql"
```

---

## 3. Redis — upgrade to a supported version + lock to loopback (#9, #3)

```bash
redis-server --version           # if 5.x, upgrade below

# Install current Redis (7.x) from the official repo:
sudo apt install -y lsb-release curl gpg
curl -fsSL https://packages.redis.io/gpg | sudo gpg --dearmor \
     -o /usr/share/keyrings/redis-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] \
https://packages.redis.io/deb $(lsb_release -cs) main" \
     | sudo tee /etc/apt/sources.list.d/redis.list
sudo apt update && sudo apt install -y redis
redis-server --version           # confirm 7.x
```

Lock Redis to the server only:

```bash
sudo tee -a /etc/redis/redis.conf < "$APP_DIR/deploy/hardening/redis-hardening.conf"
sudo systemctl restart redis-server
redis-cli ping                   # expect: PONG
```

---

## 4. Mosquitto (MQTT) — lock to loopback (#3)

```bash
sudo cp "$APP_DIR/deploy/hardening/mosquitto-loopback.conf" \
        /etc/mosquitto/conf.d/local-only.conf
sudo systemctl restart mosquitto
sudo ss -tlnp '( sport = :1883 )'    # should show 127.0.0.1:1883 only
```

---

## 5. Application secrets — create `deploy/.env` (#5, #6)

```bash
cp "$APP_DIR/deploy/.env.example" "$APP_DIR/deploy/.env"
chmod 600 "$APP_DIR/deploy/.env"
nano "$APP_DIR/deploy/.env"
```

Set in `deploy/.env`:
- `VZI_DB_PASSWORD` = the **same** strong password from Step 2.
- `VZI_CORS_ALLOW_ORIGINS` is already `https://ramp-congestion.ghialunifiedapps.in`.
- Leave the rest at defaults unless you set a Redis password (then uncomment
  `VZI_REDIS_URL` with the password).

The app refuses to start if `VZI_DB_PASSWORD` is unset — this is intentional.

---

## 6. Metis NPU reset helper + sudo rule (#8)

Install the root-owned reset helper and the narrow sudoers rule (grants
passwordless sudo for **only** this one command):

```bash
sudo install -o root -g root -m 0755 "$APP_DIR/deploy/metis-reset.sh" \
    /usr/local/sbin/metis-reset.sh

sed "s/__USER__/$USER/" "$APP_DIR/deploy/axelera-metis-nopasswd.in" \
  | sudo install -o root -g root -m 0440 /dev/stdin \
        /etc/sudoers.d/axelera-metis-nopasswd

sudo visudo -c                   # must report "parsed OK"
```

---

## 7. Nginx — the HTTPS front door (#2, #7)

The config is pre-filled for `ramp-congestion.ghialunifiedapps.in` and the cert
paths from the Assumptions section.

```bash
sudo cp "$APP_DIR/deploy/nginx/vehicle-dashboard.conf" \
        /etc/nginx/sites-available/vehicle-dashboard.conf
sudo ln -sf /etc/nginx/sites-available/vehicle-dashboard.conf \
            /etc/nginx/sites-enabled/vehicle-dashboard.conf
sudo rm -f /etc/nginx/sites-enabled/default      # remove the default site
sudo nginx -t                                    # must say: syntax is ok / successful
sudo systemctl reload nginx
```

---

## 8. Start the application

```bash
cd "$APP_DIR"
./start.sh
```

`start.sh` loads `deploy/.env`, binds the app to `127.0.0.1:8002` (reachable
only via Nginx), and starts the edge supervisor. Leave it running (or wrap it
in a service later). Watch the logs in `.logs/` if anything fails.

> The edge agent needs the Voyager SDK environment (`VOYAGER_PYTHON`,
> `SDK_ROOT`, etc.) — set those as on any existing host if the edge fails to
> launch.

---

## 9. Host firewall (#3, #4)

> ⚠️ This restricts SSH to `172.27.0.0/16`. Make sure you connect from that
> range and keep a console session open in case of a mistake.

```bash
sudo bash "$APP_DIR/deploy/firewall/ufw-setup.sh" --dry-run    # review the rules
sudo bash "$APP_DIR/deploy/firewall/ufw-setup.sh"              # apply
sudo ufw status verbose
```

---

## 10. Verify everything

```bash
# 1. No sensitive service is LAN-facing (run on the server):
sudo bash "$APP_DIR/deploy/verify-ports.sh"        # RESULT: OK

# 2. Dashboard is reachable over HTTPS with a valid chain
#    (run from a workstation inside 172.27.0.0/16):
curl -I https://ramp-congestion.ghialunifiedapps.in/health      # HTTP/2 200
openssl s_client -connect 172.27.6.226:443 \
    -servername ramp-congestion.ghialunifiedapps.in </dev/null | head -15
#    → no "unable to get local issuer certificate" error

# 3. From a SEPARATE LAN machine, confirm closed doors:
nmap -Pn -p 443,554,8002,8003,5432,6379,1883 172.27.6.226
#    → 443 open; everything else filtered/closed
```

Then open `https://ramp-congestion.ghialunifiedapps.in/` in a browser from an
allowed workstation — you should see the dashboard with live data and a valid
certificate (padlock).

---

## Appendix

### Port reference

| Port | Service | LAN-reachable? |
|------|---------|----------------|
| 443  | Dashboard via Nginx (HTTPS) | ✅ from `172.27.0.0/16` |
| 554  | RTSP ingest | ❌ (server dials *out* to cameras; inbound stays closed) |
| 8002 | FastAPI app | ❌ loopback only |
| 8003 | Snapshot service | ❌ loopback only |
| 5432 | PostgreSQL | ❌ loopback only |
| 6379 | Redis | ❌ loopback only |
| 1883 | Mosquitto MQTT | ❌ loopback only |
| 22   | SSH | ✅ from `172.27.0.0/16` |

### Certificate renewal

The wildcard cert expires **2026-12-12**. In late November, replace the two
files under `/etc/ssl/` with the renewed fullchain + key (same names) and run
`sudo systemctl reload nginx`.

### Access model (no login yet)

There is no per-user login. Access is controlled by the network allowlist
(`172.27.0.0/16`) at Nginx and the firewall. On the dashboard, the **server
console** (browsing from the host itself) can edit cameras/zones; everyone
arriving over the LAN is a **read-only viewer**. App-level login is a separate,
deferred item (#1).

### Common issues

- **502 Bad Gateway** → the app isn't running; check `./start.sh` and `.logs/`.
- **Browser cert warning** → the fullchain is missing the intermediate, or DNS
  points elsewhere; re-check the cert file and `nslookup`.
- **App won't start, "Database password is not set"** → `VZI_DB_PASSWORD`
  missing in `deploy/.env`.
- **Can't reach the dashboard** → you're outside `172.27.0.0/16`, or DNS is
  wrong.
