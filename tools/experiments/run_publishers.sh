#!/usr/bin/env bash
set -euo pipefail
EXPERIMENT_ID=${experiment_id:-exp}; SCENARIO=${scenario:-baseline}; RUN_ID=${run_id:-run1}
CONCURRENCY=${concurrency:-1}; DURATION=${duration_seconds:-60}; BITRATE=${bitrate:-10000k}
BASE=${RTMP_INPUT_BASE:-rtmp://proxy-lb.media.svc.cluster.local/live}
for i in $(seq 1 "$CONCURRENCY"); do
  key="${EXPERIMENT_ID}-${SCENARIO}-${RUN_ID}-${i}"
  ffmpeg -re -f lavfi -i testsrc=size=1920x1080:rate=30 -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100 \
   -c:v libx264 -preset veryfast -pix_fmt yuv420p -r 30 -g 60 -keyint_min 60 -sc_threshold 0 -bf 2 -refs 1 -coder 1 -b:v "$BITRATE" -minrate "$BITRATE" -maxrate "$BITRATE" -bufsize 20000k -c:a aac -b:a 128k -ar 44100 -f flv "$BASE/$key" >/tmp/pub-$key.log 2>&1 &
  echo $! > "/tmp/pub-$key.pid"
done
sleep "$DURATION"
