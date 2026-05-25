#!/bin/bash

set -e

STREAM_NAME="$1"
CONTROLLER_API="http://controller.media.svc.cluster.local:8000"
PROXY_POD=$(hostname)

echo "[$(date)] [on_publish_done] Stream '$STREAM_NAME' ended on proxy '$PROXY_POD'"

curl -sf -X POST "$CONTROLLER_API/streams/ended?stream=$STREAM_NAME&proxy_pod=$PROXY_POD" || \
  echo "[$(date)] [on_publish_done] WARNING: Failed to notify controller stream end"

echo "[$(date)] [on_publish_done] Controller notified"
exit 0
