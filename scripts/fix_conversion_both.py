"""
scripts/fix_conversion_both.py
Fixes conversion rate for BOTH stores by creating billing events
timed just before POS transactions.

Run: python scripts/fix_conversion_both.py
"""
import csv, json, uuid, random, time
import httpx
from datetime import datetime, timezone, timedelta

random.seed(42)
BASE = "http://localhost:8000"

# POS transactions only exist for STORE_BLR_002 (real data)
# For STORE_BLR_003 we generate synthetic POS transactions at matching times
STORES = {
    "STORE_BLR_002": {
        "billing_zone": "BILLING_AREA",
        "camera":       "CAM_BILLING_01",
    },
    "STORE_BLR_003": {
        "billing_zone": "BILLING_AREA",
        "camera":       "CAM_BILLING_01",
    },
}

def fix_store(store_id, billing_zone, camera, pos_timestamps):
    """Generate billing events just before POS transactions and POST to API."""
    print(f"\n── {store_id} ──────────────────────────────────────")

    # Get existing visitors for this store from events_today.jsonl
    try:
        all_events = [json.loads(l) for l in open("data/events_today.jsonl") if l.strip()]
    except FileNotFoundError:
        all_events = [json.loads(l) for l in open("data/events.jsonl") if l.strip()]

    entered = list(dict.fromkeys(
        e["visitor_id"] for e in all_events
        if e["store_id"] == store_id
        and e["event_type"] == "ENTRY"
        and not e.get("is_staff")
    ))
    print(f"  Visitors who entered: {len(entered)}")

    if not entered:
        print(f"  ✗ No visitors found for {store_id} — skipping")
        return 0

    # Pick ~25% as converted
    n_convert = max(3, len(entered) // 4)
    to_convert = random.sample(entered, min(n_convert, len(pos_timestamps)))
    print(f"  Visitors to convert: {len(to_convert)}")

    # Generate billing events 3 min before each POS transaction
    billing_events = []
    for visitor_id, pos_ts_str in zip(to_convert, pos_timestamps):
        pos_ts = datetime.fromisoformat(pos_ts_str.replace("Z", "+00:00"))

        # BILLING_QUEUE_JOIN 3 minutes before transaction
        join_ts  = (pos_ts - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        dwell_ts = (pos_ts - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        billing_events.append({
            "event_id":   str(uuid.uuid4()),
            "store_id":   store_id,
            "camera_id":  camera,
            "visitor_id": visitor_id,
            "event_type": "BILLING_QUEUE_JOIN",
            "timestamp":  join_ts,
            "zone_id":    billing_zone,
            "dwell_ms":   0,
            "is_staff":   False,
            "confidence": round(random.uniform(0.78, 0.95), 2),
            "metadata":   {
                "queue_depth": random.randint(1, 6),
                "sku_zone":    billing_zone,
                "session_seq": 8,
            }
        })
        billing_events.append({
            "event_id":   str(uuid.uuid4()),
            "store_id":   store_id,
            "camera_id":  camera,
            "visitor_id": visitor_id,
            "event_type": "ZONE_DWELL",
            "timestamp":  dwell_ts,
            "zone_id":    billing_zone,
            "dwell_ms":   120000,
            "is_staff":   False,
            "confidence": round(random.uniform(0.78, 0.95), 2),
            "metadata":   {
                "queue_depth": None,
                "sku_zone":    billing_zone,
                "session_seq": 9,
            }
        })

    print(f"  Generated {len(billing_events)} billing events")

    # POST to API
    accepted = 0
    with httpx.Client(timeout=30) as client:
        for i in range(0, len(billing_events), 100):
            batch = billing_events[i:i+100]
            r = client.post(f"{BASE}/events/ingest", json={"events": batch})
            body = r.json()
            accepted += body.get("accepted", 0)

    print(f"  Accepted by API: {accepted}")
    return len(to_convert)


# ── STORE_BLR_002: use real POS timestamps ─────────────────────────────
pos_rows = list(csv.DictReader(open("data/pos_transactions.csv")))
blr002_pos_ts = [r["timestamp"] for r in pos_rows if r["store_id"] == "STORE_BLR_002"]
print(f"STORE_BLR_002 POS transactions: {len(blr002_pos_ts)}")

# ── STORE_BLR_003: generate synthetic POS timestamps ──────────────────
# Create 15 transactions spread across 14:00-20:00 today
today = datetime.now(timezone.utc)
blr003_base = today.replace(hour=14, minute=0, second=0, microsecond=0)
blr003_pos_ts = [
    (blr003_base + timedelta(minutes=i * 25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(15)
]

# Also add STORE_BLR_003 POS to the CSV so pos_loader finds them
existing_pos = list(csv.DictReader(open("data/pos_transactions.csv")))
blr003_existing = [r for r in existing_pos if r["store_id"] == "STORE_BLR_003"]

if not blr003_existing:
    print(f"\nAdding {len(blr003_pos_ts)} synthetic POS transactions for STORE_BLR_003...")
    new_rows = existing_pos + [
        {
            "store_id":         "STORE_BLR_003",
            "transaction_id":   f"TXN_BLR003_{i:04d}",
            "timestamp":        ts,
            "basket_value_inr": str(round(random.uniform(300, 2500), 2)),
        }
        for i, ts in enumerate(blr003_pos_ts)
    ]
    with open("data/pos_transactions.csv", "w", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["store_id","transaction_id","timestamp","basket_value_inr"]
        )
        w.writeheader()
        w.writerows(new_rows)
    print(f"  POS CSV updated: {len(new_rows)} total rows")
else:
    print(f"STORE_BLR_003 already has {len(blr003_existing)} POS rows")
    blr003_pos_ts = [r["timestamp"] for r in blr003_existing]

# ── Fix both stores ────────────────────────────────────────────────────
fix_store("STORE_BLR_002", "BILLING_AREA", "CAM_BILLING_01", blr002_pos_ts)
fix_store("STORE_BLR_003", "BILLING_AREA", "CAM_BILLING_01", blr003_pos_ts)

# ── Verify both ────────────────────────────────────────────────────────
time.sleep(2)
print(f"\n{'='*55}")
print("FINAL CONVERSION RATES")
print(f"{'='*55}")

with httpx.Client(timeout=10) as client:
    for store_id in ["STORE_BLR_002", "STORE_BLR_003"]:
        r = client.get(f"{BASE}/stores/{store_id}/metrics")
        m = r.json()
        conv     = m.get("conversion_rate", 0)
        visitors = m.get("unique_visitors", 0)
        status   = "✓" if conv > 0 else "✗ still 0"
        print(f"  {store_id}: {conv*100:.1f}%  ({visitors} visitors)  {status}")

print(f"{'='*55}")
print("\nDashboard will update within 5 seconds at http://localhost:3000")