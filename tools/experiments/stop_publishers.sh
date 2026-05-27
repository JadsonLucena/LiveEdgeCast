#!/usr/bin/env bash
set -euo pipefail
for f in /tmp/liveedgecast-pids/*.pid; do [ -f "$f" ] || continue; kill "$(cat "$f")" || true; done
