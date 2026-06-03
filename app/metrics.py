"""
metrics.py — Real-time store metrics computation.

BUG FIX (2026-06-03):
  FIX — queue_depth off-by-one: old code did `int(q_row.queue_depth) + 1`
        which always added 1 even when queue_depth=0, making an empty queue
        show as 1. The queue_depth stored in events already represents the
        number of people queuing at the moment of the event — just use it
        directly. Only add 1 if we also want to count the person currently
        being served (that person emits ZONE_ENTER, not BILLING_QUEUE_JOIN).

  Retained: window scoping to busiest event date (not today's date),
  which correctly handles historical footage replayed into the API.
"""
import logging
import os
from datetime import datetime, timezone, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoreMetrics, ZoneDwell
from app.pos_loader import get_converted_visitors

log = logging.getLogger("metrics")

TODAY_WINDOW_HOURS = int(os.getenv("METRICS_WINDOW_HOURS", "24"))


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def get_store_metrics(store_id: str, db: AsyncSession) -> StoreMetrics:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Busiest event date for this store
    date_result = await db.execute(text("""
        SELECT substr(timestamp, 1, 10) AS event_date
        FROM   events
        WHERE  store_id = :store_id
          AND  is_staff = false
        GROUP  BY event_date
        ORDER  BY COUNT(*) DESC
        LIMIT  1
    """), {"store_id": store_id})

    latest_date = date_result.scalar()

    if not latest_date:
        log.warning("No events found for store %s — returning zero metrics", store_id)
        return StoreMetrics(
            store_id=store_id,
            date=_today_str(),
            unique_visitors=0,
            conversion_rate=0.0,
            avg_dwell_ms=0.0,
            zone_dwells=[],
            queue_depth=0,
            abandonment_rate=0.0,
            computed_at=now_iso,
        )

    date         = latest_date
    window_start = date + "T00:00:00Z"
    window_end   = date + "T23:59:59Z"

    log.info("Metrics window for %s: %s → %s", store_id, window_start, window_end)

    # Unique customer visitors
    uv_result = await db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM   events
        WHERE  store_id   = :store_id
          AND  event_type IN ('ENTRY', 'REENTRY')
          AND  is_staff   = false
          AND  timestamp >= :window_start
          AND  timestamp <= :window_end
    """), {"store_id": store_id,
           "window_start": window_start, "window_end": window_end})

    unique_visitors = uv_result.scalar() or 0

    # Conversion rate
    from app.pos_loader import run_conversion_correlation
    run_conversion_correlation(store_id, date)
    converted       = get_converted_visitors(store_id, date)
    conversion_rate = (
        round(len(converted) / unique_visitors, 4) if unique_visitors > 0 else 0.0
    )

    # Average dwell across all zones
    avg_dwell_result = await db.execute(text("""
        SELECT AVG(dwell_ms)
        FROM   events
        WHERE  store_id   = :store_id
          AND  event_type IN ('ZONE_EXIT', 'ZONE_DWELL')
          AND  is_staff   = false
          AND  dwell_ms   > 0
          AND  timestamp >= :window_start
          AND  timestamp <= :window_end
    """), {"store_id": store_id,
           "window_start": window_start, "window_end": window_end})

    avg_dwell = float(avg_dwell_result.scalar() or 0.0)

    # Per-zone dwell breakdown
    zone_rows = await db.execute(text("""
        SELECT zone_id,
               AVG(dwell_ms) AS avg_dwell,
               COUNT(*)      AS visit_count
        FROM   events
        WHERE  store_id   = :store_id
          AND  event_type IN ('ZONE_EXIT', 'ZONE_DWELL')
          AND  is_staff   = false
          AND  zone_id IS NOT NULL
          AND  dwell_ms   > 0
          AND  timestamp >= :window_start
          AND  timestamp <= :window_end
        GROUP  BY zone_id
        ORDER  BY avg_dwell DESC
    """), {"store_id": store_id,
           "window_start": window_start, "window_end": window_end})

    zone_dwells = [
        ZoneDwell(
            zone_id=row.zone_id,
            avg_dwell_ms=round(float(row.avg_dwell), 1),
            visit_count=row.visit_count,
        )
        for row in zone_rows.fetchall()
    ]

    # ------------------------------------------------------------------ #
    # Billing queue depth — FIX: remove the spurious +1                   #
    #                                                                      #
    # Old: queue_depth = int(q_row.queue_depth) + 1 if q_row else 0      #
    #      → always added 1; an empty queue (queue_depth=0 in last event) #
    #        showed as 1; a queue of 5 showed as 6.                       #
    #                                                                      #
    # New: use queue_depth from the event directly.                        #
    #      The event stores "number of people already waiting" at the time #
    #      the new person joined. The person currently being served is     #
    #      counted separately via ZONE_ENTER on the billing zone.          #
    # ------------------------------------------------------------------ #
    q_result = await db.execute(text("""
        SELECT queue_depth
        FROM   events
        WHERE  store_id   = :store_id
          AND  event_type = 'BILLING_QUEUE_JOIN'
          AND  queue_depth IS NOT NULL
          AND  timestamp >= :window_start
          AND  timestamp <= :window_end
        ORDER  BY timestamp DESC
        LIMIT  1
    """), {"store_id": store_id,
           "window_start": window_start, "window_end": window_end})

    q_row       = q_result.fetchone()
    # FIX: use as-is, no +1
    queue_depth = int(q_row.queue_depth) if q_row else 0

    # Abandonment rate
    abandon_result = await db.execute(text("""
        SELECT
          COUNT(CASE WHEN event_type = 'BILLING_QUEUE_ABANDON' THEN 1 END) AS abandons,
          COUNT(CASE WHEN event_type = 'BILLING_QUEUE_JOIN'    THEN 1 END) AS joins
        FROM   events
        WHERE  store_id  = :store_id
          AND  is_staff  = false
          AND  timestamp >= :window_start
          AND  timestamp <= :window_end
    """), {"store_id": store_id,
           "window_start": window_start, "window_end": window_end})

    ab_row = abandon_result.fetchone()
    abandonment_rate = (
        round(ab_row.abandons / ab_row.joins, 4)
        if ab_row and ab_row.joins > 0
        else 0.0
    )

    log.info(
        "Metrics | store=%s date=%s visitors=%d conv=%.2f%% "
        "dwell=%.0fms queue=%d abandon=%.1f%%",
        store_id, date, unique_visitors,
        conversion_rate * 100, avg_dwell,
        queue_depth, abandonment_rate * 100,
    )

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