#!/bin/bash
#
# on_worker_publish.sh - Chamado quando uma stream é publicada no worker
# Responsabilidades:
#   1. Obter chave do YouTube para a stream via Controller API
#   2. Gerar arquivo de configuração dinâmica com push para YouTube
#   3. Recarregar NGINX para aplicar configuração
#
# Argumentos:
#   $1 = stream name (ex: "mystream")
#

set -e

STREAM_NAME="$1"
CONTROLLER_API="http://rtmp-controller.media.svc.cluster.local:8000"
PUSH_CONFIG_FILE="/tmp/worker_push_${STREAM_NAME}.conf"
MAX_RETRIES=10
RETRY_COUNT=0

echo "[$(date)] [on_worker_publish] Worker received publish for stream '$STREAM_NAME'"

# Obter chave do YouTube para essa stream via Controller
echo "[$(date)] [on_worker_publish] Requesting YouTube key from controller..."

YOUTUBE_KEY=""
while [ -z "$YOUTUBE_KEY" ] || [ "$YOUTUBE_KEY" = "null" ]; do
  RETRY_COUNT=$((RETRY_COUNT + 1))
  
  if [ $RETRY_COUNT -gt $MAX_RETRIES ]; then
    echo "[$(date)] [on_worker_publish] ERROR: Failed to get YouTube key after ${MAX_RETRIES} attempts"
    exit 1
  fi
  
  YOUTUBE_KEY=$(curl -sf "$CONTROLLER_API/stream-key?stream=$STREAM_NAME" | jq -r '.youtubeKey // empty')
  
  if [ -z "$YOUTUBE_KEY" ] || [ "$YOUTUBE_KEY" = "null" ]; then
    echo "[$(date)] [on_worker_publish] Waiting for YouTube key... ($RETRY_COUNT/$MAX_RETRIES)"
    sleep 1
  fi
done

echo "[$(date)] [on_worker_publish] YouTube key obtained: $YOUTUBE_KEY"

# Gerar arquivo de configuração dinâmica para push
cat > "$PUSH_CONFIG_FILE" << EOF
# Auto-generated push configuration for stream: $STREAM_NAME
# Generated at: $(date)
push rtmp://a.rtmp.youtube.com/live2/$YOUTUBE_KEY;
EOF

echo "[$(date)] [on_worker_publish] Push configuration created: $PUSH_CONFIG_FILE"

# Recarregar NGINX para aplicar a nova configuração
nginx -s reload

echo "[$(date)] [on_worker_publish] NGINX reloaded successfully"
echo "[$(date)] [on_worker_publish] Stream '$STREAM_NAME' is now pushing to YouTube with key: $YOUTUBE_KEY"

exit 0
