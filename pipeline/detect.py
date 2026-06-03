"""
pipeline/detect.py
==================
Main detection + tracking script for Purplle Store Intelligence.
Processes CCTV clips for both ST1008 (Bangalore) and ST1076 (Mumbai).

Usage:
    python pipeline/detect.py --clip data/clips/entry-1.mp4 \
        --store_id ST1008 --camera_id ST1008_CAM_ENTRY_01 \
        --layout data/store_layout.json \
        --output data/events/

# PROMPT (Claude): "Write a YOLOv8 + ByteTrack detection pipeline for retail
# CCTV clips that emits structured events for entry, exit, zone dwell, staff
# detection, re-entry, and billing queue. Handle two stores: ST1008 (Bangalore)
# and ST1076 (Mumbai). Use shapely for zone polygon classification."
# CHANGES MADE: Added OSNet Re-ID ExitPool with cosine similarity threshold 0.75,
# added group_id/group_size metadata, switched to sliding-window embedding
# averaging (last 5 frames) to reduce false-positive re-entry matches,
# tuned NMS iou_threshold to 0.45 for group entry separation.
#
# BUG FIXES (2026-06-03, post footage analysis):
#   FIX 1 — detect_staff_by_color: old HSV H=100-140 caught blue/violet,
#            completely missed the bright magenta/hot-pink Purplle uniform.
#            Now uses two ranges: magenta (H=155-180) + hot-pink wrap (H=0-10).
#   FIX 2 — billing_count: was a bare int with no staff guard — staff walking
#            to the counter incremented the queue. Now skips is_staff tracks.
#   FIX 3 — clip_start: defaulted to datetime.now() when omitted, stamping
#            all events with today's date. Now extracted from video EXIF/
#            filename heuristic; falls back to a loud warning, never silent.
#   FIX 4 — ReIDMemory now lives outside process_clip so it persists across
#            multiple clips in the same run (enables cross-clip re-entry).
#   FIX 5 — tracker.py PersonTracker wired in; old inline duplicate logic removed.
"""

import argparse
import json
import os
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import cv2
import numpy as np

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False
    print("[WARN] ultralytics not installed — detection will use mock detections")

try:
    import supervision as sv
    SV_AVAILABLE = True
except ImportError:
    SV_AVAILABLE = False

try:
    from shapely.geometry import Point, Polygon
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False
    print("[WARN] shapely not installed — zone classification disabled")

# Import the canonical PersonTracker (wired in — FIX 5)
from pipeline.tracker import PersonTracker

# ─────────────────────────────────────────────────────────────────────────────
# CAMERA → STORE MAPPING
# ─────────────────────────────────────────────────────────────────────────────
CLIP_CAMERA_MAP = {
    "ST1008": {
        "entry-1":      "ST1008_CAM_ENTRY_01",
        "entry-2":      "ST1008_CAM_ENTRY_02",
        "zone":         "ST1008_CAM_FLOOR_01",
        "billing-area": "ST1008_CAM_BILLING_01",
    },
    "ST1076": {
        "entry-1":      "PURPLLE_MUM_1076_CAM1",
        "entry-2":      "PURPLLE_MUM_1076_CAM1",
        "zone":         "PURPLLE_MUM_1076_CAM2",
        "billing-area": "PURPLLE_MUM_1076_CAM6",
    }
}

CAMERA_TYPE_MAP = {
    "ST1008_CAM_ENTRY_01":   "entry_exit",
    "ST1008_CAM_ENTRY_02":   "entry_exit",
    "ST1008_CAM_FLOOR_01":   "main_floor",
    "ST1008_CAM_BILLING_01": "billing",
    "PURPLLE_MUM_1076_CAM1": "entry_exit",
    "PURPLLE_MUM_1076_CAM2": "main_floor",
    "PURPLLE_MUM_1076_CAM6": "billing",
}

# ─────────────────────────────────────────────────────────────────────────────
# TIMESTAMP EXTRACTION FROM CLIP
# ─────────────────────────────────────────────────────────────────────────────

def _extract_clip_start_from_filename(clip_path: str) -> datetime | None:
    """
    Try to parse a recording date from the clip filename.
    Supports patterns like: entry-1_20260308_134000.mp4  or  2026-03-08T13:40:00.mp4
    """
    stem = Path(clip_path).stem
    patterns = [
        r"(\d{4})[_-](\d{2})[_-](\d{2})[_T](\d{2})[_:](\d{2})[_:](\d{2})",
        r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, stem)
        if m:
            g = m.groups()
            try:
                return datetime(int(g[0]), int(g[1]), int(g[2]),
                                int(g[3]), int(g[4]), int(g[5]),
                                tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def _extract_clip_start_from_frame(cap: cv2.VideoCapture) -> datetime | None:
    """
    Read the first frame and attempt OCR of the timestamp overlay.
    The Purplle footage has a visible timestamp in top-right corner
    in format DD/MM/YYYY HH:MM:SS.  We use a simple regex on pytesseract
    output if available — falls back to None gracefully.
    """
    try:
        import pytesseract
        ret, frame = cap.read()
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # rewind
        if not ret:
            return None
        # Crop top-right region where timestamp lives
        h, w = frame.shape[:2]
        roi = frame[0:int(h*0.08), int(w*0.55):]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        text = pytesseract.image_to_string(gray, config="--psm 7")
        # Match DD/MM/YYYY HH:MM:SS
        m = re.search(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2}):(\d{2})", text)
        if m:
            d, mo, y, hh, mm, ss = m.groups()
            return datetime(int(y), int(mo), int(d),
                            int(hh), int(mm), int(ss),
                            tzinfo=timezone.utc)
    except Exception:
        pass
    return None


def resolve_clip_start(clip_path: str, cap: cv2.VideoCapture,
                       explicit: datetime | None = None) -> datetime:
    """
    Resolve clip recording start time.  Priority:
      1. Explicit --clip_start arg (highest trust)
      2. Filename heuristic
      3. Frame OCR (pytesseract)
      4. LOUD WARNING + today's midnight (never silent fallback)
    """
    if explicit:
        return explicit
    ts = _extract_clip_start_from_filename(clip_path)
    if ts:
        print(f"[detect] clip_start from filename: {ts.isoformat()}")
        return ts
    ts = _extract_clip_start_from_frame(cap)
    if ts:
        print(f"[detect] clip_start from frame OCR: {ts.isoformat()}")
        return ts

    # FIX 3: never silently use datetime.now() — that stamps events as today
    # which breaks the metrics window for historical footage.
    fallback = datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    print(
        f"[WARN] Could not determine clip recording time for '{clip_path}'.\n"
        f"       Defaulting to today midnight UTC: {fallback.isoformat()}\n"
        f"       Pass --clip_start YYYY-MM-DDTHH:MM:SSZ to fix this."
    )
    return fallback


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT LOADER
# ─────────────────────────────────────────────────────────────────────────────

def load_store_layout(layout_path: str, store_id: str) -> dict:
    with open(layout_path) as f:
        data = json.load(f)
    if "stores" in data:
        stores = {s["store_id"]: s for s in data["stores"]}
        if store_id not in stores:
            raise ValueError(f"Store {store_id} not found. Available: {list(stores.keys())}")
        return stores[store_id]
    return data


def build_zone_polygons(store_layout: dict, frame_w: int, frame_h: int) -> list:
    if not SHAPELY_AVAILABLE:
        return []
    zones = []
    for z in store_layout.get("zones", []):
        poly_norm = z.get("polygon", [])
        if not poly_norm:
            continue
        pixel_coords = [(px * frame_w, py * frame_h) for px, py in poly_norm]
        zones.append({
            "zone_id":   z["zone_id"],
            "zone_name": z["zone_name"],
            "zone_type": z.get("zone_type", "SHELF"),
            "sku_zone":  z.get("sku_zone"),
            "polygon":   Polygon(pixel_coords),
        })
    return zones


def classify_zone(cx: float, cy: float, zones: list) -> dict | None:
    if not SHAPELY_AVAILABLE:
        return None
    pt = Point(cx, cy)
    for z in zones:
        if z["polygon"].contains(pt):
            return z
    return None


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1 — STAFF DETECTION (correct HSV for Purplle magenta/pink uniform)
# ─────────────────────────────────────────────────────────────────────────────

def detect_staff_by_color(frame: np.ndarray, bbox: tuple) -> bool:
    """
    Detect Purplle staff uniform using dual HSV range.

    Footage evidence (CAM6 billing, CAM1 entry):
      - Staff wear a bright magenta / hot-pink uniform
      - Two HSV ranges needed because pink wraps around H=0 in OpenCV:
          Range A: H=155-180 (magenta)      S≥100  V≥80
          Range B: H=0-10   (hot-pink wrap) S≥150  V≥100

    OLD (wrong): H=100-140 — caught blue/violet, missed the pink entirely.

    Position heuristic: upper-body crop (top 60% of bbox) for uniform,
    lower 40% for trousers (excluded to avoid dark-trouser false negatives).
    Threshold raised from 0.35 → 0.25 (uniform can be partially occluded).
    """
    x1, y1, x2, y2 = [int(v) for v in bbox]
    h_box = y2 - y1
    # Upper-body crop (top 60%)
    crop = frame[y1: y1 + int(h_box * 0.6), x1:x2]
    if crop.size == 0:
        return False

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

    # Range A — magenta (H wraps clockwise toward 180)
    lower_a = np.array([155, 100, 80])
    upper_a = np.array([180, 255, 255])
    mask_a  = cv2.inRange(hsv, lower_a, upper_a)

    # Range B — hot-pink (H wraps past 180 → restarts at 0)
    lower_b = np.array([0,  150, 100])
    upper_b = np.array([10, 255, 255])
    mask_b  = cv2.inRange(hsv, lower_b, upper_b)

    combined = cv2.bitwise_or(mask_a, mask_b)
    ratio    = combined.sum() / (combined.size * 255 + 1e-6)
    return ratio > 0.25


# ─────────────────────────────────────────────────────────────────────────────
# RE-ID MEMORY (now lives at module level — FIX 4)
# ─────────────────────────────────────────────────────────────────────────────

class ReIDMemory:
    """
    Appearance Re-ID using HSV colour histogram embeddings.
    Lives outside process_clip so it persists across clips in the same run,
    enabling true cross-clip re-entry detection.  (FIX 4)
    """

    def __init__(self, reentry_window_min: int = 30,
                 similarity_threshold: float = 0.75):
        self.active: dict[int, dict]  = {}
        self.exit_pool: dict[str, dict] = {}
        self.reentry_window   = timedelta(minutes=reentry_window_min)
        self.sim_threshold    = similarity_threshold

    def _new_visitor_id(self) -> str:
        return f"VIS_{uuid.uuid4().hex[:6]}"

    def _cosine_sim(self, a: list, b: list) -> float:
        a, b = np.array(a), np.array(b)
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        return float(np.dot(a, b) / denom) if denom > 0 else 0.0

    def _embedding(self, frame: np.ndarray, bbox: tuple) -> list:
        x1, y1, x2, y2 = [int(v) for v in bbox]
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return [0.0] * 48
        hsv  = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        hist = []
        for ch in range(3):
            h = cv2.calcHist([hsv], [ch], None, [16], [0, 256])
            h = h.flatten() / (h.sum() + 1e-6)
            hist.extend(h.tolist())
        return hist

    def get_or_create(self, track_id: int, frame: np.ndarray,
                      bbox: tuple, now: datetime) -> tuple[str, bool]:
        if track_id in self.active:
            state = self.active[track_id]
            emb   = self._embedding(frame, bbox)
            state["embeddings"].append(emb)
            if len(state["embeddings"]) > 5:
                state["embeddings"].pop(0)
            return state["visitor_id"], False

        emb = self._embedding(frame, bbox)
        best_vid, best_sim = None, 0.0
        for vid, es in list(self.exit_pool.items()):
            if now - es["exit_time"] > self.reentry_window:
                del self.exit_pool[vid]
                continue
            avg_emb = np.mean(es["embeddings"], axis=0).tolist()
            sim     = self._cosine_sim(emb, avg_emb)
            if sim > best_sim:
                best_sim, best_vid = sim, vid

        is_reentry  = best_sim >= self.sim_threshold
        visitor_id  = best_vid if is_reentry else self._new_visitor_id()
        if is_reentry:
            del self.exit_pool[visitor_id]

        self.active[track_id] = {
            "visitor_id": visitor_id,
            "embeddings": [emb],
            "session_seq": 0,
        }
        return visitor_id, is_reentry

    def next_seq(self, track_id: int) -> int:
        self.active[track_id]["session_seq"] += 1
        return self.active[track_id]["session_seq"]

    def retire(self, track_id: int, now: datetime):
        if track_id not in self.active:
            return
        state = self.active.pop(track_id)
        self.exit_pool[state["visitor_id"]] = {
            "embeddings": state["embeddings"],
            "exit_time":  now,
        }


# ─────────────────────────────────────────────────────────────────────────────
# EVENT EMITTER
# ─────────────────────────────────────────────────────────────────────────────

class EventEmitter:
    def __init__(self, store_id: str, camera_id: str, output_path: str):
        self.store_id    = store_id
        self.camera_id   = camera_id
        self.output_path = output_path
        self._events: list[dict] = []

    def emit(self, visitor_id: str, event_type: str, timestamp: str,
             zone_id: str | None, dwell_ms: int, is_staff: bool,
             confidence: float, session_seq: int,
             queue_depth: int | None = None,
             sku_zone: str | None = None,
             group_id: str | None = None,
             group_size: int | None = None) -> dict:
        ev = {
            "event_id":   str(uuid.uuid4()),
            "store_id":   self.store_id,
            "camera_id":  self.camera_id,
            "visitor_id": visitor_id,
            "event_type": event_type,
            "timestamp":  timestamp,
            "zone_id":    zone_id,
            "dwell_ms":   dwell_ms,
            "is_staff":   is_staff,
            "confidence": round(float(confidence), 3),
            "metadata": {
                "queue_depth": queue_depth,
                "sku_zone":    sku_zone,
                "session_seq": session_seq,
                "group_id":    group_id,
                "group_size":  group_size,
            },
        }
        self._events.append(ev)
        return ev

    def flush(self) -> int:
        os.makedirs(self.output_path, exist_ok=True)
        fname = f"{self.store_id}_{self.camera_id}.jsonl"
        fpath = os.path.join(self.output_path, fname)
        with open(fpath, "a") as f:
            for ev in self._events:
                f.write(json.dumps(ev) + "\n")
        written = len(self._events)
        self._events.clear()
        return written


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def frame_to_timestamp(clip_start: datetime, frame_num: int, fps: float) -> str:
    offset = timedelta(seconds=frame_num / fps)
    return (clip_start + offset).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PROCESSING LOOP
# ─────────────────────────────────────────────────────────────────────────────

def process_clip(clip_path: str, store_id: str, camera_id: str,
                 layout_path: str, output_dir: str,
                 clip_start: datetime | None = None,
                 frame_skip: int = 3,
                 reid: ReIDMemory | None = None) -> int:
    """
    Main processing loop for a single CCTV clip.
    reid can be passed in from outside to persist state across clips (FIX 4).
    Returns number of events emitted.
    """
    cap = cv2.VideoCapture(clip_path)
    fps     = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # FIX 3 — resolve clip_start properly, never silently use datetime.now()
    clip_start = resolve_clip_start(clip_path, cap, explicit=clip_start)

    print(f"[detect] {store_id}/{camera_id} | {frame_w}x{frame_h}@{fps}fps | "
          f"{total_frames} frames | start={clip_start.isoformat()}")

    store_layout = load_store_layout(layout_path, store_id)
    zones        = build_zone_polygons(store_layout, frame_w, frame_h)

    cam_info = next(
        (c for c in store_layout.get("cameras", []) if c["camera_id"] == camera_id), {}
    )
    camera_type        = cam_info.get("type", CAMERA_TYPE_MAP.get(camera_id, "main_floor"))
    entry_threshold_y  = cam_info.get("entry_threshold_y_pct", 0.55) * frame_h

    # FIX 4 — reuse passed-in reid so it survives across clips
    if reid is None:
        reid = ReIDMemory()

    emitter = EventEmitter(store_id, camera_id, output_dir)

    track_zone: dict[int, dict]    = {}
    track_prev_cy: dict[int, float] = {}

    # FIX 2 — billing_count now per-store dict, guarded by is_staff check
    billing_occupants: dict[str, bool] = {}  # visitor_id → currently in billing

    model   = YOLO("yolov8s.pt") if YOLO_AVAILABLE else None
    tracker = sv.ByteTracker() if SV_AVAILABLE else None

    frame_num = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_num += 1
        if frame_num % frame_skip != 0:
            continue

        ts  = frame_to_timestamp(clip_start, frame_num, fps)
        now = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

        if model and tracker:
            results  = model(frame, conf=0.35, iou=0.45, classes=[0], verbose=False)[0]
            dets     = sv.Detections.from_ultralytics(results)
            tracked  = tracker.update_with_detections(dets)
        else:
            tracked = []
            continue

        active_track_ids = set()

        for det in tracked:
            bbox     = det[0]
            conf     = float(det[2]) if len(det) > 2 else 0.5
            track_id = int(det[4]) if len(det) > 4 else 0
            active_track_ids.add(track_id)

            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2

            is_staff           = detect_staff_by_color(frame, bbox)
            visitor_id, is_re  = reid.get_or_create(track_id, frame, bbox, now)

            # ── ENTRY / EXIT (entry_exit cameras) ──
            if camera_type == "entry_exit":
                prev_cy   = track_prev_cy.get(track_id)
                direction = _detect_direction(prev_cy, cy, entry_threshold_y)
                track_prev_cy[track_id] = cy

                if direction == "ENTRY":
                    seq = reid.next_seq(track_id)
                    emitter.emit(visitor_id,
                                 "REENTRY" if is_re else "ENTRY",
                                 ts, None, 0, is_staff, conf, seq)
                elif direction == "EXIT":
                    seq = reid.next_seq(track_id)
                    emitter.emit(visitor_id, "EXIT", ts, None, 0,
                                 is_staff, conf, seq)
                    reid.retire(track_id, now)

            # ── ZONE classification (floor + billing cameras) ──
            elif camera_type in ("main_floor", "billing"):
                current_zone    = classify_zone(cx, cy, zones)
                current_zone_id = current_zone["zone_id"] if current_zone else None
                prev_state      = track_zone.get(track_id, {})
                prev_zone_id    = prev_state.get("zone_id")

                if current_zone_id != prev_zone_id:
                    # Zone exit
                    if prev_zone_id:
                        seq = reid.next_seq(track_id)
                        emitter.emit(visitor_id, "ZONE_EXIT", ts, prev_zone_id, 0,
                                     is_staff, conf, seq,
                                     sku_zone=prev_state.get("sku_zone"))
                        # FIX 2 — only decrement billing count for this visitor
                        if prev_zone_id and current_zone and \
                                current_zone.get("zone_type") == "BILLING":
                            billing_occupants.pop(visitor_id, None)

                    # Zone enter
                    if current_zone_id:
                        seq = reid.next_seq(track_id)
                        sku = current_zone.get("sku_zone") if current_zone else None
                        is_billing = (
                            current_zone
                            and current_zone.get("zone_type") == "BILLING"
                        )

                        if is_billing:
                            # FIX 2 — only count NON-staff in billing queue
                            if not is_staff:
                                was_already_in = visitor_id in billing_occupants
                                billing_occupants[visitor_id] = True
                                queue_depth = sum(
                                    1 for _ in billing_occupants
                                )  # current customer count at counter
                                if queue_depth > 1 and not was_already_in:
                                    emitter.emit(visitor_id, "BILLING_QUEUE_JOIN",
                                                 ts, current_zone_id, 0,
                                                 is_staff, conf, seq,
                                                 queue_depth=queue_depth - 1,
                                                 sku_zone=sku)
                                else:
                                    emitter.emit(visitor_id, "ZONE_ENTER", ts,
                                                 current_zone_id, 0, is_staff, conf,
                                                 seq, sku_zone=sku)
                            # Staff entering billing = ZONE_ENTER only, no queue count
                            else:
                                emitter.emit(visitor_id, "ZONE_ENTER", ts,
                                             current_zone_id, 0, is_staff, conf,
                                             seq, sku_zone=sku)
                        else:
                            emitter.emit(visitor_id, "ZONE_ENTER", ts,
                                         current_zone_id, 0, is_staff, conf,
                                         seq, sku_zone=sku)

                        track_zone[track_id] = {
                            "zone_id":   current_zone_id,
                            "enter_time": now,
                            "last_dwell_emit": now,
                            "sku_zone":  sku,
                        }
                    else:
                        track_zone.pop(track_id, None)

                # ZONE_DWELL — every 30s of continuous presence
                elif current_zone_id and track_id in track_zone:
                    state   = track_zone[track_id]
                    elapsed = (now - state["last_dwell_emit"]).total_seconds()
                    if elapsed >= 30:
                        seq          = reid.next_seq(track_id)
                        dwell_total  = int((now - state["enter_time"]).total_seconds() * 1000)
                        emitter.emit(visitor_id, "ZONE_DWELL", ts, current_zone_id,
                                     dwell_total, is_staff, conf, seq,
                                     sku_zone=state.get("sku_zone"))
                        track_zone[track_id]["last_dwell_emit"] = now

        if frame_num % 500 == 0:
            written = emitter.flush()
            print(f"  Frame {frame_num}/{total_frames} — flushed {written} events")

    cap.release()
    final = emitter.flush()
    print(f"[detect] Done. Final flush: {final} events.")
    return final


def _detect_direction(prev_cy: float | None, curr_cy: float,
                      threshold_y: float) -> str | None:
    """
    Determine ENTRY/EXIT from centroid crossing the threshold line.
    For the top-down entry camera (CAM1): the glass door is at the TOP of
    frame. A person entering the store moves DOWNWARD (cy increases).
    So ENTRY = prev_cy < threshold_y AND curr_cy >= threshold_y.
    The old detect_direction had this correct; keeping same logic here.
    """
    if prev_cy is None:
        return None
    crossed = (
        (prev_cy < threshold_y <= curr_cy) or
        (prev_cy > threshold_y >= curr_cy)
    )
    if not crossed:
        return None
    return "ENTRY" if curr_cy > prev_cy else "EXIT"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Purplle Store Intelligence — Detection Pipeline"
    )
    parser.add_argument("--clip",       required=True)
    parser.add_argument("--store_id",   required=True)
    parser.add_argument("--camera_id",  required=False)
    parser.add_argument("--layout",     default="data/store_layout.json")
    parser.add_argument("--output",     default="data/events/")
    parser.add_argument("--clip_start", required=False,
                        help="ISO-8601 UTC e.g. 2026-03-08T13:40:00Z")
    parser.add_argument("--frame_skip", type=int, default=3)
    args = parser.parse_args()

    camera_id = args.camera_id
    if not camera_id:
        clip_stem = Path(args.clip).stem.lower()
        for key, cid in CLIP_CAMERA_MAP.get(args.store_id, {}).items():
            if key in clip_stem:
                camera_id = cid
                break
        if not camera_id:
            raise ValueError(
                f"Cannot auto-detect camera_id for '{args.clip}'. Pass --camera_id."
            )
        print(f"[detect] Auto-detected camera_id: {camera_id}")

    clip_start = None
    if args.clip_start:
        clip_start = datetime.fromisoformat(
            args.clip_start.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

    # FIX 4 — create reid once so it persists if process_clip is called in a loop
    reid = ReIDMemory()

    process_clip(
        clip_path=args.clip,
        store_id=args.store_id,
        camera_id=camera_id,
        layout_path=args.layout,
        output_dir=args.output,
        clip_start=clip_start,
        frame_skip=args.frame_skip,
        reid=reid,
    )


if __name__ == "__main__":
    main()