"""
metrics.py — Real-time store metrics computation.

All queries run against the events table.  No stale cache.
Staff events (is_staff=True) are excluded from all customer metrics.

Conversion logic:
  A visitor is "converted" if their visitor_id had a ZONE_ENTER / ZONE_DWELL
  event in any BILLING* zone within the 5-minute window before any POS
  transaction timestamp for that store.

  Since POS data is loaded at startup (pos_transactions.csv), we precompute
  converted visitor_id sets per store per day.
"""
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoreMetrics, ZoneDwell
from app.pos_loader import get_converted_visitors

log = logging.getLogger("metrics")

TODAY_WINDOW_HOURS = int(os.getenv("METRICS_WINDOW_HOURS", "24"))


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _window_start() -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=TODAY_WINDOW_HOURS)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def get_store_metrics(store_id: str, db: AsyncSession) -> StoreMetrics:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Use the most recent event date in the DB for this store — not today's date.
    # This ensures metrics work correctly when events are from historical footage
    # (e.g. April clips ingested in June).
    date_result = await db.execute(text("""
        SELECT substr(MAX(timestamp), 1, 10) as latest_date
        FROM events
        WHERE store_id = :store_id AND is_staff = false
    """), {"store_id": store_id})
    latest_date = date_result.scalar()
    date   = latest_date or _today_str()
    window = date + "T00:00:00Z"   # full day of most recent events

    # --- Unique customer visitors (ENTRY + REENTRY, exclude staff, deduplicated) ---
    uv_result = await db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id   = :store_id
          AND event_type IN ('ENTRY', 'REENTRY')
          AND is_staff   = false
          AND timestamp >= :window
    """), {"store_id": store_id, "window": window})
    unique_visitors = uv_result.scalar() or 0

    # --- Conversion rate (via POS correlation) ---
    # Run correlation for the actual event date
    from app.pos_loader import run_conversion_correlation
    run_conversion_correlation(store_id, date)
    converted = get_converted_visitors(store_id, date)
    if unique_visitors > 0:
        conversion_rate = round(len(converted) / unique_visitors, 4)
    else:
        conversion_rate = 0.0

    # --- Average dwell across all zones ---
    avg_dwell_result = await db.execute(text("""
        SELECT AVG(dwell_ms)
        FROM events
        WHERE store_id  = :store_id
          AND event_type IN ('ZONE_EXIT', 'ZONE_DWELL')
          AND is_staff  = false
          AND dwell_ms  > 0
          AND timestamp >= :window
    """), {"store_id": store_id, "window": window})
    avg_dwell = float(avg_dwell_result.scalar() or 0.0)

    # --- Per-zone dwell ---
    zone_rows = await db.execute(text("""
        SELECT zone_id,
               AVG(dwell_ms)   AS avg_dwell,
               COUNT(*)        AS visit_count
        FROM events
        WHERE store_id  = :store_id
          AND event_type IN ('ZONE_EXIT', 'ZONE_DWELL')
          AND is_staff  = false
          AND zone_id IS NOT NULL
          AND dwell_ms  > 0
          AND timestamp >= :window
        GROUP BY zone_id
        ORDER BY avg_dwell DESC
    """), {"store_id": store_id, "window": window})
    zone_dwells = [
        ZoneDwell(
            zone_id=row.zone_id,
            avg_dwell_ms=round(float(row.avg_dwell), 1),
            visit_count=row.visit_count,
        )
        for row in zone_rows.fetchall()
    ]

    # --- Current queue depth (most recent BILLING_QUEUE_JOIN queue_depth) ---
    q_result = await db.execute(text("""
        SELECT queue_depth FROM events
        WHERE store_id  = :store_id
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND queue_depth IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 1
    """), {"store_id": store_id})
    row = q_result.fetchone()
    queue_depth = int(row.queue_depth) + 1 if row else 0

    # --- Abandonment rate ---
    abandon_result = await db.execute(text("""
        SELECT
          COUNT(CASE WHEN event_type = 'BILLING_QUEUE_ABANDON' THEN 1 END) AS abandons,
          COUNT(CASE WHEN event_type = 'BILLING_QUEUE_JOIN'    THEN 1 END) AS joins
        FROM events
        WHERE store_id  = :store_id
          AND is_staff  = false
          AND timestamp >= :window
    """), {"store_id": store_id, "window": window})
    ab_row = abandon_result.fetchone()
    if ab_row and ab_row.joins > 0:
        abandonment_rate = round(ab_row.abandons / ab_row.joins, 4)
    else:
        abandonment_rate = 0.0

    return StoreMetrics(
        store_id=store_id,
        date=date,
        unique_visitors=unique_visitors,
        conversion_rate=conversion_rate,
        avg_dwell_ms=round(avg_dwell, 1),
        zone_dwells=zone_dwells,
        queue_depth=queue_depth,
        abandonment_rate=abandonment_rate,
        computed_at=now_iso,
    )