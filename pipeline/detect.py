"""
detect.py — Main detection + tracking script
Processes CCTV clips using YOLOv8 + ByteTrack, emits structured events.
"""

import cv2
import json
import logging
import argparse
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
from ultralytics import YOLO

from pipeline.tracker import PersonTracker
from pipeline.emit import EventEmitter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("detect")


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

PERSON_CLASS_ID = 0
MIN_CONFIDENCE = 0.35
TARGET_FPS = 15


# ---------------------------------------------------------------------------
# STORE LOADER
# ---------------------------------------------------------------------------

def load_store_layout(layout_path: Path) -> dict:
    with open(layout_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# TIMESTAMP
# ---------------------------------------------------------------------------

def parse_clip_start_time(clip_path: Path) -> datetime:
    stem = clip_path.stem
    parts = stem.split("_")

    for part in reversed(parts):
        try:
            return datetime.strptime(part, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue

    # FIX 2: Use today's date as fallback instead of hardcoded 2026-03-03
    log.warning("Fallback epoch used for %s", clip_path.name)
    return datetime(2026, 5, 31, 9, 0, 0, tzinfo=timezone.utc)


def frame_to_timestamp(start_time: datetime, frame_idx: int, fps: float) -> str:
    offset_ms = int((frame_idx / fps) * 1000)
    ts = start_time + timedelta(milliseconds=offset_ms)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# ZONE RESOLVER
# ---------------------------------------------------------------------------

def resolve_zone(cx: float, cy: float, store_zones: list[dict], camera_id: str):
    point = (float(cx), float(cy))

    for zone in store_zones:
        cameras = zone.get("cameras", [])

        if cameras and camera_id not in cameras:
            continue

        poly = np.array(zone.get("polygon", []), dtype=np.float32)

        if poly.size == 0:
            continue

        poly = poly.reshape((-1, 1, 2))

        try:
            if cv2.pointPolygonTest(poly, point, False) >= 0:
                return zone.get("zone_id")
        except cv2.error:
            continue

    return None


# ---------------------------------------------------------------------------
# CAMERA ID
# ---------------------------------------------------------------------------

def extract_camera_id(clip_path: Path) -> str:
    stem = clip_path.stem  # e.g. "CAM 1" or "CAM_ENTRY_01"

    # FIX 1: Handle "CAM N" style names (with space or no underscore)
    m = re.match(r"CAM\s*(\d+)", stem, re.IGNORECASE)
    if m:
        cam_map = {
            "1": "CAM_ENTRY_01",
            "2": "CAM_FLOOR_01",
            "3": "CAM_FLOOR_01",
            "4": "CAM_BILLING_01",
            "5": "CAM_BILLING_01",
        }
        return cam_map.get(m.group(1), f"CAM_FLOOR_{m.group(1).zfill(2)}")

    # Handle "CAM_ENTRY_01" style names
    parts = stem.split("_")
    for i, p in enumerate(parts):
        if p == "CAM" and i + 1 < len(parts):
            return f"{parts[i]}_{parts[i+1]}"
        if p.startswith("CAM"):
            return p

    return "CAM_UNKNOWN_01"


# ---------------------------------------------------------------------------
# PROCESS CLIP
# ---------------------------------------------------------------------------

def process_clip(
    clip_path: Path,
    store_id: str,
    store_layout: dict,
    emitter: EventEmitter,
    model: YOLO,
    clip_start: Optional[datetime] = None,
):
    start_time = clip_start or parse_clip_start_time(clip_path)

    # FIX 3: Layout JSON is flat {"store_id":..., "zones":[...]}, not nested by store_id
    if "zones" in store_layout:
        store_zones = store_layout["zones"]
    else:
        store_zones = store_layout.get(store_id, {}).get("zones", [])

    camera_id = extract_camera_id(clip_path)

    log.info(
        "Processing clip: %s | camera_id=%s | zones=%d | start=%s",
        clip_path.name, camera_id, len(store_zones), start_time.isoformat()
    )

    cap = cv2.VideoCapture(str(clip_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {clip_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or TARGET_FPS

    frame_idx = 0
    stats = {"frames": 0, "detections": 0, "events_emitted": 0}

    tracker = PersonTracker(
        store_id=store_id,
        camera_id=camera_id,
        frame_w=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
        frame_h=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
    )

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        timestamp = frame_to_timestamp(start_time, frame_idx, fps)

        results = model.track(
            frame,
            persist=True,
            classes=[PERSON_CLASS_ID],
            conf=MIN_CONFIDENCE,
            iou=0.45,
            verbose=False,
        )

        detections = []

        if results and results[0].boxes is not None:
            boxes = results[0].boxes

            for i in range(len(boxes)):
                xyxy = boxes.xyxy[i].cpu().numpy().astype(int)
                conf = float(boxes.conf[i].cpu().numpy())

                tid = -1
                if boxes.id is not None and boxes.id[i] is not None:
                    tid = int(boxes.id[i].cpu().numpy())

                x1, y1, x2, y2 = xyxy
                cx = (x1 + x2) / 2
                cy = (y1 + y2) / 2

                zone_id = resolve_zone(cx, cy, store_zones, camera_id)

                detections.append({
                    "track_id": tid,
                    "bbox": (x1, y1, x2, y2),
                    "centroid": (cx, cy),
                    "confidence": round(conf, 3),
                    "zone_id": zone_id,
                    "timestamp": timestamp,
                    "frame_idx": frame_idx,
                    "is_staff": False,
                    "staff_confidence": 0.0,
                })

                stats["detections"] += 1

        events = tracker.update(detections, timestamp, frame_idx, fps)

        for event in events:
            emitter.emit(event)
            stats["events_emitted"] += 1

        frame_idx += 1
        stats["frames"] += 1

    cap.release()

    if hasattr(tracker, "flush"):
        closing_events = tracker.flush(timestamp)
        for event in closing_events:
            emitter.emit(event)
            stats["events_emitted"] += 1

    log.info(
        "Clip done: %s | frames=%d detections=%d events=%d",
        clip_path.name, stats["frames"], stats["detections"], stats["events_emitted"]
    )

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--clips-dir", required=True)
    parser.add_argument("--layout", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="yolov8s.pt")

    # kept for spec compatibility
    parser.add_argument("--store-id", default="STORE_BLR_002")
    parser.add_argument("--clip-start", default=None)

    args = parser.parse_args()

    store_layout = load_store_layout(Path(args.layout))
    model = YOLO(args.model)

    emitter = EventEmitter(Path(args.output))

    clips = sorted(Path(args.clips_dir).glob("**/*.mp4"))

    if not clips:
        log.error("No .mp4 files found in %s", args.clips_dir)
        return

    log.info("Found %d clips to process", len(clips))

    for clip in clips:
        process_clip(
            clip,
            store_id=args.store_id,
            store_layout=store_layout,
            emitter=emitter,
            model=model,
        )

    emitter.close()
    print("Pipeline complete")


if __name__ == "__main__":
    main()