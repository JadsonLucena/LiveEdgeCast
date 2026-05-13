#!/bin/bash

set -euo pipefail

NAMESPACE_MEDIA="${NAMESPACE_MEDIA:-media}"
NAMESPACE_MONITORING="${NAMESPACE_MONITORING:-monitoring}"
PROM_SVC="${PROM_SVC:-kube-prometheus-stack-prometheus}"
PROM_PORT="${PROM_PORT:-9090}"

echo "=== Phase 0 Observability Check ==="

check() {
  local description="$1"
  shift
  if "$@"; then
    echo "✅ $description"
  else
    echo "❌ $description"
    return 1
  fi
}

check_warn() {
  local description="$1"
  shift
  if "$@"; then
    echo "✅ $description"
  else
    echo "⚠️  $description"
  fi
}

check "monitoring namespace exists" kubectl get ns "$NAMESPACE_MONITORING" >/dev/null
check "media namespace exists" kubectl get ns "$NAMESPACE_MEDIA" >/dev/null
check "prometheus service exists" kubectl get svc -n "$NAMESPACE_MONITORING" "$PROM_SVC" >/dev/null

echo "Checking ServiceMonitors..."
check "proxy ServiceMonitor exists" kubectl get servicemonitor -n "$NAMESPACE_MONITORING" rtmp-proxy-metrics >/dev/null
check "worker ServiceMonitor exists" kubectl get servicemonitor -n "$NAMESPACE_MONITORING" rtmp-worker-metrics >/dev/null
check "controller ServiceMonitor exists" kubectl get servicemonitor -n "$NAMESPACE_MONITORING" rtmp-controller-metrics >/dev/null

echo "Checking PrometheusRule..."
check "proxy observability recording rules exist" kubectl get prometheusrule -n "$NAMESPACE_MONITORING" liveedgecast-proxy-observability >/dev/null

echo "Checking proxy metrics endpoint..."
PROXY_POD="$(kubectl get pod -n "$NAMESPACE_MEDIA" -l app=rtmp-proxy -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -n "${PROXY_POD}" ]]; then
  check_warn "proxy exporter /metrics responds" kubectl exec -n "$NAMESPACE_MEDIA" "$PROXY_POD" -c nginx-prometheus-exporter -- wget -qO- "http://127.0.0.1:9113/metrics" >/dev/null
else
  echo "⚠️  no proxy pod found; skipping in-pod metrics check"
fi

echo ""
echo "Suggested quick PromQL checks:"
echo "  liveedgecast:proxy:inbound_mbps_total"
echo "  liveedgecast:proxy:inbound_mbps_avg_per_pod"
echo "  liveedgecast:proxy:cpu_percent"
echo "  liveedgecast:proxy:memory_percent"
echo ""
echo "Phase 0 check completed."
