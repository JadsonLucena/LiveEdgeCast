#!/usr/bin/env bash
set -euo pipefail

: "${LIVEEDGECAST_RTMP_URL:?Set LIVEEDGECAST_RTMP_URL, e.g. rtmp://127.0.0.1:1935/live}"
: "${PROMETHEUS_URL:?Set PROMETHEUS_URL, e.g. http://127.0.0.1:9090}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
NAMESPACE="${NAMESPACE:-media}"
OUTPUT_DIR="${OUTPUT_DIR:-./reports-smoke}"
EXPERIMENT_ID="${EXPERIMENT_ID:-smoke-cold-start-$(date +%s)}"
RUN_ID="${RUN_ID:-smoke-run}"
STREAM_KEYS="${STREAM_KEYS:-${STREAM_KEY:-smoke-key-$(date +%s)}}"
STREAM_KEYS_FILE="${STREAM_KEYS_FILE:-}"
DURATION_SECONDS="${DURATION_SECONDS:-30}"
BITRATE="${BITRATE:-10000k}"
TESTSRC_SIZE="${TESTSRC_SIZE:-1920x1080}"
TESTSRC_RATE="${TESTSRC_RATE:-30}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
CONTROLLER_URL="${CONTROLLER_URL:-${LIVEEDGECAST_CONTROLLER_URL:-http://127.0.0.1:8000}}"
PATCH_PROXY_CONTEXT="${PATCH_PROXY_CONTEXT:-false}"
REQUIRE_NETWORK_METRICS="${REQUIRE_NETWORK_METRICS:-false}"
REQUIRE_DESTINATION_RECEIVED="${REQUIRE_DESTINATION_RECEIVED:-false}"

EXTRA_ARGS=()
if [[ -n "${CONTROLLER_URL}" ]]; then
  EXTRA_ARGS+=(--controller-url "${CONTROLLER_URL}")
fi
if [[ "${PATCH_PROXY_CONTEXT}" == "true" || "${PATCH_PROXY_CONTEXT}" == "1" ]]; then
  EXTRA_ARGS+=(--patch-proxy-context)
fi
if [[ "${REQUIRE_NETWORK_METRICS}" == "true" || "${REQUIRE_NETWORK_METRICS}" == "1" ]]; then
  EXTRA_ARGS+=(--require-network-metrics)
fi
if [[ "${REQUIRE_DESTINATION_RECEIVED}" == "true" || "${REQUIRE_DESTINATION_RECEIVED}" == "1" ]]; then
  EXTRA_ARGS+=(--require-destination-received)
fi

STREAM_ARGS=()
if [[ -n "${STREAM_KEYS_FILE}" ]]; then
  STREAM_ARGS=(--stream-keys-file "${STREAM_KEYS_FILE}")
else
  STREAM_ARGS=(--stream-keys "${STREAM_KEYS}")
fi

"${PYTHON_BIN}" tools/experiments/run_experiment.py \
  "${STREAM_ARGS[@]}" \
  --scenario cold-start \
  --experiment-id "${EXPERIMENT_ID}" \
  --run-id "${RUN_ID}" \
  --duration-seconds "${DURATION_SECONDS}" \
  --repetitions 1 \
  --rtmp-url "${LIVEEDGECAST_RTMP_URL}" \
  --prometheus-url "${PROMETHEUS_URL}" \
  --namespace "${NAMESPACE}" \
  --output-dir "${OUTPUT_DIR}" \
  --bitrate "${BITRATE}" \
  --testsrc-size "${TESTSRC_SIZE}" \
  --testsrc-rate "${TESTSRC_RATE}" \
  --audio-bitrate "${AUDIO_BITRATE}" \
  "${EXTRA_ARGS[@]}" \
  --require-prometheus-analysis \
  --overwrite

REPORT_ROOT="${OUTPUT_DIR%/}/${EXPERIMENT_ID}"
test -s "${REPORT_ROOT}/report.md"
test -s "${REPORT_ROOT}/metrics/activation_metrics.csv"
test -s "${REPORT_ROOT}/metrics/correctness_metrics.csv"

"${PYTHON_BIN}" - <<PY
import csv
import json
from pathlib import Path
root = Path('${REPORT_ROOT}')
activation = list(csv.DictReader((root / 'metrics' / 'activation_metrics.csv').open()))
correctness = list(csv.DictReader((root / 'metrics' / 'correctness_metrics.csv').open()))
report = json.loads((root / 'report.json').read_text())
summary = report.get('summary') or {}
if not summary.get('prometheus_analysis_ready'):
    raise SystemExit(f"prometheus_analysis_ready is false; incomplete metrics: {summary.get('prometheus_incomplete_metrics')}")
if summary.get('prometheus_incomplete_metrics'):
    raise SystemExit(f"Prometheus metrics incomplete: {summary.get('prometheus_incomplete_metrics')}")
if not activation:
    raise SystemExit('activation_metrics.csv has no rows')
if not correctness:
    raise SystemExit('correctness_metrics.csv has no rows')
import math

def finite_number(raw):
    try:
        return raw not in (None, '', 'None', 'null') and math.isfinite(float(raw))
    except (TypeError, ValueError):
        return False

observable_activation = [
    row for row in activation
    if finite_number(row.get('total_activation_seconds'))
]
worker_observed = [
    row for row in correctness
    if str(row.get('worker_observed_for_stream')).lower() == 'true'
]
if not observable_activation:
    raise SystemExit('no finite total_activation_seconds sample found; partial lifecycle rows are insufficient for article cold-start validation')
if not worker_observed:
    raise SystemExit('no worker observation found in correctness_metrics.csv; inspect Kubernetes annotations/controller events')
print(f'Smoke experiment OK with finite activation duration and worker evidence: {root}')
PY
