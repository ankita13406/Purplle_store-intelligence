"""
scripts/debug_conversion.py
Diagnoses why conversion_rate is 0.0
Run: python scripts/debug_conversion.py
"""
import httpx
import json

BASE = "http://localhost:8000"
STORE = "STORE_BLR_002"

print("=" * 60)
print("CONVERSION RATE DIAGNOSIS")
print("=" * 60)

# 1. Check metrics
r = httpx.get(f"{BASE}/stores/{STORE}/metrics")
m = r.json()
print(f"\n1. Metrics for {STORE}:")
print(f"   unique_visitors:  {m.get('unique_visitors')}")
print(f"   conversion_rate:  {m.get('conversion_rate')}")
print(f"   date:             {m.get('date')}")

# 2. Check funnel
r = httpx.get(f"{BASE}/stores/{STORE}/funnel")
f = r.json()
print(f"\n2. Funnel stages:")
for s in f.get("stages", []):
    print(f"   {s['stage']:<20} count={s['count']}")

# 3. Check what events exist via ingest test
print(f"\n3. Testing billing event recording...")
import uuid
from datetime import datetime, timezone, timedelta

now = datetime.now(timezone.utc)
billing_ts = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
txn_ts     = now.strftime("%Y-%m-%dT%H:%M:%SZ")
test_vid   = "VIS_debugtest01"

# Send a billing zone event
billing_event = {
    "event_id":   str(uuid.uuid4()),
    "store_id":   STORE,
    "camera_id":  "CAM_BILLING_01",
    "visitor_id": test_vid,
    "event_type": "BILLING_QUEUE_JOIN",
    "timestamp":  billing_ts,
    "zone_id":    "BILLING_AREA",
    "dwell_ms":   0,
    "is_staff":   False,
    "confidence": 0.91,
    "metadata":   {"queue_depth": 3, "sku_zone": None, "session_seq": 5}
}
r = httpx.post(f"{BASE}/events/ingest", json={"events": [billing_event]})
print(f"   Billing event ingest: {r.status_code} → {r.json()}")

# Wait a moment and check metrics
import time
time.sleep(1)
r = httpx.get(f"{BASE}/stores/{STORE}/metrics")
m2 = r.json()
print(f"\n4. Metrics after billing event:")
print(f"   conversion_rate: {m2.get('conversion_rate')}")
print(f"   (still 0 = POS date mismatch)")
print(f"   (non-zero = billing tracking fixed but POS still missing)")

# 4. Check POS file directly
print(f"\n5. POS transactions file check:")
try:
    import csv
    rows = list(csv.DictReader(open("data/pos_transactions.csv")))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_rows = [r for r in rows if r["timestamp"][:10] == today]
    print(f"   Total POS rows:      {len(rows)}")
    print(f"   POS rows for today ({today}): {len(today_rows)}")
    if rows:
        print(f"   POS date range: {rows[0]['timestamp'][:10]} → {rows[-1]['timestamp'][:10]}")
    if not today_rows:
        print("   *** POS file has NO transactions for today ***")
        print("   *** Run: python scripts/restamp_pos.py then rebuild Docker ***")
except Exception as e:
    print(f"   ERROR reading POS file: {e}")

# 5. Check event dates
print(f"\n6. Event dates in events_today.jsonl:")
try:
    from collections import Counter
    events = [json.loads(l) for l in open("data/events_today.jsonl") if l.strip()]
    dates = Counter(e["timestamp"][:10] for e in events)
    billing_events = [e for e in events
                      if e.get("zone_id") and "BILLING" in e.get("zone_id","").upper()]
    print(f"   Total events: {len(events)}")
    print(f"   Date breakdown: {dict(dates)}")
    print(f"   Billing zone events: {len(billing_events)}")
    if billing_events:
        print(f"   Sample billing event ts: {billing_events[0]['timestamp']}")
except Exception as e:
    print(f"   ERROR: {e}")

print("\n" + "=" * 60)
print("DIAGNOSIS COMPLETE")
print("=" * 60)
print("\nIf POS date != event date → run restamp_pos.py + rebuild Docker")
print("If billing events = 0 → events.jsonl missing BILLING_QUEUE_JOIN")
print("If conversion still 0 after fix → check pos_loader._billing_presence")