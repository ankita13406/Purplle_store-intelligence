"""
emit.py — Event schema validation + emission

Handles two output channels:
  1. Local JSONL file (always written — source of truth for replay)
  2. Live HTTP POST to /events/ingest (optional, for Part E real-time dashboard)

Batching strategy:
  - Buffer up to BATCH_SIZE events or BATCH_INTERVAL_SECONDS, then flush.
  - On API failure: log warning, continue writing to JSONL (no data loss).
  - Events are schema-validated before write; invalid events are logged
    and written to a separate .rejected.jsonl for debugging.
"""

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

log = logging.getLogger("emit")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

VALID_EVENT_TYPES = {
    "ENTRY",
    "EXIT",
    "ZONE_ENTER",
    "ZONE_EXIT",
    "ZONE_DWELL",
    "BILLING_QUEUE_JOIN",
    "BILLING_QUEUE_ABANDON",
    "REENTRY",
}

REQUIRED_FIELDS = {
    "event_id",
    "store_id",
    "camera_id",
    "visitor_id",
    "event_type",
    "timestamp",
    "is_staff",
    "confidence",
}


def validate_event(event: dict) -> list[str]:
    """
    Validate an event dict against the schema.
    Returns list of validation error strings (empty = valid).
    """
    errors: list[str] = []

    # Required fields present
    for field in REQUIRED_FIELDS:
        if field not in event:
            errors.append(f"missing required field: {field}")

    # event_type in catalogue
    et = event.get("event_type")
    if et and et not in VALID_EVENT_TYPES:
        errors.append(f"unknown event_type: {et}")

    # event_id is UUID-like
    eid = event.get("event_id", "")
    if not eid or len(eid) < 8:
        errors.append(f"invalid event_id: {eid!r}")

    # timestamp is ISO-8601
    ts = event.get("timestamp", "")
    try:
        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        errors.append(f"invalid timestamp format: {ts!r}")

    # confidence in [0, 1]
    conf = event.get("confidence")
    if conf is not None and not (0.0 <= float(conf) <= 1.0):
        errors.append(f"confidence out of range: {conf}")

    # visitor_id non-empty
    vid = event.get("visitor_id", "")
    if not vid:
        errors.append("visitor_id must be non-empty")

    # zone_id required for zone events
    if et in {"ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL"} and not event.get("zone_id"):
        errors.append(f"{et} event missing zone_id")

    # dwell_ms non-negative
    dwell = event.get("dwell_ms", 0)
    if dwell is not None and dwell < 0:
        errors.append(f"dwell_ms must be >= 0, got {dwell}")

    # metadata present (can be empty dict)
    if "metadata" not in event:
        errors.append("metadata field missing")

    return errors


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------

BATCH_SIZE             = 100        # flush after this many events
BATCH_INTERVAL_SECONDS = 2.0        # or after this many seconds


class EventEmitter:
    """
    Thread-safe (single-threaded use assumed) event emitter.

    Usage:
        emitter = EventEmitter(output_path=Path("events.jsonl"), api_endpoint="http://localhost:8000")
        emitter.emit(event_dict)
        ...
        emitter.close()
    """

    def __init__(
        self,
        output_path: Path,
        api_endpoint: Optional[str] = None,
        batch_size: int = BATCH_SIZE,
        batch_interval: float = BATCH_INTERVAL_SECONDS,
    ):
        self.output_path   = output_path
        self.api_endpoint  = api_endpoint
        self.batch_size    = batch_size
        self.batch_interval = batch_interval

        self._buffer: list[dict] = []
        self._last_flush = time.monotonic()
        self._total_emitted = 0
        self._total_rejected = 0

        self._out_fh = open(output_path, "a", buffering=1)   # line-buffered
        rejected_path = output_path.with_suffix(".rejected.jsonl")
        self._rej_fh  = open(rejected_path, "a", buffering=1)

        if api_endpoint:
            try:
                import httpx
                self._http = httpx.Client(
                    base_url=api_endpoint,
                    timeout=5.0,
                    headers={"Content-Type": "application/json"},
                )
                log.info("Live API endpoint configured: %s", api_endpoint)
            except ImportError:
                log.warning("httpx not installed — live API posting disabled")
                self._http = None
                self.api_endpoint = None
        else:
            self._http = None

        log.info("EventEmitter ready → %s", output_path)

    # ------------------------------------------------------------------

    def emit(self, event: dict) -> bool:
        """
        Validate and buffer one event.
        Returns True if event was accepted, False if rejected.
        """
        errors = validate_event(event)

        if errors:
            self._total_rejected += 1
            log.warning(
                "Event rejected (visitor=%s type=%s): %s",
                event.get("visitor_id"), event.get("event_type"), "; ".join(errors),
            )
            self._rej_fh.write(json.dumps({"event": event, "errors": errors}) + "\n")
            return False

        self._buffer.append(event)
        self._total_emitted += 1

        # Flush conditions
        elapsed = time.monotonic() - self._last_flush
        if len(self._buffer) >= self.batch_size or elapsed >= self.batch_interval:
            self._flush()

        return True

    def close(self):
        """Flush remaining buffer and close file handles."""
        if self._buffer:
            self._flush()
        self._out_fh.close()
        self._rej_fh.close()
        if self._http:
            self._http.close()
        log.info(
            "EventEmitter closed | emitted=%d rejected=%d",
            self._total_emitted, self._total_rejected,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _flush(self):
        if not self._buffer:
            return

        batch = list(self._buffer)
        self._buffer.clear()
        self._last_flush = time.monotonic()

        # Write to JSONL (always)
        for event in batch:
            self._out_fh.write(json.dumps(event) + "\n")

        # POST to API (optional, non-blocking on failure)
        if self._http and self.api_endpoint:
            self._post_batch(batch)

    def _post_batch(self, batch: list[dict]):
        """POST a batch to /events/ingest.  Fails silently (JSONL is the truth)."""
        try:
            resp = self._http.post(
                "/events/ingest",
                content=json.dumps({"events": batch}),
            )
            if resp.status_code not in (200, 201, 207):
                log.warning(
                    "API ingest returned %d for batch of %d events",
                    resp.status_code, len(batch),
                )
        except Exception as exc:
            log.warning("API post failed (will retry on next flush): %s", exc)
            # Re-buffer for next flush attempt (one retry only)
            self._buffer = batch + self._buffer

    @property
    def stats(self) -> dict:
        return {
            "total_emitted": self._total_emitted,
            "total_rejected": self._total_rejected,
            "buffer_pending": len(self._buffer),
        }


# ---------------------------------------------------------------------------
# Standalone schema test utility
# ---------------------------------------------------------------------------

def load_and_validate_jsonl(path: Path) -> dict:
    """
    Load an existing events.jsonl and report validation statistics.
    Useful for post-processing QA.
    """
    total = 0
    valid = 0
    type_counts: dict[str, int] = {}
    errors_by_type: dict[str, list] = {}

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                log.error("JSON parse error: %s", e)
                continue

            total += 1
            errors = validate_event(event)
            et = event.get("event_type", "UNKNOWN")
            type_counts[et] = type_counts.get(et, 0) + 1

            if errors:
                errors_by_type.setdefault(et, []).extend(errors)
            else:
                valid += 1

    return {
        "total": total,
        "valid": valid,
        "invalid": total - valid,
        "type_counts": type_counts,
        "errors_by_type": errors_by_type,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        result = load_and_validate_jsonl(Path(sys.argv[1]))
        print(json.dumps(result, indent=2))