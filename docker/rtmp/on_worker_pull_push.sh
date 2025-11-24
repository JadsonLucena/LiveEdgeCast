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
CONTROLLER_API="http://rtmp-controller.media.svc.cluster.local:8000"
MAX_RETRIES=10
RETRY_COUNT=0
PID_FILE="/tmp/ffmpeg_${STREAM_NAME}.pid"

echo "[$(date)] [worker_publish] Worker ready - getting stream info from controller..."

# Obter YouTube key E proxy DNS do Controller
YOUTUBE_KEY=""
PROXY_DNS=""
while [ -z "$YOUTUBE_KEY" ] || [ "$YOUTUBE_KEY" = "null" ] || [ -z "$PROXY_DNS" ] || [ "$PROXY_DNS" = "null" ]; do
  RETRY_COUNT=$((RETRY_COUNT + 1))
  
  if [ $RETRY_COUNT -gt $MAX_RETRIES ]; then
    echo "[$(date)] [worker_publish] ERROR: Failed to get stream info after ${MAX_RETRIES} attempts"
    exit 1
  fi
  
  STREAM_INFO=$(curl -sf "$CONTROLLER_API/stream-key?stream=$STREAM_NAME")
  YOUTUBE_KEY=$(echo "$STREAM_INFO" | jq -r '.youtubeKey // empty')
  PROXY_DNS=$(echo "$STREAM_INFO" | jq -r '.proxyDns // empty')
  
  if [ -z "$YOUTUBE_KEY" ] || [ "$YOUTUBE_KEY" = "null" ] || [ -z "$PROXY_DNS" ] || [ "$PROXY_DNS" = "null" ]; then
    echo "[$(date)] [worker_publish] Waiting for stream info... ($RETRY_COUNT/$MAX_RETRIES)"
    sleep 1
  fi
done

PROXY_RTMP="rtmp://${PROXY_DNS}:1935"

echo "[$(date)] [worker_publish] YouTube key: $YOUTUBE_KEY"
echo "[$(date)] [worker_publish] Proxy DNS: $PROXY_DNS"
echo "[$(date)] [worker_publish] Starting FFmpeg PULL: $PROXY_RTMP/live/$STREAM_NAME → YouTube"

# Verificar resolução DNS antes de iniciar FFmpeg
if ! nslookup a.rtmp.youtube.com > /dev/null 2>&1; then
  echo "[$(date)] [worker_publish] WARNING: Cannot resolve a.rtmp.youtube.com"
  echo "[$(date)] [worker_publish] Trying with Google DNS (8.8.8.8)..."
fi

# Iniciar FFmpeg em background para fazer pull+push
# Input:  Proxy RTMP server (pull)
# Output: YouTube RTMP (push)
nohup ffmpeg \
  -loglevel verbose \
  -i "$PROXY_RTMP/live/$STREAM_NAME" \
  -c:v copy \
  -c:a copy \
  -f flv "rtmp://a.rtmp.youtube.com/live2/$YOUTUBE_KEY" \
  > "/tmp/ffmpeg_${STREAM_NAME}.log" 2>&1 &

FFMPEG_PID=$!
echo "$FFMPEG_PID" > "$PID_FILE"

echo "[$(date)] [worker_publish] FFmpeg started (PID: $FFMPEG_PID)"
echo "[$(date)] [worker_publish] Stream '$STREAM_NAME' now streaming to YouTube"

exit 0
