#!/usr/bin/env bash

set -euo pipefail

NAMESPACE="${NAMESPACE:-media}"
CONTROLLER_DEPLOYMENT="${CONTROLLER_DEPLOYMENT:-rtmp-controller}"
PROXY_SELECTOR="${PROXY_SELECTOR:-app=rtmp-proxy}"
WORKER_SELECTOR="${WORKER_SELECTOR:-app=rtmp-worker}"
STREAM_NAME="${STREAM_NAME:-}"
SINCE="${SINCE:-10m}"

green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red() { printf "\033[31m%s\033[0m\n" "$*"; }

check() {
  local title="$1"
  shift
  echo ""
  echo "=== $title ==="
  if "$@"; then
    green "OK: $title"
  else
    red "FAIL: $title"
    return 1
  fi
}

log_snippet() {
  local pod="$1"
  local pattern="${2:-}"
  if [[ -n "$pattern" ]]; then
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" 2>/dev/null | rg -n "$pattern" || true
  else
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" 2>/dev/null | tail -n 50 || true
  fi
}

echo "Runtime checklist"
echo "- namespace: $NAMESPACE"
echo "- stream_name filter: ${STREAM_NAME:-<none>}"
echo "- logs since: $SINCE"

check "kubectl connectivity" kubectl version --client >/dev/null
check "namespace exists" kubectl get ns "$NAMESPACE" >/dev/null
check "controller deployment exists" kubectl get deploy -n "$NAMESPACE" "$CONTROLLER_DEPLOYMENT" >/dev/null
check "proxy pods present" bash -lc "kubectl get pods -n '$NAMESPACE' -l '$PROXY_SELECTOR' --no-headers | wc -l | awk '{exit !(\$1>0)}'"
check "worker pods present" bash -lc "kubectl get pods -n '$NAMESPACE' -l '$WORKER_SELECTOR' --no-headers | wc -l | awk '{exit !(\$1>0)}'"

echo ""
echo "=== Pod status ==="
kubectl get pods -n "$NAMESPACE" -l "$PROXY_SELECTOR"
kubectl get pods -n "$NAMESPACE" -l "$WORKER_SELECTOR"
kubectl get deploy -n "$NAMESPACE" "$CONTROLLER_DEPLOYMENT"

echo ""
echo "=== Controller /health and /metrics ==="
CTRL_POD="$(kubectl get pod -n "$NAMESPACE" -l app="$CONTROLLER_DEPLOYMENT" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "$CTRL_POD" ]]; then
  CTRL_POD="$(kubectl get pod -n "$NAMESPACE" -l app=rtmp-controller -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
fi
if [[ -n "$CTRL_POD" ]]; then
  check "controller /health" kubectl exec -n "$NAMESPACE" "$CTRL_POD" -- sh -lc "wget -qO- http://127.0.0.1:8000/health | rg -q 'ok'"
  check "controller /metrics has stream gauges" kubectl exec -n "$NAMESPACE" "$CTRL_POD" -- sh -lc "wget -qO- http://127.0.0.1:8000/metrics | rg -q 'stream_delivery_status|stream_uptime_seconds|recovery_attempt_total'"
else
  yellow "WARN: could not resolve controller pod by label; skipping in-pod endpoint checks"
fi

echo ""
echo "=== Controller logs (allocation lifecycle) ==="
if [[ -n "$CTRL_POD" ]]; then
  if [[ -n "$STREAM_NAME" ]]; then
    log_snippet "$CTRL_POD" "$STREAM_NAME|Allocate|Release|StartWorker|Delivery|Recovery|pending allocation"
  else
    log_snippet "$CTRL_POD" "Allocate|Release|StartWorker|Delivery|Recovery|pending allocation"
  fi
fi

echo ""
echo "=== Proxy logs (publish hooks) ==="
for pod in $(kubectl get pods -n "$NAMESPACE" -l "$PROXY_SELECTOR" -o name); do
  echo "--- $pod ---"
  if [[ -n "$STREAM_NAME" ]]; then
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | rg -n "on_publish_start|on_publish_done|$STREAM_NAME|allocat|worker" || true
  else
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | rg -n "on_publish_start|on_publish_done|allocat|worker" || true
  fi
done

echo ""
echo "=== Worker logs (recovery/ffmpeg) ==="
for pod in $(kubectl get pods -n "$NAMESPACE" -l "$WORKER_SELECTOR" -o name); do
  echo "--- $pod ---"
  if [[ -n "$STREAM_NAME" ]]; then
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | rg -n "$STREAM_NAME|worker_recovery|ffmpeg|recovery-report|heartbeat|error" || true
  else
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | rg -n "worker_recovery|ffmpeg|recovery-report|heartbeat|error" || true
  fi
done

echo ""
echo "=== Objective checklist (manual confirmation) ==="
cat <<'EOF'
[ ] Proxy log has "on_publish_start" for your stream.
[ ] Controller log has "Allocate" and either "allocated" or "scaled deployment".
[ ] Controller log has "StartWorker" success for selected worker.
[ ] Worker log has recovery loop start and FFmpeg start for stream.
[ ] Controller /metrics includes stream_delivery_status and recovery_attempt_total.
[ ] On stream stop, proxy has "on_publish_done" and controller has "Release".
EOF

green "Checklist script finished."
