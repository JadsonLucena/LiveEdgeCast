#!/usr/bin/env bash
set -euo pipefail
for c in 1 5 10 15 20; do concurrency=$c ./tools/experiments/run_publishers.sh; ./tools/experiments/stop_publishers.sh; done
