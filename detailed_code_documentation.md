# Vehicle Zone Intelligence: Exhaustive Codebase Documentation

This document provides a line-by-line, comprehensive walkthrough of the Vehicle Zone Intelligence codebase. It covers both the edge computer vision pipelines and the cloud ingestion systems.

---

## Part 1: Stream Ingestion & Normalization

### File: `rtsp_utils.py`
Before video streams can be processed, their URLs must be normalized to prevent decoder failures. This file handles URL sanitization.

```python
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

_TRAILING_LABEL_RE = re.compile(r"\s+(?:IR|OR)-[A-Za-z0-9_-]+$", re.IGNORECASE)
```
- **Line 10-11**: Imports `re` for regular expressions and URL parsing utilities from `urllib.parse`.
- **Line 13**: Defines a regex pattern `_TRAILING_LABEL_RE`. Often, users paste RTSP URLs from camera UI setups that include trailing labels (e.g., `rtsp://cam/live IR-Z03`). These labels crash the GStreamer decoder. This regex looks for whitespace followed by `IR-` or `OR-` and alphanumeric characters at the end of the string.

```python
def normalize_rtsp_url(source_url: str | None) -> str:
    url = (source_url or "").strip()
    if not url.lower().startswith("rtsp://"):
        return url
```
- **Line 16-17**: Takes a string or `None`. Cleans leading/trailing whitespace.
- **Line 18-19**: Returns immediately if the protocol isn't `rtsp://`.

```python
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    netloc = parts.netloc
    if hostname.lower() == "localhost":
        auth = ""
        if parts.username:
            auth = parts.username
            if parts.password:
                auth = f"{auth}:{parts.password}"
            auth = f"{auth}@"
        host = "127.0.0.1"
        if parts.port:
            host = f"{host}:{parts.port}"
        netloc = f"{auth}{host}"
```
- **Line 22-24**: Splits the URL into its components (scheme, netloc, path, query, fragment).
- **Line 25-35**: Special handling for `localhost`. Some decoders struggle with the string "localhost", so this manually reconstructs the network location (credentials + host + port), replacing "localhost" with `127.0.0.1`.

```python
    clean_path = unquote(parts.path or "")
    clean_path = _TRAILING_LABEL_RE.sub("", clean_path).strip()
    encoded_path = quote(clean_path, safe="/-._~!$&'()*+,;=:@")
    return urlunsplit((parts.scheme, netloc, encoded_path, parts.query, parts.fragment))
```
- **Line 36**: URL-decodes the path portion so we can cleanly manipulate it.
- **Line 37**: Uses the `_TRAILING_LABEL_RE` compiled earlier to strip out the garbage labels.
- **Line 38**: URL-encodes the path back into a safe string, explicitly telling `quote` not to encode standard URL delimiter characters (`safe=...`).
- **Line 39**: Reconstructs the final, clean URL.

---

## Part 2: Edge Inference & CV Pipeline

### File: `edge_agent/pipeline/voyager_engine.py`
This is the core of the Edge subsystem. It interacts directly with the Axelera Metis AIPU to decode video and run YOLO detections.

#### Initialization & Hardware Checking
```python
def __init__(self, network: str | None = None, conf: float = 0.25, iou: float = 0.45, frame_registry = None):
    if not cfg.VOYAGER_ALLOW_HARDWARE_CODEC or not cfg.VOYAGER_ENABLE_VAAPI:
        if os.environ.get("VOYAGER_ALLOW_SOFTWARE_FALLBACK", "0") != "1":
            raise RuntimeError(...)
```
- **Initialization block**: Takes the neural network path (`network`), confidence threshold (`conf`), IOU threshold for NMS (`iou`), and a shared memory registry (`frame_registry`).
- **Hardware Check**: The system strictly enforces hardware decoding. Software decoding of 40 RTSP streams would cripple the CPU. It explicitly checks `cfg.VOYAGER_ALLOW_HARDWARE_CODEC` and throws a `RuntimeError` if it's disabled, forcing the user to use VA-API.

#### Memory Pre-Allocation & Lookup Tables
```python
        self._vehicle_class_ids = set(cfg.VEHICLE_CLASS_IDS)
        if self._vehicle_class_ids:
            _max_cls = max(self._vehicle_class_ids) + 1
            self._vehicle_class_lut = np.zeros(_max_cls, dtype=bool)
            for _cid in self._vehicle_class_ids:
                self._vehicle_class_lut[_cid] = True
        else:
            self._vehicle_class_lut = np.zeros(0, dtype=bool)
```
- **Lookup Table (LUT)**: Python `in` set operations inside a tight loop are slow. To fix this, the code pre-allocates a boolean Numpy array (`_vehicle_class_lut`) indexed by class ID. Checking if class `3` is a vehicle is now an instantaneous array lookup (`_vehicle_class_lut[3]`), eliminating Python overhead.

#### The Iteration Loop (The Hot Path)
```python
    def _iteration_loop(self) -> None:
        if self._iter_cpu_set:
            from edge_agent.pipeline.cpu_topology import pin_current_thread
            if pin_current_thread(self._iter_cpu_set):
                logger.info("[Voyager] iter thread pinned to CPU %s", ...)
```
- **CPU Pinning**: The `_iteration_loop` runs continuously. It pins itself to specific CPU cores (`_iter_cpu_set`) to prevent OS context switching and preserve L1/L2 CPU cache.

```python
        while True:
            try:
                stream = self._stream
                for frame_result in stream:
                    self._handle_frame_result(frame_result, registry)
            except StopIteration:
                return
```
- **The Main Loop**: Iterates directly over the `self._stream` object (the Voyager hardware output stream). Every frame yielded is passed to `_handle_frame_result`.

#### Frame Result Handling & Memory Copies
```python
    def _handle_frame_result(self, frame_result, registry) -> None:
        self._total_frames_inferenced += 1
        sid = frame_result.stream_id
        cam_id = self._sid_to_cam.get(sid)
```
- **Frame Counting & Mapping**: Increments the total frame counter and resolves the internal hardware stream ID (`sid`) back into a readable `cam_id`.

```python
        if registry is not None and self._should_write_slot(cam_id, now):
            try:
                path = self._asarray_path.get(cam_id)
                if path is None:
                    frame_np, path = _probe_asarray_path(frame_result.image)
                    self._asarray_path[cam_id] = path
                else:
                    frame_np = _as_bgr(frame_result.image, path)
                if frame_np is not None:
                    slot = registry.get(cam_id)
                    if slot is not None:
                        slot.write(frame_np, now)
```
- **Zero-Copy Intentions**: The code checks `_should_write_slot()` to see if any user is currently watching the live dashboard feed. If not, it skips copying the image entirely to save memory bandwidth.
- **Probe Path**: `_probe_asarray_path` checks if the SDK supports direct "BGR" extraction. It caches this decision so future frames don't incur error-checking overhead.
- **Shared Memory (SHM)**: It fetches the `FrameSlot` for this camera and calls `.write()`. This does a raw memory copy into `/dev/shm`, completely avoiding Python's slow `pickle` serialization.

#### High-Speed Vectorized Detection Filtering
```python
        try:
            det_list = frame_result.detections
            n_in = len(det_list) if det_list is not None else 0
            if n_in == 0:
                detections = np.empty((0, 6), dtype=np.float32)
            else:
                raw = np.empty((n_in, 6), dtype=np.float32)
                for i, det in enumerate(det_list):
                    b = det.box
                    raw[i, 0] = b[0]
                    raw[i, 1] = b[1]
                    raw[i, 2] = b[2]
                    raw[i, 3] = b[3]
                    raw[i, 4] = det.score
                    raw[i, 5] = det.class_id
```
- **Data Unpacking**: Extracts YOLO detections. It allocates an empty numpy array `raw` of size `(N, 6)` for N detections. The 6 columns are `[x1, y1, x2, y2, confidence, class_id]`. It loops through the hardware objects and populates the matrix.

```python
                bw = raw[:, 2] - raw[:, 0]
                bh = raw[:, 3] - raw[:, 1]
                area = bw * bh
                aspect = bw / np.maximum(bh, 1.0)
```
- **Vectorized Math**: Calculates bounding box width (`bw`), height (`bh`), `area`, and `aspect` ratio for *all* detections simultaneously using Numpy vectorization.

```python
                cls_ids = raw[:, 5].astype(np.int32, copy=False)
                in_bounds = (cls_ids >= 0) & (cls_ids < lut.size)
                cls_ok = np.zeros(n_in, dtype=bool)
                cls_ok[in_bounds] = lut[cls_ids[in_bounds]]
```
- **Class Filtering**: Uses the `lut` created in `__init__` to filter out non-vehicles (like humans, dogs, etc.) in a single array operation.

```python
                keep = (
                    cls_ok
                    & (raw[:, 4] >= self._conf)
                    & (bw > 0) & (bh > 0)
                    & (area >= MIN_BBOX_AREA) & (area <= MAX_BBOX_AREA)
                    & (aspect >= MIN_ASPECT) & (aspect <= MAX_ASPECT)
                )
                detections = raw[keep]
```
- **The Filter Mask**: Creates a boolean mask `keep` applying all constraints: class validity, confidence (`_conf`), minimum/maximum area limits, and aspect ratio limits.
- **`raw[keep]`**: Instantly drops all bad bounding boxes from the array.

---

## Part 3: Vehicle Analytics & Math

### File: `edge_agent/pipeline/vehicle_analytics.py`
Once the detections are filtered and tracked, they enter the analytics engine to determine zone occupancy and dwell times.

#### Setting up the Point-in-Polygon Mask
```python
    def _update_polygons(self, zone_cfg: ZoneConfig) -> None:
        zp = zone_cfg.zone_poly or []
        self._zone_poly_canvas = (
            np.array(zp, dtype=np.float32).reshape(-1, 1, 2)
            if len(zp) >= 3 else None
        )
        self._mask_cache.clear()
```
- **Polygon Conversion**: Receives the drawn UI polygon points and shapes them into a `(N, 1, 2)` Numpy array required by OpenCV contour functions.

```python
    def _get_mask(self, frame_w: int, frame_h: int) -> Optional[np.ndarray]:
        sx = frame_w / cfg.CANVAS_W
        sy = frame_h / cfg.CANVAS_H
        scaled = self._zone_poly_canvas.copy().astype(np.float64)
        scaled[:, 0, 0] *= sx
        scaled[:, 0, 1] *= sy
        poly_int = scaled.astype(np.int32)

        mask = np.zeros((frame_h, frame_w), dtype=np.uint8)
        cv2.fillPoly(mask, [poly_int], 1)
        self._mask_cache[key] = mask
        return mask
```
- **O(1) Mask Creation**: To determine if a car is in a zone, doing complex geometry math per frame is too slow.
- Instead, it creates a blank black canvas `np.zeros(...)` the size of the video frame.
- It draws the polygon on it in solid white `cv2.fillPoly(..., 1)`. 
- This mask is cached. Checking if coordinate `(X, Y)` is in the zone now simply means checking if `mask[Y, X] == 1`.

#### Core Metric Calculation
```python
    def update(self, tracked_dets: list[tuple], frame_w: int, frame_h: int, ts: float) -> VehicleZoneMetrics:
        mask = self._get_mask(frame_w, frame_h)
        current_zone_ids: set[int] = set()

        for item in tracked_dets:
            track_id, x1, y1, x2, y2, conf = item[:6]
            cx = (x1 + x2) * 0.5
            cy = (y1 + y2) * 0.5
            
            in_zone = False
            if mask is not None:
                ix, iy = int(cx), int(cy)
                if 0 <= ix < frame_w and 0 <= iy < frame_h:
                    in_zone = bool(mask[iy, ix])
```
- **The Loop**: Iterates through tracked vehicles. Calculates the center X (`cx`) and center Y (`cy`) of the bounding box.
- **The O(1) Check**: `bool(mask[iy, ix])` checks the pre-rendered pixel. Instantly know if the car is inside.

```python
            if in_zone:
                current_zone_ids.add(track_id)
                if track_id not in self._zone_entry:
                    self._zone_entry[track_id] = ts
                    self._total_entered += 1
```
- **Entry Tracking**: If the car is inside, its `track_id` is recorded. If it wasn't seen before, the current timestamp `ts` is recorded as its `entry_time`.

```python
        exited = self._prev_zone_ids - current_zone_ids
        for tid in exited:
            if tid in self._zone_entry:
                del self._zone_entry[tid]
                self._total_exited += 1
        self._prev_zone_ids = current_zone_ids
```
- **Exit Tracking**: Subtracts `current_zone_ids` from `_prev_zone_ids` (from the last frame). Any ID left over has exited the zone. Their `entry_time` is deleted.

```python
        self._recent_counts.append(len(current_zone_ids))
        vehicle_count = int(np.median(self._recent_counts))
```
- **Median Smoothing**: Object trackers can flicker (lose a car for 1 frame, then find it again). Appending to a `deque` of length 5 and taking the median ensures the dashboard count is perfectly stable and ignores single-frame blips.

---

## Part 4: Cloud Ingestion & Dispatch

### File: `cloud/modules/ingestion/mqtt_consumer.py`
The cloud server receives thousands of these metric JSONs per second via MQTT.

#### Queueing & Dropping
```python
    def _enqueue(self, item: Any) -> None:
        try:
            self._queue.put_nowait(item)
            return
        except queue.Full:
            pass

        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        self._queue.put_nowait(item)
```
- **Non-blocking Ingest**: When an MQTT message arrives, it goes into a memory queue. If the queue is full, the system drops the *oldest* item (`get_nowait`) to make room for the new one. This ensures the dashboard never displays lagging, outdated data—only fresh data.

#### The Dispatcher (Redis Updates)
```python
    async def _handle_zone_metric(self, item: _ZoneMsg) -> None:
        hash_key = f"vzone:{msg.zone_id}:latest"
        mapping = { ... }
        
        try:
            pipe = self._redis.pipeline()
            pipe.hset(hash_key, mapping=mapping)
            pipe.publish(f"vmetrics:{msg.zone_id}", item.raw_json)
            await pipe.execute()
```
- **Redis Pipeline**: It constructs a Redis mapping. Instead of making two network calls to Redis, it uses a pipeline to `HSET` (update the current state snapshot) and `PUBLISH` (send an event to WebSocket listeners) simultaneously.

```python
        ts_dt = datetime.fromtimestamp(msg.ts, tz=timezone.utc)
        self._batch.append((
            ts_dt, msg.edge_id, msg.zone_id, msg.camera_id,
            msg.vehicle_count, json.dumps(msg.vehicle_count_by_type),
            msg.occupancy_pct, msg.overstay_count,
            ...
        ))
```
- **Batching**: Finally, the data is added to an internal Python `list` called `_batch`. It is NOT written to the database yet.

#### The Database Flusher (TimescaleDB)
```python
    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(1.0)
            await self._flush_batch()

    async def _flush_batch(self) -> None:
        rows = self._batch
        self._batch = []
        await self._db.executemany(_INSERT_ZONE_METRICS, rows)
```
- **1-Second Flush**: A background coroutine wakes up every 1.0 seconds. It swaps out the `_batch` list for a fresh empty list, and executes `executemany`. This takes potentially 1,000 individual metric updates and writes them to TimescaleDB in a single, hyper-efficient PostgreSQL transaction, protecting the disk from I/O exhaustion.

---

## Part 5: Real-time WebSocket Gateway

### File: `cloud/modules/websocket/gateway.py`
This module sends the ingested data directly to the web dashboard.

```python
async def _pubsub_listener() -> None:
    redis = await get_redis()
    pubsub = redis.pubsub()
    await pubsub.psubscribe("vmetrics:*", "valert:*")

    async for message in pubsub.listen():
        channel = message["channel"]
        data = json.loads(message["data"])

        if channel.startswith("vmetrics:"):
            zone_id = channel.split(":", 1)[1]
            _overview_zone_cache[zone_id] = data
            await _dispatch_metric(zone_id, data)
```
- **Redis Pub/Sub**: The gateway creates an async listener attached to Redis. When `mqtt_consumer.py` calls `pipe.publish()`, this listener immediately wakes up.
- **Overview Cache**: It saves a copy of the incoming metric to `_overview_zone_cache`. This memory dictionary is crucial for the 1-second global dashboard overview.
- **Dispatch**: It forwards the metric to `_dispatch_metric`.

```python
async def _dispatch_metric(zone_id: str, data: dict[str, Any]) -> None:
    dead = []
    for client in list(_clients):
        if f"zone:{zone_id}" in client.subscriptions:
            ok = await _safe_send(client, {
                "type": "zone_metric",
                "zone_id": zone_id,
                "data": data,
            })
            if not ok:
                dead.append(client)
    for c in dead:
        _clients.discard(c)
```
- **Selective Fan-out**: The server iterates over all connected WebSocket clients. It strictly checks `client.subscriptions`. If a client is viewing Zone B, it will *not* receive updates for Zone A, saving immense bandwidth.
- **Dead Connection Handling**: If `_safe_send` fails (e.g., the user closed their laptop), the client is marked as `dead` and forcefully garbage-collected from the active pool.

```python
async def _overview_timer() -> None:
    while True:
        await asyncio.sleep(1)
        subscribers = [c for c in _clients if "overview" in c.subscriptions]
        
        total_vehicles = 0
        for zid, data in _overview_zone_cache.items():
            total_vehicles += data.get("vehicle_count", 0)
        
        payload = { "type": "overview", "data": { "total_vehicles": total_vehicles ... } }
        for client in subscribers:
            await _safe_send(client, payload)
```
- **The Overview Broadcast**: Every 1 second, this loop loops through the `_overview_zone_cache` built in the pub/sub listener. It mathematically aggregates all zones (total vehicles, total alerts) into a single payload, and pushes it to all clients subscribed to the `overview` topic. This is how the top-bar summary on the dashboard updates globally without requiring the frontend to download all individual zone data.
