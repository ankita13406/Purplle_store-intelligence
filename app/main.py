"""
main.py — FastAPI entrypoint for Store Intelligence API

Fixes applied vs previous version:
  1. Calls seed_events_if_empty() at startup — Docker PostgreSQL starts empty,
     this loads events.jsonl into the DB automatically on first boot.
  2. _backfill_billing_presence() now uses is_staff = false (PostgreSQL boolean)
     not is_staff = 0 (integer) — prevents silent empty result on PostgreSQL.
  3. EVENTS_FILE env var wired into docker-compose so seed path is explicit.
  4. Indentation bug in ingestion.py bulk-insert block fixed (was dead code).
"""

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, init_db, check_db, AsyncSessionLocal
from app.models import IngestRequest, IngestResponse
from app.ingestion import ingest_events
from app.metrics import get_store_metrics
from app.funnel import get_store_funnel
from app.heatmap import get_store_heatmap
from app.anomalies import get_store_anomalies
from app.health import get_health
from app.pos_loader import load_pos_file, run_conversion_correlation
from app.seed import seed_events_if_empty

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
)
log = logging.getLogger("api")


# ---------------------------------------------------------------------------
# Billing backfill — runs after seed so historical data gets correlated
# ---------------------------------------------------------------------------

async def _backfill_billing_presence():
    """
    Seed pos_loader billing presence from events already in the DB.
    Fix: uses is_staff = false (PostgreSQL boolean literal, not integer 0).
    """
    from app.pos_loader import record_billing_event, run_conversion_correlation
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(text("""
                SELECT store_id, visitor_id, timestamp
                FROM events
                WHERE (zone_id LIKE '%BILLING%' OR zone_id LIKE '%CASH%')
                  AND is_staff = false
                ORDER BY timestamp
            """))
            rows = result.fetchall()

        dates_seen: set = set()
        for row in rows:
            record_billing_event(row.store_id, row.visitor_id, row.timestamp)
            event_date = str(row.timestamp)[:10]
            dates_seen.add((row.store_id, event_date))

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for sid in {row.store_id for row in rows}:
            dates_seen.add((sid, today))

        for store_id, date in dates_seen:
            run_conversion_correlation(store_id, date)

        log.info(
            '"Billing backfill: %d events across %d store-dates"',
            len(rows), len(dates_seen)
        )
    except Exception as exc:
        log.warning('"Billing backfill failed (non-fatal): %s"', exc)


# ---------------------------------------------------------------------------
# Lifespan — startup order matters:
#   1. init_db()               — create tables
#   2. load_pos_file()         — load POS CSV into memory
#   3. seed_events_if_empty()  — load events.jsonl → DB if empty (Docker fix)
#   4. _backfill_billing()     — correlate billing presence → conversion rate
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info('"Starting Store Intelligence API"')

    # 1. Create tables
    await init_db()

    # 2. Load POS transactions into memory
    pos_count = load_pos_file()
    log.info('"POS transactions loaded: %d"', pos_count)

    # 3. *** KEY FIX: Seed DB from events.jsonl if empty ***
    # This is why Docker returned zeros — fresh PostgreSQL has no data.
    # Local uvicorn used SQLite which persisted between runs.
    seeded = await seed_events_if_empty()
    if seeded > 0:
        log.info('"Seeded %d events into fresh database"', seeded)

    # 4. Backfill billing presence for conversion rate
    await _backfill_billing_presence()

    yield
    log.info('"Shutting down"')


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Store Intelligence API",
    version=os.getenv("APP_VERSION", "1.0.0"),
    description="Real-time store analytics from CCTV event stream",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    trace_id   = str(uuid.uuid4())[:8]
    start_time = time.perf_counter()
    request.state.trace_id = trace_id
    response   = await call_next(request)
    latency_ms = round((time.perf_counter() - start_time) * 1000, 1)
    log.info(json.dumps({
        "trace_id":    trace_id,
        "method":      request.method,
        "path":        request.url.path,
        "store_id":    request.path_params.get("store_id", "-"),
        "status_code": response.status_code,
        "latency_ms":  latency_ms,
    }))
    response.headers["X-Trace-ID"]   = trace_id
    response.headers["X-Latency-Ms"] = str(latency_ms)
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", "?")
    log.error('"Unhandled error trace_id=%s: %s"', trace_id, str(exc), exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error":    "internal_server_error",
            "message":  "An unexpected error occurred.",
            "trace_id": trace_id,
        },
    )


async def _require_db(db: AsyncSession) -> AsyncSession:
    if not await check_db():
        raise HTTPException(
            status_code=503,
            detail={
                "error":   "service_unavailable",
                "message": "Database is not reachable. Please try again shortly.",
            },
        )
    return db


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/events/ingest", response_model=IngestResponse, status_code=207)
async def ingest(body: IngestRequest, db: AsyncSession = Depends(get_db)):
    await _require_db(db)
    result = await ingest_events(body, db)

    from app.pos_loader import record_billing_event, record_entry
    store_date_pairs: set = set()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for event in body.events:
        if event.is_staff:
            continue
        event_date = event.timestamp[:10]
        if (
            event.zone_id
            and ("BILLING" in event.zone_id.upper() or "CASH" in event.zone_id.upper())
            and event.event_type in (
                "BILLING_QUEUE_JOIN", "ZONE_ENTER", "ZONE_DWELL", "ZONE_EXIT"
            )
        ):
            record_billing_event(event.store_id, event.visitor_id, event.timestamp)
        if event.event_type == "ENTRY":
            record_entry(event.store_id, event_date)
        store_date_pairs.add((event.store_id, event_date))

    for sid in {e.store_id for e in body.events}:
        store_date_pairs.add((sid, today))
    for sid, date in store_date_pairs:
        run_conversion_correlation(sid, date)

    return result


@app.get("/stores/{store_id}/metrics")
async def metrics(store_id: str, db: AsyncSession = Depends(get_db)):
    await _require_db(db)
    return await get_store_metrics(store_id, db)


@app.get("/stores/{store_id}/funnel")
async def funnel(store_id: str, db: AsyncSession = Depends(get_db)):
    await _require_db(db)
    return await get_store_funnel(store_id, db)


@app.get("/stores/{store_id}/heatmap")
async def heatmap(store_id: str, db: AsyncSession = Depends(get_db)):
    await _require_db(db)
    return await get_store_heatmap(store_id, db)


@app.get("/stores/{store_id}/anomalies")
async def anomalies(store_id: str, db: AsyncSession = Depends(get_db)):
    await _require_db(db)
    return await get_store_anomalies(store_id, db)


@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    return await get_health(db)


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    dash_path = os.path.join(os.path.dirname(__file__), "..", "dashboard", "index.html")
    try:
        with open(dash_path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h2>Dashboard not found.</h2>")


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=os.getenv("DEV", "false").lower() == "true",
        log_config=None,
    )