#!/bin/bash
#
# on_worker_pull_push.sh - Chamado via kubectl exec pelo controller quando worker é alocado
# Responsabilidades:
#   1. Obter YouTube key E proxy DNS do Controller
#   2. Iniciar FFmpeg para PULL do proxy específico + PUSH para YouTube
#
# Argumentos:
#   $1 = stream name (ex: "2tww-t6fv-z2mh-0rsq-4z8t")
#
# PULL-ONLY ARCHITECTURE:
#   - Controller chama este script via kubectl exec quando aloca worker
#   - Worker faz PULL do proxy ESPECÍFICO que recebeu a stream
#   - Evita problema de load balance entre múltiplos proxies
#   - Worker faz PUSH para YouTube
#

set -e

STREAM_NAME="$1"
MANAGER_PID_FILE="/tmp/ffmpeg_manager_${STREAM_NAME}.pid"

echo "[$(date)] [worker_publish] Starting worker recovery manager for stream '$STREAM_NAME'..."

if [ -f "$MANAGER_PID_FILE" ]; then
  EXISTING_PID="$(cat "$MANAGER_PID_FILE" 2>/dev/null || true)"
  if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "[$(date)] [worker_publish] Recovery manager already running for stream '$STREAM_NAME' (PID: $EXISTING_PID). Skipping duplicate start."
    exit 0
  fi
  echo "[$(date)] [worker_publish] Found stale PID file for stream '$STREAM_NAME' (PID: ${EXISTING_PID:-unknown}). Replacing."
fi

nohup /scripts/worker_recovery_loop.sh "$STREAM_NAME" \
  > "/tmp/ffmpeg_manager_${STREAM_NAME}.log" 2>&1 &

echo "$!" > "$MANAGER_PID_FILE"
echo "[$(date)] [worker_publish] Recovery manager started (PID: $(cat "$MANAGER_PID_FILE"))"

exit 0
