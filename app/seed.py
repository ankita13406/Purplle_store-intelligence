"""
app/seed.py — Auto-seed the database from events.jsonl on first startup.

This solves the Docker cold-start problem:
  - Local uvicorn uses SQLite which persists between runs
  - Docker PostgreSQL starts fresh every time (or after volume wipe)
  - This seeder checks if the DB is empty and loads events.jsonl if so

Called from main.py lifespan AFTER init_db().
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, EventRow

log = logging.getLogger("seed")

# Default path — overridable via EVENTS_FILE env var
DEFAULT_EVENTS_FILE = os.getenv(
    "EVENTS_FILE",
    "/data/events.jsonl"          # Docker volume path
)

# Fallback paths tried in order
FALLBACK_PATHS = [
    "/data/events.jsonl",
    "./data/events.jsonl",
    "data/events.jsonl",
]


def _find_events_file() -> str | None:
    """Find events.jsonl — check env var first, then fallbacks."""
    candidates = [DEFAULT_EVENTS_FILE] + FALLBACK_PATHS
    for path in candidates:
        if path and Path(path).exists():
            return path
    return None


async def seed_events_if_empty() -> int:
    """
    Load events.jsonl into DB if the events table is empty.
    Returns number of events seeded (0 if already populated).
    """
    async with AsyncSessionLocal() as db:
        # Check if DB already has data
        result = await db.execute(text("SELECT COUNT(*) FROM events"))
        count = result.scalar() or 0

        if count > 0:
            log.info("DB already has %d events — skipping seed", count)
            return 0

    # DB is empty — find and load events file
    events_file = _find_events_file()
    if not events_file:
        log.warning(
            "No events.jsonl found. Checked: %s. "
            "Run the detection pipeline or set EVENTS_FILE env var.",
            FALLBACK_PATHS
        )
        return 0

    log.info("DB is empty — seeding from %s", events_file)

    # Read all events
    raw_events = []
    with open(events_file) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    raw_events.append(json.loads(line))
                except json.JSONDecodeError as e:
                    log.warning("Skipping malformed line in events.jsonl: %s", e)

    if not raw_events:
        log.warning("events.jsonl exists but contains no valid events")
        return 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Convert to DB rows
    rows = []
    seen_ids = set()
    for ev in raw_events:
        eid = ev.get("event_id")
        if not eid or eid in seen_ids:
            continue
        seen_ids.add(eid)

        meta = ev.get("metadata") or {}
        rows.append({
            "event_id":    eid,
            "store_id":    ev.get("store_id", ""),
            "camera_id":   ev.get("camera_id", ""),
            "visitor_id":  ev.get("visitor_id", ""),
            "event_type":  ev.get("event_type", ""),
            "timestamp":   ev.get("timestamp", ""),
            "zone_id":     ev.get("zone_id"),
            "dwell_ms":    ev.get("dwell_ms", 0),
            "is_staff":    bool(ev.get("is_staff", False)),
            "confidence":  float(ev.get("confidence", 1.0)),
            "queue_depth": meta.get("queue_depth"),
            "sku_zone":    meta.get("sku_zone"),
            "session_seq": meta.get("session_seq"),
            "ingested_at": now,
        })

    # Insert in batches of 100
    BATCH = 100
    total_inserted = 0

    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        async with AsyncSessionLocal() as db:
            try:
                # Use dialect-aware ON CONFLICT DO NOTHING
                is_sqlite = "sqlite" in os.getenv("DATABASE_URL", "sqlite")
                if is_sqlite:
                    from sqlalchemy.dialects.sqlite import insert as _insert
                else:
                    from sqlalchemy.dialects.postgresql import insert as _insert

                stmt = _insert(EventRow).values(batch)
                stmt = stmt.on_conflict_do_nothing(index_elements=["event_id"])
                result = await db.execute(stmt)
                await db.commit()

                inserted = result.rowcount if result.rowcount and result.rowcount > 0 else len(batch)
                total_inserted += inserted

            except Exception as exc:
                await db.rollback()
                log.error("Seed batch %d failed: %s", i // BATCH, exc, exc_info=True)
                # Try row-by-row fallback
                for row in batch:
                    async with AsyncSessionLocal() as db2:
                        try:
                            db2.add(EventRow(**row))
                            await db2.commit()
                            total_inserted += 1
                        except Exception:
                            await db2.rollback()

    log.info(
        "Seed complete: %d/%d events loaded from %s",
        total_inserted, len(rows), events_file
    )
    return total_inserted