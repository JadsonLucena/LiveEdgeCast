#!/bin/bash
set -euo pipefail

STREAM_KEY="${STREAM_KEY:-}"

log() {
  echo "[$(date)] [worker_stream_runner] $1"
}

log "Starting worker stream runner for stream key '$STREAM_KEY'"

SOURCE_RTMP_URL="${SOURCE_RTMP_URL:-}"
TARGET_RTMP_URL="${TARGET_RTMP_URL:-}"

if [ -z "$SOURCE_RTMP_URL" ] || [ -z "$STREAM_KEY" ] || [ -z "$TARGET_RTMP_URL" ]; then
  log "Missing required startup args (SOURCE_RTMP_URL/STREAM_KEY/TARGET_RTMP_URL). Crashing worker."
  exit 1
fi

log "Launching FFmpeg for stream '$STREAM_KEY'"

exec ffmpeg \
  -loglevel warning \
  -nostats \
  -rw_timeout 5000000 \
  -i "$SOURCE_RTMP_URL" \
  -c:v copy \
  -c:a copy \
  -f flv "$TARGET_RTMP_URL"
