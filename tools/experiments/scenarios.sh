#!/usr/bin/env bash
set -euo pipefail
for c in 1 5 10 15 20; do concurrency=$c duration_seconds=${duration_seconds:-120} bitrate=${bitrate:-2500k} ./tools/experiments/run_publishers.sh; ./tools/experiments/stop_publishers.sh; done
