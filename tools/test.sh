#!/usr/bin/env bash

set -euo pipefail

NAMESPACE="${NAMESPACE:-media}"
CONTROLLER_DEPLOYMENT="${CONTROLLER_DEPLOYMENT:-rtmp-controller}"
PROXY_SELECTOR="${PROXY_SELECTOR:-app=rtmp-proxy}"
WORKER_SELECTOR="${WORKER_SELECTOR:-app=rtmp-worker}"
STREAM_NAME="${STREAM_NAME:-}"
SINCE="${SINCE:-10m}"
TAIL_LINES="${TAIL_LINES:-120}"

usage() {
  cat <<USAGE
Usage: tools/test.sh [options]

Options:
  -n, --namespace <name>            Kubernetes namespace (default: ${NAMESPACE})
  -c, --controller <deployment>     Controller deployment name (default: ${CONTROLLER_DEPLOYMENT})
  -p, --proxy-selector <selector>   Label selector for proxy pods (default: ${PROXY_SELECTOR})
  -w, --worker-selector <selector>  Label selector for worker pods (default: ${WORKER_SELECTOR})
  -s, --stream-name <name>          Stream name filter (also accepts STREAM_NAME env)
      --since <duration>            Log window passed to kubectl logs (default: ${SINCE})
      --tail-lines <n>              Tail lines per pod when no filter match (default: ${TAIL_LINES})
  -h, --help                        Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--namespace) NAMESPACE="$2"; shift 2 ;;
    -c|--controller) CONTROLLER_DEPLOYMENT="$2"; shift 2 ;;
    -p|--proxy-selector) PROXY_SELECTOR="$2"; shift 2 ;;
    -w|--worker-selector) WORKER_SELECTOR="$2"; shift 2 ;;
    -s|--stream-name) STREAM_NAME="$2"; shift 2 ;;
    --since) SINCE="$2"; shift 2 ;;
    --tail-lines) TAIL_LINES="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red() { printf "\033[31m%s\033[0m\n" "$*"; }
HAS_RG=0
if command -v rg >/dev/null 2>&1; then
  HAS_RG=1
fi

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

check_warn() {
  local title="$1"
  shift
  echo ""
  echo "=== $title ==="
  if "$@"; then
    green "OK: $title"
    return 0
  fi
  yellow "WARN: $title"
  return 0
}

log_snippet() {
  local pod="$1"
  local pattern="${2:-}"
  if [[ -n "$pattern" ]]; then
    if [[ "$HAS_RG" == "1" ]]; then
      kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" 2>/dev/null | rg -n "$pattern" || true
    else
      kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" 2>/dev/null | grep -En "$pattern" || true
    fi
  else
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" 2>/dev/null | tail -n "$TAIL_LINES" || true
  fi
}

filter_stream() {
  local pattern="$1"
  if [[ "$HAS_RG" == "1" ]]; then
    rg -n "$pattern" || true
  else
    grep -En "$pattern" || true
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
check_warn "worker pods present" bash -lc "kubectl get pods -n '$NAMESPACE' -l '$WORKER_SELECTOR' --no-headers | wc -l | awk '{exit !(\$1>0)}'"

echo ""
echo "=== Pod status ==="
kubectl get pods -n "$NAMESPACE" -l "$PROXY_SELECTOR"
kubectl get pods -n "$NAMESPACE" -l "$WORKER_SELECTOR"
kubectl get deploy -n "$NAMESPACE" "$CONTROLLER_DEPLOYMENT"

WORKER_COUNT="$(kubectl get pods -n "$NAMESPACE" -l "$WORKER_SELECTOR" --no-headers 2>/dev/null | wc -l | tr -d ' ')"

if [[ "$WORKER_COUNT" == "0" ]]; then
  echo ""
  echo "=== Worker not created: focused diagnostics ==="
  echo "No worker pods matched selector '$WORKER_SELECTOR'. Collecting likely causes..."

  echo ""
  echo "--- Worker deployment status (replicas/conditions) ---"
  kubectl get deploy -n "$NAMESPACE" rtmp-worker -o wide 2>/dev/null || yellow "WARN: deployment/rtmp-worker not found"
  kubectl describe deploy -n "$NAMESPACE" rtmp-worker 2>/dev/null | tail -n 80 || true

  echo ""
  echo "--- HPA / KEDA objects related to worker ---"
  kubectl get scaledobject -n "$NAMESPACE" 2>/dev/null | filter_stream "worker|NAME|NAMESPACE"
  kubectl get hpa -n "$NAMESPACE" 2>/dev/null | filter_stream "worker|NAME"

  echo ""
  echo "--- Recent namespace events (worker/controller/proxy) ---"
  kubectl get events -n "$NAMESPACE" --sort-by=.lastTimestamp 2>/dev/null | tail -n 120 | filter_stream "worker|rtmp-worker|controller|proxy|Failed|Warning|BackOff|Insufficient|Forbidden|Error"
fi

echo ""
echo "=== Controller /health and /metrics ==="
CTRL_POD="$(kubectl get pod -n "$NAMESPACE" -l app="$CONTROLLER_DEPLOYMENT" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "$CTRL_POD" ]]; then
  CTRL_POD="$(kubectl get pod -n "$NAMESPACE" -l app=rtmp-controller -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
fi
if [[ -n "$CTRL_POD" ]]; then
  check_warn "controller /health" kubectl exec -n "$NAMESPACE" "$CTRL_POD" -- sh -lc "if command -v curl >/dev/null 2>&1; then curl -fsS http://127.0.0.1:8000/health; elif command -v wget >/dev/null 2>&1; then wget -qO- http://127.0.0.1:8000/health; else echo 'missing curl/wget'; exit 1; fi | grep -Eq 'ok'"
  check_warn "controller /metrics has stream gauges" kubectl exec -n "$NAMESPACE" "$CTRL_POD" -- sh -lc "if command -v curl >/dev/null 2>&1; then curl -fsS http://127.0.0.1:8000/metrics; elif command -v wget >/dev/null 2>&1; then wget -qO- http://127.0.0.1:8000/metrics; else echo 'missing curl/wget'; exit 1; fi | grep -Eq 'stream_delivery_status|stream_uptime_seconds|recovery_attempt_total'"
else
  yellow "WARN: could not resolve controller pod by label; skipping in-pod endpoint checks"
fi

echo ""
echo "=== Controller logs (allocation lifecycle) ==="
if [[ -n "$CTRL_POD" ]]; then
  if [[ -n "$STREAM_NAME" ]]; then
    log_snippet "$CTRL_POD" "$STREAM_NAME|Allocate|Release|StartWorker|Delivery|Recovery|pending allocation|Registry|ProxyHealth"
  else
    log_snippet "$CTRL_POD" "Allocate|Release|StartWorker|Delivery|Recovery|pending allocation|Registry|ProxyHealth"
  fi
fi

echo ""
echo "=== Proxy logs (publish hooks + routing) ==="
for pod in $(kubectl get pods -n "$NAMESPACE" -l "$PROXY_SELECTOR" -o name); do
  echo "--- $pod ---"
  if [[ -n "$STREAM_NAME" ]]; then
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | filter_stream "on_publish_start|on_publish_done|$STREAM_NAME|allocat|worker|heartbeat|error|warn"
  else
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | filter_stream "on_publish_start|on_publish_done|allocat|worker|heartbeat|error|warn"
  fi
done

echo ""
echo "=== Worker logs (recovery/ffmpeg) ==="
for pod in $(kubectl get pods -n "$NAMESPACE" -l "$WORKER_SELECTOR" -o name); do
  echo "--- $pod ---"
  if [[ -n "$STREAM_NAME" ]]; then
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | filter_stream "$STREAM_NAME|worker_recovery|ffmpeg|recovery-report|heartbeat|error|warn|youtube|tcp"
  else
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | filter_stream "worker_recovery|ffmpeg|recovery-report|heartbeat|error|warn|youtube|tcp"
  fi
done

echo ""
echo "=== Objective checklist (manual confirmation) ==="
cat <<'CHECKLIST'
[ ] Proxy log has "on_publish_start" for your stream.
[ ] Controller log has "Allocate" and either "allocated" or "scaled deployment".
[ ] Controller log has "StartWorker" success for selected worker.
[ ] Worker log has recovery loop start and FFmpeg start for stream.
[ ] Worker log shows no repeated FFmpeg/network errors for your stream.
[ ] Controller /metrics includes stream_delivery_status and recovery_attempt_total.
[ ] On stream stop, proxy has "on_publish_done" and controller has "Release".
CHECKLIST

green "Checklist script finished."
