#!/bin/bash
set -euo pipefail

STREAM_KEY="${STREAM_KEY:-}"
PID_FILE="/tmp/ffmpeg_${STREAM_KEY}.pid"
PROGRESS_FILE="/tmp/ffmpeg_${STREAM_KEY}.progress"
EXIT_FILE="/tmp/ffmpeg_${STREAM_KEY}.exit"
PROGRESS_NOTIFY_FILE="/tmp/ffmpeg_${STREAM_KEY}.progress_notified"
PROGRESS_NOTIFY_LOCK="/tmp/ffmpeg_${STREAM_KEY}.progress_notify.lock"
PROGRESS_NOTIFY_ERROR_FILE="/tmp/ffmpeg_${STREAM_KEY}.progress_notify_error_logged"
PROGRESS_LOG_FILE="/tmp/ffmpeg_${STREAM_KEY}.first_progress_logged"
PROGRESS_LOG_LOCK="/tmp/ffmpeg_${STREAM_KEY}.first_progress_log.lock"
PROGRESS_READER_PID=""
FFMPEG_PID=""
RUNNER_START_MS="$(python3 - <<'PYMS'
import time
print(time.time_ns() // 1_000_000)
PYMS
)"
FFMPEG_START_MS=""

json_escape() {
  local value="${1:-}"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

current_epoch_ms() {
  python3 - <<'PYMS'
import time
print(time.time_ns() // 1_000_000)
PYMS
}

elapsed_ms() {
  local start_ms="${1:-$RUNNER_START_MS}"
  local now_ms
  now_ms="$(current_epoch_ms)"
  if [[ "$start_ms" =~ ^[0-9]+$ ]] && [ "$now_ms" -ge "$start_ms" ]; then
    printf '%s' "$((now_ms - start_ms))"
  else
    printf '0'
  fi
}

log_json() {
  local event_type="$1"
  local status="${2:-ok}"
  local duration_ms="${3:-$(elapsed_ms "$RUNNER_START_MS")}"
  local timestamp
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{"timestamp":"%s","event_type":"%s","stream":"%s","generation":"%s","proxy_pod":"%s","worker_pod":"%s","experiment_id":"%s","scenario":"%s","run_id":"%s","duration_ms":%s,"status":"%s"}\n' \
    "$(json_escape "$timestamp")" \
    "$(json_escape "$event_type")" \
    "$(json_escape "${STREAM_KEY:-}")" \
    "$(json_escape "${STREAM_GENERATION:-}")" \
    "$(json_escape "${PROXY_POD:-${PROXY_DNS:-}}")" \
    "$(json_escape "${WORKER_POD:-${HOSTNAME:-unknown-worker}}")" \
    "$(json_escape "${EXPERIMENT_ID:-}")" \
    "$(json_escape "${SCENARIO:-}")" \
    "$(json_escape "${RUN_ID:-}")" \
    "$duration_ms" \
    "$(json_escape "$status")"
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

log_first_progress_once() {
  if [ -f "$PROGRESS_LOG_FILE" ]; then
    return 0
  fi
  if mkdir "$PROGRESS_LOG_LOCK" 2>/dev/null; then
    if [ ! -f "$PROGRESS_LOG_FILE" ]; then
      log_json "ffmpeg_first_progress" "ok" "$(elapsed_ms "${FFMPEG_START_MS:-$RUNNER_START_MS}")"
      : > "$PROGRESS_LOG_FILE"
    fi
    rmdir "$PROGRESS_LOG_LOCK" 2>/dev/null || true
  fi
  return 0
}

notify_first_progress_once() {
  log_first_progress_once
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
    if [ ! -f "$PROGRESS_NOTIFY_ERROR_FILE" ]; then
      log_json "worker_error" "first_progress_notify_failed" "$(elapsed_ms "$RUNNER_START_MS")"
      : > "$PROGRESS_NOTIFY_ERROR_FILE"
    fi
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
    rm -f "$PID_FILE" "$PROGRESS_NOTIFY_FILE" "$PROGRESS_NOTIFY_ERROR_FILE" "$PROGRESS_LOG_FILE"
    rm -rf "$PROGRESS_NOTIFY_LOCK" "$PROGRESS_LOG_LOCK"
  fi
}
trap cleanup EXIT

RTMP_PUSH_BASE_URL="${RTMP_PUSH_BASE_URL:-}"
PROXY_ADDR="${PROXY_DNS:-}"
CONTROLLER_API="${CONTROLLER_API:-http://controller.media.svc.cluster.local:8000}"
WORKER_POD="${WORKER_POD:-${HOSTNAME:-unknown-worker}}"

if [ -z "$PROXY_ADDR" ] || [ -z "$STREAM_KEY" ] || [ -z "$RTMP_PUSH_BASE_URL" ]; then
  log_json "worker_error" "missing_required_startup_args" "$(elapsed_ms "$RUNNER_START_MS")"
  exit 1
fi

PROXY_RTMP="rtmp://${PROXY_ADDR}:1935/live/${STREAM_KEY}"
TARGET_RTMP="${RTMP_PUSH_BASE_URL}/${STREAM_KEY}"
rm -f "$PROGRESS_FILE" "$PROGRESS_NOTIFY_FILE" "$PROGRESS_NOTIFY_ERROR_FILE" "$PROGRESS_LOG_FILE"
rm -rf "$PROGRESS_NOTIFY_LOCK" "$PROGRESS_LOG_LOCK"
: > "$PROGRESS_FILE"

notify_controller "/workers/ffmpeg/started" \
  || log_json "worker_error" "ffmpeg_start_notify_failed" "$(elapsed_ms "$RUNNER_START_MS")"

FFMPEG_START_MS="$(current_epoch_ms)"
ffmpeg \
  -loglevel warning \
  -nostats \
  -rw_timeout 5000000 \
  -progress "/tmp/ffmpeg_${STREAM_KEY}.progress" \
  -i "$PROXY_RTMP" \
  -c:v copy \
  -c:a copy \
  -progress "$PROGRESS_FILE" \
  -f flv "$TARGET_RTMP" \
  >> "/tmp/ffmpeg_${STREAM_KEY}.log" 2>&1 &

FFMPEG_PID=$!
echo "$FFMPEG_PID" > "$PID_FILE"
FFMPEG_RUN_ID="${EPOCHREALTIME:-$(date +%s)}-${FFMPEG_PID}-${RANDOM}"
log_json "ffmpeg_started" "ok" "$(elapsed_ms "$FFMPEG_START_MS")"

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

if wait "$FFMPEG_PID"; then
  EXIT_CODE=0
else
  EXIT_CODE=$?
fi
echo "$EXIT_CODE" > "/tmp/ffmpeg_${STREAM_KEY}.exit"

if progress_file_has_complete_line; then
  notify_first_progress_once || true
fi

printf '%s %s\n' "$FFMPEG_RUN_ID" "$EXIT_CODE" >> "$EXIT_FILE"
rm -f "$PID_FILE"

log_json "ffmpeg_exited" "exit_${EXIT_CODE}" "$(elapsed_ms "$FFMPEG_START_MS")"

if [ "$EXIT_CODE" -ne 0 ]; then
  log_json "worker_error" "ffmpeg_exit_${EXIT_CODE}" "$(elapsed_ms "$RUNNER_START_MS")"
  tail -n 40 "/tmp/ffmpeg_${STREAM_KEY}.log" | sed 's/^/[ffmpeg] /' || true
  exit "$EXIT_CODE"
fi

exit 0
