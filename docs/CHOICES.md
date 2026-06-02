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
|--------|------|------|
| ByteTrack | Handles low-confidence detections (occlusion) | No appearance model (Re-ID needed separately) |
| DeepSORT | Appearance model built-in | Re-initialises tracks on occlusion → double-counts |
| StrongSORT | Best accuracy overall | Complex, slower |

### What AI Suggested

I asked Claude to evaluate the tradeoffs between ByteTrack and DeepSORT specifically for the billing queue partial occlusion case. Claude correctly identified that DeepSORT terminates tracks when confidence drops (causing new track IDs and inflated counts), while ByteTrack's two-pass association keeps tracks alive through brief occlusions. Claude recommended ByteTrack for this reason, and suggested YOLOv8m for accuracy. I asked a follow-up: "What breaks with YOLOv8s on the group entry case?" Claude noted lower confidence on tightly-clustered bounding boxes — the mitigation I implemented was setting IOU threshold to 0.45 (below default 0.7) to reduce bounding box merging.

### What I Chose and Why

**YOLOv8s + ByteTrack.** The Brigade Road footage runs at 1080p/30fps — YOLOv8s processes this on CPU at ~8fps (acceptable with frame skipping). ByteTrack was the correct call for the billing occlusion case. The separation of tracking (ByteTrack, intra-clip continuity) from Re-ID (spatial trajectory, cross-clip deduplication) is intentional — these are different problems.

---

## Decision 2: Event Schema Design

### The Core Design Problem

The schema must simultaneously support: real-time streaming, session-level analytics, POS correlation, anomaly detection, and cross-camera deduplication — from a single event record.

### Options Considered

| Option | Description | Verdict |
|--------|-------------|---------|
| Detection-level events (one per frame) | Simplest to produce | Storage prohibitive, API reconstruction slow |
| Session-level summary only | Easiest to query | Loses zone granularity, can't power heatmap |
| Semantic transition events | Emit on state changes only | Chosen — 10–50 events/visitor vs thousands |

### What AI Suggested

I asked Claude to critique the event schema draft. Claude suggested two improvements I adopted:
1. **`session_seq`** in metadata — ordinal event counter per visitor session, enabling ordering without clock precision
2. **`confidence` always emitted** — never suppress low-confidence events at pipeline level; let the API apply thresholds

One suggestion I rejected: Claude proposed a `ZONE_DWELL_END` event to explicitly close dwell periods. I decided this was redundant — `ZONE_EXIT` already carries `dwell_ms`. Adding `ZONE_DWELL_END` would double event volume with no queryability gain.

### What I Chose and Why

Semantic transition events with `visitor_id` + `session_seq`. The API can reconstruct full visit journeys from the event stream without raw video or bounding boxes. `is_staff` is stored on every event — not just `ENTRY` — so any query can filter staff without a JOIN.

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

Claude suggested TimescaleDB for the events table, arguing that time-series queries (rolling 7-day anomaly detection) would be significantly faster. I investigated and found TimescaleDB's hypertable partitioning helps at 100M+ events/day. For 40 stores with ~1,000 events/store/day (40,000 total), standard PostgreSQL with three composite indexes covers all query patterns. I overrode this — the added dependency would complicate the `docker compose up` acceptance gate without measurable benefit at this scale.

Claude also suggested Redis caching for `/metrics` responses. I chose not to — metrics compute in under 50ms on the indexed schema, and a cache introduces a correctness risk given the spec's "real-time — not cached from yesterday" requirement.

### What I Chose and Why

**Async FastAPI + PostgreSQL (asyncpg) + SQLAlchemy 2.0.** FastAPI's async handling means slow ingest batches don't block concurrent `/metrics` reads. `ON CONFLICT DO NOTHING` makes POST /events/ingest idempotent as a single SQL primitive with no application-level race conditions.

---

## Decision 4: Camera Role Assignment — Excluding Non-Customer Cameras

### The Problem

The dataset provided 5 clips from a single store. After reviewing actual frame content (not just filenames), one camera (CAM_4 / `CAM_UNKNOWN_01`) was identified as pointing at a **back room and storage area** — not a customer-facing zone. Including events from this camera in customer metrics would inflate unique visitor counts and corrupt conversion rate.

### What I Observed

- **CAM_2:** Main floor wide-angle — customers browsing, multiple people visible simultaneously
- **CAM_3 (entry):** Doorway visible — correct for ENTRY/EXIT events  
- **CAM_1 (aisle):** Product shelf close-up — customers examining products
- **CAM_BILLING_01:** Checkout area — queue depth source
- **CAM_4 / CAM_UNKNOWN_01:** Back room with staff and storage — no customers visible in any frame

### What AI Suggested

Claude suggested using a VLM (Vision Language Model) prompt to classify each camera as customer-facing vs non-customer-facing automatically. The prompt would be: "Is this camera viewing a customer-facing retail area?" I evaluated this approach but chose manual review instead for this submission — the dataset is small (5 clips) and the misclassification risk of automated VLM classification wasn't worth the complexity. At 40 stores with 120+ cameras, the VLM approach would be the correct choice.

### What I Chose and Why

**Manual frame review + zone exclusion.** CAM_UNKNOWN_01 events are stored in the DB (for audit purposes) but excluded from all customer-facing queries via the `cameras` field in `store_layout.json`. The decision is documented here so it can be revisited when scaling to more stores.