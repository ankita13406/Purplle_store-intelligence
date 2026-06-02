"""
main.py — FastAPI entrypoint for Store Intelligence API

Production features:
  • Structured JSON logging with trace_id per request
  • Request latency logged on every response
  • Graceful DB-unavailable → HTTP 503
  • No raw stack traces in error responses
  • CORS enabled for dashboard
  • Billing presence backfilled from DB at startup for conversion rate
"""
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
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

# ---------------------------------------------------------------------------
# Structured logging setup
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
)
log = logging.getLogger("api")


# ---------------------------------------------------------------------------
# Billing presence backfill
# ---------------------------------------------------------------------------

async def _backfill_billing_presence():
    """
    Seed pos_loader billing presence from events already in the DB at startup.
    This ensures conversion_rate is non-zero for historical footage that was
    ingested in a previous session — not just for live events.
    """
    from app.pos_loader import record_billing_event, run_conversion_correlation
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(text("""
                SELECT store_id, visitor_id, timestamp
                FROM events
                WHERE (zone_id LIKE '%BILLING%'
                   OR  zone_id = 'CASH_COUNTER')
                  AND is_staff = false
                ORDER BY timestamp
            """))
            rows = result.fetchall()

        dates_seen: set[tuple[str, str]] = set()
        for row in rows:
            record_billing_event(row.store_id, row.visitor_id, row.timestamp)
            dates_seen.add((row.store_id, row.timestamp[:10]))

        for store_id, date in dates_seen:
            run_conversion_correlation(store_id, date)

        log.info(
            '"Billing backfill: %d events across %d store-dates"',
            len(rows), len(dates_seen),
        )
    except Exception as exc:
        log.warning('"Billing backfill failed (non-fatal): %s"', exc)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info('"Starting Store Intelligence API"')
    await init_db()
    pos_count = load_pos_file()
    log.info('"POS transactions loaded: %d"', pos_count)

    # Backfill billing presence so conversion rate works for historical data
    await _backfill_billing_presence()

    yield
    log.info('"Shutting down"')


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


# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    trace_id   = str(uuid.uuid4())[:8]
    start_time = time.perf_counter()

    request.state.trace_id = trace_id

    response = await call_next(request)

    latency_ms = round((time.perf_counter() - start_time) * 1000, 1)

    log.info(
        json.dumps({
            "trace_id":    trace_id,
            "method":      request.method,
            "path":        request.url.path,
            "store_id":    request.path_params.get("store_id", "-"),
            "status_code": response.status_code,
            "latency_ms":  latency_ms,
        })
    )
    response.headers["X-Trace-ID"]   = trace_id
    response.headers["X-Latency-Ms"] = str(latency_ms)
    return response


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", "?")
    log.error('"Unhandled error trace_id=%s: %s"', trace_id, str(exc), exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "message": "An unexpected error occurred.",
            "trace_id": trace_id,
        },
    )


async def _require_db(db: AsyncSession) -> AsyncSession:
    """Dependency: raise 503 if DB is not reachable."""
    if not await check_db():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "service_unavailable",
                "message": "Database is not reachable. Please try again shortly.",
            },
        )
    return db


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post(
    "/events/ingest",
    response_model=IngestResponse,
    status_code=207,
    summary="Ingest a batch of store events (idempotent)",
)
async def ingest(
    body: IngestRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Accept up to 500 events per call. Idempotent by event_id.
    Returns 207 Multi-Status with per-batch counts.
    Malformed events are rejected with per-event errors; valid ones are stored.
    """
    await _require_db(db)

    result = await ingest_events(body, db)

    # Trigger POS correlation for the actual event dates, not just today
    from app.pos_loader import record_billing_event
    store_date_pairs: set[tuple[str, str]] = set()
    for event in body.events:
        if (
            event.zone_id
            and ("BILLING" in event.zone_id.upper() or "CASH" in event.zone_id.upper())
            and not event.is_staff
        ):
            record_billing_event(event.store_id, event.visitor_id, event.timestamp)
        store_date_pairs.add((event.store_id, event.timestamp[:10]))

    for sid, date in store_date_pairs:
        run_conversion_correlation(sid, date)

    return result


@app.get(
    "/stores/{store_id}/metrics",
    summary="Real-time store metrics",
)
async def metrics(store_id: str, db: AsyncSession = Depends(get_db)):
    await _require_db(db)
    return await get_store_metrics(store_id, db)


@app.get(
    "/stores/{store_id}/funnel",
    summary="Conversion funnel: Entry → Zone → Billing → Purchase",
)
async def funnel(store_id: str, db: AsyncSession = Depends(get_db)):
    await _require_db(db)
    return await get_store_funnel(store_id, db)


@app.get(
    "/stores/{store_id}/heatmap",
    summary="Zone visit frequency + avg dwell heatmap",
)
async def heatmap(store_id: str, db: AsyncSession = Depends(get_db)):
    await _require_db(db)
    return await get_store_heatmap(store_id, db)


@app.get(
    "/stores/{store_id}/anomalies",
    summary="Active operational anomalies",
)
async def anomalies(store_id: str, db: AsyncSession = Depends(get_db)):
    await _require_db(db)
    return await get_store_anomalies(store_id, db)


@app.get("/health", summary="Service health + feed freshness")
async def health(db: AsyncSession = Depends(get_db)):
    return await get_health(db)


# ---------------------------------------------------------------------------
# Live Dashboard (Part E bonus)
# ---------------------------------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    """Serve the live dashboard HTML."""
    dash_path = os.path.join(os.path.dirname(__file__), "..", "dashboard", "index.html")
    try:
        with open(dash_path) as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h2>Dashboard not found. Run the detection pipeline first.</h2>")


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=os.getenv("DEV", "false").lower() == "true",
        log_config=None,
    )