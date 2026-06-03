#!/usr/bin/env bash
# pipeline/run.sh
# ═══════════════════════════════════════════════════════════════════════════
# Processes ALL CCTV clips for both stores and emits structured events.
# Usage: bash pipeline/run.sh
# Output: data/events/<store_id>_<camera_id>.jsonl + data/events_all.jsonl
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

LAYOUT="data/store_layout.json"
OUTPUT="data/events/"
CLIPS_DIR="data/clips"
MERGED="data/events_all.jsonl"

mkdir -p "$OUTPUT"

echo "═══════════════════════════════════════════════════════"
echo " Purplle Store Intelligence — Detection Pipeline"
echo " Processing both stores: ST1008 (Bangalore) + ST1076 (Mumbai)"
echo "═══════════════════════════════════════════════════════"

# ── STORE 1: ST1008 — Brigade Road, Bangalore ──────────────────────────────
STORE1="ST1008"
STORE1_DIR="${CLIPS_DIR}/ST1008"
echo ""
echo "▶ Processing ${STORE1} clips..."

if [ -f "${STORE1_DIR}/entry-1.mp4" ]; then
    python pipeline/detect.py \
        --clip "${STORE1_DIR}/entry-1.mp4" \
        --store_id "$STORE1" \
        --camera_id "ST1008_CAM_ENTRY_01" \
        --layout "$LAYOUT" --output "$OUTPUT" \
        --clip_start "2026-04-10T12:00:00"
    echo "  ✓ entry-1 processed"
fi

if [ -f "${STORE1_DIR}/entry-2.mp4" ]; then
    python pipeline/detect.py \
        --clip "${STORE1_DIR}/entry-2.mp4" \
        --store_id "$STORE1" \
        --camera_id "ST1008_CAM_ENTRY_02" \
        --layout "$LAYOUT" --output "$OUTPUT" \
        --clip_start "2026-04-10T12:00:00"
    echo "  ✓ entry-2 processed"
fi

if [ -f "${STORE1_DIR}/zone.mp4" ]; then
    python pipeline/detect.py \
        --clip "${STORE1_DIR}/zone.mp4" \
        --store_id "$STORE1" \
        --camera_id "ST1008_CAM_FLOOR_01" \
        --layout "$LAYOUT" --output "$OUTPUT" \
        --clip_start "2026-04-10T12:00:00"
    echo "  ✓ zone processed"
fi

if [ -f "${STORE1_DIR}/billing-area.mp4" ]; then
    python pipeline/detect.py \
        --clip "${STORE1_DIR}/billing-area.mp4" \
        --store_id "$STORE1" \
        --camera_id "ST1008_CAM_BILLING_01" \
        --layout "$LAYOUT" --output "$OUTPUT" \
        --clip_start "2026-04-10T12:00:00"
    echo "  ✓ billing-area processed"
fi

# ── STORE 2: ST1076 — Mumbai ───────────────────────────────────────────────
STORE2="ST1076"
STORE2_DIR="${CLIPS_DIR}/ST1076"
echo ""
echo "▶ Processing ${STORE2} clips..."

if [ -f "${STORE2_DIR}/entry-1.mp4" ]; then
    python pipeline/detect.py \
        --clip "${STORE2_DIR}/entry-1.mp4" \
        --store_id "$STORE2" \
        --camera_id "PURPLLE_MUM_1076_CAM1" \
        --layout "$LAYOUT" --output "$OUTPUT" \
        --clip_start "2026-03-08T18:00:00"
    echo "  ✓ entry-1 processed"
fi

if [ -f "${STORE2_DIR}/zone.mp4" ]; then
    python pipeline/detect.py \
        --clip "${STORE2_DIR}/zone.mp4" \
        --store_id "$STORE2" \
        --camera_id "PURPLLE_MUM_1076_CAM2" \
        --layout "$LAYOUT" --output "$OUTPUT" \
        --clip_start "2026-03-08T18:00:00"
    echo "  ✓ zone processed"
fi

if [ -f "${STORE2_DIR}/billing-area.mp4" ]; then
    python pipeline/detect.py \
        --clip "${STORE2_DIR}/billing-area.mp4" \
        --store_id "$STORE2" \
        --camera_id "PURPLLE_MUM_1076_CAM6" \
        --layout "$LAYOUT" --output "$OUTPUT" \
        --clip_start "2026-03-08T18:00:00"
    echo "  ✓ billing-area processed"
fi

# ── Merge all events → single sorted JSONL ────────────────────────────────
echo ""
echo "▶ Merging all events → ${MERGED}"
python pipeline/merge_events.py "$OUTPUT" "$MERGED"

echo ""
echo "═══════════════════════════════════════════════════════"
echo " ✓ Detection complete"
echo " Events: ${MERGED}"
echo " Feed into API: python pipeline/replay.py --input ${MERGED} --speed 10"
echo "═══════════════════════════════════════════════════════"