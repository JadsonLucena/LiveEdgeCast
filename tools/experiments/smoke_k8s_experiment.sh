#!/usr/bin/env bash
set -euo pipefail

: "${LIVEEDGECAST_RTMP_URL:?Set LIVEEDGECAST_RTMP_URL, e.g. rtmp://127.0.0.1:1935/live}"
: "${PROMETHEUS_URL:?Set PROMETHEUS_URL, e.g. http://127.0.0.1:9090}"

NAMESPACE="${NAMESPACE:-media}"
OUTPUT_DIR="${OUTPUT_DIR:-./reports-smoke}"
EXPERIMENT_ID="${EXPERIMENT_ID:-smoke-cold-start-$(date +%s)}"
RUN_ID="${RUN_ID:-smoke-run}"
STREAM_KEY="${STREAM_KEY:-smoke-key-$(date +%s)}"
DURATION_SECONDS="${DURATION_SECONDS:-30}"

python tools/experiments/run_experiment.py \
  --stream-keys "${STREAM_KEY}" \
  --scenario cold-start \
  --experiment-id "${EXPERIMENT_ID}" \
  --run-id "${RUN_ID}" \
  --duration-seconds "${DURATION_SECONDS}" \
  --repetitions 1 \
  --rtmp-url "${LIVEEDGECAST_RTMP_URL}" \
  --prometheus-url "${PROMETHEUS_URL}" \
  --namespace "${NAMESPACE}" \
  --output-dir "${OUTPUT_DIR}" \
  --overwrite

REPORT_ROOT="${OUTPUT_DIR%/}/${EXPERIMENT_ID}"
test -s "${REPORT_ROOT}/report.md"
test -s "${REPORT_ROOT}/metrics/activation_metrics.csv"
test -s "${REPORT_ROOT}/metrics/correctness_metrics.csv"

python - <<PY
import csv
from pathlib import Path
root = Path('${REPORT_ROOT}')
activation = list(csv.DictReader((root / 'metrics' / 'activation_metrics.csv').open()))
correctness = list(csv.DictReader((root / 'metrics' / 'correctness_metrics.csv').open()))
if not activation:
    raise SystemExit('activation_metrics.csv has no rows')
if not correctness:
    raise SystemExit('correctness_metrics.csv has no rows')
observable_activation = [
    row for row in activation
    if row.get('total_activation_seconds') not in (None, '', 'None', 'null')
    or row.get('status') == 'derived_from_controller_structured_logs'
]
worker_observed = [
    row for row in correctness
    if str(row.get('worker_observed_for_stream')).lower() == 'true'
]
if not observable_activation:
    raise SystemExit('no observable activation sample found; inspect controller lifecycle logs and Prometheus scrape configuration')
if not worker_observed:
    raise SystemExit('no worker observation found in correctness_metrics.csv; inspect Kubernetes annotations/controller events')
print(f'Smoke experiment OK with observable lifecycle and worker evidence: {root}')
PY
