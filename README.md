# Store Intelligence API — Purplle Tech Challenge 2026

Real-time store analytics from raw CCTV footage. Detects visitors, tracks zone behaviour, correlates with POS transactions, and surfaces anomalies — all via a production-ready REST API with a live web dashboard.

---

## Quick Start (5 Commands)

```bash
# 1. Clone the repository
git clone https://github.com/ankita13406/Purplle_store-intelligence.git
cd Purplle_store-intelligence

# 2. Prepare dataset files
mkdir -p data/clips
cp /path/to/dataset/store_layout.json    data/
cp /path/to/dataset/pos_transactions.csv data/
cp /path/to/dataset/*.mp4                data/clips/

# 3. Start the API + database
docker compose up --build

# 4. Run the detection pipeline against the clips
bash pipeline/run.sh \
  --clips-dir data/clips \
  --layout    data/store_layout.json \
  --output    data/events.jsonl \
  --live

# 5. Open the live dashboard
open http://localhost:8000/dashboard
```

> **API is ready when:** `curl http://localhost:8000/health` returns `{"status":"ok",...}`

---

## Prerequisites

- Docker Desktop (Docker + Docker Compose v2)
- Python 3.11+ for running the detection pipeline locally
- 8 GB RAM recommended (YOLOv8s + PostgreSQL)
- Dataset ZIP from the challenge email, extracted to `data/`

---

## Step-by-Step Setup

### Step 1 — Clone the repo

```bash
git clone https://github.com/ankita13406/Purplle_store-intelligence.git
cd Purplle_store-intelligence
```

### Step 2 — Prepare the dataset

```
data/
├── store_layout.json          ← from challenge ZIP
├── pos_transactions.csv       ← from challenge ZIP
└── clips/
    ├── STORE_BLR_002_CAM_ENTRY_01_20260410T100000Z.mp4
    ├── STORE_BLR_002_CAM_FLOOR_01_20260410T100000Z.mp4
    ├── STORE_BLR_002_CAM_FLOOR_02_20260410T100000Z.mp4
    ├── STORE_BLR_002_CAM_FLOOR_03_20260410T100000Z.mp4
    ├── STORE_BLR_002_CAM_BILLING_01_20260410T100000Z.mp4
    ├── STORE_BLR_003_CAM_ENTRY_01_20260410T100000Z.mp4
    ├── STORE_BLR_003_CAM_ENTRY_02_20260410T100000Z.mp4
    ├── STORE_BLR_003_CAM_FLOOR_01_20260410T100000Z.mp4
    └── STORE_BLR_003_CAM_BILLING_01_20260410T100000Z.mp4
```

### Step 3 — Start all services

```bash
docker compose up --build
```

This starts:
- `api` on port **8000** (FastAPI + PostgreSQL)
- `db` (PostgreSQL 16)

The live dashboard is served by the API at **http://localhost:8000/dashboard** — no separate container needed.

Wait for:
```
store-intelligence-api-1  | INFO: Application startup complete.
```

### Step 4 — Install pipeline dependencies (first time only)

```bash
pip install -r requirements.txt
```

Or manually:
```bash
pip install ultralytics opencv-python-headless httpx supervision shapely
```

### Step 5 — Run the detection pipeline

The pipeline processes clips from **both stores** (STORE_BLR_002 and STORE_BLR_003) in a single run. Store ID and camera ID are inferred automatically from each clip's filename.

```bash
# Process all clips from both stores and stream events live into the API
bash pipeline/run.sh \
  --clips-dir data/clips \
  --layout    data/store_layout.json \
  --output    data/events.jsonl \
  --live
```

The `--live` flag posts events to `http://localhost:8000/events/ingest` in real time.
Without `--live`, events are written to JSONL then replayed via `pipeline/replay.py`.

To replay a pre-generated JSONL file:
```bash
python pipeline/replay.py \
  --input data/events.jsonl \
  --api   http://localhost:8000 \
  --speed 10
```

Progress is logged per clip:
```
Processing STORE_BLR_002_CAM_ENTRY_01.mp4 | store=STORE_BLR_002 camera=CAM_ENTRY_01
  frame 300/18000 | events so far: 47
Done | frames=18000 detections=4821 events=312

Processing STORE_BLR_003_CAM_ENTRY_01.mp4 | store=STORE_BLR_003 camera=CAM_ENTRY_01
  frame 300/18000 | events so far: 39
Done | frames=18000 detections=3914 events=274
```

### Step 6 — Verify the API

```bash
# Health check
curl http://localhost:8000/health

# Brigade Road metrics
curl http://localhost:8000/stores/STORE_BLR_002/metrics | python -m json.tool

# Koramangala metrics
curl http://localhost:8000/stores/STORE_BLR_003/metrics | python -m json.tool

# Conversion funnel
curl http://localhost:8000/stores/STORE_BLR_002/funnel | python -m json.tool

# Zone heatmap
curl http://localhost:8000/stores/STORE_BLR_002/heatmap | python -m json.tool

# Active anomalies
curl http://localhost:8000/stores/STORE_BLR_002/anomalies | python -m json.tool

# Ingest a test event manually
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"events": [{
    "event_id": "test-001-unique-id-here",
    "store_id": "STORE_BLR_002",
    "camera_id": "CAM_ENTRY_01",
    "visitor_id": "VIS_test0001",
    "event_type": "ENTRY",
    "timestamp": "2026-06-03T14:22:10Z",
    "zone_id": null,
    "dwell_ms": 0,
    "is_staff": false,
    "confidence": 0.92,
    "metadata": {"queue_depth": null, "sku_zone": null, "session_seq": 1}
  }]}'
```

### Step 7 — Open the live dashboard

Navigate to **http://localhost:8000/dashboard** in your browser.

The dashboard polls all API endpoints every 5 seconds and updates live. Use the store selector (top-right) to switch between:
- **STORE_BLR_002 — Brigade Road** (27 visitors, 18.52% conversion on 2026-06-03)
- **STORE_BLR_003 — Koramangala** (33 visitors, 18.18% conversion on 2026-06-03)

---

## Running Tests

```bash
# Install test dependencies
pip install pytest pytest-asyncio pytest-cov httpx aiosqlite

# Run full test suite with coverage report
pytest --cov
```

**Results: 92/92 tests passing, 82.50% coverage (requirement: ≥70%)**

```bash
# Run specific test files
pytest tests/test_api.py -v
pytest tests/test_pipeline.py -v
pytest tests/test_anomalies.py -v
```

---

## Live Metrics (verified 2026-06-04)

| Metric | STORE_BLR_002 Brigade Road | STORE_BLR_003 Koramangala |
|--------|---------------------------|--------------------------|
| Unique Visitors | 27 | 33 |
| Conversion Rate | 18.52% | 18.18% |
| Avg Zone Dwell | 58,965 ms | 65,454 ms |
| Queue Depth | 1 | 5 |
| Abandonment Rate | 8.0% | 11.11% |
| Top Zone | Skincare (63,333 ms) | Fragrance (75,000 ms) |

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/events/ingest` | POST | Ingest batch of events (max 500, idempotent by event_id) |
| `/stores/{id}/metrics` | GET | Unique visitors, conversion rate, queue depth, avg dwell |
| `/stores/{id}/funnel` | GET | Entry → Zone Visit → Billing Area → Purchase with drop-off % |
| `/stores/{id}/heatmap` | GET | Zone visit frequency + avg dwell, normalised 0–100 |
| `/stores/{id}/anomalies` | GET | Active anomalies with severity + suggested actions |
| `/health` | GET | DB status, per-store feed freshness, stale feed detection |
| `/dashboard` | GET | Live dashboard HTML (served by FastAPI) |
| `/docs` | GET | Auto-generated Swagger UI |

All responses are JSON. Returns HTTP 503 (not 500) when the database is unavailable.

**Stores in this dataset:**

| Store ID | Name | City | Cameras |
|----------|------|------|---------|
| `STORE_BLR_002` | Purplle — Brigade Road | Bangalore | CAM_ENTRY_01, CAM_ENTRY_02 (if present), CAM_FLOOR_01/02/03, CAM_BILLING_01 |
| `STORE_BLR_003` | Purplle — Koramangala | Bangalore | CAM_ENTRY_01, CAM_ENTRY_02, CAM_FLOOR_01, CAM_BILLING_01 |

---

## Architecture

```
CCTV Clips (STORE_BLR_002 + STORE_BLR_003)
   │
   ▼
pipeline/detect.py      YOLOv8s + ByteTrack (per-clip, both stores)
   │  - Store ID + camera ID inferred from clip filename
   │  - Person detection at 1080p
   │  - Staff classification (HSV uniform detection)
   │  - Zone resolution (point-in-polygon vs store_layout.json)
   │  - Entry/exit direction inference
   │
   ▼
pipeline/tracker.py     PersonTracker (stateful, per-camera)
   │  - Two-tier Re-ID: spatial trajectory + appearance fallback
   │  - Session lifecycle: ENTRY / EXIT / REENTRY
   │  - Zone dwell timers (30s milestone events)
   │  - Group handling: N simultaneous tracks = N separate ENTRY events
   │
   ▼
pipeline/emit.py        EventEmitter
   │  - Schema validation (11 required fields)
   │  - JSONL file (replayable source of truth)
   │  - Optional live HTTP POST to /events/ingest
   │
   ▼
pipeline/merge_events.py   Merges per-store JSONL into unified events.jsonl
   │
   ▼
POST /events/ingest     app/ingestion.py
   │  - Idempotent by event_id (ON CONFLICT DO NOTHING)
   │  - Partial success on malformed events
   │  - POS correlation triggered per batch
   │  - Billing presence backfilled from DB at startup
   │
   ├──▶ /stores/{id}/metrics     Visitors, conversion rate, dwell, queue
   ├──▶ /stores/{id}/funnel      Entry → Zone → Billing → Purchase
   ├──▶ /stores/{id}/heatmap     Zone heatmap, normalised 0–100
   ├──▶ /stores/{id}/anomalies   Queue spike, dead zone, stale feed
   └──▶ /health                  DB status, per-store freshness
              │
              ▼
         dashboard/index.html
         Served by FastAPI at /dashboard.
         Polls every 5s. Store selector switches between both stores.
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://purplle:purplle@db:5432/store_intelligence` | Database connection string |
| `POS_FILE` | `/data/pos_transactions.csv` | Path to POS transactions CSV |
| `EVENTS_FILE` | `/data/events.jsonl` | Path to events JSONL (used at startup) |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `STALE_FEED_MINUTES` | `10` | Minutes before a camera feed is marked stale |
| `METRICS_WINDOW_HOURS` | `24` | Rolling window for metric computation |
| `APP_VERSION` | `1.0.0` | Version string returned by /health |

---

## Repository Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py          # YOLOv8s + ByteTrack detection loop
│   ├── tracker.py         # PersonTracker: Re-ID, session state, event emission
│   ├── emit.py            # Event schema validation + JSONL/HTTP emission
│   ├── replay.py          # Replay JSONL into API (batch + speed control)
│   ├── merge_events.py    # Merge per-store JSONL files into unified output
│   ├── pos_loader.py      # POS CSV loader (pipeline-side)
│   └── run.sh             # One-command pipeline runner (both stores)
├── app/
│   ├── main.py            # FastAPI app, middleware, routes, /dashboard endpoint
│   ├── models.py          # Pydantic v2 schemas
│   ├── database.py        # SQLAlchemy async engine
│   ├── ingestion.py       # Idempotent ingest + deduplication
│   ├── metrics.py         # Real-time metric queries (busiest-day window)
│   ├── funnel.py          # Conversion funnel (session-level, BILLING zone flex match)
│   ├── heatmap.py         # Zone heatmap normalisation
│   ├── anomalies.py       # Anomaly detection rules
│   ├── health.py          # Health endpoint + stale feed detection
│   └── pos_loader.py      # POS CSV loader + conversion correlation
├── tests/
│   ├── test_api.py             # Full API test suite
│   ├── test_pipeline.py        # Pipeline unit tests
│   └── test_anomalies.py       # Anomaly detection tests
├── scripts/
│   ├── generate_events.py      # Synthetic event generator
│   ├── restamp_and_replay.py   # Re-timestamp historical events for replay
│   └── ...                     # Other utility scripts
├── dashboard/
│   └── index.html         # Live polling dashboard (served at /dashboard)
├── docs/
│   ├── DESIGN.md          # Architecture + AI-assisted decisions
│   └── CHOICES.md         # 3 engineering decisions with full reasoning
├── data/
│   ├── store_layout.json
│   ├── pos_transactions.csv
│   ├── events.jsonl       # Generated by pipeline (not committed)
│   └── clips/             # CCTV footage (not committed)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── requirements.api.txt
├── pytest.ini
└── README.md
```

---

## Notes for Reviewers

- **Video files are not committed** per submission guidelines. Place `.mp4` files in `data/clips/`.
- The pipeline infers store ID and camera ID from clip filenames (`STORE_BLR_002_CAM_ENTRY_01_*.mp4`).
- The detection pipeline runs on CPU without GPU — YOLOv8s processes at reduced fps with frame skipping.
- All API endpoints handle zero-traffic stores correctly — they return zeros, not null or 500 errors.
- The dashboard is served at `/dashboard` by FastAPI — no separate port or container required.
- **Stale feed warnings** appear in `/anomalies` when replaying historical footage — correct and expected behaviour. In production with live feeds this surfaces genuine camera failures.
- Metrics use a **busiest-day window** (date with most events, not `MAX(timestamp)`) so late-night events spilling into the next calendar date do not cause the day's metrics to report as zero.
- The idempotency guarantee is tested in `tests/test_api.py::TestIngest::test_ingest_idempotent_duplicate`.
- Staff events are stored in the DB for audit but excluded from all customer-facing metrics via `WHERE is_staff = false`.
- Test coverage: **92/92 tests passing, 82.50% coverage** (requirement ≥70% met).
