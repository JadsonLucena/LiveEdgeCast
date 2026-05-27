#!/bin/bash
set -euo pipefail

STREAM_KEY="${STREAM_KEY:-}"
PID_FILE="/tmp/ffmpeg_${STREAM_KEY}.pid"
PROGRESS_FILE="/tmp/ffmpeg_${STREAM_KEY}.progress"
EXIT_FILE="/tmp/ffmpeg_${STREAM_KEY}.exit"

log() {
  echo "[$(date)] [worker_stream_runner] $1"
}

log "Starting worker stream runner for stream key '$STREAM_KEY'"

RTMP_PUSH_BASE_URL="${RTMP_PUSH_BASE_URL:-}"
PROXY_ADDR="${PROXY_DNS:-}"

if [ -z "$PROXY_ADDR" ] || [ -z "$STREAM_KEY" ] || [ -z "$RTMP_PUSH_BASE_URL" ]; then
  log "Missing required startup args (PROXY_DNS/STREAM_KEY/RTMP_PUSH_BASE_URL). Crashing worker."
  exit 1
fi

PROXY_RTMP="rtmp://${PROXY_ADDR}:1935/live/${STREAM_KEY}"
TARGET_RTMP="${RTMP_PUSH_BASE_URL}/${STREAM_KEY}"

log "Launching FFmpeg (pull=$PROXY_RTMP push=$TARGET_RTMP)"

ffmpeg \
  -loglevel warning \
  -progress "$PROGRESS_FILE" \
  -nostats \
  -rw_timeout 5000000 \
  -i "$PROXY_RTMP" \
  -c:v copy \
  -c:a copy \
  -f flv "$TARGET_RTMP" \
  >> "/tmp/ffmpeg_${STREAM_KEY}.log" 2>&1 &

FFMPEG_PID=$!
echo "$FFMPEG_PID" > "$PID_FILE"

if wait "$FFMPEG_PID"; then
  EXIT_CODE=0
else
  EXIT_CODE=$?
fi

rm -f "$PID_FILE"
echo "$EXIT_CODE" > "$EXIT_FILE"

if [ "$EXIT_CODE" -ne 0 ]; then
  log "FFmpeg exited with code $EXIT_CODE. Crashing worker for controller replacement."
  tail -n 40 "/tmp/ffmpeg_${STREAM_KEY}.log" | sed 's/^/[ffmpeg] /' || true
  exit "$EXIT_CODE"
fi

log "FFmpeg exited cleanly."
exit 0
