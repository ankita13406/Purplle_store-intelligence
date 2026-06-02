"""
replay.py — Replay a .jsonl event file into the API in batches.
Used when the detection pipeline ran offline and you want to seed the API.
Simulates real-time by sleeping proportionally between event timestamps.
"""
import argparse
import json
import logging
import time
import sys
from pathlib import Path
from datetime import datetime, timezone

import httpx

log = logging.getLogger("replay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BATCH_SIZE = 200


def parse_ts(ts: str) -> float:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except Exception:
        return 0.0


def replay(events_path: Path, api_base: str, simulate_realtime: bool = False):
    with open(events_path) as f:
        events = [json.loads(line) for line in f if line.strip()]

    log.info("Loaded %d events from %s", len(events), events_path)

    client = httpx.Client(base_url=api_base, timeout=15.0)

    # Sort by timestamp for correct ordering
    events.sort(key=lambda e: parse_ts(e.get("timestamp", "")))

    total = len(events)
    sent  = 0
    errors = 0
    prev_ts = None

    for i in range(0, total, BATCH_SIZE):
        batch = events[i : i + BATCH_SIZE]

        if simulate_realtime and prev_ts is not None:
            batch_ts = parse_ts(batch[0].get("timestamp", ""))
            if batch_ts > prev_ts:
                sleep_s = min(batch_ts - prev_ts, 2.0)  # cap at 2s
                time.sleep(sleep_s)

        try:
            resp = client.post("/events/ingest", json={"events": batch})
            if resp.status_code in (200, 201, 207):
                result = resp.json()
                accepted = result.get("accepted", len(batch))
                sent += accepted
                log.info(
                    "Batch %d-%d → accepted=%d | total sent=%d/%d",
                    i, i + len(batch), accepted, sent, total,
                )
            else:
                log.error("HTTP %d: %s", resp.status_code, resp.text[:200])
                errors += len(batch)
        except Exception as exc:
            log.error("Request failed: %s", exc)
            errors += len(batch)

        prev_ts = parse_ts(batch[-1].get("timestamp", ""))

    client.close()
    log.info("Replay complete: sent=%d errors=%d", sent, errors)
    return errors == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", required=True)
    parser.add_argument("--api",    default="http://localhost:8000")
    parser.add_argument("--realtime", action="store_true")
    args = parser.parse_args()

    ok = replay(Path(args.events), args.api, args.realtime)
    sys.exit(0 if ok else 1)
