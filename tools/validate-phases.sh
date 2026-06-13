#!/bin/bash
set -euo pipefail
python3 -m py_compile docker/controller/main.py
bash -n docker/worker/worker_stream_runner.sh
bash -n docker/proxy/on_publish_done.sh
rg -n "STATE_SCHEMA_VERSION|stream_generation|monitor_worker_health|stream_assignment_info|generation" docker/controller/main.py >/dev/null
rg -n "STREAM_KEY|PROXY_DNS|RTMP_PUSH_BASE_URL" docker/worker/worker_stream_runner.sh >/dev/null
echo "Validation checks passed"
