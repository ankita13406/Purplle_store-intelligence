"""
pipeline/pos_loader.py
======================
Loads and normalises POS transaction data from Purplle's real CSV format.

Real CSV columns: order_id, order_date, order_time, store_id,
                  product_id, brand_name, total_amount

Normalised output (one row per transaction timestamp):
    store_id, transaction_id, timestamp, basket_value_inr, brand_names

Used by:
  - pipeline/detect.py — for BILLING_QUEUE_ABANDON correlation
  - app/metrics.py     — for conversion rate computation
"""

import csv
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import List, Dict


def load_pos_transactions(csv_path: str) -> List[Dict]:
    """
    Load and normalise POS CSV.
    Groups line-items by (order_id, date, time) into single transactions.
    Handles both real Purplle format and the canonical pipeline format.

    Returns list of dicts:
        {store_id, transaction_id, timestamp, basket_value_inr, brand_names}
    """
    if not os.path.exists(csv_path):
        return []

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        return []

    cols = set(rows[0].keys())

    # ── Real Purplle format ────────────────────────────────────────────────
    if "order_date" in cols and "order_time" in cols:
        txn_map: Dict[tuple, Dict] = defaultdict(
            lambda: {"total": 0.0, "brands": set()}
        )
        for r in rows:
            key = (r["order_id"], r["order_date"], r["order_time"], r["store_id"])
            try:
                txn_map[key]["total"] += float(r["total_amount"])
            except (ValueError, TypeError):
                pass
            brand = r.get("brand_name", "").strip()
            if brand:
                txn_map[key]["brands"].add(brand)

        result = []
        for (order_id, date_str, time_str, store_id), data in sorted(txn_map.items()):
            try:
                dt = datetime.strptime(
                    f"{date_str} {time_str}", "%d-%m-%Y %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                try:
                    dt = datetime.strptime(
                        f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue

            result.append({
                "store_id":         store_id,
                "transaction_id":   f"TXN_{str(order_id).zfill(5)}",
                "timestamp":        dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "basket_value_inr": round(data["total"], 2),
                "brand_names":      "|".join(sorted(data["brands"])),
            })
        return result

    # ── Canonical pipeline format (already normalised) ─────────────────────
    elif "transaction_id" in cols and "timestamp" in cols:
        return [
            {
                "store_id":         r["store_id"],
                "transaction_id":   r["transaction_id"],
                "timestamp":        r["timestamp"],
                "basket_value_inr": float(r.get("basket_value_inr", 0)),
                "brand_names":      r.get("brand_names", ""),
            }
            for r in rows
        ]

    else:
        raise ValueError(
            f"Unrecognised POS CSV format. Columns found: {cols}"
        )


def get_transactions_in_window(
    transactions: List[Dict],
    store_id: str,
    window_start_ts: str,
    window_end_ts: str,
) -> List[Dict]:
    """
    Filter transactions for a store within a timestamp window (inclusive).
    Used for billing zone correlation: 5-minute window before transaction.
    """
    return [
        t for t in transactions
        if t["store_id"] == store_id
        and window_start_ts <= t["timestamp"] <= window_end_ts
    ]


def correlate_conversion(
    billing_entry_ts: str,
    store_id: str,
    transactions: List[Dict],
    window_minutes: int = 5,
) -> bool:
    """
    Returns True if a POS transaction occurred within `window_minutes`
    AFTER a visitor entered the billing zone.
    This is the definition used for conversion rate computation.
    """
    from datetime import timedelta

    t_billing = datetime.strptime(billing_entry_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    window_end = (t_billing + timedelta(minutes=window_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    matches = get_transactions_in_window(
        transactions, store_id, billing_entry_ts, window_end
    )
    return len(matches) > 0


if __name__ == "__main__":
    # Quick test
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "data/pos_transactions.csv"
    txns = load_pos_transactions(path)
    print(f"Loaded {len(txns)} transactions")
    for t in txns[:5]:
        print(t)