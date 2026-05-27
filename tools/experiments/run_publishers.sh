#!/usr/bin/env bash
set -euo pipefail
EXPERIMENT_ID=${experiment_id:-${EXPERIMENT_ID:-exp}}; SCENARIO=${scenario:-${SCENARIO:-base}}; RUN_ID=${run_id:-${RUN_ID:-r1}}
CONCURRENCY=${concurrency:-1}; DURATION=${duration_seconds:-60}; BITRATE=${bitrate:-2500k}; BASE=${rtmp_base:-rtmp://localhost/live}
mkdir -p /tmp/liveedgecast-pids
for i in $(seq 1 "$CONCURRENCY"); do
  key="${EXPERIMENT_ID}-${SCENARIO}-${RUN_ID}-s${i}"
  ffmpeg -re -f lavfi -i testsrc=size=1280x720:rate=30 -f lavfi -i sine=frequency=1000 -c:v libx264 -b:v "$BITRATE" -c:a aac -f flv "$BASE/$key" >/tmp/liveedgecast-pids/$key.log 2>&1 &
  echo $! > /tmp/liveedgecast-pids/$key.pid
 done
sleep "$DURATION"
