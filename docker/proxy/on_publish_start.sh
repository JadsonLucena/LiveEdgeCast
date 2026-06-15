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
mkdir -p "$SESSION_DIR"

sanitize_file_key() {
  jq -rn --arg value "${1:-unknown}" '
    ($value | gsub("[^a-zA-Z0-9_.:-]+"; "_") | .[:160]) as $sanitized
    | if $sanitized == "" then "unknown" else $sanitized end
  '
}

stream_session_file() {
  printf '%s/%s.session' "$SESSION_DIR" "$(sanitize_file_key "$STREAM_NAME")"
}

generate_session_id() {
  local raw hash
  raw="${PROXY_POD}|${STREAM_NAME}|${PUBLISH_START_TS}|$$|${RANDOM:-0}"
  if command -v sha256sum >/dev/null 2>&1; then
    hash="$(printf '%s' "$raw" | sha256sum | awk '{print $1}')"
  else
    hash="$(printf '%s' "$raw" | cksum | awk '{print $1}')"
  fi
  printf '%s' "$hash"
}

persist_session() {
  local file
  file="$(stream_session_file)"
  {
    printf 'session_id=%s\n' "$SESSION_ID"
    printf 'publish_start_ts=%s\n' "$PUBLISH_START_TS"
    printf 'proxy_pod=%s\n' "$PROXY_POD"
  } > "${file}.tmp"
  mv "${file}.tmp" "$file"
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

high_resolution_epoch_seconds() {
  local epoch
  epoch="$(date +%s.%N 2>/dev/null || true)"
  if [[ "$epoch" =~ ^[0-9]+\.[0-9]+$ && "$epoch" != *N* ]]; then
    printf '%s' "$epoch"
    return
  fi
  date +%s
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

notify_stream_started() {
  curl -sfo /dev/null --connect-timeout "${CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS:-1}" --max-time "${CONTROLLER_CALLBACK_MAX_TIME_SECONDS:-2}" -X POST --get \
    -H "X-LiveEdgeCast-Experiment-Id: ${EXPERIMENT_ID}" \
    -H "X-LiveEdgeCast-Scenario: ${SCENARIO}" \
    -H "X-LiveEdgeCast-Run-Id: ${RUN_ID}" \
    --data-urlencode "stream=${STREAM_NAME}" \
    --data-urlencode "proxy_pod=${PROXY_POD}" \
    --data-urlencode "t_publish_start_proxy=${PUBLISH_START_TS}" \
    --data-urlencode "session_id=${SESSION_ID}" \
    --data-urlencode "experiment_id=${EXPERIMENT_ID}" \
    --data-urlencode "scenario=${SCENARIO}" \
    --data-urlencode "run_id=${RUN_ID}" \
    "${CONTROLLER_API}/streams/started"
}

PUBLISH_START_TS="$(high_resolution_epoch_seconds)"
SESSION_ID="${LIVEEDGECAST_SESSION_ID:-$(generate_session_id)}"
persist_session
log_event "proxy_publish_started" "received"

if notify_stream_started; then
  log_event "proxy_publish_start_notified" "success"
else
  log_event "proxy_publish_start_notify_failed" "failed"
fi

exit 0
