#!/usr/bin/env bash
set -euo pipefail
for f in /tmp/pub-*.pid; do [ -f "$f" ] || continue; kill "$(cat "$f")" || true; rm -f "$f"; done
