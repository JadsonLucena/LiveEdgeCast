#!/bin/bash
set -euo pipefail

STREAM_KEY="${STREAM_KEY:-}"
PID_FILE="/tmp/ffmpeg_${STREAM_KEY}.pid"
PROGRESS_FILE="/tmp/ffmpeg_${STREAM_KEY}.progress"
EXIT_EVENT_FILE="${FFMPEG_EXIT_FILE:-/tmp/ffmpeg_${STREAM_KEY}.exit_events}"
LAST_EXIT_FILE="/tmp/ffmpeg_${STREAM_KEY}.last_exit"
PROGRESS_NOTIFY_FILE="/tmp/ffmpeg_${STREAM_KEY}.progress_notified"
PROGRESS_NOTIFY_LOCK="/tmp/ffmpeg_${STREAM_KEY}.progress_notify.lock"
PROGRESS_NOTIFY_ERROR_FILE="/tmp/ffmpeg_${STREAM_KEY}.progress_notify_error_logged"
PROGRESS_LOG_FILE="/tmp/ffmpeg_${STREAM_KEY}.first_progress_logged"
PROGRESS_LOG_LOCK="/tmp/ffmpeg_${STREAM_KEY}.first_progress_log.lock"
STARTED_NOTIFY_FILE="/tmp/ffmpeg_${STREAM_KEY}.started_notified"
STARTED_NOTIFY_LOCK="/tmp/ffmpeg_${STREAM_KEY}.started_notify.lock"
STARTED_NOTIFY_ERROR_FILE="/tmp/ffmpeg_${STREAM_KEY}.started_notify_error_logged"
STARTED_LOG_FILE="/tmp/ffmpeg_${STREAM_KEY}.started_logged"
STARTED_LOG_LOCK="/tmp/ffmpeg_${STREAM_KEY}.started_log.lock"
PROGRESS_READER_PID=""
FFMPEG_PID=""
CURRENT_ATTEMPT=""
FFMPEG_RUN_ID=""
RUNNER_START_MS="$(python3 - <<'PYMS'
import time
print(time.time_ns() // 1_000_000)
PYMS
)"
FFMPEG_START_MS=""

# Opening an RTMP stream can be transiently racy in a cold-start path:
# publish event -> controller -> pod scheduling -> container start -> FFmpeg play.
# Do not fail the worker on the first input-open I/O error.
FFMPEG_INPUT_OPEN_TIMEOUT_SECONDS="${FFMPEG_INPUT_OPEN_TIMEOUT_SECONDS:-60}"
FFMPEG_INPUT_ATTEMPT_TIMEOUT_SECONDS="${FFMPEG_INPUT_ATTEMPT_TIMEOUT_SECONDS:-15}"
FFMPEG_INPUT_RETRY_INTERVAL_SECONDS="${FFMPEG_INPUT_RETRY_INTERVAL_SECONDS:-2}"
FFMPEG_PROGRESS_NOTIFY_POLL_SECONDS="${PROGRESS_NOTIFY_POLL_SECONDS:-0.2}"
FFMPEG_RW_TIMEOUT_MICROSECONDS="${FFMPEG_RW_TIMEOUT_MICROSECONDS:-30000000}"
FFMPEG_LOGLEVEL="${FFMPEG_LOGLEVEL:-warning}"
INPUT_OPEN_TIMEOUT_EXIT_CODE="${INPUT_OPEN_TIMEOUT_EXIT_CODE:-251}"
ALLOW_FFMPEG_EXIT_ZERO_WITH_UNKNOWN_STREAM_STATUS="${ALLOW_FFMPEG_EXIT_ZERO_WITH_UNKNOWN_STREAM_STATUS:-false}"

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
  printf '{"timestamp":"%s","event_type":"%s","stream":"%s","generation":"%s","proxy_pod":"%s","worker_pod":"%s","experiment_id":"%s","scenario":"%s","run_id":"%s","duration_ms":%s,"status":"%s","attempt":"%s","ffmpeg_run_id":"%s"}\n' \
    "$(json_escape "$timestamp")" \
    "$(json_escape "$event_type")" \
    "$(json_escape "${STREAM_KEY:-}")" \
    "$(json_escape "${STREAM_GENERATION:-}")" \
    "$(json_escape "${PROXY_POD:-}")" \
    "$(json_escape "${WORKER_POD:-${HOSTNAME:-unknown-worker}}")" \
    "$(json_escape "${EXPERIMENT_ID:-}")" \
    "$(json_escape "${SCENARIO:-}")" \
    "$(json_escape "${RUN_ID:-}")" \
    "$duration_ms" \
    "$(json_escape "$status")" \
    "$(json_escape "${CURRENT_ATTEMPT:-}")" \
    "$(json_escape "${FFMPEG_RUN_ID:-}")"
}

notify_controller() {
  local path="$1"
  curl -sf --connect-timeout "${CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS:-1}" --max-time "${CONTROLLER_CALLBACK_MAX_TIME_SECONDS:-2}" -X POST --get \
    --data-urlencode "stream=${STREAM_KEY}" \
    --data-urlencode "worker_pod=${WORKER_POD}" \
    "${CONTROLLER_API}${path}"
}

controller_stream_activity_state() {
  local response rc
  local curl_args=(
    -sf
    --connect-timeout "${CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS:-1}"
    --max-time "${CONTROLLER_CALLBACK_MAX_TIME_SECONDS:-2}"
    -G
    --data-urlencode "stream=${STREAM_KEY}"
  )
  if [ -n "${PROXY_POD:-}" ]; then
    curl_args+=(--data-urlencode "proxy_pod=${PROXY_POD}")
  fi
  set +e
  response="$({ curl "${curl_args[@]}" "${CONTROLLER_API}/streams/status"; } 2>/dev/null)"
  rc=$?
  set -e
  if [ "$rc" -ne 0 ] || [ -z "$response" ]; then
    printf 'unknown'
    return 0
  fi
  RESPONSE_JSON="$response" python3 - <<'PYSTATUS' 2>/dev/null || printf 'unknown'
import json, os
try:
    data = json.loads(os.environ.get("RESPONSE_JSON", "{}"))
except json.JSONDecodeError:
    print("unknown")
else:
    active = data.get("active")
    if active is True:
        print("active")
    elif active is False:
        print("inactive")
    else:
        print("unknown")
PYSTATUS
}

exit_cleanly_if_stream_inactive() {
  local state
  state="$(controller_stream_activity_state)"
  if [ "$state" = "inactive" ]; then
    log_json "worker_shutdown" "stream_already_ended" "$(elapsed_ms "$RUNNER_START_MS")"
    exit 0
  fi
}

progress_file_has_complete_line() {
  local progress_line
  [ -f "$PROGRESS_FILE" ] || return 1
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

log_ffmpeg_started_once() {
  if [ -f "$STARTED_LOG_FILE" ]; then
    return 0
  fi
  if mkdir "$STARTED_LOG_LOCK" 2>/dev/null; then
    if [ ! -f "$STARTED_LOG_FILE" ]; then
      # This means FFmpeg produced observable progress, not merely that the process was spawned.
      log_json "ffmpeg_started" "ok" "$(elapsed_ms "${FFMPEG_START_MS:-$RUNNER_START_MS}")"
      : > "$STARTED_LOG_FILE"
    fi
    rmdir "$STARTED_LOG_LOCK" 2>/dev/null || true
  fi
  return 0
}

notify_ffmpeg_started_once() {
  if [ -f "$STARTED_NOTIFY_FILE" ]; then
    return 0
  fi
  if mkdir "$STARTED_NOTIFY_LOCK" 2>/dev/null; then
    set +e
    notify_controller "/workers/ffmpeg/started"
    notify_exit=$?
    set -e
    if [ "$notify_exit" -eq 0 ]; then
      : > "$STARTED_NOTIFY_FILE"
      rmdir "$STARTED_NOTIFY_LOCK" 2>/dev/null || true
      return 0
    fi
    if [ ! -f "$STARTED_NOTIFY_ERROR_FILE" ]; then
      log_json "worker_error" "ffmpeg_start_notify_failed" "$(elapsed_ms "$RUNNER_START_MS")"
      : > "$STARTED_NOTIFY_ERROR_FILE"
    fi
    rmdir "$STARTED_NOTIFY_LOCK" 2>/dev/null || true
  fi
  return 1
}

log_first_progress_once() {
  log_ffmpeg_started_once
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
  notify_ffmpeg_started_once || true
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

stop_progress_reader() {
  if [ -n "${PROGRESS_READER_PID:-}" ]; then
    kill -TERM "$PROGRESS_READER_PID" 2>/dev/null || true
    wait "$PROGRESS_READER_PID" 2>/dev/null || true
    PROGRESS_READER_PID=""
  fi
}

cleanup() {
  stop_progress_reader
  if [ -n "${FFMPEG_PID:-}" ] && kill -0 "$FFMPEG_PID" 2>/dev/null; then
    kill -TERM "$FFMPEG_PID" 2>/dev/null || true
    wait "$FFMPEG_PID" 2>/dev/null || true
  fi
  if [ -n "${STREAM_KEY:-}" ]; then
    rm -f "$PID_FILE" "$PROGRESS_NOTIFY_FILE" "$PROGRESS_NOTIFY_ERROR_FILE" "$PROGRESS_LOG_FILE" \
      "$STARTED_NOTIFY_FILE" "$STARTED_NOTIFY_ERROR_FILE" "$STARTED_LOG_FILE"
    rm -rf "$PROGRESS_NOTIFY_LOCK" "$PROGRESS_LOG_LOCK" "$STARTED_NOTIFY_LOCK" "$STARTED_LOG_LOCK"
  fi
}
trap cleanup EXIT

wait_for_ffmpeg_exit() {
  local pid="$1"
  local exit_code
  set +e
  wait "$pid"
  exit_code=$?
  set -e
  printf '%s' "$exit_code"
}

start_progress_reader() {
  (
    progress_notified=0
    while [ "$progress_notified" -eq 0 ]; do
      if progress_file_has_complete_line && notify_first_progress_once; then
        progress_notified=1
        break
      fi
      if [ -n "${FFMPEG_PID:-}" ] && ! kill -0 "$FFMPEG_PID" 2>/dev/null; then
        # One final pass in case FFmpeg exited immediately after writing progress.
        if progress_file_has_complete_line && notify_first_progress_once; then
          progress_notified=1
        fi
        break
      fi
      sleep "$FFMPEG_PROGRESS_NOTIFY_POLL_SECONDS"
    done
  ) &
  PROGRESS_READER_PID=$!
}

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
rm -f "$PROGRESS_FILE" "$PROGRESS_NOTIFY_FILE" "$PROGRESS_NOTIFY_ERROR_FILE" "$PROGRESS_LOG_FILE" \
  "$STARTED_NOTIFY_FILE" "$STARTED_NOTIFY_ERROR_FILE" "$STARTED_LOG_FILE"
rm -rf "$PROGRESS_NOTIFY_LOCK" "$PROGRESS_LOG_LOCK" "$STARTED_NOTIFY_LOCK" "$STARTED_LOG_LOCK"
: > "$PROGRESS_FILE"

input_open_deadline=$(( $(date +%s) + FFMPEG_INPUT_OPEN_TIMEOUT_SECONDS ))
attempt=0
last_exit_code="$INPUT_OPEN_TIMEOUT_EXIT_CODE"
input_opened=0

while true; do
  attempt=$((attempt + 1))
  CURRENT_ATTEMPT="$attempt"
  now_seconds=$(date +%s)
  if [ "$now_seconds" -ge "$input_open_deadline" ]; then
    exit_cleanly_if_stream_inactive
    log_json "worker_error" "input_open_timeout" "$(elapsed_ms "$RUNNER_START_MS")"
    exit "$INPUT_OPEN_TIMEOUT_EXIT_CODE"
  fi

  exit_cleanly_if_stream_inactive

  rm -f "$PROGRESS_FILE"
  : > "$PROGRESS_FILE"
  FFMPEG_START_MS="$(current_epoch_ms)"
  FFMPEG_RUN_ID="${EPOCHREALTIME:-$(date +%s)}-attempt_${attempt}-${RANDOM}"
  ATTEMPT_LOG_FILE="/tmp/ffmpeg_${STREAM_KEY}.attempt_${attempt}.log"
  : > "$ATTEMPT_LOG_FILE"
  log_json "ffmpeg_process_spawned" "attempt_${attempt}" "$(elapsed_ms "$FFMPEG_START_MS")"

  ffmpeg \
    -loglevel "$FFMPEG_LOGLEVEL" \
    -nostats \
    -rw_timeout "$FFMPEG_RW_TIMEOUT_MICROSECONDS" \
    -progress "/tmp/ffmpeg_${STREAM_KEY}.progress" \
    -i "$PROXY_RTMP" \
    -c:v copy \
    -c:a copy \
    -progress "$PROGRESS_FILE" \
    -f flv "$TARGET_RTMP" \
    >> "$ATTEMPT_LOG_FILE" 2>&1 &

  FFMPEG_PID=$!
  echo "$FFMPEG_PID" > "$PID_FILE"
  start_progress_reader

  attempt_deadline=$(( $(date +%s) + FFMPEG_INPUT_ATTEMPT_TIMEOUT_SECONDS ))
  input_opened=0

  while true; do
    if progress_file_has_complete_line; then
      input_opened=1
      notify_first_progress_once || true
      break
    fi

    if ! kill -0 "$FFMPEG_PID" 2>/dev/null; then
      break
    fi

    if [ "$(date +%s)" -ge "$attempt_deadline" ]; then
      log_json "ffmpeg_input_open_attempt_timeout" "attempt_${attempt}" "$(elapsed_ms "$FFMPEG_START_MS")"
      kill -TERM "$FFMPEG_PID" 2>/dev/null || true
      sleep 1
      if kill -0 "$FFMPEG_PID" 2>/dev/null; then
        kill -KILL "$FFMPEG_PID" 2>/dev/null || true
      fi
      break
    fi

    sleep "$FFMPEG_PROGRESS_NOTIFY_POLL_SECONDS"
  done

  EXIT_CODE="$(wait_for_ffmpeg_exit "$FFMPEG_PID")"
  last_exit_code="$EXIT_CODE"
  echo "$EXIT_CODE" > "$LAST_EXIT_FILE"
  printf '%s %s\n' "$FFMPEG_RUN_ID" "$EXIT_CODE" >> "$EXIT_EVENT_FILE"
  cat "$ATTEMPT_LOG_FILE" >> "/tmp/ffmpeg_${STREAM_KEY}.log" 2>/dev/null || true
  rm -f "$PID_FILE"

  if progress_file_has_complete_line; then
    input_opened=1
    notify_first_progress_once || true
  fi
  stop_progress_reader

  log_json "ffmpeg_exited" "exit_${EXIT_CODE}" "$(elapsed_ms "$FFMPEG_START_MS")"

  if [ "$input_opened" -eq 1 ]; then
    if [ "$EXIT_CODE" -ne 0 ]; then
      log_json "worker_error" "ffmpeg_exit_${EXIT_CODE}" "$(elapsed_ms "$RUNNER_START_MS")"
      tail -n 40 "/tmp/ffmpeg_${STREAM_KEY}.log" | sed 's/^/[ffmpeg] /' || true
      exit "$EXIT_CODE"
    fi

    stream_state="$(controller_stream_activity_state)"
    if [ "$stream_state" = "inactive" ]; then
      log_json "ffmpeg_completed_after_stream_end" "ok" "$(elapsed_ms "$RUNNER_START_MS")"
      exit 0
    fi
    if [ "$stream_state" = "active" ]; then
      log_json "worker_error" "ffmpeg_exit_0_while_stream_still_active" "$(elapsed_ms "$RUNNER_START_MS")"
      exit "$INPUT_OPEN_TIMEOUT_EXIT_CODE"
    fi
    if [ "${ALLOW_FFMPEG_EXIT_ZERO_WITH_UNKNOWN_STREAM_STATUS,,}" = "true" ]; then
      log_json "ffmpeg_completed_with_unknown_stream_status" "ok_allowed" "$(elapsed_ms "$RUNNER_START_MS")"
      exit 0
    fi
    log_json "worker_error" "ffmpeg_exit_0_while_stream_status_unknown" "$(elapsed_ms "$RUNNER_START_MS")"
    exit "$INPUT_OPEN_TIMEOUT_EXIT_CODE"
  fi

  # No observable input/progress was opened. This is usually a transient race while
  # the proxy accepts publish and the worker cold-starts. Retry until the overall deadline.
  if [ "$EXIT_CODE" -eq 0 ]; then
    exit_cleanly_if_stream_inactive
    log_json "ffmpeg_exited_without_progress" "exit_0_attempt_${attempt}" "$(elapsed_ms "$RUNNER_START_MS")"
  else
    log_json "ffmpeg_input_open_attempt_failed" "exit_${EXIT_CODE}_attempt_${attempt}" "$(elapsed_ms "$RUNNER_START_MS")"
  fi
  tail -n 20 "$ATTEMPT_LOG_FILE" | sed 's/^/[ffmpeg] /' || true

  now_seconds=$(date +%s)
  if [ "$now_seconds" -ge "$input_open_deadline" ]; then
    exit_cleanly_if_stream_inactive
    log_json "worker_error" "input_open_timeout_last_exit_${last_exit_code}" "$(elapsed_ms "$RUNNER_START_MS")"
    exit "$INPUT_OPEN_TIMEOUT_EXIT_CODE"
  fi

  sleep "$FFMPEG_INPUT_RETRY_INTERVAL_SECONDS"
done
