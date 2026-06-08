#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=../tools/port-forward.sh
source "${REPO_ROOT}/tools/port-forward.sh"

TESTS_RUN=0
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

reset_mock_state() {
    PROMETHEUS_SERVICE=""
    PROMETHEUS_REMOTE_PORT="9090"
    MOCK_SERVICE_LIST=""
    MOCK_KNOWN_SERVICES=""
    MOCK_PORTS_kube_prometheus_stack_prometheus=""
    MOCK_PORTS_custom_prometheus=""
    MOCK_PORTS_bad_prometheus=""
    MOCK_PORTS_prometheus_operated=""
    MOCK_PORTS_other_prometheus=""
    MOCK_PORTS_custom_web_prometheus=""
}

mock_service_known() {
    local service_name="$1"

    [[ " ${MOCK_KNOWN_SERVICES} " == *" ${service_name} "* ]]
}

mock_service_ports() {
    local service_name="$1"
    local variable_name
    variable_name="MOCK_PORTS_${service_name//-/_}"

    printf '%b' "${!variable_name:-}"
}

kubectl() {
    local args="$*"
    local service_name=""

    if [[ "$args" == *" get svc "* && "$args" == *"-o jsonpath"* ]]; then
        printf '%b' "$MOCK_SERVICE_LIST"
        return 0
    fi

    for arg in "$@"; do
        if [[ "$arg" == svc/* ]]; then
            service_name="${arg#svc/}"
            break
        fi
    done

    if [[ -n "$service_name" ]]; then
        if ! mock_service_known "$service_name"; then
            return 1
        fi

        if [[ "$args" == *"-o jsonpath"* ]]; then
            mock_service_ports "$service_name"
        fi
        return 0
    fi

    return 0
}

assert_equals() {
    local expected="$1"
    local actual="$2"
    local message="$3"

    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$actual" != "$expected" ]]; then
        printf 'FAIL: %s\n  expected: %s\n  actual:   %s\n' "$message" "$expected" "$actual" >&2
        exit 1
    fi
    printf 'ok: %s\n' "$message"
}

assert_success() {
    local message="$1"
    shift

    TESTS_RUN=$((TESTS_RUN + 1))
    if ! "$@"; then
        printf 'FAIL: %s\n' "$message" >&2
        exit 1
    fi
    printf 'ok: %s\n' "$message"
}

assert_failure() {
    local message="$1"
    shift

    TESTS_RUN=$((TESTS_RUN + 1))
    if ("$@"); then
        printf 'FAIL: %s\n  command succeeded unexpectedly\n' "$message" >&2
        exit 1
    fi
    printf 'ok: %s\n' "$message"
}

run_resolve() {
    resolve_prometheus_service >"${TMP_DIR}/resolve.out" 2>"${TMP_DIR}/resolve.err"
}

reset_mock_state
MOCK_KNOWN_SERVICES="kube-prometheus-stack-prometheus"
MOCK_PORTS_kube_prometheus_stack_prometheus=$'9090\tweb\tweb\n'
run_resolve
assert_equals "kube-prometheus-stack-prometheus" "$(cat "${TMP_DIR}/resolve.out")" "prefers valid default Prometheus service"
assert_equals "kube-prometheus-stack-prometheus" "$PROMETHEUS_SERVICE" "default resolution preserves service for caller"
assert_equals "9090" "$PROMETHEUS_REMOTE_PORT" "default resolution preserves remote port for caller"

reset_mock_state
PROMETHEUS_SERVICE="custom-prometheus"
MOCK_KNOWN_SERVICES="custom-prometheus kube-prometheus-stack-prometheus"
MOCK_PORTS_custom_prometheus=$'9090\tweb\t9090\n'
MOCK_PORTS_kube_prometheus_stack_prometheus=$'9090\tweb\tweb\n'
run_resolve
assert_equals "custom-prometheus" "$(cat "${TMP_DIR}/resolve.out")" "uses explicit PROMETHEUS_SERVICE override"
assert_equals "9090" "$PROMETHEUS_REMOTE_PORT" "override with service port 9090 keeps remote port 9090"

reset_mock_state
PROMETHEUS_SERVICE="custom-web-prometheus"
MOCK_KNOWN_SERVICES="custom-web-prometheus"
MOCK_PORTS_custom_web_prometheus=$'80\tweb\t9090\n'
run_resolve
assert_equals "custom-web-prometheus" "$(cat "${TMP_DIR}/resolve.out")" "uses override service with named web port"
assert_equals "80" "$PROMETHEUS_REMOTE_PORT" "override with web service port uses actual service port"

reset_mock_state
PROMETHEUS_SERVICE="bad-prometheus"
MOCK_KNOWN_SERVICES="bad-prometheus"
MOCK_PORTS_bad_prometheus=$'80\thttp\t80\n'
assert_failure "rejects override service without Prometheus port" run_resolve

reset_mock_state
MOCK_KNOWN_SERVICES="custom-prometheus"
MOCK_SERVICE_LIST=$'prometheus-grafana\tgrafana\t80:http:80,\ncustom-prometheus\tprometheus\t9090:web:web,\n'
run_resolve
assert_equals "custom-prometheus" "$(cat "${TMP_DIR}/resolve.out")" "discovers only Prometheus server candidates with 9090/web port"
assert_equals "9090" "$PROMETHEUS_REMOTE_PORT" "discovered service with service port 9090 keeps remote port 9090"

reset_mock_state
MOCK_KNOWN_SERVICES="custom-web-prometheus"
MOCK_SERVICE_LIST=$'custom-web-prometheus\tprometheus\t80:web:9090,\n'
run_resolve
assert_equals "custom-web-prometheus" "$(cat "${TMP_DIR}/resolve.out")" "discovers Prometheus server candidate with web service port"
assert_equals "custom-web-prometheus" "$PROMETHEUS_SERVICE" "discovery preserves selected service for caller"
assert_equals "80" "$PROMETHEUS_REMOTE_PORT" "discovered web service port uses actual service port"

reset_mock_state
MOCK_KNOWN_SERVICES="prometheus-operated other-prometheus"
MOCK_SERVICE_LIST=$'prometheus-operated\tprometheus\t9090:web:web,\nother-prometheus\tprometheus\t9090:web:web,\n'
assert_failure "fails when Prometheus service discovery is ambiguous" run_resolve

reset_mock_state
MOCK_KNOWN_SERVICES="kube-prometheus-stack-prometheus"
MOCK_PORTS_kube_prometheus_stack_prometheus=$'9090\tweb\tweb\n'
assert_success "service_exposes_prometheus_port accepts 9090/web service" service_exposes_prometheus_port kube-prometheus-stack-prometheus
assert_equals "9090" "$(prometheus_service_port kube-prometheus-stack-prometheus)" "prometheus_service_port returns service port 9090"

reset_mock_state
MOCK_KNOWN_SERVICES="custom-web-prometheus"
MOCK_PORTS_custom_web_prometheus=$'80\tweb\t9090\n'
assert_success "service_exposes_prometheus_port accepts web service port" service_exposes_prometheus_port custom-web-prometheus
assert_equals "80" "$(prometheus_service_port custom-web-prometheus)" "prometheus_service_port returns actual web service port"

reset_mock_state
MOCK_KNOWN_SERVICES="bad-prometheus"
MOCK_PORTS_bad_prometheus=$'80\thttp\t80\n'
assert_failure "service_exposes_prometheus_port rejects non-Prometheus service" service_exposes_prometheus_port bad-prometheus

printf 'All %s port-forward helper tests passed.\n' "$TESTS_RUN"
