"""
pipeline/merge_events.py
========================
Merges all per-camera .jsonl event files into a single
chronologically sorted events_all.jsonl file.

Usage:
    python pipeline/merge_events.py data/events/ data/events_all.jsonl
"""

import json
import os
import sys
from pathlib import Path


def merge_events(events_dir: str, output_path: str):
    all_events = []
    files = list(Path(events_dir).glob("*.jsonl"))
    if not files:
        print(f"[merge] No .jsonl files found in {events_dir}")
        return 0

    for fpath in files:
        with open(fpath) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        all_events.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        print(f"[merge] Skipping malformed line in {fpath}: {e}")

    # Deduplicate by event_id
    seen = set()
    deduped = []
    for ev in all_events:
        if ev["event_id"] not in seen:
            seen.add(ev["event_id"])
            deduped.append(ev)

    # Sort by timestamp, then store_id
    deduped.sort(key=lambda e: (e["timestamp"], e["store_id"]))

    with open(output_path, "w") as f:
        for ev in deduped:
            f.write(json.dumps(ev) + "\n")

    print(f"[merge] {len(files)} files → {len(deduped)} unique events → {output_path}")
    return len(deduped)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python merge_events.py <events_dir> <output_path>")
        sys.exit(1)
    merge_events(sys.argv[1], sys.argv[2])