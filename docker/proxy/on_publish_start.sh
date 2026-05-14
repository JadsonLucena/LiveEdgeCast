#!/bin/bash

set -e

STREAM_NAME="$1"
CONTROLLER_API="http://controller.media.svc.cluster.local:8000"
PROXY_POD=$(hostname)

echo "[$(date)] [on_publish_start] Stream '$STREAM_NAME' started on proxy '$PROXY_POD'"

curl -sf -X POST "$CONTROLLER_API/streams/started?stream=$STREAM_NAME&proxy_pod=$PROXY_POD" || \
  echo "[$(date)] [on_publish_start] WARNING: Failed to notify controller stream start"

echo "[$(date)] [on_publish_start] Controller notified"
exit 0
