# Vehicle Zone Intelligence — User Manual
### Airport Arrival Ramp Monitoring Dashboard

---

## About this manual

This manual documents every screen and every parameter of the **Vehicle Zone
Intelligence** dashboard — the web application that monitors vehicle occupancy,
dwell time, and alerts across the airport arrival ramp.

It is organised into two parts:

| Part | Audience | Covers |
|------|----------|--------|
| **Part A — User (Operations)** | Operators / ramp controllers | Overview, Live View, Alerts, Analytics, the notification bell, and the global shell. Day‑to‑day monitoring. |
| **Part B — Admin (Configuration)** | Administrators on the server console | Cameras, Zones, Edge Status, and the Live Data Feed. System setup and health. |

> Every red box / ellipse in the screenshots marks a parameter. The number in the
> red tag matches the numbered explanation underneath the image.

### How to open the dashboard

Open a browser and go to:

```
https://localhost/dashboard
```

The application opens on the **Overview** page. You can jump straight to a page by
adding its name after a `#`, for example `https://localhost/dashboard#alerts`.

### Two access levels (Admin vs Viewer)

The dashboard decides what you can see from **where you connect**:

- **Admin** — the browser running **on the server console (localhost)**. Sees
  everything, including the **Configuration** pages (Cameras, Zones, Edge Status)
  and the **Data Feed**.
- **Viewer** — any other client on the network. Sees the **Operations** pages
  only. The Configuration section and the Data Feed are hidden from the sidebar
  and blocked even if reached by direct link.

This is why the **Data Feed** is documented in Part B (Admin): it is an
admin‑only diagnostic surface, even though the page sits under the "Operations"
heading in the sidebar.

---

## The global shell (header & navigation)

Every page shares the same frame: a left **navigation rail** and a top **header
bar**.

![Global shell — header and navigation](./images/01-shell.png)

**Header bar (top):**

1. **Brand logo** — airport / operator branding (Rajiv Gandhi International
   Airport · GMR Aero Enterprise). Clicking it does nothing; it is an identifier.
2. **Page / system title** — "Airport Arrival Ramp Monitoring". Fixed banner for
   the whole deployment.
3. **Connection state** — live WebSocket status. **Live** (green dot) = real‑time
   data is streaming; **Connecting** = trying to (re)connect; red = connection
   error. If this is not "Live", numbers on the page may be stale.
4. **Clock** — the operator workstation's local time, updated every second.
5. **Notification bell** — opens the *Recent Alerts* drawer (see
   [Notification bell & drawer](#notification-bell--drawer)). A red **badge** on
   the bell shows the number of **unacknowledged** alerts (shows `99+` above 99).
   The bell gently pulses while unacknowledged alerts exist.

**Navigation rail (left) — Operations:**

6. **Overview** — ramp‑wide KPIs and the live occupancy map.
7. **Live View** — annotated camera streams.
8. **Data Feed** — raw message stream *(admin‑only; hidden for viewers)*.
9. **Alerts** — the full operational alert log.
10. **Analytics** — historical alert trends.

**Navigation rail (left) — Configuration** *(admin‑only; the whole section is
hidden for viewers):*

11. **Cameras** — RTSP camera sources.
12. **Zones** — ramp geometry, capacity and dwell rules.
13. **Edge Status** — edge device telemetry.

> On phones the rail collapses into a **☰ hamburger** menu, and the
> Configuration entries are hidden regardless of access level.

---

# Part A — User (Operations)

---

## 1. Overview

`#overview` — the default landing page. It answers "how busy is the ramp right
now, and is anything wrong?" at a glance.

### 1.1 Key Performance Indicators (the five cards)

![Overview KPI cards](./images/02-overview-kpis.png)

**R. Refresh** — forces an immediate re‑fetch of all overview data. (The page
also updates itself live over the WebSocket; this button is for an on‑demand
pull.)

1. **Total Vehicles** *(blue)* — the total number of vehicles currently detected
   across **all active zones**. Multi‑camera zone *groups* are summed once, so a
   single physical zone watched by four cameras is not counted four times.
2. **Near Capacity** *(amber when > 0, else green)* — how many zones are **above
   50 % occupancy but still within their limit** (the "Busy"/yellow tiles on the
   map below). A zone counted here is *not* also counted as Over Capacity.
3. **Over Capacity** *(red when > 0, else green)* — how many zones have **more
   vehicles than their Max Vehicles threshold** (strictly over the limit). These
   are the conditions that raise *overcrowding* alerts.
4. **Overstay** *(amber when > 0, else green)* — the total number of vehicles that
   have **exceeded their zone's dwell‑time threshold** (parked / waiting too
   long). These raise *overstay* alerts.
5. **Unacknowledged Alerts** *(red when > 0, else green)* — the count of alerts
   still **awaiting acknowledgement**. This card is **clickable** — click it to
   jump straight to the **Alerts** page. The same number drives the header bell
   badge.

> **Colour logic for occupancy throughout the app:** ≤ 50 % = **green** (Calm),
> > 50 % up to the limit = **amber** (Busy), and strictly over the limit =
> **red** (Over capacity).

### 1.2 Ramp Occupancy Map

A live floor plan of the arrival ramp. The top row is the **OUTER** ramp
(OUTER‑6 → OUTER‑1, left to right); the bottom row is the **INNER** ramp
(INNER‑6 → INNER‑1). Each slot shows a tile ("chip") with the live count.

![Ramp Occupancy Map](./images/03-overview-map.png)

Each chip shows **`<current count> / <Max Vehicles>`** and a small fill bar, and
is colour‑coded by occupancy:

- **R — Red chip (Over capacity):** count is over the zone's limit
  (e.g. `OUTER‑6 12 / 10`). Matches the *Over Capacity* KPI and overcrowding
  alerts.
- **A — Amber chip (Busy):** above 50 % but still within the limit. Matches the
  *Near Capacity* KPI.
- **G — Green chip (Calm):** at or below 50 % occupancy.
- **M — Muted / grey dashed chip (`—`):** a ramp slot that exists on the floor
  plan but has **no monitored zone** mapped to it. Shown so the plan stays
  visually complete. (Hover any chip for an exact `count/max (percent)` tooltip.)

> A zone only appears on this map if its **Map slot** name is exactly one of
> `OUTER‑1`…`OUTER‑6` or `INNER‑1`…`INNER‑6` (case‑sensitive). See
> [Zones](#8-zones-admin) for how that is configured.

---

## 2. Live View

`#live` — annotated camera streams with edge‑side overlays and per‑camera health.

![Live View](./images/04-live.png)

1. **Camera selector** — choose which camera to watch. Each entry shows the
   camera name and the zones it covers, e.g. `cam 1 (INNER‑1, INNER‑2)`.
2. **Stream title** — the friendly name of the selected camera (e.g. `cam 1`).
3. **Stream meta** — the camera's technical ID and the edge runtime it is
   assigned to, e.g. `arrival_cam_1 · vehicle-edge-01`.
4. **Camera status chip** — `online` (green) / `offline` / `error` /
   `warning`, reflecting whether the edge is currently receiving frames.
5. **Annotated video frame** — the live picture with the edge's analytics drawn
   on top: **zone polygons** (the shaded/green outline of each monitored area)
   and **detection boxes** around each detected vehicle.

**Side panel — "Zones & counts"** (right). A live roll‑up for the selected
camera:

6. **Panel heading** — "Zones & counts".
7. **Per‑camera metrics**, each on its own row:
   - **Zones on this camera** — how many monitored zones this camera covers.
   - **Vehicles in view** — total vehicles currently detected across those zones
     (green when > 0).
   - **Active overstays** — vehicles over their dwell threshold right now (amber
     when > 0).
   - **Avg dwell (peak zone)** — the average dwell time of the busiest zone on
     this camera, e.g. `6m 50s`.
   - **Overcrowding** — **YES** (red) if any zone on this camera is over its
     limit, otherwise **no** (grey).

---

## 3. Alerts

`#alerts` — the complete operational alert log across all ramp zones. This is the
main page operators act from. The screen has three parts: summary cards, a filter
toolbar, and the alert list.

### 3.1 Summary cards

![Alerts summary cards](./images/05-alerts-stats.png)

These reflect the **full** set matching the current type/zone filter (not just the
visible page), so the severity counts are always true.

1. **Critical** *(red)* — number of critical‑severity alerts.
2. **Warning** *(amber)* — number of warning‑severity alerts.
3. **Acked today** *(green)* — number of alerts that have been acknowledged.
4. **Total in view** — total number of alerts matching the current filters.

**R. Refresh** (top‑right of the page) re‑pulls the alert log from the server.

### 3.2 Filter toolbar

![Alerts filter toolbar](./images/06-alerts-toolbar.png)

1. **Search** — free‑text search over message text, zone, camera, and vehicle ID.
2. **Type filter** — *All types*, **Overcrowding**, **Overstay**, or
   **Camera offline**.
3. **Severity filter** — *All severities*, **Critical only**, or **Warning only**.
   (Picking a severity also sorts critical‑first; otherwise the list is
   newest‑first.)
4. **Zone filter** — limit to a single zone/zone‑group by its friendly label
   (e.g. `OUTER‑5`). The list is built from your configured zone names.
5. **Time filter** — *All time*, or the last **3 / 6 / 12 / 24 hours**.
6. **Unacknowledged only** — checkbox (on by default) to hide alerts that have
   already been acknowledged.
7. **Per page** — page size for the list: **10 / 50 / 100** (default 50).
8. **Acknowledge all visible** — acknowledges every un‑acknowledged alert in the
   current filtered view in one click. The button shows the count
   (e.g. *Acknowledge all visible (500)*) and is disabled when there is nothing
   to acknowledge.

### 3.3 Anatomy of an alert row

![A single alert row](./images/07-alert-item.png)

1. **Severity badge** — **CRITICAL** (red left border) or **WARNING** (amber).
   Acknowledged rows are dimmed and lose their colour accent.
2. **Message headline** — always led by the live **zone label**, then a
   plain‑language description:
   - *Overstay* → `… — Overstay 15m 0s in zone` (and `, N over limit` when past
     the threshold).
   - *Overcrowding* → `… — Overcrowding — N vehicles in zone`.
   - *Camera offline/recovered* → the maintenance message verbatim.
3. **View image** — opens the **evidence photo** for this alert in a new tab (a
   vehicle snapshot for overstay, or a frame snapshot for overcrowding). Shown
   only when an image exists.
4. **Time** — how long ago the alert fired (`5m ago`, `2h ago`, …). Hover for the
   exact timestamp.
5. **Zone chip** *(blue)* — the zone (or zone group) the alert belongs to. Hover
   to see the raw zone ID.
6. **Vehicle chip** *(orange)* — the tracked vehicle's ID, e.g. `vehicle #25`
   (present when the alert is tied to a specific vehicle).
7. **Arrived chip** — *overstay only* — when the vehicle entered the zone, e.g.
   `arrived 12:56`.
8. **Acknowledge** — marks this single alert as handled. The row then dims and
   shows `✓ Acknowledged`, the bell badge and *Unacknowledged Alerts* KPI drop by
   one. (Acknowledging never deletes an alert; it just clears it from the active
   queue.)

> **Toasts.** When a new alert arrives, a pop‑up toast slides in at the top‑right
> (up to three at once, auto‑dismiss after ~5 s, red for critical / amber for
> warning). Click a toast to open the notification drawer.

### 3.4 When does an alert fire? (timing & frequency)

Alerts are **not** raised the instant a number crosses a line — both alert
families use deliberate timing so a brief blip does not spam the log, and a
sustained problem keeps escalating.

**Overcrowding (per zone / zone-group):**

- A zone must stay **over its Max Vehicles limit continuously for 2 minutes**
  before the **first** overcrowding alert fires. A spike that clears within
  2 minutes raises nothing.
- While the zone stays over the limit, a **repeat** alert fires **every
  15 minutes**.
- The moment the count drops back to / below the limit, the timer resets — the
  next breach must again be sustained for 2 minutes before it alerts.

| Over-limit duration | Alert |
|---|---|
| 0 - 2 min | (none - debounce) |
| 2 min | 1st alert |
| 17 min | 2nd alert |
| 32 min | 3rd alert |
| every +15 min while still over | 4th, 5th ... |

**Overstay (per vehicle - an escalating "milestone ladder"):**

Overstay milestones are multiples of the zone's **Max Dwell** threshold **T**
(default `T = 15 min`). Each milestone fires **exactly once per visit**, carrying
the vehicle's current (growing) dwell time, and escalates in severity. Because
they scale with T, the cadence stays correct if you reconfigure a zone's dwell
limit.

| # | Fires at | Default (T = 15 min) | Severity |
|---|---|---|---|
| 1st | 1 x T | 15 min | warning |
| 2nd | 2 x T | 30 min | warning |
| 3rd | 4 x T | 60 min | critical |
| 4th | 8 x T | 120 min | critical |
| 5th | 12 x T | 180 min | critical |
| ... | every +4 x T | every +60 min (hourly) | critical |

> Notes: with the defaults there is a deliberate gap between the 30-minute
> warning and the 60-minute first critical (nothing fires at 45 min). After the
> first critical, criticals repeat hourly for as long as the vehicle stays. The
> ladder resets when the vehicle leaves the zone, and the milestone already
> reached is remembered (even across an edge restart) so a step never re-fires.

---

## 4. Analytics

`#analytics` — recorded alert activity over time. Use it to spot patterns
(busy hours, repeat‑offender zones).

### 4.1 Controls and the trend chart

![Analytics controls and chart](./images/08-analytics-chart.png)

1. **Range** — the time window and bar granularity: **Last hour**, **Last 24h**
   (hourly bars), **Past 7 days**, **Past 30 days** (daily bars).
2. **Type** — which alert family to chart: **Zone capacity exceeding**
   (overcrowding) or **Vehicle overstay**.
3. **Chart title** — names the active series (e.g. *Zones exceeding capacity*).
4. **Total** — total alert count in the selected window and series
   (e.g. `170 alerts`).
5. **Bar chart** — one bar per hour or day. Bar height = alert count for that
   bucket (value printed above each bar). Y‑axis = *Alerts*, X‑axis = *Hour* (or
   *Day*).

### 4.2 Top zones

![Analytics top zones](./images/09-analytics-top.png)

6. **Panel title** — "Top zones".
7. **Subtitle** — how many zones are ranked, sorted by alert count.
8. **Zone filter dropdown** — restrict the whole Analytics view to one zone
   (or *All zones*).
9. **Ranked zone row** — for each zone: its **rank and name** (`1. OUTER‑1`), the
   underlying **zone ID and share of total** (`arrival_zone_24 · 41% of total`),
   and the **alert count** for the window (red number on the right). A pager
   appears beneath the list when there are more than ten zones.

---

## Notification bell & drawer

Available from the header on every page. Click the **🔔 bell** to slide in the
*Recent Alerts* drawer.

![Notification drawer](./images/17-drawer.png)

1. **Drawer title** — "Recent Alerts".
2. **Subtitle** — "Latest overcrowding and overstay signals."
3. **Recent alert card** — the most recent alerts (up to 15), each with its
   severity badge, headline, **View image** button, and type · time. This is a
   read‑only quick‑look; it mirrors the Alerts page.
4. **View all alerts →** — closes the drawer and opens the full **Alerts** page.

Close the drawer with the **✕** button or by clicking the dimmed background.

---

# Part B — Admin (Configuration)

> These pages are visible only to the **admin** console (localhost). They change
> system configuration and expose raw diagnostics. Edits here directly affect
> what the edge runtime detects and what alerts fire.

---

## 5. Cameras

`#cameras` — the RTSP camera sources assigned to the vehicle edge runtime.

![Cameras table](./images/10-cameras.png)

**R. Add Camera** — opens the *Add Camera* form (below).

Table columns:

1. **Camera ID** — the unique technical identifier (e.g. `arrival_cam_1`). Used
   everywhere else to link zones and alerts to this camera.
2. **Name** — the friendly label shown to operators (e.g. `cam 1`).
3. **Source** — the **RTSP URL** the edge pulls frames from (credentials included;
   truncated for display — hover for the full string).
4. **Resolution / FPS** — the negotiated stream resolution and frame rate, e.g.
   `1920×1080 · 15.0 fps`. Shows `—` until the edge reports it.
5. **Status** — `online` (green) when the edge is decoding the stream, otherwise
   `offline` / `error`.
6. **Delete** — removes the camera from the system. (Zones bound to it lose their
   feed — delete with care.)

### 5.1 Add Camera form

![Add Camera modal](./images/11-camera-modal.png)

1. **Camera ID** *(required)* — unique ID, e.g. `cam-01`. Cannot clash with an
   existing camera.
2. **Name** — friendly display name, e.g. `Arrival Ramp 1`.
3. **RTSP URL** *(required)* — the full stream URL, e.g.
   `rtsp://user:pass@10.0.0.5:554/stream1`.
4. **Assigned Edge** — which edge runtime will run this camera
   (e.g. `vehicle-edge-01`).
5. **Add Camera** — saves the camera and pushes it to the edge. (**Cancel**
   discards.)

---

## 6. Zones

`#zones` — ramp geometry, occupancy thresholds, and dwell rules. A *zone* is a
polygon drawn on one camera's view; vehicles inside it are counted and timed.

![Zones table](./images/12-zones.png)

**R. Add Zone** — opens the zone editor (below).

Table columns:

1. **Zone ID** — unique technical identifier (e.g. `arrival_zone_1`).
2. **Name** — friendly label (e.g. `INNER‑2`).
3. **Map slot** — which slot on the Overview Ramp Map this zone feeds:
   - a green `OUTER‑n` / `INNER‑n` = renders on the map at that slot;
   - **`— not mapped`** (grey italic) = the name doesn't match any floor‑plan
     slot, so it won't appear on the map;
   - **`⚠ <slot>`** (red) = two zones claim the same slot — only one will render.
4. **Ramp Side** — **Inner ramp** or **Outer ramp**.
5. **Camera** — the camera this zone is drawn on (e.g. `arrival_cam_1`).
6. **Max Vehicles** — the occupancy threshold. Exceeding it = over capacity →
   overcrowding alert.
7. **Max Dwell** — the dwell‑time threshold (e.g. `15m 0s`). A vehicle staying
   longer = overstay alert.
8. **Status** — `online` (zone active) or `offline` (inactive).
9. **Edit / Delete** — open the editor for this zone, or remove it.

### 6.1 Add / Edit Zone editor

![Add Zone modal](./images/13-zone-modal.png)

1. **Zone ID** *(required)* — unique ID, e.g. `zone-arrival-ramp-1`.
2. **Name** — friendly label. To appear on the Ramp Map, name it exactly
   `OUTER‑1`…`OUTER‑6` or `INNER‑1`…`INNER‑6`.
3. **Camera** — the camera whose view you'll draw on. Selecting it loads a live
   snapshot into the drawing canvas (10).
4. **Group (optional)** — attach this zone to a **zone group** so several cameras
   covering the *same* physical area are aggregated and alert once. Use **+ New**
   to create a group (Name, Group ID, and an optional **group Max Vehicles** that
   fires a single overcrowding alert for the summed count) or **Edit** to change
   the selected one.
5. **Ramp Side** — **Inner ramp** / **Outer ramp**.
6. **Max Vehicles** — occupancy threshold (default 20).
7. **Max Dwell (s)** — dwell threshold in **seconds** (default 900 = 15 min).
8. **Zone Polygon JSON** — the polygon coordinates. Read‑only; it is generated
   automatically as you draw on the canvas.
9. **Polygon tools** — **Finish Polygon** (close the shape), **Undo Point**
   (remove the last vertex), **Clear Polygon** (start over), **Reload Snapshot**
   (refresh the camera still).
10. **Drawing canvas** — click on the camera snapshot to drop polygon points and
    outline the zone. (Drawing needs a mouse and a wide screen — it is disabled
    on mobile.)
11. **Create Zone / Save** — writes the zone and pushes the new geometry to the
    edge. (**Cancel** discards.)

## 7. Live Data Feed

`#feed` — the raw edge‑to‑cloud MQTT messages flowing through the dashboard
WebSocket. A diagnostic / verification tool (admin‑only).

![Live Data Feed](./images/15-feed.png)

**Statistics (top):**

1. **Messages/sec** — current throughput (averaged over a 5‑second window).
2. **Total Messages** — cumulative messages received this session.
3. **Total Data** — cumulative bytes received.
4. **Avg Size** — average message size (Total Data ÷ Total Messages).

**Toolbar:**

5. **Pause / Resume** — freezes the live list so you can inspect a message
   (statistics keep counting). The button turns amber when paused.
6. **Clear** — empties the list and resets the session counters.
7. **Export** — downloads the current buffered messages as a timestamped JSON
   file.
8. **Filter** — free‑text filter over message type, topic, and content.

**Message table:**

9. **Column headers** — **Time** (HH:MM:SS.mmm), **Type**, **Topic**, **Size**,
   **Preview**.
10. **Message row** — one MQTT message. The **Type** badge is colour‑coded:
    `zone_metric` (blue, per‑zone counts), `overview` (green, ramp‑wide rollup),
    `alert` (red), `subscribed/connected` (system), and muted greys for
    disconnect/error events. The **Preview** summarises the payload — for a
    `zone_metric`: `V:<vehicles> D:<avg dwell> Over:<overstays>`. Click any row to
    inspect it.

### 7.1 Message Detail

Click a row (pause first to stop the list scrolling) to see the full message on
the right.

![Message Detail](./images/16-feed-detail.png)

1. **Time** — exact arrival time of the message.
2. **Size** — message size in bytes.
3. **Type** — the message type badge (e.g. `zone_metric`).
4. **Topic** — the MQTT topic, e.g. `zone/arrival_zone_3b`.
5. **Full Payload** — the complete decoded JSON. For a `zone_metric` this
   includes the edge ID, camera ID, timestamp, `vehicle_count`,
   `vehicle_count_by_type`, `max_vehicles`, `occupancy_pct`, `overstay_count`,
   `avg_dwell_time_s`, `max_dwell_time_s`, `total_entered`, and more — the raw
   numbers that drive every other screen in the dashboard.

---

## Appendix — quick reference

### Alert types

| Type | Raised when | Severity |
|------|-------------|----------|
| **Overcrowding** | A zone's vehicle count exceeds its **Max Vehicles** | warning / critical |
| **Overstay** | A vehicle stays longer than the zone's **Max Dwell** | warning / critical |
| **Camera offline** | The edge stops receiving frames from a camera | warning / critical |
| **Camera recovered** | A previously offline camera resumes | info |

### Occupancy colour bands (zones / map / KPIs)

| Band | Condition | Colour |
|------|-----------|--------|
| Calm | occupancy ≤ 50 % | green |
| Busy / Near capacity | > 50 % and within the limit | amber |
| Over capacity | count strictly over Max Vehicles | red |

### Edge resource colours

| Utilisation | Colour |
|-------------|--------|
| < 55 % | green |
| 55 – 85 % | amber |
| ≥ 85 % | red |

### Access summary

| Page | Operator (LAN viewer) | Admin (server console) |
|------|:---:|:---:|
| Overview, Live View, Alerts, Analytics | ✅ | ✅ |
| Data Feed | ❌ | ✅ |
| Cameras, Zones, Edge Status | ❌ | ✅ |
