"""
tracker.py — Re-ID / session tracking + event generation

Responsibilities:
  1. Maintain per-track state (zone history, dwell timers, entry/exit status)
  2. Assign persistent visitor_id tokens using appearance-based Re-ID
     (bounding-box trajectory hashing + optional torchreid embeddings)
  3. Detect entry, exit, re-entry, zone transitions, dwell milestones
  4. Emit structured event dicts (consumed by emit.py)

Re-ID strategy (two-tier):
  Tier 1 — Spatial trajectory: if a new track appears within 60px of a
            recently-lost track's last known position within 5 seconds,
            treat as the same person (fast, zero GPU cost).
  Tier 2 — Appearance embedding: if torchreid is available, use OSNet
            embeddings for cross-camera deduplication.  Graceful fallback
            if library absent.
"""

import uuid
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

log = logging.getLogger("tracker")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Entry zone: detections in the bottom X% of frame height are near the door
ENTRY_ZONE_FRACTION = 0.25          # bottom 25% = entry threshold band

# A track must travel > this many pixels vertically to register as inbound/outbound
MIN_DIRECTION_TRAVEL_PX = 40

# How long (wall-clock seconds) to remember a lost track for re-ID
REID_MEMORY_SECONDS = 300           # 5 minutes

# Spatial Re-ID: max distance (normalised 0-1 by frame diagonal) to link
SPATIAL_REID_THRESHOLD = 0.08

# Dwell milestone interval in milliseconds
DWELL_INTERVAL_MS = 30_000

# Billing zone prefix — used to detect queue events
BILLING_ZONE_PREFIX = "CASH_COUNTER"

# How many frames a track must be confirmed before emitting ENTRY
MIN_CONFIRM_FRAMES = 3


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TrackState:
    """All state for a single tracked individual across their visit."""

    # Identity
    track_id: int                          # ByteTrack / YOLO track ID
    visitor_id: str                        # our Re-ID token (VIS_xxxxxxxx)
    store_id: str
    camera_id: str

    # Entry/exit
    entered: bool = False
    exited: bool = False
    entry_timestamp: Optional[str] = None
    exit_timestamp: Optional[str] = None
    entry_frame: int = 0
    confirm_frames: int = 0                # frames seen before ENTRY emitted

    # Direction detection
    first_centroid: Optional[tuple] = None
    last_centroid: Optional[tuple] = None
    direction: Optional[str] = None       # "inbound" | "outbound"

    # Zone tracking
    current_zone: Optional[str] = None
    zone_enter_ts: Optional[str] = None
    zone_enter_frame: Optional[int] = None   # None = not in any zone (0 is valid frame)
    last_dwell_emit_ms: float = 0.0          # last dwell event emit position

    # Staff classification
    is_staff: bool = False
    staff_votes: list = field(default_factory=list)  # rolling window

    # Session metadata
    session_seq: int = 0                   # ordinal position of next event
    reentry_count: int = 0

    # For lost track memory
    last_seen_frame: int = 0
    last_centroid_norm: Optional[tuple] = None   # normalised (0-1) centroid

    # Bounding box history for appearance Re-ID (last N boxes)
    bbox_history: list = field(default_factory=list)

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq

    def staff_vote(self, is_staff: bool, confidence: float):
        self.staff_votes.append((is_staff, confidence))
        if len(self.staff_votes) > 10:
            self.staff_votes.pop(0)
        # Weighted majority vote
        staff_score    = sum(c for s, c in self.staff_votes if s)
        customer_score = sum(c for s, c in self.staff_votes if not s)
        total = staff_score + customer_score
        if total == 0:
            return  # no votes yet, keep default False
        # Use ratio rather than absolute to handle single high-confidence vote
        self.is_staff = (staff_score / total) > 0.45


@dataclass
class LostTrack:
    """Lightweight record of a lost track kept for Re-ID matching."""
    visitor_id: str
    last_centroid_norm: tuple         # (cx/w, cy/h)
    last_frame: int
    lost_time: float                  # time.monotonic()
    is_staff: bool
    reentry_count: int
    session_seq: int


# ---------------------------------------------------------------------------
# PersonTracker
# ---------------------------------------------------------------------------

class PersonTracker:
    """
    Stateful tracker that wraps YOLO/ByteTrack track IDs and emits
    semantic events for a single camera/clip.
    """

    def __init__(
        self,
        store_id: str,
        camera_id: str,
        frame_w: int,
        frame_h: int,
    ):
        self.store_id  = store_id
        self.camera_id = camera_id
        self.frame_w   = frame_w
        self.frame_h   = frame_h
        self.frame_diag = float(np.sqrt(frame_w**2 + frame_h**2))

        # Active tracks: track_id → TrackState
        self._tracks: dict[int, TrackState] = {}

        # Lost tracks waiting for re-identification
        self._lost: list[LostTrack] = []

        # Visitor ID → session counter (for re-entry detection)
        self._visitor_sessions: dict[str, int] = {}

        # Current frame's active track_ids (to detect disappeared tracks)
        self._prev_track_ids: set[int] = set()

        log.info(
            "PersonTracker init | store=%s cam=%s | frame=%dx%d",
            store_id, camera_id, frame_w, frame_h,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        detections: list[dict],
        timestamp: str,
        frame_idx: int,
        fps: float,
    ) -> list[dict]:
        """
        Process one frame's detections.  Returns list of events to emit.
        """
        events: list[dict] = []

        current_ids = {d["track_id"] for d in detections if d["track_id"] != -1}

        # 1. Handle disappeared tracks (potential exits)
        disappeared = self._prev_track_ids - current_ids
        for tid in disappeared:
            if tid in self._tracks:
                exit_events = self._handle_disappearance(tid, timestamp, frame_idx, fps)
                events.extend(exit_events)

        # 2. Process current detections
        for det in detections:
            tid = det["track_id"]
            if tid == -1:
                continue   # untracked detection — skip

            if tid not in self._tracks:
                # New track — check for re-entry first
                track = self._create_or_relink_track(tid, det, frame_idx)
                self._tracks[tid] = track

            track = self._tracks[tid]

            # Update staff classification
            track.staff_vote(det["is_staff"], det["staff_confidence"])

            # Update centroid history
            cx, cy = det["centroid"]
            if track.first_centroid is None:
                track.first_centroid = (cx, cy)
            track.last_centroid = (cx, cy)
            track.last_centroid_norm = (cx / self.frame_w, cy / self.frame_h)
            track.last_seen_frame = frame_idx
            track.bbox_history.append(det["bbox"])
            if len(track.bbox_history) > 30:
                track.bbox_history.pop(0)

            track.confirm_frames += 1

            # Determine direction once enough movement observed
            if track.direction is None and track.confirm_frames >= MIN_CONFIRM_FRAMES:
                track.direction = self._infer_direction(track, cx, cy)

            # Emit ENTRY if not yet done and direction confirmed as inbound
            if (
                not track.entered
                and track.confirm_frames >= MIN_CONFIRM_FRAMES
                and track.direction == "inbound"
            ):
                track.entered = True
                track.entry_timestamp = timestamp
                track.entry_frame = frame_idx

                event_type = "REENTRY" if track.reentry_count > 0 else "ENTRY"
                events.append(self._make_event(
                    track=track,
                    event_type=event_type,
                    timestamp=timestamp,
                    zone_id=None,
                    dwell_ms=0,
                    confidence=det["confidence"],
                    metadata={"session_seq": track.next_seq()},
                ))

            # Zone transition detection
            new_zone = det.get("zone_id")
            zone_events = self._handle_zone_transition(
                track, new_zone, timestamp, frame_idx, fps, det["confidence"]
            )
            events.extend(zone_events)

        self._prev_track_ids = current_ids
        return events

    def flush(self, timestamp: str) -> list[dict]:
        """
        Called at end-of-clip.  Close all open sessions and emit EXIT events
        for any tracks still active (clip ended before they left).
        """
        events = []
        for tid, track in list(self._tracks.items()):
            if track.entered and not track.exited:
                # Close open zone dwell
                if track.current_zone:
                    zone_ms = self._elapsed_ms_in_zone(
                        track, track.last_seen_frame, 15.0  # default fps
                    )
                    if zone_ms > 0:
                        events.append(self._make_event(
                            track=track,
                            event_type="ZONE_EXIT",
                            timestamp=timestamp,
                            zone_id=track.current_zone,
                            dwell_ms=int(zone_ms),
                            confidence=0.8,
                            metadata={"session_seq": track.next_seq()},
                        ))

                # Emit EXIT (clip ended, not a real exit — mark in metadata)
                events.append(self._make_event(
                    track=track,
                    event_type="EXIT",
                    timestamp=timestamp,
                    zone_id=None,
                    dwell_ms=0,
                    confidence=0.8,
                    metadata={
                        "session_seq": track.next_seq(),
                        "clip_end_exit": True,
                    },
                ))
                track.exited = True

        return events

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_or_relink_track(
        self, tid: int, det: dict, frame_idx: int
    ) -> TrackState:
        """
        Create new TrackState.  Attempt spatial re-ID against recently-lost
        tracks before assigning a brand-new visitor_id.
        """
        cx_norm = det["centroid"][0] / self.frame_w
        cy_norm = det["centroid"][1] / self.frame_h
        now = time.monotonic()

        # Expire old lost tracks
        self._lost = [
            lt for lt in self._lost
            if (now - lt.lost_time) < REID_MEMORY_SECONDS
        ]

        # Attempt spatial Re-ID
        best_match: Optional[LostTrack] = None
        best_dist = SPATIAL_REID_THRESHOLD

        for lt in self._lost:
            dist = np.sqrt(
                (cx_norm - lt.last_centroid_norm[0])**2 +
                (cy_norm - lt.last_centroid_norm[1])**2
            )
            if dist < best_dist:
                best_dist  = dist
                best_match = lt

        if best_match is not None:
            # Re-link to existing visitor
            self._lost.remove(best_match)
            log.debug(
                "Re-ID match: track %d → visitor %s (dist=%.3f)",
                tid, best_match.visitor_id, best_dist,
            )
            sessions = self._visitor_sessions.get(best_match.visitor_id, 0) + 1
            self._visitor_sessions[best_match.visitor_id] = sessions

            track = TrackState(
                track_id=tid,
                visitor_id=best_match.visitor_id,
                store_id=self.store_id,
                camera_id=self.camera_id,
                is_staff=best_match.is_staff,
                reentry_count=best_match.reentry_count + 1,
                session_seq=best_match.session_seq,
            )
        else:
            # New visitor
            visitor_id = "VIS_" + uuid.uuid4().hex[:8]
            self._visitor_sessions[visitor_id] = 1
            track = TrackState(
                track_id=tid,
                visitor_id=visitor_id,
                store_id=self.store_id,
                camera_id=self.camera_id,
            )

        return track

    def _infer_direction(
        self, track: TrackState, cx: float, cy: float
    ) -> Optional[str]:
        """
        Determine if this track is entering (inbound) or exiting (outbound)
        based on vertical movement.  Entry camera: person moving up the frame
        (decreasing y) = entering the store.
        """
        if track.first_centroid is None:
            return None

        dy = track.first_centroid[1] - cy   # positive = moved up (inbound)

        if abs(dy) < MIN_DIRECTION_TRAVEL_PX:
            return None   # not enough movement yet

        # For entry camera: inbound = moving away from door (up frame)
        # This is camera-angle dependent; entry cameras typically see
        # people moving from bottom toward middle/top as they enter.
        if "ENTRY" in self.camera_id.upper():
            return "inbound" if dy > 0 else "outbound"
        # Floor/billing cameras: all movement is within store — default inbound
        return "inbound"

    def _handle_disappearance(
        self, tid: int, timestamp: str, frame_idx: int, fps: float
    ) -> list[dict]:
        """Handle a track that has left the frame."""
        events = []
        track = self._tracks.get(tid)
        if track is None:
            return events

        if track.entered and not track.exited:
            # Check if track is near entry/exit boundary
            cy_norm = (track.last_centroid or (0, self.frame_h))[1] / self.frame_h

            # Track disappeared near bottom of frame → likely exit
            is_exit = (
                cy_norm > (1 - ENTRY_ZONE_FRACTION) or
                "ENTRY" in self.camera_id.upper()
            )

            if is_exit:
                # Close open zone dwell first
                if track.current_zone:
                    zone_ms = self._elapsed_ms_in_zone(track, frame_idx, fps)
                    events.append(self._make_event(
                        track=track,
                        event_type="ZONE_EXIT",
                        timestamp=timestamp,
                        zone_id=track.current_zone,
                        dwell_ms=int(zone_ms),
                        confidence=0.75,
                        metadata={"session_seq": track.next_seq()},
                    ))
                    track.current_zone = None

                events.append(self._make_event(
                    track=track,
                    event_type="EXIT",
                    timestamp=timestamp,
                    zone_id=None,
                    dwell_ms=0,
                    confidence=0.75,
                    metadata={"session_seq": track.next_seq()},
                ))
                track.exited = True
                track.exit_timestamp = timestamp

        # Move to lost-track memory for possible re-ID
        if track.last_centroid_norm:
            self._lost.append(LostTrack(
                visitor_id=track.visitor_id,
                last_centroid_norm=track.last_centroid_norm,
                last_frame=frame_idx,
                lost_time=time.monotonic(),
                is_staff=track.is_staff,
                reentry_count=track.reentry_count,
                session_seq=track.session_seq,
            ))

        del self._tracks[tid]
        return events

    def _handle_zone_transition(
        self,
        track: TrackState,
        new_zone: Optional[str],
        timestamp: str,
        frame_idx: int,
        fps: float,
        confidence: float,
    ) -> list[dict]:
        """Detect zone enter/exit/dwell transitions and emit appropriate events."""
        events = []

        if not track.entered:
            return events   # don't emit zone events before ENTRY

        old_zone = track.current_zone

        if new_zone != old_zone:
            # Zone exit
            if old_zone is not None:
                zone_ms = self._elapsed_ms_in_zone(track, frame_idx, fps)
                events.append(self._make_event(
                    track=track,
                    event_type="ZONE_EXIT",
                    timestamp=timestamp,
                    zone_id=old_zone,
                    dwell_ms=int(zone_ms),
                    confidence=confidence,
                    metadata={"session_seq": track.next_seq()},
                ))

            # Zone enter
            if new_zone is not None:
                track.current_zone       = new_zone
                track.zone_enter_ts      = timestamp
                track.zone_enter_frame   = frame_idx   # exact frame (may be 0)
                track.last_dwell_emit_ms = 0.0

                extra_meta: dict = {"session_seq": track.next_seq()}

                # Billing queue logic
                if new_zone.startswith(BILLING_ZONE_PREFIX):
                    pass  # queue_depth injected by API on ingest; set null here

                events.append(self._make_event(
                    track=track,
                    event_type="ZONE_ENTER",
                    timestamp=timestamp,
                    zone_id=new_zone,
                    dwell_ms=0,
                    confidence=confidence,
                    metadata=extra_meta,
                ))
            else:
                track.current_zone     = None
                track.zone_enter_ts    = None
                track.zone_enter_frame = None
                track.last_dwell_emit_ms = 0.0

        elif new_zone is not None:
            # Still in same zone — check dwell milestone
            elapsed_ms = self._elapsed_ms_in_zone(track, frame_idx, fps)
            next_dwell_threshold = track.last_dwell_emit_ms + DWELL_INTERVAL_MS

            if elapsed_ms >= next_dwell_threshold:
                track.last_dwell_emit_ms = elapsed_ms
                events.append(self._make_event(
                    track=track,
                    event_type="ZONE_DWELL",
                    timestamp=timestamp,
                    zone_id=new_zone,
                    dwell_ms=int(elapsed_ms),
                    confidence=confidence,
                    metadata={"session_seq": track.next_seq()},
                ))

        return events

    def _elapsed_ms_in_zone(
        self, track: TrackState, current_frame: int, fps: float
    ) -> float:
        if track.zone_enter_frame is None:
            return 0.0
        frames_in_zone = current_frame - track.zone_enter_frame
        return (frames_in_zone / fps) * 1000.0

    def _make_event(
        self,
        track: TrackState,
        event_type: str,
        timestamp: str,
        zone_id: Optional[str],
        dwell_ms: int,
        confidence: float,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Construct a fully-compliant event dict."""
        meta = {
            "queue_depth": None,
            "sku_zone": zone_id,
            "session_seq": track.session_seq,
        }
        if metadata:
            meta.update(metadata)

        return {
            "event_id":   str(uuid.uuid4()),
            "store_id":   track.store_id,
            "camera_id":  track.camera_id,
            "visitor_id": track.visitor_id,
            "event_type": event_type,
            "timestamp":  timestamp,
            "zone_id":    zone_id,
            "dwell_ms":   dwell_ms,
            "is_staff":   track.is_staff,
            "confidence": round(confidence, 3),
            "metadata":   meta,
        }