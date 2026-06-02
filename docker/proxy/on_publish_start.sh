#!/bin/bash

set -e

STREAM_NAME="$1"
CONTROLLER_API="${CONTROLLER_API:-http://controller.media.svc.cluster.local:8000}"
PROXY_POD=$(hostname)

notify_stream_started() {
  curl -sf --connect-timeout "${CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS:-1}" --max-time "${CONTROLLER_CALLBACK_MAX_TIME_SECONDS:-2}" -X POST --get \
    --data-urlencode "stream=${STREAM_NAME}" \
    --data-urlencode "proxy_pod=${PROXY_POD}" \
    --data-urlencode "t_publish_start_proxy=${PUBLISH_START_TS}" \
    "${CONTROLLER_API}/streams/started"
}

echo "[$(date)] [on_publish_start] Stream '$STREAM_NAME' started on proxy '$PROXY_POD'"

PUBLISH_START_TS="$(date +%s.%N)"
notify_stream_started || \
  echo "[$(date)] [on_publish_start] WARNING: Failed to notify controller stream start"

echo "[$(date)] [on_publish_start] Controller notified"
exit 0
