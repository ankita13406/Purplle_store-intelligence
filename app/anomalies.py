"""
anomalies.py — Real-time anomaly detection

Detects:
  BILLING_QUEUE_SPIKE    — queue depth > threshold (WARN/CRITICAL)
  CONVERSION_DROP        — conversion rate < 7-day rolling average by >20%
  DEAD_ZONE              — no ZONE_ENTER for a zone in last 30 minutes during open hours
  STALE_CAMERA_FEED      — no events from a camera in last 10 minutes
  HIGH_ABANDONMENT       — abandonment rate > 50%
"""
import logging
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Anomaly, AnomaliesResponse, AnomalySeverity
from app.pos_loader import get_conversion_history

log = logging.getLogger("anomalies")

QUEUE_SPIKE_WARN     = 5
QUEUE_SPIKE_CRITICAL = 10
DEAD_ZONE_MINUTES    = 30
STALE_FEED_MINUTES   = 10
CONVERSION_DROP_PCT  = 0.20   # 20% below 7-day avg
HIGH_ABANDON_RATE    = 0.50


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes_ago(n: int) -> str:
    return (_now() - timedelta(minutes=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago(n: int) -> str:
    return (_now() - timedelta(days=n)).strftime("%Y-%m-%d")


async def get_store_anomalies(store_id: str, db: AsyncSession) -> AnomaliesResponse:
    anomalies: list[Anomaly] = []
    now_iso = _now_iso()

    # ------------------------------------------------------------------
    # 1. Billing queue spike
    # ------------------------------------------------------------------
    q_result = await db.execute(text("""
        SELECT queue_depth FROM events
        WHERE store_id   = :store_id
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND queue_depth IS NOT NULL
          AND timestamp  >= :since
        ORDER BY timestamp DESC
        LIMIT 1
    """), {"store_id": store_id, "since": _minutes_ago(5)})
    row = q_result.fetchone()
    if row and row.queue_depth is not None:
        depth = row.queue_depth
        if depth >= QUEUE_SPIKE_CRITICAL:
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                store_id=store_id,
                anomaly_type="BILLING_QUEUE_SPIKE",
                severity=AnomalySeverity.CRITICAL,
                description=f"Billing queue depth is critically high: {depth} customers waiting.",
                suggested_action="Deploy additional billing counter immediately. Consider express checkout.",
                detected_at=now_iso,
                metadata={"queue_depth": depth, "threshold": QUEUE_SPIKE_CRITICAL},
            ))
        elif depth >= QUEUE_SPIKE_WARN:
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                store_id=store_id,
                anomaly_type="BILLING_QUEUE_SPIKE",
                severity=AnomalySeverity.WARN,
                description=f"Billing queue depth elevated: {depth} customers waiting.",
                suggested_action="Alert floor staff to open additional counter or assist with express billing.",
                detected_at=now_iso,
                metadata={"queue_depth": depth, "threshold": QUEUE_SPIKE_WARN},
            ))

    # ------------------------------------------------------------------
    # 2. Conversion drop vs 7-day average
    # ------------------------------------------------------------------
    history = get_conversion_history(store_id, days=7)
    if len(history) >= 3:
        avg_7d = sum(history) / len(history)
        today  = _today_conversion(store_id)   # estimated from current data
        if avg_7d > 0 and today is not None and today < avg_7d * (1 - CONVERSION_DROP_PCT):
            drop_pct = round((avg_7d - today) / avg_7d * 100, 1)
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                store_id=store_id,
                anomaly_type="CONVERSION_DROP",
                severity=AnomalySeverity.WARN,
                description=f"Conversion rate {today:.1%} is {drop_pct}% below 7-day average ({avg_7d:.1%}).",
                suggested_action="Review floor layout, check staff availability, inspect recent zone heatmap for engagement drops.",
                detected_at=now_iso,
                metadata={"today": today, "avg_7d": avg_7d, "drop_pct": drop_pct},
            ))

    # ------------------------------------------------------------------
    # 3. Dead zones (no visits in last 30 minutes)
    # ------------------------------------------------------------------
    active_zones_result = await db.execute(text("""
        SELECT DISTINCT zone_id FROM events
        WHERE store_id   = :store_id
          AND event_type = 'ZONE_ENTER'
          AND is_staff   = false
          AND timestamp  >= :lookback
          AND zone_id IS NOT NULL
    """), {"store_id": store_id, "lookback": _minutes_ago(60 * 24)})
    all_zones = {row.zone_id for row in active_zones_result.fetchall()}

    recent_zones_result = await db.execute(text("""
        SELECT DISTINCT zone_id FROM events
        WHERE store_id   = :store_id
          AND event_type = 'ZONE_ENTER'
          AND is_staff   = false
          AND timestamp  >= :since
          AND zone_id IS NOT NULL
    """), {"store_id": store_id, "since": _minutes_ago(DEAD_ZONE_MINUTES)})
    recent_zones = {row.zone_id for row in recent_zones_result.fetchall()}

    dead_zones = all_zones - recent_zones
    for zone in sorted(dead_zones):
        anomalies.append(Anomaly(
            anomaly_id=str(uuid.uuid4()),
            store_id=store_id,
            anomaly_type="DEAD_ZONE",
            severity=AnomalySeverity.INFO,
            description=f"Zone '{zone}' has had no customer visits in the last {DEAD_ZONE_MINUTES} minutes.",
            suggested_action=f"Check if '{zone}' zone is accessible. Consider a staff walkthrough or promotional display.",
            detected_at=now_iso,
            metadata={"zone_id": zone, "dead_minutes": DEAD_ZONE_MINUTES},
        ))

    # ------------------------------------------------------------------
    # 4. Stale camera feed
    # ------------------------------------------------------------------
    camera_result = await db.execute(text("""
        SELECT camera_id, MAX(timestamp) AS last_ts
        FROM events
        WHERE store_id = :store_id
        GROUP BY camera_id
    """), {"store_id": store_id})
    stale_threshold = _minutes_ago(STALE_FEED_MINUTES)
    for row in camera_result.fetchall():
        if row.last_ts and row.last_ts < stale_threshold:
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                store_id=store_id,
                anomaly_type="STALE_CAMERA_FEED",
                severity=AnomalySeverity.WARN,
                description=f"No events from camera '{row.camera_id}' in last {STALE_FEED_MINUTES} minutes.",
                suggested_action="Check camera connectivity and detection pipeline health.",
                detected_at=now_iso,
                metadata={"camera_id": row.camera_id, "last_event": row.last_ts},
            ))

    # ------------------------------------------------------------------
    # 5. High abandonment rate
    # ------------------------------------------------------------------
    ab_result = await db.execute(text("""
        SELECT
          COUNT(CASE WHEN event_type = 'BILLING_QUEUE_ABANDON' THEN 1 END) AS abandons,
          COUNT(CASE WHEN event_type = 'BILLING_QUEUE_JOIN' THEN 1 END) AS joins
        FROM events
        WHERE store_id  = :store_id
          AND is_staff  = false
          AND timestamp >= :since
    """), {"store_id": store_id, "since": _minutes_ago(60)})
    ab_row = ab_result.fetchone()
    if ab_row and ab_row.joins >= 5:
        rate = ab_row.abandons / ab_row.joins
        if rate >= HIGH_ABANDON_RATE:
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                store_id=store_id,
                anomaly_type="HIGH_ABANDONMENT",
                severity=AnomalySeverity.WARN,
                description=f"Billing queue abandonment rate is {rate:.0%} in the last hour.",
                suggested_action="Increase billing counter staffing. Consider mobile checkout options.",
                detected_at=now_iso,
                metadata={"rate": round(rate, 3), "abandons": ab_row.abandons, "joins": ab_row.joins},
            ))

    return AnomaliesResponse(store_id=store_id, anomalies=anomalies)


def _today_conversion(store_id: str) -> float | None:
    """Estimated today conversion — looks up from pos_loader's live tally."""
    from app.pos_loader import get_today_conversion
    return get_today_conversion(store_id)