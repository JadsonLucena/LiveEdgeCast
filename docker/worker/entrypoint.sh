#!/bin/bash
set -euo pipefail

if [ -n "${STREAM_KEY:-}" ] && [ -n "${PROXY_DNS:-}" ]; then
  /scripts/worker_stream_runner.sh "${STREAM_KEY}" &
fi

exec nginx -g 'daemon off;'
