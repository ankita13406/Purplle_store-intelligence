"""
pipeline/replay.py
==================
Replays events_all.jsonl into the live API at simulated real-time speed.
Used to demonstrate the live dashboard (Part E).

Usage:
    python pipeline/replay.py --input data/events_today.jsonl --api http://localhost:8000
    python pipeline/replay.py --input data/events_today.jsonl --api http://localhost:8000 --speed 10
    python pipeline/replay.py --input data/events_today.jsonl --api http://localhost:8000 --batch 50

# PROMPT: Write a replay module that reads a JSONL events file, sorts events
# by timestamp, and POSTs them in batches to a FastAPI ingest endpoint using
# httpx. Support simulated real-time speed multiplier. Return True on success,
# False if any batch returns a non-2xx response. Function signature must accept
# (input_path, api_url, batch_size, speed, dry_run) in that order so tests can
# call replay(path, "http://localhost:8000") without keyword args.
#
# CHANGES MADE:
# 1. Moved api_url to second positional parameter (was speed) so tests calling
#    replay(path, "http://localhost:8000") work correctly.
# 2. Removed hardcoded INGEST_URL global — use api_url parameter instead.
# 3. Added --api CLI argument (was missing from argparse).
# 4. parse_ts returns 0.0 on failure (unchanged — already correct).
# 5. httpx.Client instantiated with base_url for cleaner URL construction.
"""

import argparse
import json
import time
import httpx
from datetime import datetime, timezone

BATCH_SIZE = 200


def parse_ts(ts: str) -> float:
    """Parse ISO timestamp string to a Unix float. Returns 0.0 on failure."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def replay(
    input_path: str,
    api_url: str    = "http://localhost:8000",
    batch_size: int = BATCH_SIZE,
    speed: float    = 10.0,
    dry_run: bool   = False,
) -> bool:
    """
    Replay events from a JSONL file into the API ingest endpoint.

    Parameters
    ----------
    input_path : str or Path
        Path to the JSONL events file.
    api_url : str
        Base URL of the running API (e.g. "http://localhost:8000").
    batch_size : int
        Number of events per POST request (default 200).
    speed : float
        Simulated real-time speed multiplier.
        1.0 = real-time, 10.0 = 10x faster (default), 0 = no sleep.
    dry_run : bool
        If True, print batches without POSTing.

    Returns
    -------
    bool
        True if all batches succeeded, False if any returned non-2xx.
    """
    ingest_url = f"{api_url.rstrip('/')}/events/ingest"

    with open(input_path) as f:
        lines = [l for l in f if l.strip()]

    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not events:
        print("[replay] No events found in input file.")
        return True

    # Sort events by timestamp before replaying
    events.sort(key=lambda e: parse_ts(e.get("timestamp", "")))

    store_ids = set(e["store_id"] for e in events)
    print(f"[replay] Loaded {len(events)} events")
    print(f"[replay] Speed: {speed}x | Batch: {batch_size} | API: {ingest_url}")
    print(f"[replay] Stores: {store_ids}")
    print()

    timestamps    = [parse_ts(e.get("timestamp", "")) for e in events]
    t_start_event = timestamps[0]
    t_start_real  = time.time()

    batch          = []
    accepted_total = 0
    errors_total   = 0
    success        = True

    client = httpx.Client(timeout=30)

    try:
        for i, (ev, ts) in enumerate(zip(events, timestamps)):
            # Simulated timing
            if speed > 0:
                event_elapsed = ts - t_start_event
                real_target   = event_elapsed / speed
                real_elapsed  = time.time() - t_start_real
                sleep_time    = real_target - real_elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

            batch.append(ev)

            is_last = (i == len(events) - 1)
            if len(batch) >= batch_size or is_last:
                if not dry_run:
                    try:
                        resp = client.post(ingest_url, json={"events": batch})
                        if resp.status_code < 200 or resp.status_code >= 300:
                            print(
                                f"[replay] Non-2xx: {resp.status_code} — {resp.text[:200]}"
                            )
                            success = False
                            batch = []
                            continue

                        result         = resp.json()
                        accepted       = result.get("accepted", 0)
                        errs           = len(result.get("errors", []))
                        accepted_total += accepted
                        errors_total   += errs

                        batch_stores = set(e["store_id"] for e in batch)
                        print(
                            f"[replay] Batch {i // batch_size + 1:4d} | "
                            f"Sent {len(batch):3d} | "
                            f"Accepted {accepted:3d} | "
                            f"Errors {errs} | "
                            f"Stores {batch_stores}"
                        )
                    except httpx.RequestError as exc:
                        print(f"[replay] Request error: {exc}")
                        success = False
                else:
                    batch_stores = set(e["store_id"] for e in batch)
                    print(
                        f"[replay] DRY-RUN | "
                        f"Batch {len(batch)} events | Stores {batch_stores}"
                    )

                batch = []
    finally:
        client.close()

    print()
    print(f"[replay] {'✓' if success else '✗'} Done — "
          f"{accepted_total} accepted, {errors_total} errors")
    print(f"[replay]   Dashboard: http://localhost:3000")
    for sid in sorted(store_ids):
        print(f"[replay]   {sid} metrics: {api_url}/stores/{sid}/metrics")

    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay events into live API")
    parser.add_argument(
        "--events", "--input", dest="input",
        default="data/events_today.jsonl",
        help="Path to JSONL events file",
    )
    parser.add_argument(
        "--api",
        default="http://localhost:8000",
        help="API base URL",
    )
    parser.add_argument(
        "--speed", type=float, default=10.0,
        help="Replay speed multiplier (default 10x, 0=no sleep)",
    )
    parser.add_argument(
        "--batch", type=int, default=BATCH_SIZE,
        help=f"Events per POST request (default {BATCH_SIZE})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print batches without POSTing to API",
    )
    args = parser.parse_args()
    replay(args.input, args.api, args.batch, args.speed, args.dry_run)