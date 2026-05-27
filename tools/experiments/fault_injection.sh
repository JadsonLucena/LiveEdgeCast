#!/usr/bin/env bash
set -euo pipefail
ACTION=${1:-kill-worker}; NS=${namespace:-media}
case "$ACTION" in
  kill-worker) kubectl -n "$NS" delete pod -l app=worker --force --grace-period=0 ;;
  kill-proxy) kubectl -n "$NS" delete pod -l app=proxy --force --grace-period=0 ;;
  reconnect-duplicate) echo "Execute publisher restart with same streamKey" ;;
esac
