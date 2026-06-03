"""
scripts/restamp_and_replay.py
Restamps events.jsonl to TODAY's date so the metrics 24-hour window picks them up,
assigns new event_ids (so ingest accepts them as new), then replays to the API.

Run: python scripts/restamp_and_replay.py
"""
import json, uuid, httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path

INPUT_FILE  = "data/events.jsonl"
OUTPUT_FILE = "data/events_today.jsonl"
API_URL     = "http://localhost:8000"
BATCH_SIZE  = 200

# Load events
events = [json.loads(l) for l in open(INPUT_FILE) if l.strip()]
print(f"Loaded {len(events)} events from {INPUT_FILE}")

# Find original time range
orig_timestamps = sorted(e["timestamp"] for e in events)
orig_start = datetime.fromisoformat(orig_timestamps[0].replace("Z", "+00:00"))
orig_end   = datetime.fromisoformat(orig_timestamps[-1].replace("Z", "+00:00"))
orig_span  = (orig_end - orig_start).total_seconds()

# New base = today at 10:00 AM UTC
now  = datetime.now(timezone.utc)
base = now.replace(hour=10, minute=0, second=0, microsecond=0)

print(f"Restamping to: {base.strftime('%Y-%m-%d')} (today)")

# Restamp preserving relative ordering
restamped = []
for e in events:
    orig_ts  = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
    offset   = (orig_ts - orig_start).total_seconds()
    # Scale to 8-hour window
    new_offset = int(offset * (8 * 3600) / max(orig_span, 1))
    new_ts   = base + timedelta(seconds=new_offset)
    e2 = dict(e)
    e2["event_id"]  = str(uuid.uuid4())   # new ID = ingest accepts it
    e2["timestamp"] = new_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    restamped.append(e2)

# Write output
with open(OUTPUT_FILE, "w") as f:
    for e in restamped:
        f.write(json.dumps(e) + "\n")
print(f"Written {len(restamped)} restamped events to {OUTPUT_FILE}")
print(f"Time range: {restamped[0]['timestamp']} → {restamped[-1]['timestamp']}")

# Replay to API
print(f"\nReplaying to {API_URL} ...")
total_accepted = 0
total_errors   = 0

with httpx.Client(timeout=30) as client:
    for i in range(0, len(restamped), BATCH_SIZE):
        batch = restamped[i:i+BATCH_SIZE]
        try:
            r = client.post(f"{API_URL}/events/ingest", json={"events": batch})
            body = r.json()
            accepted = body.get("accepted", 0)
            errors   = len(body.get("errors", []))
            total_accepted += accepted
            total_errors   += errors
            print(f"  Batch {i}-{i+len(batch)}: accepted={accepted} errors={errors}")
        except Exception as ex:
            print(f"  Batch {i}: ERROR {ex}")
            total_errors += 1

print(f"\nReplay complete: accepted={total_accepted} errors={total_errors}")
print(f"Check: http://localhost:8000/stores/STORE_BLR_002/metrics")
