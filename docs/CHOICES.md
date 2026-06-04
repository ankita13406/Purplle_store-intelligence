# CHOICES.md — Engineering Decision Log

## Decision 1: Detection Model — YOLOv8s + ByteTrack

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| YOLOv8n (nano) | Fastest inference, lowest memory | Lower accuracy on partial occlusion, misses groups |
| YOLOv8s (small) | Good accuracy/speed tradeoff, well-maintained | Slightly slower than nano |
| YOLOv8m (medium) | Best accuracy in the family | 2× slower, needs GPU for real-time |
| RT-DETR | Transformer-based, strong on crowded scenes | Complex setup, less community tooling |
| MediaPipe | No setup, runs on CPU | Poor on occlusion, no tracking built-in |

For tracking:

| Option | Pros | Cons |
|---------|------|------|
| ByteTrack | Handles low-confidence detections (occlusion) | No appearance model — Re-ID needed separately |
| DeepSORT | Appearance model built-in | Re-initialises tracks on occlusion → double-counts |
| StrongSORT | Best accuracy overall | Complex, slower |

### What AI Suggested

I asked AI to evaluate the tradeoffs between ByteTrack and DeepSORT specifically for the billing queue partial occlusion case described in the problem statement. AI correctly identified that DeepSORT terminates tracks when confidence drops, causing new track IDs and inflated visitor counts at the billing counter, while ByteTrack's two-pass association keeps tracks alive through brief occlusions. AI recommended ByteTrack and suggested YOLOv8m for maximum accuracy.

I asked a follow-up: "What breaks with YOLOv8s on the group entry case?" AI noted lower confidence on tightly-clustered bounding boxes — the mitigation I implemented was setting IOU threshold to 0.45 (below the default 0.7) to reduce bounding box merging on groups.

### What I Chose and Why

**YOLOv8s + ByteTrack.** Both stores' footage is at 1080p — YOLOv8s processes this on CPU at ~8fps with frame skipping, which is acceptable for the challenge. ByteTrack was the correct call for the billing occlusion case. The separation of tracking (ByteTrack, intra-clip continuity) from Re-ID (spatial trajectory, cross-clip deduplication) is intentional — they are different problems at different timescales.

Both `yolov8n.pt` and `yolov8s.pt` weights are present in the repo root. YOLOv8s is used by default; YOLOv8n is available as a CPU-constrained fallback.

---

## Decision 2: Event Schema Design

### The Core Design Problem

The schema must simultaneously support real-time streaming, session-level analytics, POS correlation, anomaly detection, and cross-camera deduplication — from a single event record, across two stores with different zone naming conventions.

### Options Considered

| Option | Description | Verdict |
|--------|-------------|---------|
| Detection-level events (one per frame) | Simplest to produce | Storage prohibitive, API reconstruction slow |
| Session-level summary only | Easiest to query | Loses zone granularity, cannot power heatmap |
| Semantic transition events | Emit on state changes only | Chosen — 10–50 events/visitor vs thousands |

### What AI Suggested

I asked AI to critique the event schema draft. AI suggested two improvements I adopted:
1. **`session_seq`** in metadata — ordinal event counter per visitor session, enabling ordering without millisecond clock precision
2. **`confidence` always emitted** — never suppress low-confidence events at pipeline level; let the API apply thresholds

One suggestion I rejected: AI proposed a `ZONE_DWELL_END` event to explicitly close dwell periods. I decided this was redundant — `ZONE_EXIT` already carries `dwell_ms`. Adding `ZONE_DWELL_END` would double event volume with no queryability gain.

### What I Chose and Why

Semantic transition events with `visitor_id` + `session_seq`. The API reconstructs full visit journeys from this stream without raw video or bounding boxes. `is_staff` is stored on every event — not just `ENTRY` — so any query can filter staff without a JOIN.

**Cross-store schema compatibility:** Both stores use the same 11-field schema. Zone IDs differ by store (`BILLING_AREA` for STORE_BLR_002, `PURPLLE_MUM_1076_Z_BILLING_01` for STORE_BLR_003) — the funnel query handles this with a pattern match (`UPPER(zone_id) LIKE '%BILLING%'`) rather than a hardcoded literal, ensuring both stores' billing stages are correctly populated.

---

## Decision 3: API Architecture — Async FastAPI + PostgreSQL

### Options Considered

| Storage | Pros | Cons |
|---------|------|------|
| SQLite | Zero setup, file-based | Write lock contention under concurrent ingest |
| PostgreSQL | ACID, concurrent writes, indexing | Requires Docker service |
| TimescaleDB | Optimised for time-series | Overkill at 40 stores, adds operational complexity |
| Redis | Fast counters | Not durable without extra config |

### What AI Suggested

AI suggested TimescaleDB for the events table, arguing that time-series queries (rolling 7-day anomaly detection) would benefit from hypertable partitioning. I investigated and found this helps at 100M+ events/day. For 40 stores at ~1,000 events/store/day (40,000 total), standard PostgreSQL with three composite indexes covers all query patterns. I overrode this — the added dependency would complicate the `docker compose up` acceptance gate without measurable benefit at this scale.

AI also suggested Redis caching for `/metrics` responses. I chose not to implement this — metrics compute in under 50ms on the indexed schema, and a cache introduces a correctness risk given the spec's "real-time — not cached from yesterday" requirement.

### What I Chose and Why

**Async FastAPI + PostgreSQL (asyncpg) + SQLAlchemy 2.0.** FastAPI's async handling means slow ingest batches do not block concurrent `/metrics` reads. `ON CONFLICT DO NOTHING` makes `POST /events/ingest` idempotent as a single SQL primitive with no application-level race conditions.

The dashboard is served directly by FastAPI at `/dashboard` via `HTMLResponse` — no separate Nginx, Node, or dashboard container. This simplifies `docker compose up` to two services (api + db) and eliminates CORS issues from opening HTML as a `file://` URL.

---

## Decision 4: Metrics Date Window — Busiest Day vs MAX Timestamp

### The Problem

Events from STORE_BLR_002 span `2026-06-03T14:02Z` to `2026-06-04T01:36Z`. A few late-night REENTRY events spill into June 4th. Using `MAX(timestamp)` to determine the metrics window resolves to June 4th — which has only 27 events and 7 visitors — while the actual trading day (June 3rd) has 271 events and 27 customers.

### Options Considered

| Option | Result | Problem |
|--------|--------|---------|
| Use today's date | Wrong for all historical footage | Breaks whenever clips predate deployment |
| Use MAX(timestamp) | Picks June 4th for BLR_002 | Late-night events corrupt the window |
| Use date with most events | Picks June 3rd correctly | Robust to any timestamp distribution |

### What AI Suggested

I asked AI to evaluate all three approaches. AI correctly identified option 3 (busiest day) as the most robust and suggested the implementation:
```sql
SELECT substr(timestamp, 1, 10) AS event_date
FROM events WHERE store_id = :store_id AND is_staff = false
GROUP BY event_date ORDER BY COUNT(*) DESC LIMIT 1
```
I adopted this exactly. It is applied in both `metrics.py` and `funnel.py`.

### Verified Impact

| Store | Without fix | With fix |
|-------|-------------|----------|
| STORE_BLR_002 | 7 visitors, 0.0% conversion (June 4th) | 27 visitors, 18.52% conversion (June 3rd) |
| STORE_BLR_003 | 33 visitors, 18.18% (unaffected — all events on June 3rd) | 33 visitors, 18.18% (unchanged) |
