#!/bin/bash

set -e

STREAM_NAME="$1"
CONTROLLER_API="${CONTROLLER_API:-http://controller.media.svc.cluster.local:8000}"
PROXY_POD="${PROXY_POD:-$(hostname)}"
EXPERIMENT_ID="${EXPERIMENT_ID:-unknown}"
SCENARIO="${SCENARIO:-unknown}"
RUN_ID="${RUN_ID:-unknown}"

json_escape() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
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
  printf '{"timestamp":"%s","event_type":"%s","stream":"%s","proxy_pod":"%s","experiment_id":"%s","scenario":"%s","run_id":"%s","status":"%s"}\n' \
    "$(utc_timestamp)" \
    "$(json_escape "$event_type")" \
    "$(json_escape "$STREAM_NAME")" \
    "$(json_escape "$PROXY_POD")" \
    "$(json_escape "$EXPERIMENT_ID")" \
    "$(json_escape "$SCENARIO")" \
    "$(json_escape "$RUN_ID")" \
    "$(json_escape "$status")"
}

notify_stream_ended() {
  curl -sf --connect-timeout "${CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS:-1}" --max-time "${CONTROLLER_CALLBACK_MAX_TIME_SECONDS:-2}" -X POST --get \
    -H "X-LiveEdgeCast-Experiment-Id: ${EXPERIMENT_ID}" \
    -H "X-LiveEdgeCast-Scenario: ${SCENARIO}" \
    -H "X-LiveEdgeCast-Run-Id: ${RUN_ID}" \
    --data-urlencode "stream=${STREAM_NAME}" \
    --data-urlencode "proxy_pod=${PROXY_POD}" \
    --data-urlencode "experiment_id=${EXPERIMENT_ID}" \
    --data-urlencode "scenario=${SCENARIO}" \
    --data-urlencode "run_id=${RUN_ID}" \
    "${CONTROLLER_API}/streams/ended"
}

log_event "proxy_publish_ended" "received"

if notify_stream_ended; then
  log_event "proxy_publish_done_notified" "success"
else
  log_event "proxy_publish_done_notify_failed" "failed"
fi

exit 0
