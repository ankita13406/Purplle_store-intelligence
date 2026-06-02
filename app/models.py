"""
models.py — Pydantic v2 schemas for events and all API responses
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    ENTRY                  = "ENTRY"
    EXIT                   = "EXIT"
    ZONE_ENTER             = "ZONE_ENTER"
    ZONE_EXIT              = "ZONE_EXIT"
    ZONE_DWELL             = "ZONE_DWELL"
    BILLING_QUEUE_JOIN     = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON  = "BILLING_QUEUE_ABANDON"
    REENTRY                = "REENTRY"


class AnomalySeverity(str, Enum):
    INFO     = "INFO"
    WARN     = "WARN"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Inbound event schema
# ---------------------------------------------------------------------------

class EventMetadata(BaseModel):
    queue_depth:  Optional[int]  = None
    sku_zone:     Optional[str]  = None
    session_seq:  Optional[int]  = None
    clip_end_exit: Optional[bool] = None

    model_config = {"extra": "allow"}   # forward-compatible


class StoreEvent(BaseModel):
    event_id:   str   = Field(..., description="UUID-v4 string, globally unique")
    store_id:   str   = Field(..., min_length=1)
    camera_id:  str   = Field(..., min_length=1)
    visitor_id: str   = Field(..., min_length=1)
    event_type: EventType
    timestamp:  str   = Field(..., description="ISO-8601 UTC e.g. 2026-03-03T14:22:10Z")
    zone_id:    Optional[str] = None
    dwell_ms:   int   = Field(default=0, ge=0)
    is_staff:   bool  = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata:   EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ValueError(f"timestamp must be ISO-8601 UTC (YYYY-MM-DDTHH:MM:SSZ), got: {v!r}")
        return v

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("event_id too short")
        return v

    @model_validator(mode="after")
    def zone_required_for_zone_events(self) -> "StoreEvent":
        zone_events = {
            EventType.ZONE_ENTER,
            EventType.ZONE_EXIT,
            EventType.ZONE_DWELL,
            EventType.BILLING_QUEUE_JOIN,
            EventType.BILLING_QUEUE_ABANDON,
        }
        if self.event_type in zone_events and not self.zone_id:
            raise ValueError(f"{self.event_type} requires zone_id")
        return self

    def parsed_timestamp(self) -> datetime:
        return datetime.strptime(self.timestamp, "%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Ingest request / response
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    events: list[StoreEvent] = Field(..., max_length=500)


class EventError(BaseModel):
    event_id: Optional[str] = None
    index:    int
    error:    str


class IngestResponse(BaseModel):
    accepted:  int
    rejected:  int
    duplicate: int
    errors:    list[EventError] = []


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------

class ZoneDwell(BaseModel):
    zone_id:       str
    avg_dwell_ms:  float
    visit_count:   int


class StoreMetrics(BaseModel):
    store_id:          str
    date:              str                    # YYYY-MM-DD
    unique_visitors:   int
    conversion_rate:   float                  # 0.0 – 1.0
    avg_dwell_ms:      float
    zone_dwells:       list[ZoneDwell] = []
    queue_depth:       int = 0
    abandonment_rate:  float = 0.0
    computed_at:       str                    # ISO-8601


# ---------------------------------------------------------------------------
# /funnel
# ---------------------------------------------------------------------------

class FunnelStage(BaseModel):
    stage:       str
    count:       int
    drop_off_pct: float = 0.0


class StoreFunnel(BaseModel):
    store_id: str
    date:     str
    stages:   list[FunnelStage]
    sessions: int


# ---------------------------------------------------------------------------
# /heatmap
# ---------------------------------------------------------------------------

class HeatmapZone(BaseModel):
    zone_id:          str
    visit_frequency:  int
    avg_dwell_ms:     float
    normalised_score: float   # 0–100
    data_confidence:  bool    # False if < 20 sessions


class StoreHeatmap(BaseModel):
    store_id: str
    date:     str
    zones:    list[HeatmapZone]


# ---------------------------------------------------------------------------
# /anomalies
# ---------------------------------------------------------------------------

class Anomaly(BaseModel):
    anomaly_id:        str
    store_id:          str
    anomaly_type:      str
    severity:          AnomalySeverity
    description:       str
    suggested_action:  str
    detected_at:       str
    metadata:          dict[str, Any] = {}


class AnomaliesResponse(BaseModel):
    store_id:  str
    anomalies: list[Anomaly]


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class StoreHealthStatus(BaseModel):
    store_id:           str
    last_event_ts:      Optional[str]
    events_last_10min:  int
    stale_feed:         bool


class HealthResponse(BaseModel):
    status:      str            # "ok" | "degraded"
    version:     str
    db_ok:       bool
    stores:      list[StoreHealthStatus]
    checked_at:  str
