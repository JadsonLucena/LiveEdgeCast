#!/bin/bash
#
# on_publish_start.sh - Chamado quando uma stream é publicada no proxy
# Responsabilidades:
#   1. Notificar Controller para alocar worker
#   2. Passar proxy_pod para worker fazer pull correto (Pull-Only)
#
# Argumentos:
#   $1 = stream name (ex: "2tww-t6fv-z2mh-0rsq-4z8t")
#
# PULL-ONLY ARCHITECTURE:
#   - Proxy apenas aceita stream e notifica controller
#   - Controller aloca worker e informa qual proxy usar
#   - Worker faz PULL do proxy específico + PUSH para YouTube
#   - Sem FFmpeg relay no proxy (mais eficiente)
#

set -e

STREAM_NAME="$1"
CONTROLLER_API="http://rtmp-controller.media.svc.cluster.local:8000"
MAX_RETRIES=10
RETRY_COUNT=0
HEARTBEAT_PID_FILE="/tmp/stream_hb_${STREAM_NAME}.pid"
HEARTBEAT_INTERVAL=5
HEARTBEAT_LOG_FILE="/tmp/stream_heartbeat_${STREAM_NAME}.log"

# Obter nome do pod do proxy (para Pull-Only)
PROXY_POD=$(hostname)

echo "[$(date)] [on_publish_start] Stream '$STREAM_NAME' published on proxy '$PROXY_POD' - notifying controller..."

# Registrar stream no controller com TTL curto
curl -sf -X POST "$CONTROLLER_API/streams/register?stream=$STREAM_NAME&proxy_pod=$PROXY_POD" >/dev/null

# Iniciar heartbeat periódico em background enquanto stream estiver ativa
nohup bash -c "
  while true; do
    curl -sf -X POST '$CONTROLLER_API/streams/heartbeat?stream=$STREAM_NAME&proxy_pod=$PROXY_POD' >/dev/null || true
    sleep $HEARTBEAT_INTERVAL
  done
" >/tmp/stream_heartbeat_${STREAM_NAME}.log 2>&1 &
echo $! > "$HEARTBEAT_PID_FILE"

cleanup_heartbeat_on_error() {
  if [ -f "$HEARTBEAT_PID_FILE" ]; then
    HB_PID=$(cat "$HEARTBEAT_PID_FILE")
    if ps -p "$HB_PID" >/dev/null 2>&1; then
      kill "$HB_PID" 2>/dev/null || true
    fi
    rm -f "$HEARTBEAT_PID_FILE" "$HEARTBEAT_LOG_FILE"
  fi
}

trap cleanup_heartbeat_on_error ERR

# Chamar API do controller para alocar worker
# Passar proxy_pod para worker fazer pull do proxy correto
RESPONSE=$(curl -sf "$CONTROLLER_API/allocate?stream=$STREAM_NAME&proxy_pod=$PROXY_POD")
WORKER_POD=$(echo "$RESPONSE" | jq -r '.name // empty')
WORKER_DNS=$(echo "$RESPONSE" | jq -r '.pod // empty')

# Aguardar worker ficar ready (com timeout)
while [ -z "$WORKER_POD" ] || [ "$WORKER_POD" = "null" ]; do
  RETRY_COUNT=$((RETRY_COUNT + 1))
  
  if [ $RETRY_COUNT -gt $MAX_RETRIES ]; then
    echo "[$(date)] [on_publish_start] ERROR: Timeout waiting for worker allocation (${MAX_RETRIES}s)"
    exit 1
  fi
  
  echo "[$(date)] [on_publish_start] Waiting for worker... ($RETRY_COUNT/$MAX_RETRIES)"
  sleep 1
  RESPONSE=$(curl -sf "$CONTROLLER_API/allocate?stream=$STREAM_NAME&proxy_pod=$PROXY_POD")
  WORKER_POD=$(echo "$RESPONSE" | jq -r '.name // empty')
  WORKER_DNS=$(echo "$RESPONSE" | jq -r '.pod // empty')
done

echo "[$(date)] [on_publish_start] Worker allocated: $WORKER_POD (DNS: $WORKER_DNS)"
echo "[$(date)] [on_publish_start] Notifying worker to start pull+push..."

# Notificar worker para iniciar pull via HTTP API do controller
curl -sf "$CONTROLLER_API/start-worker?stream=$STREAM_NAME&worker=$WORKER_POD" || true

echo "[$(date)] [on_publish_start] Worker '$WORKER_POD' will PULL from proxy '$PROXY_POD' and PUSH to YouTube"
echo "[$(date)] [on_publish_start] Pull-Only Architecture - No relay, no trigger publish"

trap - ERR

exit 0
