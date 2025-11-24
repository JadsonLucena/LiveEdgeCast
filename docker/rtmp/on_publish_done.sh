#!/bin/bash
#
# on_publish_done.sh - Chamado quando stream termina no proxy
# Responsabilidades:
#   1. Matar FFmpeg relay (proxy → worker)
#   2. Notificar Controller para liberar worker
#
# Argumentos:
#   $1 = stream name
#

set -e

STREAM_NAME="$1"
CONTROLLER_API="http://rtmp-controller.media.svc.cluster.local:8000"
PID_FILE="/tmp/relay_${STREAM_NAME}.pid"

echo "[$(date)] [on_publish_done] Stream '$STREAM_NAME' ended - cleaning up..."

# Matar processo FFmpeg relay (proxy → worker)
if [ -f "$PID_FILE" ]; then
  FFMPEG_PID=$(cat "$PID_FILE")
  if ps -p "$FFMPEG_PID" > /dev/null 2>&1; then
    echo "[$(date)] [on_publish_done] Stopping FFmpeg relay (PID: $FFMPEG_PID)"
    kill -9 "$FFMPEG_PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  rm -f "/tmp/relay_${STREAM_NAME}.log"
fi

# Notificar controller para liberar worker
curl -sf -X POST "$CONTROLLER_API/release?stream=$STREAM_NAME" || \
  echo "[$(date)] [on_publish_done] WARNING: Failed to notify controller"

echo "[$(date)] [on_publish_done] Cleanup complete"

exit 0
