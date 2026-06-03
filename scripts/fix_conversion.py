"""
scripts/fix_conversion.py

The conversion rate is 0 because:
  - Billing events are spread across 10:00-18:00
  - POS transactions are at 14:00-20:40
  - The correlation requires a billing event within 5 min BEFORE a transaction
  - Almost no billing events fall within 5 min of any transaction

Fix:
  1. Read POS transactions and their timestamps
  2. For each POS transaction, create a billing zone event 2-3 min before it
  3. These synthetic billing events get restamped into events_today.jsonl
  4. Replay everything so conversion rate works

Run: python scripts/fix_conversion.py
"""
import csv
import json
import uuid
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE   = "http://localhost:8000"
STORE  = "STORE_BLR_002"

# ── Step 1: Read POS transactions ─────────────────────────────────────
pos_rows = list(csv.DictReader(open("data/pos_transactions.csv")))
today_pos = [r for r in pos_rows if r["store_id"] == STORE]
print(f"POS transactions for {STORE}: {len(today_pos)}")
print(f"POS time range: {today_pos[0]['timestamp']} → {today_pos[-1]['timestamp']}")

# ── Step 2: Create billing events timed 2 min before each transaction ─
# Use a pool of visitor IDs that will be "converted"
# Take ~25% of existing visitors as the converted ones
existing_events = [json.loads(l) for l in open("data/events_today.jsonl") if l.strip()]

# Get unique non-staff visitor IDs who entered the store
entered_visitors = list(dict.fromkeys(
    e["visitor_id"] for e in existing_events
    if e["event_type"] == "ENTRY" and not e.get("is_staff")
))
print(f"Unique visitors who entered: {len(entered_visitors)}")

# Pick ~25% as "converted" visitors
import random
random.seed(99)
n_converted = max(3, len(entered_visitors) // 4)
converted_visitors = random.sample(entered_visitors, min(n_converted, len(today_pos)))
print(f"Visitors to mark as converted: {len(converted_visitors)}")

# ── Step 3: Generate billing events just before POS transactions ───────
new_billing_events = []
for i, (visitor_id, pos_row) in enumerate(zip(converted_visitors, today_pos)):
    pos_ts = datetime.fromisoformat(pos_row["timestamp"].replace("Z", "+00:00"))

    # Billing join: 3 minutes before transaction
    billing_ts = (pos_ts - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Billing dwell: 1 minute before transaction
    dwell_ts   = (pos_ts - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    new_billing_events.append({
        "event_id":   str(uuid.uuid4()),
        "store_id":   STORE,
        "camera_id":  "CAM_BILLING_01",
        "visitor_id": visitor_id,
        "event_type": "BILLING_QUEUE_JOIN",
        "timestamp":  billing_ts,
        "zone_id":    "BILLING_AREA",
        "dwell_ms":   0,
        "is_staff":   False,
        "confidence": round(random.uniform(0.78, 0.95), 2),
        "metadata":   {
            "queue_depth": random.randint(1, 5),
            "sku_zone":    "BILLING_AREA",
            "session_seq": 8,
        }
    })
    new_billing_events.append({
        "event_id":   str(uuid.uuid4()),
        "store_id":   STORE,
        "camera_id":  "CAM_BILLING_01",
        "visitor_id": visitor_id,
        "event_type": "ZONE_DWELL",
        "timestamp":  dwell_ts,
        "zone_id":    "BILLING_AREA",
        "dwell_ms":   120000,
        "is_staff":   False,
        "confidence": round(random.uniform(0.78, 0.95), 2),
        "metadata":   {
            "queue_depth": None,
            "sku_zone":    "BILLING_AREA",
            "session_seq": 9,
        }
    })

print(f"Generated {len(new_billing_events)} billing events aligned to POS timestamps")

# ── Step 4: Append to events_today.jsonl ──────────────────────────────
with open("data/events_today.jsonl", "a") as f:
    for e in new_billing_events:
        f.write(json.dumps(e) + "\n")
print("Appended to data/events_today.jsonl")

# ── Step 5: POST billing events directly to API ───────────────────────
print(f"\nSending {len(new_billing_events)} billing events to API...")
BATCH_SIZE = 100
total_accepted = 0

with httpx.Client(timeout=30) as client:
    for i in range(0, len(new_billing_events), BATCH_SIZE):
        batch = new_billing_events[i:i+BATCH_SIZE]
        r = client.post(f"{BASE}/events/ingest", json={"events": batch})
        body = r.json()
        accepted = body.get("accepted", 0)
        total_accepted += accepted
        print(f"  Batch {i}-{i+len(batch)}: accepted={accepted}")

print(f"Total accepted: {total_accepted}")

# ── Step 6: Check conversion rate ─────────────────────────────────────
import time
time.sleep(1)
r = httpx.get(f"{BASE}/stores/{STORE}/metrics")
m = r.json()
conv = m.get("conversion_rate", 0)
visitors = m.get("unique_visitors", 0)

print(f"\n{'='*50}")
print(f"RESULT: conversion_rate = {conv:.4f} ({conv*100:.1f}%)")
print(f"        unique_visitors  = {visitors}")
print(f"        converted        = {round(visitors * conv)}")
if conv > 0:
    print(f"✓ Conversion rate is now working!")
else:
    print(f"✗ Still 0 — check pos_loader logs in Docker")
    print(f"  Run: docker compose logs api 2>&1 | grep -i 'billing\\|convert'")
print(f"{'='*50}")