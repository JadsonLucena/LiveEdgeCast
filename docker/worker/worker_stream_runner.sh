#!/bin/bash
set -euo pipefail

STREAM_KEY="${STREAM_KEY:-}"
PID_FILE="/tmp/ffmpeg_${STREAM_KEY}.pid"
PROGRESS_FILE="/tmp/ffmpeg_${STREAM_KEY}.progress"
EXIT_FILE="/tmp/ffmpeg_${STREAM_KEY}.exit"
PROGRESS_NOTIFY_FILE="/tmp/ffmpeg_${STREAM_KEY}.progress_notified"
PROGRESS_NOTIFY_LOCK="/tmp/ffmpeg_${STREAM_KEY}.progress_notify.lock"
PROGRESS_READER_PID=""
FFMPEG_PID=""

log() {
  echo "[$(date)] [worker_stream_runner] $1"
}

notify_controller() {
  local path="$1"
  curl -sf --connect-timeout "${CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS:-1}" --max-time "${CONTROLLER_CALLBACK_MAX_TIME_SECONDS:-2}" -X POST --get \
    --data-urlencode "stream=${STREAM_KEY}" \
    --data-urlencode "worker_pod=${WORKER_POD}" \
    "${CONTROLLER_API}${path}"
}

progress_file_has_complete_line() {
  local progress_line
  while IFS= read -r progress_line; do
    case "$progress_line" in
      *=*)
        if [ -n "${progress_line%%=*}" ]; then
          return 0
        fi
        ;;
    esac
  done < "$PROGRESS_FILE"
  return 1
}

notify_first_progress_once() {
  if [ -f "$PROGRESS_NOTIFY_FILE" ]; then
    return 0
  fi
  if mkdir "$PROGRESS_NOTIFY_LOCK" 2>/dev/null; then
    set +e
    notify_controller "/workers/progress"
    notify_exit=$?
    set -e
    if [ "$notify_exit" -eq 0 ]; then
      : > "$PROGRESS_NOTIFY_FILE"
      rmdir "$PROGRESS_NOTIFY_LOCK" 2>/dev/null || true
      return 0
    fi
    log "Failed to notify controller about first FFmpeg progress; will retry."
    rmdir "$PROGRESS_NOTIFY_LOCK" 2>/dev/null || true
  fi
  return 1
}

cleanup() {
  if [ -n "${PROGRESS_READER_PID:-}" ]; then
    kill -TERM "$PROGRESS_READER_PID" 2>/dev/null || true
    wait "$PROGRESS_READER_PID" 2>/dev/null || true
  fi
  if [ -n "${STREAM_KEY:-}" ]; then
    rm -f "$PID_FILE" "$PROGRESS_NOTIFY_FILE"
    rm -rf "$PROGRESS_NOTIFY_LOCK"
  fi
}
trap cleanup EXIT

log "Starting worker stream runner for stream key '$STREAM_KEY'"

RTMP_PUSH_BASE_URL="${RTMP_PUSH_BASE_URL:-}"
PROXY_ADDR="${PROXY_DNS:-}"
CONTROLLER_API="${CONTROLLER_API:-http://controller.media.svc.cluster.local:8000}"
WORKER_POD="${HOSTNAME:-unknown-worker}"

if [ -z "$PROXY_ADDR" ] || [ -z "$STREAM_KEY" ] || [ -z "$RTMP_PUSH_BASE_URL" ]; then
  log "Missing required startup args (PROXY_DNS/STREAM_KEY/RTMP_PUSH_BASE_URL). Crashing worker."
  exit 1
fi

PROXY_RTMP="rtmp://${PROXY_ADDR}:1935/live/${STREAM_KEY}"
TARGET_RTMP="${RTMP_PUSH_BASE_URL}/${STREAM_KEY}"
rm -f "$PROGRESS_FILE" "$PROGRESS_NOTIFY_FILE"
rm -rf "$PROGRESS_NOTIFY_LOCK"
: > "$PROGRESS_FILE"

(
  progress_notified=0
  while [ "$progress_notified" -eq 0 ]; do
    if progress_file_has_complete_line && notify_first_progress_once; then
      progress_notified=1
      break
    fi
    sleep "${PROGRESS_NOTIFY_POLL_SECONDS:-0.2}"
  done
) &
PROGRESS_READER_PID=$!

log "Launching FFmpeg (pull=$PROXY_RTMP push=$TARGET_RTMP progress_file=$PROGRESS_FILE)"
notify_controller "/workers/ffmpeg/started" \
  || log "Failed to notify controller about FFmpeg start; continuing."

ffmpeg \
  -loglevel warning \
  -nostats \
  -rw_timeout 5000000 \
  -i "$PROXY_RTMP" \
  -c:v copy \
  -c:a copy \
  -progress "$PROGRESS_FILE" \
  -f flv "$TARGET_RTMP" \
  >> "/tmp/ffmpeg_${STREAM_KEY}.log" 2>&1 &

FFMPEG_PID=$!
echo "$FFMPEG_PID" > "$PID_FILE"
FFMPEG_RUN_ID="$(date +%s%N)-${FFMPEG_PID}"

if wait "$FFMPEG_PID"; then
  EXIT_CODE=0
else
  EXIT_CODE=$?
fi

if progress_file_has_complete_line; then
  notify_first_progress_once || true
fi

printf '%s %s\n' "$FFMPEG_RUN_ID" "$EXIT_CODE" >> "$EXIT_FILE"
rm -f "$PID_FILE"

if [ "$EXIT_CODE" -ne 0 ]; then
  log "FFmpeg exited with code $EXIT_CODE. Crashing worker for controller replacement."
  tail -n 40 "/tmp/ffmpeg_${STREAM_KEY}.log" | sed 's/^/[ffmpeg] /' || true
  exit "$EXIT_CODE"
fi

log "FFmpeg exited cleanly."
exit 0
