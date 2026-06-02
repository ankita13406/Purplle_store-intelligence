"""
health.py — /health endpoint: DB status, per-store feed freshness.
"""
import logging
import os
from datetime import datetime, timezone, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HealthResponse, StoreHealthStatus
from app.database import check_db

log = logging.getLogger("health")

STALE_FEED_MINUTES = int(os.getenv("STALE_FEED_MINUTES", "10"))
VERSION = os.getenv("APP_VERSION", "1.0.0")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stale_threshold() -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=STALE_FEED_MINUTES)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ten_min_ago() -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=10)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def get_health(db: AsyncSession) -> HealthResponse:
    db_ok = await check_db()
    stale_threshold = _stale_threshold()
    ten_min_ago = _ten_min_ago()

    store_statuses: list[StoreHealthStatus] = []

    if db_ok:
        try:
            # Per-store: last event timestamp + events in last 10 min
            result = await db.execute(text("""
                SELECT
                    store_id,
                    MAX(timestamp)     AS last_event_ts,
                    COUNT(CASE WHEN timestamp >= :ten_min THEN 1 END) AS recent_count
                FROM events
                GROUP BY store_id
            """), {"ten_min": ten_min_ago})

            for row in result.fetchall():
                stale = (row.last_event_ts is None) or (row.last_event_ts < stale_threshold)
                store_statuses.append(StoreHealthStatus(
                    store_id=row.store_id,
                    last_event_ts=row.last_event_ts,
                    events_last_10min=row.recent_count or 0,
                    stale_feed=stale,
                ))
        except Exception as exc:
            log.error("Health store query failed: %s", exc)

    overall = "ok" if db_ok else "degraded"
    if any(s.stale_feed for s in store_statuses):
        overall = "degraded"

    return HealthResponse(
        status=overall,
        version=VERSION,
        db_ok=db_ok,
        stores=store_statuses,
        checked_at=_now_iso(),
    )