#!/bin/bash
set -euo pipefail

ENTRYPOINT_START_MS="$(python3 - <<'PY'
import time
print(time.time_ns() // 1_000_000)
PY
)"
SHUTDOWN_LOGGED=0

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
  python3 - <<'PY'
import time
print(time.time_ns() // 1_000_000)
PY
}

elapsed_ms() {
  local start_ms="${1:-$ENTRYPOINT_START_MS}"
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
  local duration_ms="${3:-$(elapsed_ms "$ENTRYPOINT_START_MS")}"
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

log_shutdown_once() {
  local status="${1:-ok}"
  if [ "$SHUTDOWN_LOGGED" -eq 0 ]; then
    SHUTDOWN_LOGGED=1
    log_json "worker_shutdown" "$status" "$(elapsed_ms "$ENTRYPOINT_START_MS")"
  fi
}

handle_signal() {
  local signal="$1"
  log_shutdown_once "signal_${signal}"
  kill -TERM "${RUNNER_PID:-}" "${METRICS_EXPORTER_PID:-}" "${NGINX_PID:-}" 2>/dev/null || true
  wait "${RUNNER_PID:-}" "${METRICS_EXPORTER_PID:-}" "${NGINX_PID:-}" 2>/dev/null || true
  exit 143
}

trap 'handle_signal TERM' TERM
trap 'handle_signal INT' INT
trap 'handle_signal QUIT' QUIT

log_json "worker_entrypoint_started" "ok" 0

/scripts/metrics_exporter.py &
METRICS_EXPORTER_PID=$!

/scripts/worker_stream_runner.sh &
RUNNER_PID=$!

nginx -g 'daemon off;' &
NGINX_PID=$!

set +e
wait -n -p EXITED_PID "$RUNNER_PID" "$METRICS_EXPORTER_PID" "$NGINX_PID"
FIRST_EXIT=$?
set -e

if [ "$EXITED_PID" = "$METRICS_EXPORTER_PID" ]; then
  log_json "worker_error" "metrics_exporter_exit_${FIRST_EXIT}" "$(elapsed_ms "$ENTRYPOINT_START_MS")"
  kill -TERM "$RUNNER_PID" "$NGINX_PID" 2>/dev/null || true
  wait "$RUNNER_PID" "$NGINX_PID" 2>/dev/null || true
  log_shutdown_once "error"
  exit "$FIRST_EXIT"
fi

if [ "$EXITED_PID" = "$NGINX_PID" ]; then
  log_json "worker_error" "nginx_exit_${FIRST_EXIT}" "$(elapsed_ms "$ENTRYPOINT_START_MS")"
  kill -TERM "$RUNNER_PID" "$METRICS_EXPORTER_PID" 2>/dev/null || true
  wait "$RUNNER_PID" "$METRICS_EXPORTER_PID" 2>/dev/null || true
  log_shutdown_once "error"
  exit "$FIRST_EXIT"
fi

RUNNER_EXIT=$FIRST_EXIT
if [ "$RUNNER_EXIT" -ne 0 ]; then
  log_json "worker_error" "runner_exit_${RUNNER_EXIT}" "$(elapsed_ms "$ENTRYPOINT_START_MS")"
  kill -TERM "$NGINX_PID" "$METRICS_EXPORTER_PID" 2>/dev/null || true
  wait "$NGINX_PID" "$METRICS_EXPORTER_PID" 2>/dev/null || true
  log_shutdown_once "error"
  exit "$RUNNER_EXIT"
fi

set +e
wait -n -p EXITED_PID "$NGINX_PID" "$METRICS_EXPORTER_PID"
FINAL_EXIT=$?
set -e

if [ "$EXITED_PID" = "$METRICS_EXPORTER_PID" ]; then
  log_json "worker_error" "metrics_exporter_exit_${FINAL_EXIT}" "$(elapsed_ms "$ENTRYPOINT_START_MS")"
  kill -TERM "$NGINX_PID" 2>/dev/null || true
  wait "$NGINX_PID" 2>/dev/null || true
  log_shutdown_once "error"
  exit "$FINAL_EXIT"
fi

kill -TERM "$METRICS_EXPORTER_PID" 2>/dev/null || true
wait "$METRICS_EXPORTER_PID" 2>/dev/null || true
if [ "$FINAL_EXIT" -ne 0 ]; then
  log_json "worker_error" "nginx_exit_${FINAL_EXIT}" "$(elapsed_ms "$ENTRYPOINT_START_MS")"
  log_shutdown_once "error"
else
  log_shutdown_once "nginx_exit_${FINAL_EXIT}"
fi
exit "$FINAL_EXIT"
