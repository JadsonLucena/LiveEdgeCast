#!/bin/bash
set -euo pipefail

STREAM_KEY="${STREAM_KEY:-}"

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

exec ffmpeg \
  -loglevel warning \
  -nostats \
  -rw_timeout 5000000 \
  -i "$PROXY_RTMP" \
  -c:v copy \
  -c:a copy \
  -f flv "$TARGET_RTMP"
