#!/bin/bash
set -euo pipefail

if [ -n "${STREAM_KEY:-}" ] && [ -n "${PROXY_DNS:-}" ]; then
  /scripts/worker_stream_runner.sh "${STREAM_KEY}" &
elif [ -n "${STREAM_KEY:-}" ] || [ -n "${PROXY_DNS:-}" ]; then
  echo "[$(date)] [worker_entrypoint] Partial startup env detected. Waiting for controller-triggered start."
else
  echo "[$(date)] [worker_entrypoint] No stream env at boot. Running idle; controller will trigger worker start via kubectl exec."
fi

exec nginx -g 'daemon off;'
