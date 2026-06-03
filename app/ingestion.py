"""
ingestion.py — Ingest, deduplicate, and persist events.

Fixes vs previous version:
  1. Bulk insert block was indented inside an empty if-block (dead code).
     The return statement fired before the insert ran. Fixed indentation.
  2. IS_SQLITE detection now checks DATABASE_URL env var correctly.
  3. rowcount handling: PostgreSQL asyncpg returns -1 for rowcount on
     ON CONFLICT DO NOTHING — treat -1 as "all inserted" to avoid
     over-counting duplicates.
"""
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoreEvent, IngestRequest, IngestResponse, EventError
from app.database import EventRow

log = logging.getLogger("ingestion")

IS_SQLITE = "sqlite" in os.getenv("DATABASE_URL", "sqlite")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def ingest_events(
    request: IngestRequest,
    db: AsyncSession,
) -> IngestResponse:
    """
    Validate, deduplicate, and persist a batch of events.
    Idempotent by event_id. Returns counts of accepted/rejected/duplicate.
    """
    accepted  = 0
    rejected  = 0
    duplicate = 0
    errors: list[EventError] = []

    now = _now_iso()

    # In-batch deduplication
    seen_in_batch: set[str] = set()
    rows_to_insert: list[dict] = []

    for event in request.events:
        if event.event_id in seen_in_batch:
            duplicate += 1
            continue
        seen_in_batch.add(event.event_id)
        rows_to_insert.append(_event_to_row(event, now))

    if not rows_to_insert:
        return IngestResponse(
            accepted=accepted,
            rejected=rejected,
            duplicate=duplicate,
            errors=errors,
        )

    # ── Bulk upsert ─────────────────────────────────────────────────────────
    # Fix: this block was previously inside a dead code path due to wrong indent.
    # Fix: rowcount = -1 on PostgreSQL asyncpg with ON CONFLICT DO NOTHING
    #      → treat as "all rows processed" and let duplicate count stay at 0.
    try:
        if IS_SQLITE:
            from sqlalchemy.dialects.sqlite import insert as _insert
        else:
            from sqlalchemy.dialects.postgresql import insert as _insert

        stmt = _insert(EventRow).values(rows_to_insert)
        stmt = stmt.on_conflict_do_nothing(index_elements=["event_id"])

        result = await db.execute(stmt)
        await db.commit()

        rowcount = result.rowcount
        if rowcount is None or rowcount < 0:
            # PostgreSQL asyncpg returns -1 for ON CONFLICT DO NOTHING
            # Assume all rows were processed (duplicates handled at DB level)
            accepted  += len(rows_to_insert)
        else:
            db_dupes   = len(rows_to_insert) - rowcount
            duplicate += db_dupes
            accepted  += rowcount

    except Exception as exc:
        await db.rollback()
        log.error("Bulk insert failed: %s", exc, exc_info=True)
        # Fallback: row-by-row for partial success
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
        "event_type":  event.event_type.value
                       if hasattr(event.event_type, "value")
                       else str(event.event_type),
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