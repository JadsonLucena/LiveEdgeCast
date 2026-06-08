#!/bin/bash

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

STARTED_PIDS=()
STARTED_LABELS=()
PROMETHEUS_SERVICE="${PROMETHEUS_SERVICE:-}"
DEFAULT_PROMETHEUS_SERVICE="kube-prometheus-stack-prometheus"
PROMETHEUS_REMOTE_PORT="${PROMETHEUS_REMOTE_PORT:-9090}"
PID_DIR="${LIVEEDGECAST_PORT_FORWARD_PID_DIR:-/tmp}"

print_step() {
    echo -e "${BLUE}📋 $1${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

check_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        print_error "$1 not found. Please install $1 first."
        exit 1
    fi
}

cleanup_started_port_forwards() {
    if ((${#STARTED_PIDS[@]} == 0)); then
        return
    fi

    print_warning "Cleaning up port-forwards started by this run..."
    for pid in "${STARTED_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done

    for label in "${STARTED_LABELS[@]}"; do
        rm -f "$(pid_file_for_label "$label")"
    done
}

pid_file_for_label() {
    local label="$1"

    echo "${PID_DIR}/liveedgecast-${label}-port-forward.pid"
}

stop_tracked_port_forward() {
    local label="$1"
    local pid_file
    pid_file="$(pid_file_for_label "$label")"

    if [[ ! -f "$pid_file" ]]; then
        return
    fi

    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    rm -f "$pid_file"

    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        print_warning "Stopped tracked ${label} port-forward (pid ${pid})"
    fi
}

stop_existing_port_forward() {
    local namespace="$1"
    local resource="$2"
    local local_port="$3"
    local remote_port="$4"
    local pattern="kubectl.*-n ${namespace}.*port-forward ${resource}.*${local_port}:${remote_port}"

    if pkill -f "$pattern" 2>/dev/null; then
        print_warning "Stopped existing kubectl port-forward for ${namespace}/${resource} ${local_port}:${remote_port}"
    fi
}

local_port_is_open() {
    local local_port="$1"

    (echo >"/dev/tcp/127.0.0.1/${local_port}") >/dev/null 2>&1
}

port_forward_log_has_failure() {
    local log_file="$1"

    grep -Eiq "address already in use|unable to listen|error|failed" "$log_file" 2>/dev/null
}

wait_for_port_forward() {
    local pid="$1"
    local log_file="$2"
    local label="$3"
    local local_port="$4"

    for _ in {1..20}; do
        if grep -q "Forwarding from" "$log_file" 2>/dev/null; then
            if local_port_is_open "$local_port"; then
                return 0
            fi
            print_error "${label} port-forward reported ready, but localhost:${local_port} is not accepting connections. See ${log_file}."
            cat "$log_file" 2>/dev/null || true
            return 1
        fi

        if port_forward_log_has_failure "$log_file"; then
            print_error "${label} port-forward failed before becoming ready. See ${log_file}."
            cat "$log_file" 2>/dev/null || true
            return 1
        fi

        if ! kill -0 "$pid" 2>/dev/null; then
            print_error "${label} port-forward exited before becoming ready. See ${log_file}."
            cat "$log_file" 2>/dev/null || true
            return 1
        fi

        sleep 0.5
    done

    print_error "Timed out waiting for ${label} port-forward readiness on localhost:${local_port}. See ${log_file}."
    cat "$log_file" 2>/dev/null || true
    return 1
}

start_port_forward() {
    local namespace="$1"
    local resource="$2"
    local local_port="$3"
    local remote_port="$4"
    local label="$5"
    local log_file="/tmp/liveedgecast-${label}-port-forward.log"
    local pid_file
    pid_file="$(pid_file_for_label "$label")"

    mkdir -p "$PID_DIR"
    : >"$log_file"
    nohup kubectl -n "$namespace" port-forward "$resource" "${local_port}:${remote_port}" >"$log_file" 2>&1 &
    local pid=$!
    STARTED_PIDS+=("$pid")
    STARTED_LABELS+=("$label")
    echo "$pid" >"$pid_file"

    wait_for_port_forward "$pid" "$log_file" "$label" "$local_port"
    print_success "${label}: localhost:${local_port} -> ${namespace}/${resource}:${remote_port} (pid ${pid}, log ${log_file})"
}

prometheus_service_port() {
    local service_name="$1"

    kubectl -n monitoring get "svc/${service_name}" \
        -o jsonpath='{range .spec.ports[*]}{.port}{"\t"}{.name}{"\t"}{.targetPort}{"\n"}{end}' \
        2>/dev/null \
        | awk '
            $1 == "9090" { print $1; found = 1; exit }
            !found && ($2 == "web" || $2 == "http-web") { candidate = $1 }
            !found && candidate == "" && $3 == "9090" { candidate = $1 }
            END { if (!found && candidate != "") print candidate; else if (!found) exit 1 }
        '
}

service_exposes_prometheus_port() {
    local service_name="$1"

    prometheus_service_port "$service_name" >/dev/null
}

list_prometheus_service_candidates() {
    kubectl -n monitoring get svc \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.app\.kubernetes\.io/name}{"\t"}{range .spec.ports[*]}{.port}{":"}{.name}{":"}{.targetPort}{","}{end}{"\n"}{end}' \
        2>/dev/null \
        | awk -F '\t' '
            function prometheus_service_port(ports, entries, count, i, parts, fallback) {
                count = split(ports, entries, ",")
                for (i = 1; i <= count; i++) {
                    if (entries[i] == "") continue
                    split(entries[i], parts, ":")
                    if (parts[1] == "9090") return parts[1]
                    if (fallback == "" && (parts[2] == "web" || parts[2] == "http-web")) fallback = parts[1]
                    if (fallback == "" && parts[3] == "9090") fallback = parts[1]
                }
                return fallback
            }
            ($2 == "prometheus" || $1 == "prometheus-operated" || $1 ~ /(^|-)prometheus$/) {
                port = prometheus_service_port($3)
                if (port != "") print $1 "\t" port
            }
        '
}

resolve_prometheus_service() {
    local service_port

    if [[ -n "$PROMETHEUS_SERVICE" ]]; then
        kubectl -n monitoring get "svc/${PROMETHEUS_SERVICE}" >/dev/null
        service_port="$(prometheus_service_port "$PROMETHEUS_SERVICE" || true)"
        if [[ -z "$service_port" ]]; then
            print_error "Configured Prometheus service svc/${PROMETHEUS_SERVICE} does not expose port 9090/web." >&2
            exit 1
        fi
        PROMETHEUS_REMOTE_PORT="$service_port"
        echo "$PROMETHEUS_SERVICE"
        return
    fi

    if kubectl -n monitoring get "svc/${DEFAULT_PROMETHEUS_SERVICE}" >/dev/null 2>&1; then
        service_port="$(prometheus_service_port "$DEFAULT_PROMETHEUS_SERVICE" || true)"
        if [[ -n "$service_port" ]]; then
            PROMETHEUS_SERVICE="$DEFAULT_PROMETHEUS_SERVICE"
            PROMETHEUS_REMOTE_PORT="$service_port"
            echo "$PROMETHEUS_SERVICE"
            return
        fi
        print_warning "Default service svc/${DEFAULT_PROMETHEUS_SERVICE} exists but does not expose port 9090/web; trying discovery." >&2
    fi

    local candidates=()
    while IFS=$'\t' read -r candidate service_port; do
        [[ -n "$candidate" && -n "$service_port" ]] && candidates+=("${candidate}:${service_port}")
    done < <(list_prometheus_service_candidates | sort -u)

    case "${#candidates[@]}" in
        0)
            print_error "Prometheus service was not found in namespace monitoring." >&2
            echo "Expected a Service with label app.kubernetes.io/name=prometheus or a Prometheus-like name that exposes port 9090/web." >&2
            echo "Available services in namespace monitoring:" >&2
            kubectl -n monitoring get svc >&2 2>/dev/null || true
            exit 1
            ;;
        1)
            PROMETHEUS_SERVICE="${candidates[0]%:*}"
            PROMETHEUS_REMOTE_PORT="${candidates[0]##*:}"
            print_warning "Using discovered Prometheus service svc/${PROMETHEUS_SERVICE}:${PROMETHEUS_REMOTE_PORT}. Set PROMETHEUS_SERVICE to override." >&2
            echo "$PROMETHEUS_SERVICE"
            ;;
        *)
            print_error "Multiple Prometheus service candidates found: ${candidates[*]}" >&2
            echo "Set PROMETHEUS_SERVICE=<service-name> to choose one explicitly." >&2
            exit 1
            ;;
    esac
}

main() {
    check_command kubectl

    if ! kubectl cluster-info >/dev/null 2>&1; then
        print_error "Cannot connect to Kubernetes cluster."
        kubectl config current-context 2>/dev/null || true
        exit 1
    fi

    print_step "Validating required Services..."
    kubectl -n media get svc/proxy >/dev/null
    resolve_prometheus_service >/dev/null

    trap cleanup_started_port_forwards ERR
    trap 'cleanup_started_port_forwards; exit 130' INT
    trap 'cleanup_started_port_forwards; exit 143' TERM

    print_step "Stopping existing LiveEdgeCast port-forwards..."
    stop_tracked_port_forward rtmp
    stop_tracked_port_forward proxy-http
    stop_tracked_port_forward prometheus
    stop_existing_port_forward media svc/proxy 1935 1935
    stop_existing_port_forward media svc/proxy 8080 8080
    stop_existing_port_forward monitoring "svc/${PROMETHEUS_SERVICE}" 9090 "$PROMETHEUS_REMOTE_PORT"

    print_step "Starting LiveEdgeCast port-forwards..."
    start_port_forward media svc/proxy 1935 1935 rtmp
    start_port_forward media svc/proxy 8080 8080 proxy-http
    start_port_forward monitoring "svc/${PROMETHEUS_SERVICE}" 9090 "$PROMETHEUS_REMOTE_PORT" prometheus

    trap - ERR INT TERM
    print_success "Port-forwards configured. Open Prometheus at http://localhost:9090."
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
