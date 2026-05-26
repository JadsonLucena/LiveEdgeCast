#!/bin/bash
set -euo pipefail

STREAM_KEY="${STREAM_KEY:-}"
PID_FILE="/tmp/ffmpeg_${STREAM_KEY}.pid"
LOG_FILE="/tmp/ffmpeg_${STREAM_KEY}.log"
PROGRESS_FILE="/tmp/ffmpeg_${STREAM_KEY}.progress"
HEALTH_FILE="/tmp/worker-health"
HEALTH_META_FILE="/tmp/worker-health-meta"
HEALTH_STALE_SECONDS="${HEALTH_STALE_SECONDS:-15}"

log() {
  echo "[$(date)] [worker_stream_runner] $1"
}

write_health_state() {
  local state="$1"
  local message="$2"
  printf '%s\n' "$state" > "$HEALTH_FILE"
  printf 'state=%s stream=%s ts=%s msg=%s\n' "$state" "$STREAM_KEY" "$(date -Iseconds)" "$message" > "$HEALTH_META_FILE"
}

read_progress_out_time_us() {
  local file="$1"
  if [ ! -s "$file" ]; then
    return 1
  fi

  awk -F= '/^out_time_us=/{v=$2} END{ if (v != "") print v; else exit 1 }' "$file" 2>/dev/null
}

monitor_ffmpeg_media_health() {
  local ffmpeg_pid="$1"
  local last_progress_epoch="$(date +%s)"
  local last_out_time_us=""

  while kill -0 "$ffmpeg_pid" 2>/dev/null; do
    local current_out_time_us=""
    if current_out_time_us="$(read_progress_out_time_us "$PROGRESS_FILE" 2>/dev/null)"; then
      if [ -n "$current_out_time_us" ] && [ "$current_out_time_us" != "$last_out_time_us" ]; then
        last_progress_epoch="$(date +%s)"
        last_out_time_us="$current_out_time_us"
        write_health_state "healthy" "ffmpeg_pid=${ffmpeg_pid} out_time_us=${current_out_time_us}"
      fi
    fi

    local now_epoch="$(date +%s)"
    if [ $((now_epoch - last_progress_epoch)) -ge "$HEALTH_STALE_SECONDS" ]; then
      write_health_state "unhealthy" "ffmpeg_pid=${ffmpeg_pid} no_ffmpeg_progress_for=${HEALTH_STALE_SECONDS}s"
    fi

    sleep 2
  done
}

log "Starting worker stream runner for stream key '$STREAM_KEY'"

RTMP_PUSH_BASE_URL="${RTMP_PUSH_BASE_URL:-}"
PROXY_ADDR="${PROXY_DNS:-}"

if [ -z "$PROXY_ADDR" ] || [ -z "$STREAM_KEY" ] || [ -z "$RTMP_PUSH_BASE_URL" ]; then
  log "Missing required startup args (PROXY_DNS/STREAM_KEY/RTMP_PUSH_BASE_URL). Crashing worker."
  write_health_state "unhealthy" "missing_required_startup_args"
  exit 1
fi

PROXY_RTMP="rtmp://${PROXY_ADDR}:1935/live/${STREAM_KEY}"
TARGET_RTMP="${RTMP_PUSH_BASE_URL}/${STREAM_KEY}"

rm -f "$PROGRESS_FILE"
write_health_state "starting" "launching_ffmpeg"
log "Launching FFmpeg (pull=$PROXY_RTMP push=$TARGET_RTMP)"

ffmpeg \
  -loglevel warning \
  -nostats \
  -progress "$PROGRESS_FILE" \
  -rw_timeout 5000000 \
  -i "$PROXY_RTMP" \
  -c:v copy \
  -c:a copy \
  -f flv "$TARGET_RTMP" \
  >> "$LOG_FILE" 2>&1 &

FFMPEG_PID=$!
echo "$FFMPEG_PID" > "$PID_FILE"

monitor_ffmpeg_media_health "$FFMPEG_PID" &
MONITOR_PID=$!

if wait "$FFMPEG_PID"; then
  EXIT_CODE=0
else
  EXIT_CODE=$?
fi

kill -TERM "$MONITOR_PID" 2>/dev/null || true
wait "$MONITOR_PID" 2>/dev/null || true

rm -f "$PID_FILE"

if [ "$EXIT_CODE" -ne 0 ]; then
  log "FFmpeg exited with code $EXIT_CODE. Crashing worker for controller replacement."
  write_health_state "unhealthy" "ffmpeg_exited_code=${EXIT_CODE}"
  tail -n 40 "$LOG_FILE" | sed 's/^/[ffmpeg] /' || true
  exit "$EXIT_CODE"
fi

write_health_state "unhealthy" "ffmpeg_exited_cleanly"
log "FFmpeg exited cleanly."
exit 0
