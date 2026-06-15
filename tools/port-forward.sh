#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

MEDIA_NAMESPACE="${MEDIA_NAMESPACE:-${NAMESPACE:-media}}"
MONITORING_NAMESPACE="${MONITORING_NAMESPACE:-monitoring}"

CONTROLLER_SERVICE="${CONTROLLER_SERVICE:-controller}"
CONTROLLER_LOCAL_PORT="${CONTROLLER_LOCAL_PORT:-8000}"
CONTROLLER_REMOTE_PORT="${CONTROLLER_REMOTE_PORT:-8000}"

PROXY_ENTRY_SERVICE="${PROXY_ENTRY_SERVICE:-proxy-entry}"
RTMP_LOCAL_PORT="${RTMP_LOCAL_PORT:-1935}"
RTMP_REMOTE_PORT="${RTMP_REMOTE_PORT:-1935}"

PROMETHEUS_SERVICE="${PROMETHEUS_SERVICE:-}"
DEFAULT_PROMETHEUS_SERVICE="${DEFAULT_PROMETHEUS_SERVICE:-kube-prometheus-stack-prometheus}"
PROMETHEUS_LOCAL_PORT="${PROMETHEUS_LOCAL_PORT:-9090}"
PROMETHEUS_REMOTE_PORT="${PROMETHEUS_REMOTE_PORT:-9090}"

# Optional helper: forward proxy HTTP/stat endpoint if needed for manual debug.
ENABLE_PROXY_HTTP_FORWARD="${ENABLE_PROXY_HTTP_FORWARD:-false}"
PROXY_HTTP_SERVICE="${PROXY_HTTP_SERVICE:-proxy}"
PROXY_HTTP_LOCAL_PORT="${PROXY_HTTP_LOCAL_PORT:-8080}"
PROXY_HTTP_REMOTE_PORT="${PROXY_HTTP_REMOTE_PORT:-8080}"

BIND_ADDRESS="${LIVEEDGECAST_PORT_FORWARD_ADDRESS:-127.0.0.1}"
PID_DIR="${LIVEEDGECAST_PORT_FORWARD_PID_DIR:-/tmp/liveedgecast-port-forward}"
LOG_DIR="${LIVEEDGECAST_PORT_FORWARD_LOG_DIR:-/tmp/liveedgecast-port-forward}"
RESTART_DELAY_SECONDS="${LIVEEDGECAST_PORT_FORWARD_RESTART_DELAY_SECONDS:-2}"
READINESS_TIMEOUT_SECONDS="${LIVEEDGECAST_PORT_FORWARD_READINESS_TIMEOUT_SECONDS:-75}"

print_step() { echo -e "${BLUE}📋 $*${NC}"; }
print_success() { echo -e "${GREEN}✅ $*${NC}"; }
print_warning() { echo -e "${YELLOW}⚠️  $*${NC}"; }
print_error() { echo -e "${RED}❌ $*${NC}"; }

usage() {
  cat <<USAGE
Usage: $0 [start|stop|restart|status|logs|doctor]

Managed forwards:
  Controller: localhost:${CONTROLLER_LOCAL_PORT} -> ${MEDIA_NAMESPACE}/svc/${CONTROLLER_SERVICE}:${CONTROLLER_REMOTE_PORT}
  RTMP:       localhost:${RTMP_LOCAL_PORT} -> ${MEDIA_NAMESPACE}/svc/${PROXY_ENTRY_SERVICE}:${RTMP_REMOTE_PORT}
  Prometheus: localhost:${PROMETHEUS_LOCAL_PORT} -> ${MONITORING_NAMESPACE}/svc/<prometheus-service>:${PROMETHEUS_REMOTE_PORT}

Environment overrides:
  MEDIA_NAMESPACE, MONITORING_NAMESPACE
  CONTROLLER_SERVICE, CONTROLLER_LOCAL_PORT, CONTROLLER_REMOTE_PORT
  PROXY_ENTRY_SERVICE, RTMP_LOCAL_PORT, RTMP_REMOTE_PORT
  PROMETHEUS_SERVICE, DEFAULT_PROMETHEUS_SERVICE, PROMETHEUS_LOCAL_PORT, PROMETHEUS_REMOTE_PORT
  ENABLE_PROXY_HTTP_FORWARD=true
  LIVEEDGECAST_PORT_FORWARD_PID_DIR, LIVEEDGECAST_PORT_FORWARD_LOG_DIR
USAGE
}

check_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    print_error "$1 not found."
    exit 1
  fi
}

script_path() {
  readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0"
}

pid_file_for_label() { echo "${PID_DIR}/liveedgecast-${1}-port-forward.pid"; }
log_file_for_label() { echo "${LOG_DIR}/liveedgecast-${1}-port-forward.log"; }

local_port_is_open() {
  local port="$1"
  (echo >/dev/tcp/127.0.0.1/"$port") >/dev/null 2>&1
}

service_port() {
  local namespace="$1"
  local service_name="$2"
  local desired_port="$3"

  kubectl -n "$namespace" get "svc/${service_name}" \
    -o jsonpath='{range .spec.ports[*]}{.port}{"\t"}{.name}{"\t"}{.targetPort}{"\n"}{end}' \
    2>/dev/null \
    | awk -v desired="$desired_port" '
        $1 == desired { print $1; found = 1; exit }
        !found && $3 == desired { candidate = $1 }
        END { if (!found && candidate != "") print candidate; else if (!found) exit 1 }
      '
}

prometheus_service_port() {
  local service_name="$1"

  kubectl -n "$MONITORING_NAMESPACE" get "svc/${service_name}" \
    -o jsonpath='{range .spec.ports[*]}{.port}{"\t"}{.name}{"\t"}{.targetPort}{"\n"}{end}' \
    2>/dev/null \
    | awk '
        $1 == "9090" { print $1; found = 1; exit }
        !found && ($2 == "web" || $2 == "http-web") { candidate = $1 }
        !found && candidate == "" && $3 == "9090" { candidate = $1 }
        END { if (!found && candidate != "") print candidate; else if (!found) exit 1 }
      '
}

list_prometheus_service_candidates() {
  kubectl -n "$MONITORING_NAMESPACE" get svc \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.app\.kubernetes\.io/name}{"\t"}{range .spec.ports[*]}{.port}{":"}{.name}{":"}{.targetPort}{","}{end}{"\n"}{end}' \
    2>/dev/null \
    | awk -F '\t' '
        function find_port(ports, entries, n, i, parts, fallback) {
          n = split(ports, entries, ",")
          for (i = 1; i <= n; i++) {
            if (entries[i] == "") continue
            split(entries[i], parts, ":")
            if (parts[1] == "9090") return parts[1]
            if (fallback == "" && (parts[2] == "web" || parts[2] == "http-web")) fallback = parts[1]
            if (fallback == "" && parts[3] == "9090") fallback = parts[1]
          }
          return fallback
        }
        ($2 == "prometheus" || $1 == "prometheus-operated" || $1 ~ /prometheus/) {
          port = find_port($3)
          if (port != "") print $1 "\t" port
        }
      '
}

resolve_prometheus_service() {
  local svc="" port=""

  if [[ -n "$PROMETHEUS_SERVICE" ]]; then
    kubectl -n "$MONITORING_NAMESPACE" get "svc/${PROMETHEUS_SERVICE}" >/dev/null
    port="$(prometheus_service_port "$PROMETHEUS_SERVICE" || true)"
    if [[ -z "$port" ]]; then
      print_error "Configured Prometheus service ${MONITORING_NAMESPACE}/svc/${PROMETHEUS_SERVICE} does not expose port 9090/web." >&2
      exit 1
    fi
    PROMETHEUS_REMOTE_PORT="$port"
    return 0
  fi

  if kubectl -n "$MONITORING_NAMESPACE" get "svc/${DEFAULT_PROMETHEUS_SERVICE}" >/dev/null 2>&1; then
    port="$(prometheus_service_port "$DEFAULT_PROMETHEUS_SERVICE" || true)"
    if [[ -n "$port" ]]; then
      PROMETHEUS_SERVICE="$DEFAULT_PROMETHEUS_SERVICE"
      PROMETHEUS_REMOTE_PORT="$port"
      return 0
    fi
  fi

  local candidates=()
  while IFS=$'\t' read -r svc port; do
    [[ -n "${svc:-}" && -n "${port:-}" ]] && candidates+=("${svc}:${port}")
  done < <(list_prometheus_service_candidates | sort -u)

  case "${#candidates[@]}" in
    0)
      print_error "Prometheus service was not found in namespace ${MONITORING_NAMESPACE}." >&2
      kubectl -n "$MONITORING_NAMESPACE" get svc >&2 2>/dev/null || true
      exit 1
      ;;
    1)
      PROMETHEUS_SERVICE="${candidates[0]%:*}"
      PROMETHEUS_REMOTE_PORT="${candidates[0]##*:}"
      print_warning "Using discovered Prometheus service ${MONITORING_NAMESPACE}/svc/${PROMETHEUS_SERVICE}:${PROMETHEUS_REMOTE_PORT}." >&2
      ;;
    *)
      print_error "Multiple Prometheus service candidates found: ${candidates[*]}. Set PROMETHEUS_SERVICE explicitly." >&2
      exit 1
      ;;
  esac
}

kill_pid_file() {
  local label="$1"
  local pid_file pid
  pid_file="$(pid_file_for_label "$label")"

  [[ -f "$pid_file" ]] || return 0
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  rm -f "$pid_file"

  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 0.3
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    print_warning "Stopped ${label} supervisor pid ${pid}"
  fi
}

stop_existing_kubectl_port_forward() {
  local namespace="$1"
  local resource="$2"
  local local_port="$3"
  local remote_port="$4"
  local pattern="kubectl.*-n ${namespace}.*port-forward.*${resource}.*${local_port}:${remote_port}"

  if pkill -f "$pattern" 2>/dev/null; then
    print_warning "Stopped existing kubectl port-forward for ${namespace}/${resource} ${local_port}:${remote_port}"
  fi
}

stop_all() {
  mkdir -p "$PID_DIR" "$LOG_DIR"
  print_step "Stopping tracked LiveEdgeCast port-forward supervisors..."
  kill_pid_file controller
  kill_pid_file rtmp
  kill_pid_file prometheus
  kill_pid_file proxy-http

  print_step "Stopping matching kubectl port-forward processes..."
  stop_existing_kubectl_port_forward "$MEDIA_NAMESPACE" "svc/${CONTROLLER_SERVICE}" "$CONTROLLER_LOCAL_PORT" "$CONTROLLER_REMOTE_PORT"
  stop_existing_kubectl_port_forward "$MEDIA_NAMESPACE" "svc/${PROXY_ENTRY_SERVICE}" "$RTMP_LOCAL_PORT" "$RTMP_REMOTE_PORT"
  if [[ -n "${PROMETHEUS_SERVICE:-}" ]]; then
    stop_existing_kubectl_port_forward "$MONITORING_NAMESPACE" "svc/${PROMETHEUS_SERVICE}" "$PROMETHEUS_LOCAL_PORT" "$PROMETHEUS_REMOTE_PORT"
  fi
  stop_existing_kubectl_port_forward "$MEDIA_NAMESPACE" "svc/${PROXY_HTTP_SERVICE}" "$PROXY_HTTP_LOCAL_PORT" "$PROXY_HTTP_REMOTE_PORT"
}

__supervise() {
  local namespace="$1"
  local resource="$2"
  local local_port="$3"
  local remote_port="$4"
  local label="$5"
  local child="" rc=0

  terminate() {
    echo "[$(date -Is)] ${label}: supervisor received termination signal"
    if [[ -n "$child" ]] && kill -0 "$child" 2>/dev/null; then
      kill "$child" 2>/dev/null || true
      wait "$child" 2>/dev/null || true
    fi
    exit 0
  }

  trap terminate INT TERM

  echo "[$(date -Is)] ${label}: supervising ${namespace}/${resource} ${local_port}:${remote_port}"
  while true; do
    echo "[$(date -Is)] ${label}: starting kubectl port-forward"
    kubectl -n "$namespace" port-forward --address "$BIND_ADDRESS" "$resource" "${local_port}:${remote_port}" &
    child=$!
    set +e
    wait "$child"
    rc=$?
    set -e
    child=""
    echo "[$(date -Is)] ${label}: kubectl port-forward exited rc=${rc}; restarting in ${RESTART_DELAY_SECONDS}s"
    sleep "$RESTART_DELAY_SECONDS"
  done
}

wait_for_local_port() {
  local label="$1"
  local local_port="$2"
  local pid_file="$3"
  local log_file="$4"
  local deadline=$((SECONDS + READINESS_TIMEOUT_SECONDS))

  while (( SECONDS < deadline )); do
    if [[ ! -f "$pid_file" ]]; then
      print_error "${label}: pid file disappeared while waiting for localhost:${local_port}."
      return 1
    fi

    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
      print_error "${label}: supervisor is not running. See ${log_file}."
      cat "$log_file" 2>/dev/null || true
      return 1
    fi

    if local_port_is_open "$local_port"; then
      return 0
    fi
    sleep 0.5
  done

  print_error "${label}: timed out waiting for localhost:${local_port}. See ${log_file}."
  tail -100 "$log_file" 2>/dev/null || true
  return 1
}

start_supervisor() {
  local namespace="$1"
  local resource="$2"
  local local_port="$3"
  local remote_port="$4"
  local label="$5"
  local pid_file log_file script pid

  pid_file="$(pid_file_for_label "$label")"
  log_file="$(log_file_for_label "$label")"
  script="$(script_path)"

  mkdir -p "$PID_DIR" "$LOG_DIR"
  : >"$log_file"

  nohup "$script" __supervise "$namespace" "$resource" "$local_port" "$remote_port" "$label" >"$log_file" 2>&1 &
  pid=$!
  echo "$pid" >"$pid_file"

  wait_for_local_port "$label" "$local_port" "$pid_file" "$log_file"
  print_success "${label}: localhost:${local_port} -> ${namespace}/${resource}:${remote_port} (supervisor pid ${pid})"
}

validate_required_services() {
  print_step "Validating required Services..."
  kubectl -n "$MEDIA_NAMESPACE" get "svc/${CONTROLLER_SERVICE}" >/dev/null
  kubectl -n "$MEDIA_NAMESPACE" get "svc/${PROXY_ENTRY_SERVICE}" >/dev/null
  resolve_prometheus_service

  CONTROLLER_REMOTE_PORT="$(service_port "$MEDIA_NAMESPACE" "$CONTROLLER_SERVICE" "$CONTROLLER_REMOTE_PORT" || echo "$CONTROLLER_REMOTE_PORT")"
  RTMP_REMOTE_PORT="$(service_port "$MEDIA_NAMESPACE" "$PROXY_ENTRY_SERVICE" "$RTMP_REMOTE_PORT" || echo "$RTMP_REMOTE_PORT")"

  if [[ "$ENABLE_PROXY_HTTP_FORWARD" == "true" ]]; then
    kubectl -n "$MEDIA_NAMESPACE" get "svc/${PROXY_HTTP_SERVICE}" >/dev/null
    PROXY_HTTP_REMOTE_PORT="$(service_port "$MEDIA_NAMESPACE" "$PROXY_HTTP_SERVICE" "$PROXY_HTTP_REMOTE_PORT" || echo "$PROXY_HTTP_REMOTE_PORT")"
  fi
}

print_health() {
  print_step "Validating local endpoints..."

  if command -v curl >/dev/null 2>&1; then
    if curl -fsS "http://127.0.0.1:${CONTROLLER_LOCAL_PORT}/health" >/tmp/liveedgecast-controller-health.out 2>/tmp/liveedgecast-controller-health.err; then
      print_success "controller health: $(cat /tmp/liveedgecast-controller-health.out)"
    else
      print_warning "controller health check failed on http://127.0.0.1:${CONTROLLER_LOCAL_PORT}/health"
      cat /tmp/liveedgecast-controller-health.err 2>/dev/null || true
    fi

    if curl -fsS "http://127.0.0.1:${PROMETHEUS_LOCAL_PORT}/-/ready" >/tmp/liveedgecast-prometheus-ready.out 2>/tmp/liveedgecast-prometheus-ready.err; then
      print_success "prometheus ready: $(cat /tmp/liveedgecast-prometheus-ready.out)"
    else
      print_warning "prometheus readiness check failed on http://127.0.0.1:${PROMETHEUS_LOCAL_PORT}/-/ready"
      cat /tmp/liveedgecast-prometheus-ready.err 2>/dev/null || true
    fi
  else
    print_warning "curl not found; skipped HTTP health checks."
  fi

  if local_port_is_open "$RTMP_LOCAL_PORT"; then
    print_success "rtmp port is open: 127.0.0.1:${RTMP_LOCAL_PORT}"
  else
    print_warning "rtmp port is not open: 127.0.0.1:${RTMP_LOCAL_PORT}"
  fi
}

start_all() {
  check_command kubectl
  if ! kubectl cluster-info >/dev/null 2>&1; then
    print_error "Cannot connect to Kubernetes cluster."
    kubectl config current-context 2>/dev/null || true
    exit 1
  fi

  validate_required_services
  print_step "Restarting LiveEdgeCast managed port-forwards..."
  stop_all

  print_step "Starting self-healing port-forward supervisors..."
  start_supervisor "$MEDIA_NAMESPACE" "svc/${CONTROLLER_SERVICE}" "$CONTROLLER_LOCAL_PORT" "$CONTROLLER_REMOTE_PORT" controller
  start_supervisor "$MEDIA_NAMESPACE" "svc/${PROXY_ENTRY_SERVICE}" "$RTMP_LOCAL_PORT" "$RTMP_REMOTE_PORT" rtmp
  start_supervisor "$MONITORING_NAMESPACE" "svc/${PROMETHEUS_SERVICE}" "$PROMETHEUS_LOCAL_PORT" "$PROMETHEUS_REMOTE_PORT" prometheus

  if [[ "$ENABLE_PROXY_HTTP_FORWARD" == "true" ]]; then
    start_supervisor "$MEDIA_NAMESPACE" "svc/${PROXY_HTTP_SERVICE}" "$PROXY_HTTP_LOCAL_PORT" "$PROXY_HTTP_REMOTE_PORT" proxy-http
  fi

  print_health
  print_success "Port-forwards are configured and supervised."
  echo ""
  echo "Endpoints:"
  echo "  Controller: http://127.0.0.1:${CONTROLLER_LOCAL_PORT}/health"
  echo "  RTMP:       rtmp://127.0.0.1:${RTMP_LOCAL_PORT}/live"
  echo "  Prometheus: http://127.0.0.1:${PROMETHEUS_LOCAL_PORT}"
  echo "Logs: ${LOG_DIR}/liveedgecast-*-port-forward.log"
}

status_one() {
  local label="$1"
  local port="$2"
  local pid_file log_file pid status="stopped" port_status="closed"
  pid_file="$(pid_file_for_label "$label")"
  log_file="$(log_file_for_label "$label")"

  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      status="running(pid=${pid})"
    fi
  fi

  if local_port_is_open "$port"; then
    port_status="open"
  fi

  printf '%-12s %-22s localhost:%s=%s log=%s\n' "$label" "$status" "$port" "$port_status" "$log_file"
}

status_all() {
  status_one controller "$CONTROLLER_LOCAL_PORT"
  status_one rtmp "$RTMP_LOCAL_PORT"
  status_one prometheus "$PROMETHEUS_LOCAL_PORT"
  if [[ "$ENABLE_PROXY_HTTP_FORWARD" == "true" ]]; then
    status_one proxy-http "$PROXY_HTTP_LOCAL_PORT"
  fi
}

logs_all() {
  for label in controller rtmp prometheus proxy-http; do
    local log_file
    log_file="$(log_file_for_label "$label")"
    if [[ -f "$log_file" ]]; then
      echo ""
      echo "===== ${label}: ${log_file} ====="
      tail -100 "$log_file" || true
    fi
  done
}

doctor() {
  print_step "Kubernetes context"
  kubectl config current-context || true
  print_step "Media pods"
  kubectl -n "$MEDIA_NAMESPACE" get pods -o wide || true
  print_step "Media services"
  kubectl -n "$MEDIA_NAMESPACE" get svc || true
  print_step "Monitoring services"
  kubectl -n "$MONITORING_NAMESPACE" get svc | grep -Ei 'prometheus|NAME' || true
  print_step "Port-forward status"
  status_all
  print_health
}

main() {
  local command="${1:-start}"
  case "$command" in
    start|restart)
      start_all
      ;;
    stop)
      resolve_prometheus_service >/dev/null 2>&1 || true
      stop_all
      ;;
    status)
      status_all
      ;;
    logs)
      logs_all
      ;;
    doctor)
      doctor
      ;;
    --help|-h|help)
      usage
      ;;
    __supervise)
      shift
      __supervise "$@"
      ;;
    *)
      print_error "Unknown command: ${command}"
      usage
      exit 2
      ;;
  esac
}

main "$@"
