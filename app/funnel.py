"""
funnel.py — Conversion funnel: Entry → Zone Visit → Billing Area → Purchase

Session is the unit (not raw events).
Re-entries do NOT double-count a visitor — we use DISTINCT visitor_id.
"""

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoreFunnel, FunnelStage
from app.pos_loader import get_converted_visitors

log = logging.getLogger("funnel")

WINDOW_HOURS = 24


def _window_start() -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def get_store_funnel(store_id: str, db: AsyncSession) -> StoreFunnel:
    """
    Build conversion funnel:
    Entry -> Zone Visit -> Billing Area -> Purchase
    """

    # Use the latest event date available for this store
    date_result = await db.execute(
        text("""
            SELECT substr(MAX(timestamp), 1, 10)
            FROM events
            WHERE store_id = :store_id
              AND is_staff = false
        """),
        {"store_id": store_id},
    )

    latest = date_result.scalar()
    date = latest or _today_str()
    window = f"{date}T00:00:00Z"

    # -------------------------------------------------------------
    # Stage 1: Entry
    # -------------------------------------------------------------
    entry_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :store_id
              AND event_type IN ('ENTRY', 'REENTRY')
              AND is_staff = false
              AND timestamp >= :window
        """),
        {
            "store_id": store_id,
            "window": window,
        },
    )

    total_entries = entry_result.scalar() or 0

    # -------------------------------------------------------------
    # Stage 2: Zone Visit
    # -------------------------------------------------------------
    zone_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'ZONE_DWELL'
              AND is_staff = false
              AND timestamp >= :window
        """),
        {
            "store_id": store_id,
            "window": window,
        },
    )

    zone_visits = zone_result.scalar() or 0

    # -------------------------------------------------------------
    # Stage 3: Billing Area Visit
    # -------------------------------------------------------------
    billing_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'ZONE_DWELL'
              AND zone_id = 'BILLING_AREA'
              AND is_staff = false
              AND timestamp >= :window
        """),
        {
            "store_id": store_id,
            "window": window,
        },
    )

    billing_visits = billing_result.scalar() or 0

    # -------------------------------------------------------------
    # Stage 4: Purchase (POS correlated)
    # -------------------------------------------------------------
    converted = get_converted_visitors(store_id, date)
    purchases = len(converted)

    print(
        f"DEBUG FUNNEL | "
        f"store={store_id} | "
        f"date={date} | "
        f"entries={total_entries} | "
        f"zone_visits={zone_visits} | "
        f"billing={billing_visits} | "
        f"purchases={purchases}"
    )

    # -------------------------------------------------------------
    # Drop-off calculation
    # -------------------------------------------------------------
    def drop_off(current: int, previous: int) -> float:
        if previous == 0:
            return 0.0
        return round(((previous - current) / previous) * 100, 1)

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