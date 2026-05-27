#!/usr/bin/env bash
set -euo pipefail
for c in $(seq 1 ${max_concurrency:-30}); do concurrency=$c duration_seconds=${duration_seconds:-90} ./tools/experiments/run_publishers.sh; ./tools/experiments/stop_publishers.sh; done
