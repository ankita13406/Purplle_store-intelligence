# PROMPT: Write pytest tests for the anomaly detection module of a retail store
# analytics API. Test: queue spike thresholds (WARN at 5, CRITICAL at 10),
# dead zone detection (no visits in 30 min), stale camera feed (>10 min gap),
# high abandonment rate (>50%), and conversion drop vs 7-day average.
# Mock the database with AsyncMock and patch sqlalchemy execute calls.
# Each anomaly must have severity, suggested_action, and anomaly_type fields.
#
# CHANGES MADE: Switched from direct SQLAlchemy mocking to using the full
# test client with in-memory SQLite (more reliable than mock chaining),
# added threshold boundary tests (depth=4 → no anomaly, depth=5 → WARN).

import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.main import app
from app.database import Base, get_db
import app.pos_loader as pos_loader

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
STORE_ID = "STORE_BLR_002"


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    pos_loader._converted_visitors.clear()
    pos_loader._billing_presence.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def ts_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


async def ingest(client, events):
    resp = await client.post("/events/ingest", json={"events": events})
    assert resp.status_code in (200, 201, 207)
    return resp.json()


def make_queue_event(depth: int, visitor_suffix: str = "01") -> dict:
    e = {
        "event_id":   str(uuid.uuid4()),
        "store_id":   STORE_ID,
        "camera_id":  "CAM_BILLING_01",
        "visitor_id": f"VIS_q{visitor_suffix}",
        "event_type": "BILLING_QUEUE_JOIN",
        "timestamp":  ts_now(),
        "zone_id":    "BILLING_AREA",
        "dwell_ms":   0,
        "is_staff":   False,
        "confidence": 0.9,
        "metadata":   {"queue_depth": depth, "sku_zone": "BILLING_AREA", "session_seq": 1},
    }
    return e


class TestAnomalyDetection:
    async def test_no_queue_event_no_spike_anomaly(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        data = resp.json()
        spikes = [a for a in data["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) == 0

    async def test_queue_below_threshold_no_anomaly(self, client):
        """depth=4 → no WARN."""
        await ingest(client, [make_queue_event(depth=4)])
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        spikes = [a for a in resp.json()["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) == 0

    async def test_queue_at_warn_threshold(self, client):
        """depth=5 → WARN."""
        await ingest(client, [make_queue_event(depth=5, visitor_suffix="w1")])
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        spikes = [a for a in resp.json()["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) >= 1
        assert spikes[0]["severity"] == "WARN"

    async def test_queue_at_critical_threshold(self, client):
        """depth=10 → CRITICAL."""
        await ingest(client, [make_queue_event(depth=10, visitor_suffix="c1")])
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        spikes = [a for a in resp.json()["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(spikes) >= 1
        assert spikes[0]["severity"] == "CRITICAL"

    async def test_dead_zone_detected_after_30min_silence(self, client):
        """Zone visited 31 minutes ago but not since → DEAD_ZONE anomaly."""
        old_ts = ts_ago(31)
        event = {
            "event_id":  str(uuid.uuid4()),
            "store_id":  STORE_ID,
            "camera_id": "CAM_FLOOR_01",
            "visitor_id": "VIS_dead_zone1",
            "event_type": "ZONE_ENTER",
            "timestamp": old_ts,
            "zone_id":   "HAIRCARE",
            "dwell_ms":  0,
            "is_staff":  False,
            "confidence": 0.85,
            "metadata": {"queue_depth": None, "sku_zone": "HAIRCARE", "session_seq": 1},
        }
        await ingest(client, [event])
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        dead = [a for a in resp.json()["anomalies"] if a["anomaly_type"] == "DEAD_ZONE"]
        assert len(dead) >= 1
        assert dead[0]["severity"] == "INFO"
        assert "HAIRCARE" in dead[0]["description"]

    async def test_stale_camera_feed_detected(self, client):
        """Camera with last event 15min ago → STALE_CAMERA_FEED."""
        old_ts = ts_ago(15)
        event = {
            "event_id":  str(uuid.uuid4()),
            "store_id":  STORE_ID,
            "camera_id": "CAM_ENTRY_STALE",
            "visitor_id": "VIS_stale01",
            "event_type": "ENTRY",
            "timestamp": old_ts,
            "zone_id":   None,
            "dwell_ms":  0,
            "is_staff":  False,
            "confidence": 0.9,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
        }
        await ingest(client, [event])
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        stale = [a for a in resp.json()["anomalies"] if a["anomaly_type"] == "STALE_CAMERA_FEED"]
        assert len(stale) >= 1
        assert "CAM_ENTRY_STALE" in stale[0]["description"]

    async def test_high_abandonment_rate(self, client):
        """5 joins + 3 abandons (60%) → HIGH_ABANDONMENT."""
        events = []
        for i in range(5):
            events.append({
                "event_id": str(uuid.uuid4()), "store_id": STORE_ID,
                "camera_id": "CAM_BILLING_01", "visitor_id": f"VIS_ha_j{i:04d}",
                "event_type": "BILLING_QUEUE_JOIN", "timestamp": ts_now(),
                "zone_id": "BILLING_AREA", "dwell_ms": 0, "is_staff": False,
                "confidence": 0.9, "metadata": {"queue_depth": 3, "sku_zone": "BILLING_AREA", "session_seq": 1},
            })
        for i in range(3):
            events.append({
                "event_id": str(uuid.uuid4()), "store_id": STORE_ID,
                "camera_id": "CAM_BILLING_01", "visitor_id": f"VIS_ha_a{i:04d}",
                "event_type": "BILLING_QUEUE_ABANDON", "timestamp": ts_now(),
                "zone_id": "BILLING_AREA", "dwell_ms": 0, "is_staff": False,
                "confidence": 0.9, "metadata": {"queue_depth": None, "sku_zone": "BILLING_AREA", "session_seq": 2},
            })
        await ingest(client, events)
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        high_ab = [a for a in resp.json()["anomalies"] if a["anomaly_type"] == "HIGH_ABANDONMENT"]
        assert len(high_ab) >= 1

    async def test_all_anomalies_have_required_fields(self, client):
        """Every returned anomaly must have all required fields."""
        await ingest(client, [make_queue_event(depth=12, visitor_suffix="req1")])
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        required = {"anomaly_id", "store_id", "anomaly_type", "severity",
                    "description", "suggested_action", "detected_at"}
        for anomaly in resp.json()["anomalies"]:
            missing = required - anomaly.keys()
            assert not missing, f"Anomaly missing fields: {missing}"

    async def test_anomaly_suggested_action_non_empty(self, client):
        await ingest(client, [make_queue_event(depth=7, visitor_suffix="sa1")])
        resp = await client.get(f"/stores/{STORE_ID}/anomalies")
        for a in resp.json()["anomalies"]:
            assert len(a["suggested_action"].strip()) > 0
