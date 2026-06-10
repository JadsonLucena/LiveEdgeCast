#!/bin/bash
set -euo pipefail

log() {
  echo "[$(date)] [worker-entrypoint] $1"
}

/scripts/metrics_exporter.py &
METRICS_EXPORTER_PID=$!

/scripts/worker_stream_runner.sh &
RUNNER_PID=$!

nginx -g 'daemon off;' &
NGINX_PID=$!

set +e
wait -n -p EXITED_PID "$RUNNER_PID" "$METRICS_EXPORTER_PID" "$NGINX_PID"
FIRST_EXIT=$?
set -e

if [ "$EXITED_PID" = "$METRICS_EXPORTER_PID" ]; then
  log "metrics_exporter.py exited with code ${FIRST_EXIT}. Stopping worker and crashing pod."
  kill -TERM "$RUNNER_PID" "$NGINX_PID" 2>/dev/null || true
  wait "$RUNNER_PID" "$NGINX_PID" 2>/dev/null || true
  exit "$FIRST_EXIT"
fi

if [ "$EXITED_PID" = "$NGINX_PID" ]; then
  log "nginx exited with code ${FIRST_EXIT}. Stopping worker/exporter and crashing pod."
  kill -TERM "$RUNNER_PID" "$METRICS_EXPORTER_PID" 2>/dev/null || true
  wait "$RUNNER_PID" "$METRICS_EXPORTER_PID" 2>/dev/null || true
  exit "$FIRST_EXIT"
fi

RUNNER_EXIT=$FIRST_EXIT
if [ "$RUNNER_EXIT" -ne 0 ]; then
  log "worker_stream_runner.sh exited with code ${RUNNER_EXIT}. Stopping nginx/exporter and crashing pod."
  kill -TERM "$NGINX_PID" "$METRICS_EXPORTER_PID" 2>/dev/null || true
  wait "$NGINX_PID" "$METRICS_EXPORTER_PID" 2>/dev/null || true
  exit "$RUNNER_EXIT"
fi

log "worker_stream_runner.sh exited successfully. Waiting nginx or metrics exporter process."
set +e
wait -n -p EXITED_PID "$NGINX_PID" "$METRICS_EXPORTER_PID"
FINAL_EXIT=$?
set -e

if [ "$EXITED_PID" = "$METRICS_EXPORTER_PID" ]; then
  log "metrics_exporter.py exited with code ${FINAL_EXIT}. Stopping nginx and crashing pod."
  kill -TERM "$NGINX_PID" 2>/dev/null || true
  kill -TERM "$METRICS_PID" 2>/dev/null || true
  wait "$NGINX_PID" 2>/dev/null || true
  exit "$FINAL_EXIT"
fi

log "nginx exited with code ${FINAL_EXIT}. Stopping metrics exporter."
kill -TERM "$METRICS_EXPORTER_PID" 2>/dev/null || true
wait "$METRICS_EXPORTER_PID" 2>/dev/null || true
exit "$FINAL_EXIT"
