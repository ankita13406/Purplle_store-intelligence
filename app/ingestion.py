"""
ingestion.py — Ingest, deduplicate, and persist events.

Key guarantees:
  • Idempotent by event_id  — re-POSTing same payload is safe (no duplicates)
  • Partial success         — malformed events return per-event errors; valid ones still stored
  • Staff filter            — is_staff=True events stored but excluded from metrics queries
  • Atomic batch write      — all-or-nothing per valid sub-batch
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import os

from app.models import StoreEvent, IngestRequest, IngestResponse, EventError
from app.database import EventRow

log = logging.getLogger("ingestion")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def ingest_events(
    request: IngestRequest,
    db: AsyncSession,
) -> IngestResponse:
    """
    Validate, deduplicate, and persist a batch of events.
    Returns counts of accepted / rejected / duplicate.
    """
    accepted  = 0
    rejected  = 0
    duplicate = 0
    errors: list[EventError] = []

    now = _now_iso()

    # Collect event_ids in this batch for in-batch dedup
    seen_in_batch: set[str] = set()
    rows_to_insert: list[dict] = []

    for idx, event in enumerate(request.events):
        # In-batch duplicate check
        if event.event_id in seen_in_batch:
            duplicate += 1
            continue
        seen_in_batch.add(event.event_id)

        rows_to_insert.append(_event_to_row(event, now))

    if not rows_to_insert:
        return IngestResponse(
            accepted=accepted, rejected=rejected, duplicate=duplicate, errors=errors
        )

        # Bulk upsert — ignore on conflict (idempotent by event_id)
    # SQLite: INSERT OR IGNORE; PostgreSQL: ON CONFLICT DO NOTHING
    try:
        IS_SQLITE = "sqlite" in os.getenv("DATABASE_URL", "sqlite")

        if IS_SQLITE:
            from sqlalchemy.dialects.sqlite import insert as _insert
        else:
            from sqlalchemy.dialects.postgresql import insert as _insert

        stmt = _insert(EventRow).values(rows_to_insert)
        stmt = stmt.on_conflict_do_nothing(index_elements=["event_id"])

        result = await db.execute(stmt)
        await db.commit()

        inserted = result.rowcount if result.rowcount is not None else len(rows_to_insert)

        if inserted < 0:
            inserted = len(rows_to_insert)

        db_duplicates = len(rows_to_insert) - max(0, inserted)
        duplicate += db_duplicates
        accepted += max(0, inserted)

    

    except Exception as exc:
        await db.rollback()
        log.error("Bulk insert failed: %s", exc, exc_info=True)
        # Fall back to row-by-row insert to capture partial success
        accepted, rejected, duplicate, errors = await _row_by_row_insert(
            rows_to_insert, request.events, db, now, errors
        )

    log.info(
        "Ingest batch: accepted=%d rejected=%d duplicate=%d",
        accepted, rejected, duplicate,
    )
    return IngestResponse(
        accepted=accepted,
        rejected=rejected,
        duplicate=duplicate,
        errors=errors,
    )


async def _row_by_row_insert(
    rows: list[dict],
    events: list[StoreEvent],
    db: AsyncSession,
    now: str,
    errors: list[EventError],
) -> tuple[int, int, int, list[EventError]]:
    """Fallback: insert one row at a time for partial-success on errors."""
    accepted  = 0
    rejected  = 0
    duplicate = 0

    for idx, (row, event) in enumerate(zip(rows, events)):
        # Check DB-level duplicate
        existing = await db.execute(
            select(EventRow.id).where(EventRow.event_id == row["event_id"])
        )
        if existing.scalar_one_or_none() is not None:
            duplicate += 1
            continue

        try:
            db.add(EventRow(**row))
            await db.flush()
            accepted += 1
        except Exception as exc:
            await db.rollback()
            rejected += 1
            errors.append(EventError(
                event_id=event.event_id,
                index=idx,
                error=str(exc),
            ))

    await db.commit()
    return accepted, rejected, duplicate, errors


def _event_to_row(event: StoreEvent, ingested_at: str) -> dict:
    return {
        "event_id":    event.event_id,
        "store_id":    event.store_id,
        "camera_id":   event.camera_id,
        "visitor_id":  event.visitor_id,
        "event_type":  event.event_type.value,
        "timestamp":   event.timestamp,
        "zone_id":     event.zone_id,
        "dwell_ms":    event.dwell_ms,
        "is_staff":    event.is_staff,
        "confidence":  event.confidence,
        "queue_depth": event.metadata.queue_depth,
        "sku_zone":    event.metadata.sku_zone,
        "session_seq": event.metadata.session_seq,
        "ingested_at": ingested_at,
    }
