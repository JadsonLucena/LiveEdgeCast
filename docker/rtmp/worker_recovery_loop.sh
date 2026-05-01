#!/bin/bash

set -euo pipefail

STREAM_NAME="$1"
CONTROLLER_API="http://rtmp-controller.media.svc.cluster.local:8000"
MAX_RECOVERY_ATTEMPTS=8
MAX_RECOVERY_SECONDS=120
RETRY_DELAY_SECONDS=3
PID_FILE="/tmp/ffmpeg_${STREAM_NAME}.pid"
START_TS=$(date +%s)
ATTEMPT=0

log() {
  echo "[$(date)] [worker_recovery] $1"
}

send_heartbeat() {
  local stream=$1
  local proxy_pod=$2
  curl -sf -X POST \
    "$CONTROLLER_API/streams/heartbeat?stream=$stream&proxy_pod=$proxy_pod" \
    >/dev/null 2>&1 || log "Heartbeat send failed for stream '$stream'"
}

log "Recovery loop started for stream '$STREAM_NAME'"

while true; do
  NOW_TS=$(date +%s)
  ELAPSED=$((NOW_TS - START_TS))

  if [ "$ATTEMPT" -ge "$MAX_RECOVERY_ATTEMPTS" ] || [ "$ELAPSED" -ge "$MAX_RECOVERY_SECONDS" ]; then
    log "Recovery limit reached (attempts=$ATTEMPT/$MAX_RECOVERY_ATTEMPTS, elapsed=${ELAPSED}s/${MAX_RECOVERY_SECONDS}s). Releasing stream and stopping worker flow."
    rm -f "$PID_FILE"
    curl -sf -X POST "$CONTROLLER_API/release?stream=$STREAM_NAME" >/dev/null || true
    exit 0
  fi

  ATTEMPT=$((ATTEMPT + 1))
  log "Attempt $ATTEMPT: resolving stream '$STREAM_NAME' in controller"

  STREAM_INFO=$(curl -sf "$CONTROLLER_API/stream-key?stream=$STREAM_NAME" 2>/dev/null || true)
  if [ -z "$STREAM_INFO" ] || ! echo "$STREAM_INFO" | jq -e . >/dev/null 2>&1; then
    log "Controller returned empty/invalid JSON. Waiting ${RETRY_DELAY_SECONDS}s before next attempt."
    sleep "$RETRY_DELAY_SECONDS"
    continue
  fi

  YOUTUBE_KEY=$(echo "$STREAM_INFO" | jq -r '.youtubeKey // empty')
  PROXY_ADDR=$(echo "$STREAM_INFO" | jq -r '.proxyDns // empty')

  if [ -z "$PROXY_ADDR" ] || [ -z "$YOUTUBE_KEY" ]; then
    log "Stream info unavailable (proxy/key). Waiting ${RETRY_DELAY_SECONDS}s before next attempt."
    sleep "$RETRY_DELAY_SECONDS"
    continue
  fi

  PROXY_RTMP="rtmp://${PROXY_ADDR}:1935/live/${STREAM_NAME}"
  YT_RTMP="rtmp://a.rtmp.youtube.com/live2/${YOUTUBE_KEY}"
  
  # Extract proxy pod name from CONTROLLER for heartbeat
  # For heartbeat, we need to send proxy_pod. Get it from controller's registry
  PROXY_POD_NAME=$(hostname)  # Default to this pod name if available
  
  log "Starting FFmpeg (pull=$PROXY_RTMP, push=$YT_RTMP)"
  log "Heartbeat will be sent every 2s to keep stream alive in controller registry"
  
  # Start heartbeat loop in background (sends every 2s while FFmpeg runs)
  (
    while true; do
      sleep 2
      if [ -f "$PID_FILE" ]; then
        send_heartbeat "$STREAM_NAME" "$PROXY_POD_NAME"
      else
        break
      fi
    done
  ) &
  HEARTBEAT_PID=$!
  
  ffmpeg \
    -loglevel warning \
    -rw_timeout 5000000 \
    -i "$PROXY_RTMP" \
    -c:v copy \
    -c:a copy \
    -f flv "$YT_RTMP" \
    >> "/tmp/ffmpeg_${STREAM_NAME}.log" 2>&1 &

  FFMPEG_PID=$!
  echo "$FFMPEG_PID" > "$PID_FILE"

  if wait "$FFMPEG_PID"; then
    EXIT_CODE=0
  else
    EXIT_CODE=$?
  fi
  rm -f "$PID_FILE"
  
  # Stop heartbeat loop
  kill "$HEARTBEAT_PID" 2>/dev/null || true
  wait "$HEARTBEAT_PID" 2>/dev/null || true

  if [ "$EXIT_CODE" -eq 0 ]; then
    log "FFmpeg exited cleanly for stream '$STREAM_NAME'. Stopping recovery loop."
    exit 0
  fi

  log "FFmpeg exited with code $EXIT_CODE. Will re-resolve and retry in ${RETRY_DELAY_SECONDS}s."
  sleep "$RETRY_DELAY_SECONDS"
done
