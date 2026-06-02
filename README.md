# Store Intelligence API — Purplle Tech Challenge 2026

Real-time store analytics from raw CCTV footage. Detects visitors, tracks zone behaviour, correlates with POS transactions, and surfaces anomalies — all via a production-ready REST API.

---

## Quick Start (5 Commands)

```bash
# 1. Clone the repository
git clone https://github.com/ankita13406/store-intelligence.git
cd store-intelligence

# 2. Add your dataset files
#    Place these files from the challenge ZIP:
mkdir -p data/clips
cp /path/to/dataset/store_layout.json      data/
cp /path/to/dataset/pos_transactions.csv   data/
cp /path/to/dataset/*.mp4                  data/clips/

# 3. Start the API + database + dashboard
docker compose up --build

# 4. Run the detection pipeline against the clips
bash pipeline/run.sh --clips-dir data/clips --layout data/store_layout.json \
     --output data/events.jsonl --live

# 5. Open the live dashboard
open http://localhost:3000
# Or check the API directly:
curl http://localhost:8000/stores/ST1008/metrics
```

> **API is ready when:** `curl http://localhost:8000/health` returns `{"status":"ok",...}`

---

## Instructions to Run (Hackathon Portal)

### Prerequisites
- Docker Desktop (or Docker + Docker Compose v2) installed
- Python 3.11+ (for running the detection pipeline locally)
- 8GB RAM recommended (YOLOv8 + PostgreSQL)
- The dataset ZIP from the challenge email, extracted to `data/`

### Step-by-Step

**Step 1 — Clone and enter the repo**
```bash
git clone https://github.com/ankita13406/store-intelligence.git
cd store-intelligence
```

**Step 2 — Prepare dataset**

Create the `data/` directory structure:
```
data/
├── store_layout.json        ← from challenge ZIP
├── pos_transactions.csv     ← from challenge ZIP
└── clips/
    ├── ST1008_CAM_ENTRY_01_*.mp4
    ├── ST1008_CAM_FLOOR_01_*.mp4
    └── ...
```

```bash
mkdir -p data/clips
# Copy your dataset files into data/
```

**Step 3 — Start all services**
```bash
docker compose up --build
```

This starts:
- `api` on port **8000** (FastAPI + PostgreSQL)
- `db` (PostgreSQL 16)
- `dashboard` on port **3000** (live web UI)

Wait for:
```
store-intelligence-api-1  | INFO: Application startup complete.
```

**Step 4 — Install pipeline dependencies (first time only)**
```bash
pip install ultralytics opencv-python-headless httpx
```

Or use the provided requirements file:
```bash
pip install -r requirements.txt
```

**Step 5 — Run the detection pipeline**

```bash
# Process all clips and stream events live into the API:
bash pipeline/run.sh \
  --clips-dir  data/clips \
  --layout     data/store_layout.json \
  --output     data/events.jsonl \
  --live

# The --live flag posts events to http://localhost:8000/events/ingest in real time
# Without --live, events are written to JSONL then replayed in batch
```

Progress is logged per clip:
```
Processing ST1008_CAM_ENTRY_01.mp4 | store=ST1008 camera=CAM_ENTRY_01 ...
  frame 300/18000 | events so far: 47
  frame 600/18000 | events so far: 93
...
Done | frames=18000 detections=4821 events=312
```

**Step 6 — Verify the API**

```bash
# Health check
curl http://localhost:8000/health

# Store metrics
curl http://localhost:8000/stores/ST1008/metrics | python -m json.tool

# Conversion funnel
curl http://localhost:8000/stores/ST1008/funnel | python -m json.tool

# Zone heatmap
curl http://localhost:8000/stores/ST1008/heatmap | python -m json.tool

# Active anomalies
curl http://localhost:8000/stores/ST1008/anomalies | python -m json.tool

# Ingest test event manually
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"events": [{
    "event_id": "test-001-unique-id-here",
    "store_id": "ST1008",
    "camera_id": "CAM_ENTRY_01",
    "visitor_id": "VIS_test0001",
    "event_type": "ENTRY",
    "timestamp": "2026-03-03T14:22:10Z",
    "zone_id": null,
    "dwell_ms": 0,
    "is_staff": false,
    "confidence": 0.92,
    "metadata": {"queue_depth": null, "sku_zone": null, "session_seq": 1}
  }]}'
```

**Step 7 — Open the live dashboard**

Navigate to **http://localhost:3000** in your browser. The dashboard polls all endpoints every 5 seconds and updates live.

---

## Running Tests

```bash
# Install test dependencies
pip install pytest pytest-asyncio pytest-cov httpx aiosqlite

# Run full test suite with coverage report
pytest

# Run specific test files
pytest tests/test_api.py -v
pytest tests/test_pipeline.py -v
pytest tests/test_anomalies.py -v
```

Expected output: `PASSED` with coverage ≥ 70%.

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/events/ingest` | POST | Ingest batch of events (max 500, idempotent) |
| `/stores/{id}/metrics` | GET | Unique visitors, conversion rate, queue depth, dwell |
| `/stores/{id}/funnel` | GET | Entry → Zone → Billing → Purchase funnel |
| `/stores/{id}/heatmap` | GET | Zone visit frequency + dwell, normalised 0–100 |
| `/stores/{id}/anomalies` | GET | Active anomalies with severity + suggested actions |
| `/health` | GET | DB status, per-store feed freshness |
| `/dashboard` | GET | Live dashboard (HTML) |
| `/docs` | GET | Auto-generated Swagger UI |

All responses are JSON. The API returns HTTP 503 (not 500) when the database is unavailable.

---

## Architecture

```
CCTV Clips → YOLOv8s + ByteTrack → PersonTracker → EventEmitter
                                                          │
                                              ┌───────────▼───────────┐
                                              │  POST /events/ingest  │
                                              │  (idempotent by       │
                                              │   event_id)           │
                                              └───────────┬───────────┘
                                                          │
                                              PostgreSQL events table
                                              (3 composite indexes)
                                                          │
                              ┌───────────────────────────┼──────────────────────────┐
                              ▼                           ▼                          ▼
                         /metrics                      /funnel                  /anomalies
                    (real-time SQL)              (session dedup)           (threshold rules)
                              │
                              ▼
                    dashboard/index.html
                    (polls every 5s)
```

See `docs/DESIGN.md` for full architecture rationale.
See `docs/CHOICES.md` for the three key engineering decisions.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./store_intelligence.db` | Database connection string |
| `POS_FILE` | `data/pos_transactions.csv` | Path to POS transactions CSV |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `STALE_FEED_MINUTES` | `10` | Minutes before feed marked stale |
| `METRICS_WINDOW_HOURS` | `24` | Rolling window for metrics |
| `APP_VERSION` | `1.0.0` | Version string in /health |

---

## Repository Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py       # YOLOv8 + ByteTrack detection loop
│   ├── tracker.py      # PersonTracker: Re-ID, events, session state
│   ├── emit.py         # Event schema validation + JSONL/HTTP emission
│   ├── replay.py       # Replay JSONL into API (batch mode)
│   └── run.sh          # One-command pipeline runner
├── app/
│   ├── main.py         # FastAPI app, middleware, routes
│   ├── models.py       # Pydantic v2 schemas
│   ├── database.py     # SQLAlchemy async engine + ORM
│   ├── ingestion.py    # Idempotent ingest logic
│   ├── metrics.py      # Real-time metric queries
│   ├── funnel.py       # Conversion funnel
│   ├── heatmap.py      # Zone heatmap
│   ├── anomalies.py    # Anomaly detection
│   ├── health.py       # Health endpoint
│   └── pos_loader.py   # POS CSV + conversion correlation
├── tests/
│   ├── test_api.py          # Full API test suite
│   ├── test_pipeline.py     # Pipeline unit tests
│   └── test_anomalies.py    # Anomaly detection tests
├── dashboard/
│   └── index.html      # Live polling dashboard
├── docs/
│   ├── DESIGN.md       # Architecture + AI-assisted decisions
│   └── CHOICES.md      # 3 engineering decisions with full reasoning
├── docker-compose.yml
├── Dockerfile
├── Dockerfile.dashboard
├── requirements.txt
├── pytest.ini
└── README.md
```

---

## Notes for Reviewers

- **Video files and dataset are not included** in this repository per submission guidelines.
- The detection pipeline degrades gracefully without GPU — YOLOv8s runs on CPU.
- All API endpoints handle zero-traffic stores correctly (return zeros, not null/crash).
- The idempotency guarantee is tested in `tests/test_api.py::TestIngest::test_ingest_idempotent_duplicate`.
- Staff events are stored in the DB (for audit) but excluded from all customer-facing metrics via `WHERE is_staff = 0`.