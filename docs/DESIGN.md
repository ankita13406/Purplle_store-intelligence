# DESIGN.md — Store Intelligence System Architecture

## System Overview

The Store Intelligence System is an end-to-end pipeline that transforms raw CCTV footage into queryable, real-time retail analytics. The architecture has four distinct stages: a detection pipeline, a structured event stream, an intelligence API, and a live dashboard.

```
CCTV Clips — 2 stores, 9 clips total
  STORE_BLR_002 (Brigade Road):  CAM_ENTRY_01, CAM_FLOOR_01/02/03, CAM_BILLING_01
  STORE_BLR_003 (Koramangala):   CAM_ENTRY_01, CAM_ENTRY_02, CAM_FLOOR_01, CAM_BILLING_01
   │
   ▼
pipeline/detect.py         YOLOv8s + ByteTrack (per-clip)
   │  - Store ID + camera ID inferred from clip filename
   │  - Person detection at 1080p
   │  - Staff classification (HSV torso uniform detection)
   │  - Zone resolution (point-in-polygon against store_layout.json)
   │  - Direction inference (vertical centroid delta for entry/exit)
   │
   ▼
pipeline/tracker.py        PersonTracker (stateful, per-camera)
   │  - Two-tier Re-ID: spatial trajectory + appearance fallback
   │  - Session management: ENTRY / EXIT / REENTRY lifecycle
   │  - Zone dwell timers (30s milestone events)
   │  - Group handling: N simultaneous tracks = N separate ENTRY events
   │
   ▼
pipeline/emit.py           EventEmitter
   │  - Schema validation before write (11 required fields)
   │  - JSONL file (source of truth, replayable)
   │  - Optional live HTTP POST to /events/ingest
   │
   ▼
pipeline/merge_events.py   Merges per-store JSONL outputs into unified events.jsonl
   │
   ▼
POST /events/ingest        app/ingestion.py
   │  - Idempotent by event_id (ON CONFLICT DO NOTHING)
   │  - Partial success: malformed events rejected per-event
   │  - POS correlation triggered after each batch
   │  - Billing presence backfilled from DB at startup
   │
   ├──▶ GET /stores/{id}/metrics      Real-time: visitors, conversion, dwell, queue
   ├──▶ GET /stores/{id}/funnel       Entry → Zone → Billing → Purchase
   ├──▶ GET /stores/{id}/heatmap      Zone frequency + dwell, normalised 0–100
   ├──▶ GET /stores/{id}/anomalies    Queue spike, dead zone, stale feed, abandonment
   └──▶ GET /health                   DB status, per-store feed freshness
              │
              ▼
         dashboard/index.html
         Served by FastAPI at GET /dashboard — no separate container.
         Polls all endpoints every 5 seconds.
         Store selector switches between STORE_BLR_002 and STORE_BLR_003.
```

---

## Dataset and Store Configuration

The system covers two stores, both in Bangalore:

| Store ID | Name | Cameras | Events in DB |
|----------|------|---------|-------------|
| `STORE_BLR_002` | Purplle — Brigade Road | CAM_ENTRY_01, CAM_FLOOR_01/02/03, CAM_BILLING_01 | 298 |
| `STORE_BLR_003` | Purplle — Koramangala | CAM_ENTRY_01, CAM_ENTRY_02, CAM_FLOOR_01, CAM_BILLING_01 | 206 |

Zone definitions are loaded from `data/store_layout.json` at startup. Each zone has a normalised polygon (0–1 coordinates), a zone type, and the camera IDs that cover it. The pipeline uses these polygons for point-in-polygon zone assignment on every tracked centroid.

**Verified live metrics (2026-06-03 trading day):**

| Metric | STORE_BLR_002 | STORE_BLR_003 |
|--------|--------------|--------------|
| Unique Visitors | 27 | 33 |
| Conversion Rate | 18.52% | 18.18% |
| Avg Zone Dwell | 58,965 ms | 65,454 ms |
| Queue Depth | 1 | 5 |
| Abandonment Rate | 8.0% | 11.11% |
| Top Zone (dwell) | Skincare — 63,333 ms | Fragrance — 75,000 ms |

**Handling historical footage replayed live:**

Events from CCTV clips are timestamped relative to the clip's recording date (June 2026), not the wall-clock date when the pipeline runs. The metrics layer uses the **date with the most events** for each store — not `MAX(timestamp)` and not today's date. This prevents a few late-night events spilling into the next calendar date from causing the entire day's metrics to report as zero.

Concretely: STORE_BLR_002 has events from `2026-06-03T14:02Z` to `2026-06-04T01:36Z`. Without the busiest-day fix, `MAX(timestamp)` would resolve to June 4th (only 7 late-night visitors, 0% conversion). With the fix, the system correctly identifies June 3rd as the primary trading day (271 events vs 27 on June 4th).

**Stale feed warnings** in `/anomalies` fire when no events have arrived from a camera in 10 minutes. When replaying historical footage, all cameras appear stale — this is correct and expected behaviour. In production with live feeds, this surfaces genuine camera or pipeline failures.

---

## Key Design Decisions

### Detection Pipeline

**YOLOv8s + ByteTrack** was chosen as the detection stack. YOLOv8s provides a strong accuracy/speed tradeoff on 1080p footage without requiring GPU in development. ByteTrack was chosen over DeepSORT because it handles partial occlusion better — its second association pass retains low-confidence detections rather than terminating tracks, which is critical for the billing queue partial occlusion edge case.

**Staff classification** uses an HSV colour histogram on the torso bounding box region. Both Brigade Road and Koramangala staff wear solid dark uniforms (verified from footage). The HSV range `H=0-180, S=0-50, V=0-80` captures this. Staff votes are aggregated over a rolling 10-frame window — single-frame misclassifications do not propagate. `is_staff` is stored on every event so any query can filter staff without a JOIN.

**Re-ID is two-tier:** Tier 1 (spatial trajectory) re-links a visitor who briefly leaves frame and returns to the same area within 5 minutes, generating a `REENTRY` event rather than inflating unique visitor count. Tier 2 (appearance fallback) uses bounding-box aspect ratio and torso colour histograms for cross-camera deduplication — preventing the same person from being counted twice across overlapping camera fields of view.

**Group entry** produces N separate `ENTRY` events for N simultaneous entrants. IOU threshold is set to 0.45 (below the default 0.7) to reduce bounding box merging on tightly-clustered detections.

**Multi-store handling:** Store ID and camera ID are inferred from the clip filename at pipeline startup (`STORE_BLR_002_CAM_ENTRY_01_20260410T100000Z.mp4`). `pipeline/merge_events.py` merges per-store JSONL outputs into a single `events.jsonl` before replay.

### Event Stream

The schema was designed so **session is the unit of analysis**, not raw detections. Every event carries `visitor_id` (Re-ID token) and `session_seq` so the API can reconstruct complete visitor journeys without raw bounding boxes. The `confidence` field is always emitted — low-confidence events are never suppressed at pipeline level, giving the API full control over thresholds.

The event type catalogue covers eight transitions: `ENTRY`, `EXIT`, `ZONE_ENTER`, `ZONE_EXIT`, `ZONE_DWELL` (every 30s of continuous dwell), `BILLING_QUEUE_JOIN`, `BILLING_QUEUE_ABANDON`, and `REENTRY`. These are sufficient to power all five API endpoints.

### API and Storage

PostgreSQL is used in production (via docker-compose). SQLite + aiosqlite is used in tests for zero-setup convenience. Three composite indexes cover all hot query paths:
- `(store_id, timestamp)` — time-range queries
- `(store_id, event_type)` — metric aggregations
- `(store_id, visitor_id)` — session reconstruction

**POS correlation** runs in-memory against a preloaded `pos_transactions.csv`. The 5-minute window rule (visitor in billing zone within 5 minutes before a POS transaction for the same store) is applied after each ingest batch. At startup, `_backfill_billing_presence()` seeds the correlator from existing DB events so conversion rate is correct even for footage ingested in a previous session.

**Busiest-day window:** Both `metrics.py` and `funnel.py` use:
```sql
SELECT substr(timestamp, 1, 10) AS event_date
FROM events
WHERE store_id = :store_id AND is_staff = false
GROUP BY event_date
ORDER BY COUNT(*) DESC
LIMIT 1
```
This selects the trading day with the most events, making metrics robust to late-night spillover across calendar dates.

**Flexible billing zone matching:** The funnel query matches billing zones by pattern (`UPPER(zone_id) LIKE '%BILLING%'`) rather than hardcoded string, so it works correctly for both `BILLING_AREA` (STORE_BLR_002) and `PURPLLE_MUM_1076_Z_BILLING_01`-style zone IDs (STORE_BLR_003).

### Dashboard Architecture

The dashboard (`dashboard/index.html`) is served directly by FastAPI at the `/dashboard` route via `HTMLResponse`. It polls all API endpoints every 5 seconds via browser `fetch()`. This eliminates the need for a separate dashboard container, simplifies `docker compose up`, and avoids CORS issues that arise when opening HTML as a `file://` URL. The store selector allows switching between `STORE_BLR_002` and `STORE_BLR_003` without a page reload.

### Production Readiness

- **Structured logging:** Every request emits a JSON log line with `trace_id`, `store_id`, `endpoint`, `latency_ms`, `status_code`. Trace IDs are returned in `X-Trace-ID` response headers.
- **Graceful degradation:** DB unavailable → HTTP 503 with structured error body. No raw stack traces in API responses.
- **Idempotency:** `ON CONFLICT DO NOTHING` on `event_id` makes `POST /events/ingest` safe to retry with identical payloads.
- **Health endpoint:** Returns per-store `last_event_ts`, `events_last_10min`, and `stale_feed` flags. Overall status degrades to `"degraded"` if any store has a stale feed or the DB is unreachable.
- **Test coverage:** 92/92 tests passing, 82.50% statement coverage (requirement ≥70%).

---

## AI-Assisted Decisions

### 1. ByteTrack vs DeepSORT for the billing occlusion case

I asked AI to compare ByteTrack and DeepSORT for the partial occlusion edge case in the billing queue. AI's analysis correctly identified that DeepSORT re-initialises tracks when confidence drops — causing new track IDs and double-counting — while ByteTrack's two-pass association keeps tracks alive through brief occlusions. I adopted this recommendation after verifying it against the ByteTrack paper. The cost is no built-in appearance model, which is why a separate spatial Re-ID layer was added.

### 2. Busiest-day window for metrics

A production bug emerged during testing: STORE_BLR_002 has events from June 3rd (271 events, 27 visitors) and a few late-night events on June 4th (27 events, 7 visitors). Using `MAX(timestamp)` resolved to June 4th, returning 7 visitors and 0% conversion rate despite real data existing. I asked AI to evaluate three fixes: (1) use today's date, (2) use MAX, (3) use the date with the most events. AI correctly identified option 3 as most robust. I implemented `GROUP BY event_date ORDER BY COUNT(*) DESC LIMIT 1` in both `metrics.py` and `funnel.py`. This fix ensures correct metrics regardless of when historical footage is replayed.

### 3. Conversion correlation without customer_id

The POS data has no customer_id. I asked AI to evaluate three approaches: time-window matching, session-duration matching, and statistical attribution. AI recommended time-window (visitor in billing zone within N minutes of transaction) as the pragmatic choice. I agreed but used the 5-minute window from the problem statement rather than AI's suggested 10 minutes. I also extended the correlation to be date-aware — it runs against the actual event date, not the wall-clock date, so historical footage always produces correct conversion rates.
