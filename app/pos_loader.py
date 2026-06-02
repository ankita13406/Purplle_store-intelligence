"""
pos_loader.py — Load pos_transactions.csv and correlate with visitor sessions.

Conversion rule:
  A visitor_id counts as converted if they were in any BILLING* zone
  within the 5-minute window immediately before a POS transaction
  for the same store.

This module is loaded once at startup and caches results in memory.
The correlation runs every time new events are ingested (called from ingest path).
"""
import csv
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock
from typing import Optional

log = logging.getLogger("pos_loader")

POS_WINDOW_MINUTES = 5
POS_FILE = os.getenv("POS_FILE", "data/pos_transactions.csv")

# ---------------------------------------------------------------------------
# In-memory state (populated at startup + updated on ingest)
# ---------------------------------------------------------------------------

_lock = Lock()

# store_id → date (YYYY-MM-DD) → set of visitor_ids confirmed as converted
_converted_visitors: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

# store_id → list of (timestamp_dt, basket_value) sorted by timestamp
_transactions: dict[str, list[tuple[datetime, float]]] = defaultdict(list)

# store_id → list of daily conversion rates (last 7 days) for anomaly detection
_daily_conversions: dict[str, list[float]] = defaultdict(list)

# live visitor→billing zone timestamps: store_id → visitor_id → last_billing_ts
_billing_presence: dict[str, dict[str, datetime]] = defaultdict(dict)

# today's running conversion count for anomaly detection
_today_conversions: dict[str, int] = defaultdict(int)
_today_entries: dict[str, int] = defaultdict(int)


def load_pos_file(pos_file: str = POS_FILE) -> int:
    """Load POS CSV at startup.  Returns number of transactions loaded."""
    path = Path(pos_file)
    if not path.exists():
        log.warning("POS file not found: %s — conversion rate will be 0", pos_file)
        return 0

    count = 0
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                store_id   = row["store_id"].strip()
                ts_str     = row["timestamp"].strip()
                basket_val = float(row.get("basket_value_inr", 0))

                ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                _transactions[store_id].append((ts, basket_val))
                count += 1
            except (KeyError, ValueError) as exc:
                log.warning("Skipping bad POS row: %s | %s", row, exc)

    # Sort by timestamp for efficient window lookup
    for store_id in _transactions:
        _transactions[store_id].sort(key=lambda x: x[0])

    log.info("Loaded %d POS transactions for %d stores", count, len(_transactions))
    return count


def record_billing_event(store_id: str, visitor_id: str, event_ts: str):
    """
    Called during ingest when a ZONE_ENTER/ZONE_DWELL in BILLING* zone is seen.
    Records the timestamp for later POS correlation.
    """
    try:
        ts = datetime.strptime(event_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return

    with _lock:
        _billing_presence[store_id][visitor_id] = ts


def record_entry(store_id: str, date: str):
    """Track total entries for live conversion rate."""
    with _lock:
        _today_entries[store_id] += 1


def run_conversion_correlation(store_id: str, date: str):
    """
    Correlate billing presence against POS transactions for store_id on date.
    Updates _converted_visitors in place.
    """
    transactions = _transactions.get(store_id, [])
    if not transactions:
        return

    target_date_start = datetime.strptime(date + "T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    target_date_end   = target_date_start + timedelta(days=1)

    # Filter to transactions on target date
    day_txns = [
        (ts, val) for ts, val in transactions
        if target_date_start <= ts < target_date_end
    ]

    if not day_txns:
        return

    with _lock:
        billing = dict(_billing_presence.get(store_id, {}))

    newly_converted: set[str] = set()

    for visitor_id, billing_ts in billing.items():
        for txn_ts, _ in day_txns:
            # Visitor was in billing zone within POS_WINDOW_MINUTES before transaction
            window_start = txn_ts - timedelta(minutes=POS_WINDOW_MINUTES)
            if window_start <= billing_ts <= txn_ts:
                newly_converted.add(visitor_id)
                break

    with _lock:
        _converted_visitors[store_id][date].update(newly_converted)
        _today_conversions[store_id] = len(_converted_visitors[store_id][date])


# ---------------------------------------------------------------------------
# Read API (called by metrics.py, anomalies.py)
# ---------------------------------------------------------------------------

def get_converted_visitors(store_id: str, date: str) -> set[str]:
    with _lock:
        return set(_converted_visitors[store_id].get(date, set()))


def get_conversion_history(store_id: str, days: int = 7) -> list[float]:
    """Return list of daily conversion rates for the last N days."""
    with _lock:
        return list(_daily_conversions[store_id][-days:])


def get_today_conversion(store_id: str) -> Optional[float]:
    """Best-estimate today's conversion rate for anomaly detection."""
    with _lock:
        entries   = _today_entries.get(store_id, 0)
        converted = _today_conversions.get(store_id, 0)
    if entries == 0:
        return None
    return converted / entries
