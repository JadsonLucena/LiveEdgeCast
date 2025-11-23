#!/bin/bash
#
# on_publish_start.sh - Chamado quando uma stream é publicada no proxy
# Responsabilidades:
#   1. Alocar um worker via Controller API
#   2. Iniciar processo ffmpeg dedicado para re-transmitir essa stream para o worker
#   3. Armazenar PID do ffmpeg para cleanup posterior
#
# Argumentos:
#   $1 = stream name (ex: "mystream")
#

set -e

STREAM_NAME="$1"
CONTROLLER_API="http://rtmp-controller.media.svc.cluster.local:8000"
MAX_RETRIES=60
RETRY_COUNT=0
PID_DIR="/tmp/ffmpeg_pids"

# Criar diretório para PIDs se não existir
mkdir -p "$PID_DIR"

echo "[$(date)] [on_publish_start] Stream '$STREAM_NAME' started - allocating worker..."

# Chamar API do controller para alocar worker
WORKER_DNS=$(curl -sf "$CONTROLLER_API/allocate?stream=$STREAM_NAME" | jq -r '.pod // empty')

# Aguardar worker ficar ready (com timeout)
while [ -z "$WORKER_DNS" ] || [ "$WORKER_DNS" = "null" ]; do
  RETRY_COUNT=$((RETRY_COUNT + 1))
  
  if [ $RETRY_COUNT -gt $MAX_RETRIES ]; then
    echo "[$(date)] [on_publish_start] ERROR: Timeout waiting for worker allocation (${MAX_RETRIES}s)"
    exit 1
  fi
  
  echo "[$(date)] [on_publish_start] Waiting for worker... ($RETRY_COUNT/$MAX_RETRIES)"
  sleep 1
  WORKER_DNS=$(curl -sf "$CONTROLLER_API/allocate?stream=$STREAM_NAME" | jq -r '.pod // empty')
done

echo "[$(date)] [on_publish_start] Worker allocated: $WORKER_DNS"

# Iniciar ffmpeg para re-transmitir stream do proxy local para o worker remoto
# Input:  rtmp://127.0.0.1:1935/live/$STREAM_NAME (loopback - mesmo container)
# Output: rtmp://$WORKER_DNS:1935/live/$STREAM_NAME (worker dedicado)
nohup ffmpeg \
  -i "rtmp://127.0.0.1:1935/live/$STREAM_NAME" \
  -c:v copy -c:a copy \
  -f flv "rtmp://$WORKER_DNS:1935/live/$STREAM_NAME" \
  > "/tmp/ffmpeg_${STREAM_NAME}.log" 2>&1 &

FFMPEG_PID=$!

# Salvar PID do ffmpeg para cleanup no on_publish_done
echo "$FFMPEG_PID" > "$PID_DIR/${STREAM_NAME}.pid"

echo "[$(date)] [on_publish_start] FFmpeg started for stream '$STREAM_NAME' (PID: $FFMPEG_PID)"
echo "[$(date)] [on_publish_start] Routing: rtmp://127.0.0.1/live/$STREAM_NAME -> rtmp://$WORKER_DNS/live/$STREAM_NAME"

exit 0
