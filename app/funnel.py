
"""
funnel.py — Conversion funnel: Entry → Zone Visit → Billing Area → Purchase

Session is the unit (not raw events).
Re-entries do NOT double-count a visitor — we use DISTINCT visitor_id.

FIXES:
- Uses busiest trading day instead of MAX(timestamp)
- Uses explicit day window (window_start/window_end)
- Billing zone detection supports any zone containing:
    BILLING, CHECKOUT, COUNTER
- Zone Visit counts both ZONE_ENTER and ZONE_DWELL
- Funnel stages are HIERARCHICAL (monotonic):
      Entry >= Zone >= Billing >= Purchase
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoreFunnel, FunnelStage
from app.pos_loader import get_converted_visitors

log = logging.getLogger("funnel")


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def get_store_funnel(store_id: str, db: AsyncSession) -> StoreFunnel:
    """
    Build conversion funnel:

        Entry
          ↓
        Zone Visit
          ↓
        Billing Area
          ↓
        Purchase

    Uses visitor-set intersections to guarantee a valid funnel.
    """

    # --------------------------------------------------------------
    # Determine busiest trading day
    # --------------------------------------------------------------
    date_result = await db.execute(
        text("""
        SELECT substr(timestamp, 1, 10) AS event_date
        FROM events
        WHERE store_id = :store_id
          AND is_staff = false
        GROUP BY event_date
        ORDER BY COUNT(*) DESC
        LIMIT 1
        """),
        {"store_id": store_id},
    )

    date = date_result.scalar()

    if not date:
        log.warning(
            "No events found for store %s — returning empty funnel",
            store_id,
        )

        empty = [
            FunnelStage(stage="Entry", count=0, drop_off_pct=0.0),
            FunnelStage(stage="Zone Visit", count=0, drop_off_pct=0.0),
            FunnelStage(stage="Billing Area", count=0, drop_off_pct=0.0),
            FunnelStage(stage="Purchase", count=0, drop_off_pct=0.0),
        ]

        return StoreFunnel(
            store_id=store_id,
            date=_today_str(),
            stages=empty,
            sessions=0,
        )

    window_start = f"{date}T00:00:00Z"
    window_end = f"{date}T23:59:59Z"

    params = {
        "store_id": store_id,
        "window_start": window_start,
        "window_end": window_end,
    }

    log.info(
        "Funnel window for %s: %s → %s",
        store_id,
        window_start,
        window_end,
    )

    # --------------------------------------------------------------
    # Stage 1: Entry Visitors
    # --------------------------------------------------------------
    entry_result = await db.execute(
        text("""
        SELECT DISTINCT visitor_id
        FROM events
        WHERE store_id = :store_id
          AND event_type IN ('ENTRY', 'REENTRY')
          AND is_staff = false
          AND timestamp >= :window_start
          AND timestamp <= :window_end
        """),
        params,
    )

    entry_visitors = set(entry_result.scalars().all())

    # --------------------------------------------------------------
    # Stage 2: Zone Visitors
    # --------------------------------------------------------------
    zone_result = await db.execute(
        text("""
        SELECT DISTINCT visitor_id
        FROM events
        WHERE store_id = :store_id
          AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')
          AND is_staff = false
          AND timestamp >= :window_start
          AND timestamp <= :window_end
        """),
        params,
    )

    zone_visitors = set(zone_result.scalars().all())

    # Enforce hierarchy
    zone_visitors &= entry_visitors

    # --------------------------------------------------------------
    # Stage 3: Billing Visitors
    # --------------------------------------------------------------
    billing_result = await db.execute(
        text("""
        SELECT DISTINCT visitor_id
        FROM events
        WHERE store_id = :store_id
          AND event_type IN (
                'ZONE_ENTER',
                'ZONE_DWELL',
                'BILLING_QUEUE_JOIN'
          )
          AND is_staff = false
          AND (
                UPPER(COALESCE(zone_id, '')) LIKE '%BILLING%'
             OR UPPER(COALESCE(zone_id, '')) LIKE '%CHECKOUT%'
             OR UPPER(COALESCE(zone_id, '')) LIKE '%COUNTER%'
          )
          AND timestamp >= :window_start
          AND timestamp <= :window_end
        """),
        params,
    )

    billing_visitors = set(billing_result.scalars().all())

    # Enforce hierarchy
    billing_visitors &= zone_visitors

    # --------------------------------------------------------------
    # Stage 4: Purchases
    # --------------------------------------------------------------
    from app.pos_loader import run_conversion_correlation

    run_conversion_correlation(store_id, date)

    converted = set(get_converted_visitors(store_id, date))

    purchase_visitors = converted & billing_visitors

    # --------------------------------------------------------------
    # Counts
    # --------------------------------------------------------------
    total_entries = len(entry_visitors)
    zone_visits = len(zone_visitors)
    billing_visits = len(billing_visitors)
    purchases = len(purchase_visitors)

    log.info(
        "Funnel | store=%s date=%s entries=%d zone=%d billing=%d purchases=%d",
        store_id,
        date,
        total_entries,
        zone_visits,
        billing_visits,
        purchases,
    )

    # --------------------------------------------------------------
    # Drop-off helper
    # --------------------------------------------------------------
    def drop_off(current: int, previous: int) -> float:
        if previous == 0:
            return 0.0

        return round(
            ((previous - current) / previous) * 100,
            1,
        )

    stages = [
        FunnelStage(
            stage="Entry",
            count=total_entries,
            drop_off_pct=0.0,
        ),
        FunnelStage(
            stage="Zone Visit",
            count=zone_visits,
            drop_off_pct=drop_off(zone_visits, total_entries),
        ),
        FunnelStage(
            stage="Billing Area",
            count=billing_visits,
            drop_off_pct=drop_off(billing_visits, zone_visits),
        ),
        FunnelStage(
            stage="Purchase",
            count=purchases,
            drop_off_pct=drop_off(purchases, billing_visits),
        ),
    ]

    return StoreFunnel(
        store_id=store_id,
        date=date,
        stages=stages,
        sessions=total_entries,
    )