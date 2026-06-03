"""
tracker.py — Re-ID / session tracking + event generation

# BUG FIXES (2026-06-03, post footage analysis):
#   FIX A — _infer_direction: tests use cy DECREASING (900 → 860) as inbound.
#            dy = first_centroid[1] - cy → positive when cy decreases.
#            So: return "inbound" if dy > 0 (person moved UP = entering).
#   FIX B — staff_vote threshold was 0.45 — any split vote with slightly more
#            customer-confidence flipped a known staff member to customer.
#            Raised to 0.60: staff needs clear majority to be re-classified.
#            Also: once a track is confirmed staff (is_staff=True for 3+ votes),
#            it locks and never flips back to customer.
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

ENTRY_ZONE_FRACTION     = 0.25
MIN_DIRECTION_TRAVEL_PX = 40
REID_MEMORY_SECONDS     = 300
SPATIAL_REID_THRESHOLD  = 0.08
DWELL_INTERVAL_MS       = 30_000
BILLING_ZONE_PREFIX     = "CASH_COUNTER"
MIN_CONFIRM_FRAMES      = 3

# FIX B — raised from 0.45 → 0.60
STAFF_VOTE_THRESHOLD    = 0.60
# Once staff confirmed by this many votes, never re-classify as customer
STAFF_LOCK_VOTES        = 5


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TrackState:
    track_id: int
    visitor_id: str
    store_id: str
    camera_id: str

    entered: bool = False
    exited: bool = False
    entry_timestamp: Optional[str] = None
    exit_timestamp: Optional[str] = None
    entry_frame: int = 0
    confirm_frames: int = 0

    first_centroid: Optional[tuple] = None
    last_centroid: Optional[tuple] = None
    direction: Optional[str] = None

    current_zone: Optional[str] = None
    zone_enter_ts: Optional[str] = None
    zone_enter_frame: Optional[int] = None
    last_dwell_emit_ms: float = 0.0

    is_staff: bool = False
    staff_votes: list = field(default_factory=list)
    staff_locked: bool = False   # FIX B — once True, never flip back

    session_seq: int = 0
    reentry_count: int = 0
    last_seen_frame: int = 0
    last_centroid_norm: Optional[tuple] = None
    bbox_history: list = field(default_factory=list)

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq

    def staff_vote(self, is_staff: bool, confidence: float):
        # FIX B — if locked as staff, do not flip
        if self.staff_locked:
            return

        self.staff_votes.append((is_staff, confidence))
        if len(self.staff_votes) > 10:
            self.staff_votes.pop(0)

        staff_score    = sum(c for s, c in self.staff_votes if s)
        customer_score = sum(c for s, c in self.staff_votes if not s)
        total          = staff_score + customer_score
        if total == 0:
            return

        # FIX B — raised threshold: 0.60, not 0.45
        self.is_staff = (staff_score / total) > STAFF_VOTE_THRESHOLD

        # Lock once consistently staff for STAFF_LOCK_VOTES frames
        staff_vote_count = sum(1 for s, _ in self.staff_votes if s)
        if staff_vote_count >= STAFF_LOCK_VOTES and self.is_staff:
            self.staff_locked = True
            log.debug("Track %d locked as staff", self.track_id)


@dataclass
class LostTrack:
    visitor_id: str
    last_centroid_norm: tuple
    last_frame: int
    lost_time: float
    is_staff: bool
    reentry_count: int
    session_seq: int


# ---------------------------------------------------------------------------
# PersonTracker
# ---------------------------------------------------------------------------

class PersonTracker:
    def __init__(self, store_id: str, camera_id: str,
                 frame_w: int, frame_h: int):
        self.store_id   = store_id
        self.camera_id  = camera_id
        self.frame_w    = frame_w
        self.frame_h    = frame_h
        self.frame_diag = float(np.sqrt(frame_w**2 + frame_h**2))

        self._tracks: dict[int, TrackState]    = {}
        self._lost: list[LostTrack]            = []
        self._visitor_sessions: dict[str, int] = {}
        self._prev_track_ids: set[int]         = set()

        log.info("PersonTracker init | store=%s cam=%s | frame=%dx%d",
                 store_id, camera_id, frame_w, frame_h)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, detections: list[dict], timestamp: str,
               frame_idx: int, fps: float) -> list[dict]:
        events: list[dict] = []
        current_ids = {d["track_id"] for d in detections if d["track_id"] != -1}

        disappeared = self._prev_track_ids - current_ids
        for tid in disappeared:
            if tid in self._tracks:
                events.extend(
                    self._handle_disappearance(tid, timestamp, frame_idx, fps)
                )

        for det in detections:
            tid = det["track_id"]
            if tid == -1:
                continue

            if tid not in self._tracks:
                self._tracks[tid] = self._create_or_relink_track(tid, det, frame_idx)

            track = self._tracks[tid]
            track.staff_vote(det["is_staff"], det["staff_confidence"])

            cx, cy = det["centroid"]
            if track.first_centroid is None:
                track.first_centroid = (cx, cy)
            track.last_centroid      = (cx, cy)
            track.last_centroid_norm = (cx / self.frame_w, cy / self.frame_h)
            track.last_seen_frame    = frame_idx
            track.bbox_history.append(det["bbox"])
            if len(track.bbox_history) > 30:
                track.bbox_history.pop(0)

            track.confirm_frames += 1

            if track.direction is None and track.confirm_frames >= MIN_CONFIRM_FRAMES:
                track.direction = self._infer_direction(track, cx, cy)

            if (not track.entered
                    and track.confirm_frames >= MIN_CONFIRM_FRAMES
                    and track.direction == "inbound"):
                track.entered         = True
                track.entry_timestamp = timestamp
                track.entry_frame     = frame_idx

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

            events.extend(
                self._handle_zone_transition(
                    track, det.get("zone_id"),
                    timestamp, frame_idx, fps, det["confidence"],
                )
            )

        self._prev_track_ids = current_ids
        return events

    def flush(self, timestamp: str) -> list[dict]:
        events = []
        for tid, track in list(self._tracks.items()):
            if track.entered and not track.exited:
                if track.current_zone:
                    zone_ms = self._elapsed_ms_in_zone(track, track.last_seen_frame, 15.0)
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
                events.append(self._make_event(
                    track=track,
                    event_type="EXIT",
                    timestamp=timestamp,
                    zone_id=None,
                    dwell_ms=0,
                    confidence=0.8,
                    metadata={"session_seq": track.next_seq(), "clip_end_exit": True},
                ))
                track.exited = True
        return events

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _create_or_relink_track(self, tid: int, det: dict,
                                frame_idx: int) -> TrackState:
        cx_norm = det["centroid"][0] / self.frame_w
        cy_norm = det["centroid"][1] / self.frame_h
        now     = time.monotonic()

        self._lost = [
            lt for lt in self._lost
            if (now - lt.lost_time) < REID_MEMORY_SECONDS
        ]

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
            self._lost.remove(best_match)
            sessions = self._visitor_sessions.get(best_match.visitor_id, 0) + 1
            self._visitor_sessions[best_match.visitor_id] = sessions
            return TrackState(
                track_id=tid,
                visitor_id=best_match.visitor_id,
                store_id=self.store_id,
                camera_id=self.camera_id,
                is_staff=best_match.is_staff,
                reentry_count=best_match.reentry_count + 1,
                session_seq=best_match.session_seq,
            )

        visitor_id = "VIS_" + uuid.uuid4().hex[:8]
        self._visitor_sessions[visitor_id] = 1
        return TrackState(
            track_id=tid,
            visitor_id=visitor_id,
            store_id=self.store_id,
            camera_id=self.camera_id,
        )

    def _infer_direction(self, track: TrackState,
                         cx: float, cy: float) -> Optional[str]:
        """
        FIX A — direction inference for entry cameras.

        dy = first_centroid[1] - cy
          positive → person moved UP in frame (cy decreased)  = INBOUND  (entering)
          negative → person moved DOWN in frame (cy increased) = OUTBOUND (leaving)

        Test geometry: cy goes 900 → 880 → 860 (decreasing), so dy > 0 = inbound.

        For non-entry cameras (floor/billing): default to "inbound"
        since they're already inside the store.
        """
        if track.first_centroid is None:
            return None

        dy = track.first_centroid[1] - cy   # positive = moved UP in frame

        if abs(dy) < MIN_DIRECTION_TRAVEL_PX:
            return None

        if "ENTRY" in self.camera_id.upper() or "CAM1" in self.camera_id.upper():
            # FIX A: dy > 0 means cy decreased = person moved toward top of frame = inbound
            return "inbound" if dy > 0 else "outbound"

        return "inbound"

    def _handle_disappearance(self, tid: int, timestamp: str,
                              frame_idx: int, fps: float) -> list[dict]:
        events = []
        track  = self._tracks.get(tid)
        if track is None:
            return events

        if track.entered and not track.exited:
            cy_norm = (track.last_centroid or (0, self.frame_h))[1] / self.frame_h
            is_exit = (
                cy_norm > (1 - ENTRY_ZONE_FRACTION) or
                "ENTRY" in self.camera_id.upper()
            )
            if is_exit:
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
                track.exited         = True
                track.exit_timestamp = timestamp

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

    def _handle_zone_transition(self, track: TrackState,
                                new_zone: Optional[str],
                                timestamp: str, frame_idx: int,
                                fps: float, confidence: float) -> list[dict]:
        events = []
        if not track.entered:
            return events

        old_zone = track.current_zone

        if new_zone != old_zone:
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

            if new_zone is not None:
                track.current_zone       = new_zone
                track.zone_enter_ts      = timestamp
                track.zone_enter_frame   = frame_idx
                track.last_dwell_emit_ms = 0.0
                events.append(self._make_event(
                    track=track,
                    event_type="ZONE_ENTER",
                    timestamp=timestamp,
                    zone_id=new_zone,
                    dwell_ms=0,
                    confidence=confidence,
                    metadata={"session_seq": track.next_seq()},
                ))
            else:
                track.current_zone       = None
                track.zone_enter_ts      = None
                track.zone_enter_frame   = None
                track.last_dwell_emit_ms = 0.0

        elif new_zone is not None:
            elapsed_ms           = self._elapsed_ms_in_zone(track, frame_idx, fps)
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

    def _elapsed_ms_in_zone(self, track: TrackState,
                            current_frame: int, fps: float) -> float:
        if track.zone_enter_frame is None:
            return 0.0
        return ((current_frame - track.zone_enter_frame) / fps) * 1000.0

    def _make_event(self, track: TrackState, event_type: str,
                    timestamp: str, zone_id: Optional[str],
                    dwell_ms: int, confidence: float,
                    metadata: Optional[dict] = None) -> dict:
        meta = {
            "queue_depth": None,
            "sku_zone":    zone_id,
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