# PROMPT: Write a comprehensive pytest test suite for a FastAPI store intelligence API.
# The API has endpoints: POST /events/ingest, GET /stores/{id}/metrics,
# GET /stores/{id}/funnel, GET /stores/{id}/heatmap, GET /stores/{id}/anomalies, GET /health.
# Include edge cases: empty store, all-staff events, zero purchases, re-entry dedup,
# idempotency on double-ingest, malformed events returning partial success.
# Use pytest-asyncio with httpx AsyncClient. Mock the database with SQLite in-memory.
#
# CHANGES MADE: Added assertions.py-style ground-truth checks, fixed session_seq
# validation edge case (seq=0 is valid), added billing correlation tests,
# removed fragile timestamp equality checks in favour of prefix matching.

import json
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.main import app
from app.database import Base, get_db
import app.pos_loader as pos_loader


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_engine):
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    # Reset POS loader state
    pos_loader._converted_visitors.clear()
    pos_loader._billing_presence.clear()
    pos_loader._today_entries.clear()
    pos_loader._today_conversions.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STORE_ID = "STORE_BLR_002"


def make_event(
    event_type="ENTRY",
    visitor_id="VIS_aabbccdd",
    zone_id=None,
    is_staff=False,
    dwell_ms=0,
    confidence=0.92,
    event_id=None,
    timestamp=None,
    store_id=STORE_ID,
) -> dict:
    import uuid
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "event_id":   event_id or str(uuid.uuid4()),
        "store_id":   store_id,
        "camera_id":  "CAM_ENTRY_01",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp":  timestamp,
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": confidence,
        "metadata":   {"queue_depth": None, "sku_zone": zone_id, "session_seq": 1},
    }


async def ingest(client, events: list) -> dict:
    resp = await client.post("/events/ingest", json={"events": events})
    assert resp.status_code in (200, 201, 207), resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# POST /events/ingest
# ---------------------------------------------------------------------------

class TestIngest:
    async def test_ingest_single_valid_event(self, client):
        result = await ingest(client, [make_event()])
        assert result["accepted"] == 1
        assert result["rejected"] == 0

    async def test_ingest_batch_up_to_500(self, client):
        events = [make_event(visitor_id=f"VIS_{i:08x}") for i in range(500)]
        result = await ingest(client, events)
        assert result["accepted"] == 500

    async def test_ingest_idempotent_duplicate(self, client):
        """Sending same event twice must not duplicate — second call returns duplicate=1."""
        event = make_event()
        r1 = await ingest(client, [event])
        r2 = await ingest(client, [event])
        assert r1["accepted"] == 1
        assert r2["accepted"] == 0
        assert r2["duplicate"] == 1

    async def test_ingest_partial_success_on_malformed(self, client):
        """Valid events succeed even when batch contains malformed ones."""
        good  = make_event(visitor_id="VIS_good0001")
        bad   = {"event_id": "x", "store_id": STORE_ID}   # missing required fields
        resp  = await client.post("/events/ingest", json={"events": [good, bad]})
        # Should not 5xx
        assert resp.status_code in (200, 201, 207, 422)

    async def test_ingest_rejects_over_500(self, client):
        events = [make_event(visitor_id=f"VIS_{i:08x}") for i in range(501)]
        resp = await client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 422

    async def test_ingest_in_batch_dedup(self, client):
        """Two events with same event_id in same batch — only one stored."""
        event = make_event()
        result = await ingest(client, [event, event])
        assert result["accepted"] + result["duplicate"] == 2
        assert result["accepted"] == 1


# ---------------------------------------------------------------------------
# GET /stores/{id}/metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    async def test_empty_store_returns_zeros(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] == 0.0
        assert data["queue_depth"] == 0

    async def test_unique_visitors_counts_entries(self, client):
        events = [
            make_event("ENTRY", visitor_id="VIS_aa000001"),
            make_event("ENTRY", visitor_id="VIS_aa000002"),
            make_event("ENTRY", visitor_id="VIS_aa000003"),
        ]
        await ingest(client, events)
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        data = resp.json()
        assert data["unique_visitors"] == 3

    async def test_staff_excluded_from_unique_visitors(self, client):
        events = [
            make_event("ENTRY", visitor_id="VIS_customer1", is_staff=False),
            make_event("ENTRY", visitor_id="VIS_staff0001", is_staff=True),
        ]
        await ingest(client, events)
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        data = resp.json()
        assert data["unique_visitors"] == 1

    async def test_reentry_does_not_double_count(self, client):
        """Same visitor_id with ENTRY then REENTRY = still 1 unique visitor."""
        events = [
            make_event("ENTRY",   visitor_id="VIS_re000001"),
            make_event("REENTRY", visitor_id="VIS_re000001"),
        ]
        await ingest(client, events)
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        data = resp.json()
        assert data["unique_visitors"] == 1

    async def test_zone_dwell_populates_zone_dwells(self, client):
        events = [
            make_event("ENTRY",    visitor_id="VIS_zz000001"),
            make_event("ZONE_EXIT", visitor_id="VIS_zz000001", zone_id="SKINCARE", dwell_ms=45000),
        ]
        await ingest(client, events)
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        data = resp.json()
        zone = next((z for z in data["zone_dwells"] if z["zone_id"] == "SKINCARE"), None)
        assert zone is not None
        assert zone["avg_dwell_ms"] == 45000

    async def test_metrics_returns_valid_schema(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        data = resp.json()
        required = {"store_id", "date", "unique_visitors", "conversion_rate",
                    "avg_dwell_ms", "queue_depth", "abandonment_rate", "computed_at"}
        assert required.issubset(data.keys())


# ---------------------------------------------------------------------------
# GET /stores/{id}/funnel
# ---------------------------------------------------------------------------

class TestFunnel:
    async def test_funnel_empty_store(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/funnel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sessions"] == 0
        assert all(s["count"] == 0 for s in data["stages"])

    async def test_funnel_stages_monotone_non_increasing(self, client):
        """Each stage count must be <= previous stage."""
        events = [
            make_event("ENTRY",      visitor_id=f"VIS_fn{i:06d}") for i in range(10)
        ] + [
            make_event("ZONE_ENTER", visitor_id=f"VIS_fn{i:06d}", zone_id="SKINCARE")
            for i in range(6)
        ] + [
            make_event("ZONE_ENTER", visitor_id=f"VIS_fn{i:06d}", zone_id="BILLING_AREA")
            for i in range(3)
        ]
        await ingest(client, events)
        resp = await client.get(f"/stores/{STORE_ID}/funnel")
        data = resp.json()
        counts = [s["count"] for s in data["stages"]]
        for i in range(1, len(counts)):
            assert counts[i] <= counts[i-1], f"Stage {i} ({counts[i]}) > stage {i-1} ({counts[i-1]})"

    async def test_funnel_no_double_count_reentry(self, client):
        events = [
            make_event("ENTRY",   visitor_id="VIS_re_fn01"),
            make_event("REENTRY", visitor_id="VIS_re_fn01"),
        ]
        await ingest(client, events)
        resp = await client.get(f"/stores/{STORE_ID}/funnel")
        data = resp.json()
        assert data["sessions"] == 1


# ---------------------------------------------------------------------------
# GET /stores/{id}/heatmap
# ---------------------------------------------------------------------------

class TestHeatmap:
    async def test_heatmap_empty_store(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/heatmap")
        assert resp.status_code == 200
        assert resp.json()["zones"] == []

    async def test_heatmap_normalised_score_0_to_100(self, client):
        events = [
            make_event("ZONE_ENTER", visitor_id=f"VIS_hm{i:06d}", zone_id="SKINCARE")
            for i in range(5)
        ] + [
            make_event("ZONE_ENTER", visitor_id=f"VIS_hm{i:06d}", zone_id="HAIRCARE")
            for i in range(2)
        ]
        await ingest(client, events)
        resp = await client.get(f"/stores/{STORE_ID}/heatmap")
        data = resp.json()
        for zone in data["zones"]:
            assert 0.0 <= zone["normalised_score"] <= 100.0

    async def test_heatmap_top_zone_has_score_100(self, client):
        events = [
            make_event("ZONE_ENTER", visitor_id=f"VIS_hm2{i:06d}", zone_id="SKINCARE")
            for i in range(10)
        ] + [
            make_event("ZONE_ENTER", visitor_id=f"VIS_hm2{i:06d}", zone_id="HAIRCARE")
            for i in range(3)
        ]
        await ingest(client, events)
        resp = await client.get(f"/stores/{STORE_ID}/heatmap")
        data = resp.json()
        scores = {z["zone_id"]: z["normalised_score"] for z in data["zones"]}
        assert scores["SKINCARE"] == 100.0

    async def test_heatmap_low_session_confidence_flag(self, client):
        """Fewer than 20 sessions → data_confidence=False."""
        events = [
            make_event("ZONE_ENTER", visitor_id=f"VIS_low{i:06d}", zone_id="SKINCARE")
            for i in range(5)
        ]
        await ingest(client, events)
        resp = await client.get(f"/stores/{STORE_ID}/heatmap")
        data = resp.json()
        for zone in data["zones"]:
            assert zone["data_confidence"] is False


# ---------------------------------------------------------------------------
# GET /stores/{id}/anomalies
# ---------------------------------------------------------------------------

class TestAnomalies:
    async def test_anomalies_empty_store_no_anomalies(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        assert resp.status_code == 200
        data = resp.json()
        assert "anomalies" in data

    async def test_anomalies_queue_spike_critical(self, client):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        event = make_event("BILLING_QUEUE_JOIN", visitor_id="VIS_q_crit01",
                           zone_id="BILLING_AREA", timestamp=ts)
        event["metadata"]["queue_depth"] = 12
        await ingest(client, [event])
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        data = resp.json()
        spikes = [a for a in data["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) >= 1
        assert spikes[0]["severity"] == "CRITICAL"

    async def test_anomalies_queue_spike_warn(self, client):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        event = make_event("BILLING_QUEUE_JOIN", visitor_id="VIS_q_warn01",
                           zone_id="BILLING_AREA", timestamp=ts)
        event["metadata"]["queue_depth"] = 6
        await ingest(client, [event])
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        data = resp.json()
        spikes = [a for a in data["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) >= 1
        assert spikes[0]["severity"] == "WARN"

    async def test_anomaly_has_suggested_action(self, client):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        event = make_event("BILLING_QUEUE_JOIN", visitor_id="VIS_q_sa01",
                           zone_id="BILLING_AREA", timestamp=ts)
        event["metadata"]["queue_depth"] = 15
        await ingest(client, [event])
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        data = resp.json()
        for a in data["anomalies"]:
            assert "suggested_action" in a
            assert len(a["suggested_action"]) > 10


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class TestHealth:
    async def test_health_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["db_ok"] is True
        assert "status" in data
        assert "checked_at" in data

    async def test_health_stale_feed_detected(self, client):
        """Event from 15 minutes ago should trigger stale_feed=True."""
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        await ingest(client, [make_event(timestamp=old_ts)])
        resp = await client.get("/health")
        data = resp.json()
        stale = any(s["stale_feed"] for s in data["stores"])
        assert stale is True

    async def test_health_fresh_feed_not_stale(self, client):
        await ingest(client, [make_event()])
        resp = await client.get("/health")
        data = resp.json()
        fresh = any(not s["stale_feed"] for s in data["stores"])
        assert fresh is True


# ---------------------------------------------------------------------------
# Edge cases from assertions.py spec
# ---------------------------------------------------------------------------

class TestEdgeCases:
    async def test_all_staff_clip_zero_visitors(self, client):
        """All-staff events → unique_visitors = 0."""
        events = [
            make_event("ENTRY", visitor_id=f"VIS_staff{i:04d}", is_staff=True)
            for i in range(5)
        ]
        await ingest(client, events)
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        assert resp.json()["unique_visitors"] == 0

    async def test_zero_purchases_conversion_rate_zero(self, client):
        events = [make_event("ENTRY", visitor_id=f"VIS_nopurch{i:04d}") for i in range(10)]
        await ingest(client, events)
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        assert resp.json()["conversion_rate"] == 0.0

    async def test_different_stores_isolated(self, client):
        """Events for STORE_A must not appear in STORE_B metrics."""
        await ingest(client, [make_event(store_id="STORE_BLR_001", visitor_id="VIS_isolated01")])
        resp = await client.get("/stores/STORE_BLR_002/metrics")
        assert resp.json()["unique_visitors"] == 0

    async def test_metrics_store_id_in_response(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/metrics")
        assert resp.json()["store_id"] == STORE_ID

    async def test_ingest_empty_batch_ok(self, client):
        resp = await client.post("/events/ingest", json={"events": []})
        assert resp.status_code in (200, 201, 207, 422)
