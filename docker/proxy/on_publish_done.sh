#!/bin/bash

set -e

STREAM_NAME="$1"
CONTROLLER_API="${CONTROLLER_API:-http://controller.media.svc.cluster.local:8000}"
PROXY_POD="${PROXY_POD:-$(hostname)}"

sanitize_context_value() {
  jq -rn --arg value "${1:-unknown}" '
    ($value | gsub("^\\s+|\\s+$"; "") | gsub("[^a-zA-Z0-9_.:-]+"; "_") | .[:64]) as $sanitized
    | if $sanitized == "" then "unknown" else $sanitized end
  '
}

EXPERIMENT_ID="$(sanitize_context_value "${EXPERIMENT_ID:-unknown}")"
SCENARIO="$(sanitize_context_value "${SCENARIO:-unknown}")"
RUN_ID="$(sanitize_context_value "${RUN_ID:-unknown}")"

SESSION_DIR="${LIVEEDGECAST_SESSION_DIR:-/tmp/liveedgecast-sessions}"

sanitize_file_key() {
  jq -rn --arg value "${1:-unknown}" '
    ($value | gsub("[^a-zA-Z0-9_.:-]+"; "_") | .[:160]) as $sanitized
    | if $sanitized == "" then "unknown" else $sanitized end
  '
}

stream_session_file() {
  printf '%s/%s.session' "$SESSION_DIR" "$(sanitize_file_key "$STREAM_NAME")"
}

load_session() {
  local file line key value
  file="$(stream_session_file)"
  SESSION_ID=""
  PUBLISH_START_TS=""
  if [ -f "$file" ]; then
    while IFS='=' read -r key value; do
      case "$key" in
        session_id) SESSION_ID="$value" ;;
        publish_start_ts) PUBLISH_START_TS="$value" ;;
      esac
    done < "$file"
  fi
}

cleanup_session() {
  rm -f "$(stream_session_file)" 2>/dev/null || true
}

utc_timestamp() {
  local timestamp
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ 2>/dev/null || true)"
  if [[ -n "$timestamp" && "$timestamp" != *N* ]]; then
    printf '%s' "$timestamp"
    return
  fi
  date -u +%Y-%m-%dT%H:%M:%SZ
}

log_event() {
  local event_type="$1"
  local status="$2"
  # stream is intentionally log-only observability context; never use it as a Prometheus label.
  jq -cn \
    --arg timestamp "$(utc_timestamp)" \
    --arg event_type "$event_type" \
    --arg stream "$STREAM_NAME" \
    --arg proxy_pod "$PROXY_POD" \
    --arg experiment_id "$EXPERIMENT_ID" \
    --arg scenario "$SCENARIO" \
    --arg run_id "$RUN_ID" \
    --arg status "$status" \
    '{timestamp:$timestamp,event_type:$event_type,stream:$stream,proxy_pod:$proxy_pod,experiment_id:$experiment_id,scenario:$scenario,run_id:$run_id,status:$status}'
}

notify_stream_ended() {
  curl -sfo /dev/null --connect-timeout "${CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS:-1}" --max-time "${CONTROLLER_CALLBACK_MAX_TIME_SECONDS:-2}" -X POST --get \
    -H "X-LiveEdgeCast-Experiment-Id: ${EXPERIMENT_ID}" \
    -H "X-LiveEdgeCast-Scenario: ${SCENARIO}" \
    -H "X-LiveEdgeCast-Run-Id: ${RUN_ID}" \
    --data-urlencode "stream=${STREAM_NAME}" \
    --data-urlencode "proxy_pod=${PROXY_POD}" \
    --data-urlencode "session_id=${SESSION_ID}" \
    --data-urlencode "t_publish_start_proxy=${PUBLISH_START_TS}" \
    --data-urlencode "experiment_id=${EXPERIMENT_ID}" \
    --data-urlencode "scenario=${SCENARIO}" \
    --data-urlencode "run_id=${RUN_ID}" \
    "${CONTROLLER_API}/streams/ended"
}

load_session
log_event "proxy_publish_ended" "received"

if notify_stream_ended; then
  log_event "proxy_publish_done_notified" "success"
  cleanup_session
else
  log_event "proxy_publish_done_notify_failed" "failed"
fi

exit 0
