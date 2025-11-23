#!/bin/bash
#
# on_publish_done.sh - Chamado quando uma stream termina no proxy
# Responsabilidades:
#   1. Matar processo ffmpeg dedicado dessa stream
#   2. Liberar worker via Controller API (/release)
#   3. Limpar arquivos temporários (PIDs, logs)
#
# Argumentos:
#   $1 = stream name (ex: "mystream")
#

set -e

STREAM_NAME="$1"
CONTROLLER_API="http://rtmp-controller.media.svc.cluster.local:8000"
PID_DIR="/tmp/ffmpeg_pids"
PID_FILE="$PID_DIR/${STREAM_NAME}.pid"

echo "[$(date)] [on_publish_done] Stream '$STREAM_NAME' ended - cleaning up..."

# Verificar se existe PID file
if [ ! -f "$PID_FILE" ]; then
  echo "[$(date)] [on_publish_done] WARNING: PID file not found for stream '$STREAM_NAME'"
  exit 0
fi

# Ler PID do ffmpeg
FFMPEG_PID=$(cat "$PID_FILE")

# Matar processo ffmpeg se ainda estiver rodando
if kill -0 "$FFMPEG_PID" 2>/dev/null; then
  echo "[$(date)] [on_publish_done] Killing ffmpeg process (PID: $FFMPEG_PID)"
  kill -TERM "$FFMPEG_PID" 2>/dev/null || true
  
  # Aguardar até 5 segundos para término gracioso
  for i in {1..5}; do
    if ! kill -0 "$FFMPEG_PID" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  
  # Force kill se ainda estiver vivo
  if kill -0 "$FFMPEG_PID" 2>/dev/null; then
    echo "[$(date)] [on_publish_done] Force killing ffmpeg (PID: $FFMPEG_PID)"
    kill -KILL "$FFMPEG_PID" 2>/dev/null || true
  fi
  
  echo "[$(date)] [on_publish_done] FFmpeg process terminated"
else
  echo "[$(date)] [on_publish_done] FFmpeg process already terminated (PID: $FFMPEG_PID)"
fi

# Notificar controller para liberar worker
# O controller rastreia via stream_name, não precisa do worker_name aqui
curl -sf -X POST "$CONTROLLER_API/release?stream=$STREAM_NAME" || \
  echo "[$(date)] [on_publish_done] WARNING: Failed to notify controller about stream release"

# Limpar arquivos temporários
rm -f "$PID_FILE"
rm -f "/tmp/ffmpeg_${STREAM_NAME}.log"

echo "[$(date)] [on_publish_done] Cleanup completed for stream '$STREAM_NAME'"

exit 0
