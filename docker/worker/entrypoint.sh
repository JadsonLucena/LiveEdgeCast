#!/bin/bash
set -euo pipefail

log() {
  echo "[$(date)] [worker-entrypoint] $1"
}

/scripts/worker_stream_runner.sh &
RUNNER_PID=$!
python3 /scripts/metrics_exporter.py &
EXPORTER_PID=$!

nginx -g 'daemon off;' &
NGINX_PID=$!

wait "$RUNNER_PID"
RUNNER_EXIT=$?

if [ "$RUNNER_EXIT" -ne 0 ]; then
  log "worker_stream_runner.sh exited with code ${RUNNER_EXIT}. Stopping nginx and crashing pod."
  kill -TERM "$NGINX_PID" 2>/dev/null || true
  kill -TERM "$EXPORTER_PID" 2>/dev/null || true
  wait "$NGINX_PID" 2>/dev/null || true
  exit "$RUNNER_EXIT"
fi

log "worker_stream_runner.sh exited successfully. Waiting nginx process."
wait "$NGINX_PID"
