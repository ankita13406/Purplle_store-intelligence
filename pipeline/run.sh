#!/usr/bin/env bash
# run.sh — Process all CCTV clips and stream events into the API
# Usage: bash pipeline/run.sh [--clips-dir data/clips] [--live]
set -euo pipefail

CLIPS_DIR="${CLIPS_DIR:-data/clips}"
LAYOUT="${LAYOUT:-data/store_layout.json}"
OUTPUT="${OUTPUT:-data/events.jsonl}"
MODEL="${MODEL:-yolov8s.pt}"
API_ENDPOINT="${API_ENDPOINT:-http://localhost:8000}"
LIVE="${LIVE:-false}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clips-dir) CLIPS_DIR="$2"; shift 2 ;;
    --layout)    LAYOUT="$2";    shift 2 ;;
    --output)    OUTPUT="$2";    shift 2 ;;
    --model)     MODEL="$2";     shift 2 ;;
    --live)      LIVE="true";    shift   ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo "================================================"
echo "  Purplle Store Intelligence — Detection Pipeline"
echo "================================================"
echo "  Clips dir : $CLIPS_DIR"
echo "  Layout    : $LAYOUT"
echo "  Output    : $OUTPUT"
echo "  Model     : $MODEL"
echo "  Live feed : $LIVE"
echo ""

# Validate inputs
if [ ! -d "$CLIPS_DIR" ]; then
  echo "ERROR: clips directory not found: $CLIPS_DIR"
  exit 1
fi
if [ ! -f "$LAYOUT" ]; then
  echo "ERROR: layout file not found: $LAYOUT"
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT")"

# Build API endpoint arg
API_ARG=""
if [ "$LIVE" = "true" ]; then
  API_ARG="--api-endpoint $API_ENDPOINT"
  echo "  Waiting for API to be ready..."
  for i in $(seq 1 20); do
    if curl -sf "$API_ENDPOINT/health" > /dev/null 2>&1; then
      echo "  API is ready."
      break
    fi
    sleep 1
  done
fi

echo ""
echo "Starting detection pipeline..."
python pipeline/detect.py \
  --clips-dir  "$CLIPS_DIR" \
  --layout     "$LAYOUT" \
  --output     "$OUTPUT" \
  --model      "$MODEL" \
  $API_ARG

echo ""
echo "Detection complete. Events written to: $OUTPUT"
echo ""

# If not live, replay the JSONL into the API in batches
if [ "$LIVE" = "false" ] && curl -sf "$API_ENDPOINT/health" > /dev/null 2>&1; then
  echo "Replaying events into API..."
  python pipeline/replay.py --events "$OUTPUT" --api "$API_ENDPOINT"
  echo "Replay complete."
fi

echo ""
echo "================================================"
echo "  Pipeline finished."
echo "  Dashboard: $API_ENDPOINT/dashboard"
echo "  Metrics:   $API_ENDPOINT/stores/STORE_BLR_002/metrics"
echo "================================================"
