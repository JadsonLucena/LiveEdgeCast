#!/bin/bash
set -euo pipefail

if [ -z "${STREAM_KEY:-}" ] || [ -z "${PROXY_DNS:-}" ] || [ -z "${RTMP_PUSH_BASE_URL:-}" ]; then
  echo "[$(date)] [worker_entrypoint] Missing required env (STREAM_KEY/PROXY_DNS/RTMP_PUSH_BASE_URL). Exiting."
  exit 1
fi

/scripts/worker_stream_runner.sh &

exec nginx -g 'daemon off;'
