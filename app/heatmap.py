"""
heatmap.py — Zone visit frequency + avg dwell, normalised 0-100.
"""
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoreHeatmap, HeatmapZone

log = logging.getLogger("heatmap")

MIN_SESSIONS_FOR_CONFIDENCE = 20
WINDOW_HOURS = 24


def _window_start() -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def get_store_heatmap(store_id: str, db: AsyncSession) -> StoreHeatmap:
    # Use most recent event date, not today
    date_result = await db.execute(text("""
        SELECT substr(MAX(timestamp), 1, 10) FROM events
        WHERE store_id = :store_id AND is_staff = false
    """), {"store_id": store_id})
    latest = date_result.scalar()
    date   = latest or _today_str()
    window = date + "T00:00:00Z"

    # Unique sessions count (for confidence flag)
    sessions_result = await db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id) AS cnt
        FROM events
        WHERE store_id   = :store_id
          AND event_type IN ('ENTRY', 'REENTRY')
          AND is_staff   = false
          AND timestamp  >= :window
    """), {"store_id": store_id, "window": window})
    sessions = sessions_result.scalar() or 0
    has_confidence = sessions >= MIN_SESSIONS_FOR_CONFIDENCE

    # Zone stats
    zone_result = await db.execute(text("""
        SELECT
            zone_id,
            COUNT(DISTINCT visitor_id) AS visit_frequency,
            AVG(CASE WHEN dwell_ms > 0 THEN dwell_ms END) AS avg_dwell
        FROM events
        WHERE store_id  = :store_id
          AND event_type IN ('ZONE_ENTER', 'ZONE_EXIT', 'ZONE_DWELL')
          AND is_staff  = false
          AND zone_id IS NOT NULL
          AND timestamp >= :window
        GROUP BY zone_id
        ORDER BY visit_frequency DESC
    """), {"store_id": store_id, "window": window})

    rows = zone_result.fetchall()

    if not rows:
        return StoreHeatmap(store_id=store_id, date=date, zones=[])

    max_freq = max(r.visit_frequency for r in rows) or 1

    zones = [
        HeatmapZone(
            zone_id=row.zone_id,
            visit_frequency=row.visit_frequency,
            avg_dwell_ms=round(float(row.avg_dwell or 0), 1),
            normalised_score=round(row.visit_frequency / max_freq * 100, 1),
            data_confidence=has_confidence,
        )
        for row in rows
    ]

    return StoreHeatmap(store_id=store_id, date=date, zones=zones)