#!/usr/bin/env bash
set -euo pipefail

# LiveEdgeCast stress campaign runner for up to 30 simultaneous RTMP streams.
# Profile: YouTube-like 1080p30, H.264 10 Mbps video + AAC 128 kbps audio.
#
# Important: this script can manage the local port-forwards through ./tools/port-forward.sh.
# Use MANAGE_PORT_FORWARD=true to enable automatic restart/readiness checks.
# Set TEE_RTMP_URLS to a comma-separated list of extra RTMP base URLs when the same
# encoded stream should be mirrored to multiple destinations through ffmpeg -f tee.

PYTHON_BIN="${PYTHON_BIN:-python3}"
KUBECTL_BIN="${KUBECTL_BIN:-kubectl}"
CURL_BIN="${CURL_BIN:-curl}"
JQ_BIN="${JQ_BIN:-jq}"

NAMESPACE="${NAMESPACE:-media}"
PROMETHEUS_URL="${PROMETHEUS_URL:-${PROM_URL:-}}"
CONTROLLER_URL="${CONTROLLER_URL:-http://127.0.0.1:8000}"
RTMP_URL="${RTMP_URL:-${LIVEEDGECAST_RTMP_URL:-rtmp://127.0.0.1:1935/live}}"
SECONDARY_RTMP_URL="${SECONDARY_RTMP_URL:-}"
TEE_RTMP_URLS="${TEE_RTMP_URLS:-${LIVEEDGECAST_TEE_RTMP_URLS:-}}"

TESTSRC_SIZE="${TESTSRC_SIZE:-1920x1080}"
TESTSRC_RATE="${TESTSRC_RATE:-30}"
BITRATE="${BITRATE:-10000k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
CONSTANT_BITRATE="${CONSTANT_BITRATE:-true}"

BASE_OUTPUT_DIR="${BASE_OUTPUT_DIR:-./reports-stress/$(date +%Y%m%d-%H%M%S)}"

REQUIRE_DESTINATION_RECEIVED="${REQUIRE_DESTINATION_RECEIVED:-false}"
ALLOW_PARTIAL="${ALLOW_PARTIAL:-true}"
ALLOW_INCONCLUSIVE="${ALLOW_INCONCLUSIVE:-true}"
ALLOW_RESTORE_FAILURE="${ALLOW_RESTORE_FAILURE:-false}"
ALLOW_UNSCOPED_CONTEXT="${ALLOW_UNSCOPED_CONTEXT:-false}"
ALLOW_WORKER_CLEANUP="${ALLOW_WORKER_CLEANUP:-false}"
PATCH_PROXY_CONTEXT="${PATCH_PROXY_CONTEXT:-true}"
REQUIRE_PROMETHEUS_ANALYSIS="${REQUIRE_PROMETHEUS_ANALYSIS:-false}"

REQUIRE_MIN_DURATION="${REQUIRE_MIN_DURATION:-true}"
CONTINUE_ON_SHORT_RUN="${CONTINUE_ON_SHORT_RUN:-false}"
MIN_LEVEL_DURATION_RATIO="${MIN_LEVEL_DURATION_RATIO:-0.70}"

DRY_RUN="${DRY_RUN:-false}"
SKIP_PREFLIGHT="${SKIP_PREFLIGHT:-false}"
WAIT_TARGETS_SECONDS="${WAIT_TARGETS_SECONDS:-120}"
WAIT_TARGETS_INTERVAL_SECONDS="${WAIT_TARGETS_INTERVAL_SECONDS:-5}"
WAIT_AFTER_LEVEL_SECONDS="${WAIT_AFTER_LEVEL_SECONDS:-10}"

# Port-forward management. These variables were previously being passed by the caller but not used.
MANAGE_PORT_FORWARD="${MANAGE_PORT_FORWARD:-false}"
PORT_FORWARD_SCRIPT="${PORT_FORWARD_SCRIPT:-./tools/port-forward.sh}"
PORT_FORWARD_WATCHDOG="${PORT_FORWARD_WATCHDOG:-false}"
PORT_FORWARD_RESTART_BEFORE_PREFLIGHT="${PORT_FORWARD_RESTART_BEFORE_PREFLIGHT:-false}"
PORT_FORWARD_RESTART_BEFORE_LEVEL="${PORT_FORWARD_RESTART_BEFORE_LEVEL:-false}"
PORT_FORWARD_RESTART_AFTER_LEVEL="${PORT_FORWARD_RESTART_AFTER_LEVEL:-false}"
PORT_FORWARD_VERIFY_TIMEOUT_SECONDS="${PORT_FORWARD_VERIFY_TIMEOUT_SECONDS:-90}"
PORT_FORWARD_VERIFY_INTERVAL_SECONDS="${PORT_FORWARD_VERIFY_INTERVAL_SECONDS:-2}"
PORT_FORWARD_RESTART_COOLDOWN_SECONDS="${PORT_FORWARD_RESTART_COOLDOWN_SECONDS:-2}"
UNIQUE_KEYS_PER_REPETITION="${UNIQUE_KEYS_PER_REPETITION:-true}"

mkdir -p "$BASE_OUTPUT_DIR/keys" "$BASE_OUTPUT_DIR/logs"
CAMPAIGN_LOG="$BASE_OUTPUT_DIR/stress-campaign.log"

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*" | tee -a "$CAMPAIGN_LOG"
}

fail() {
  log "ERROR: $*"
  maybe_dump_port_forward_diagnostics
  exit 1
}

bool_true() {
  case "${1:-}" in
    true|TRUE|1|yes|YES|y|Y) return 0 ;;
    *) return 1 ;;
  esac
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

local_port_from_url() {
  "$PYTHON_BIN" - "$1" <<'PY'
from urllib.parse import urlparse
import sys
u = urlparse(sys.argv[1])
if u.port:
    print(u.port)
elif u.scheme == "https":
    print(443)
elif u.scheme == "http":
    print(80)
else:
    print("")
PY
}

local_port_is_open() {
  local port="$1"
  (echo >/dev/tcp/127.0.0.1/"$port") >/dev/null 2>&1
}

rtmp_local_port() {
  "$PYTHON_BIN" - "$RTMP_URL" <<'PY'
from urllib.parse import urlparse
import sys
u = urlparse(sys.argv[1])
print(u.port or 1935)
PY
}

port_forward_script_ok() {
  if ! bool_true "$MANAGE_PORT_FORWARD"; then
    return 1
  fi
  if [[ ! -f "$PORT_FORWARD_SCRIPT" ]]; then
    log "Port-forward management requested, but script not found: $PORT_FORWARD_SCRIPT"
    return 1
  fi
  if [[ ! -x "$PORT_FORWARD_SCRIPT" ]]; then
    log "Making port-forward script executable: $PORT_FORWARD_SCRIPT"
    chmod +x "$PORT_FORWARD_SCRIPT"
  fi
  return 0
}

wait_for_local_endpoints() {
  local deadline=$(( $(date +%s) + PORT_FORWARD_VERIFY_TIMEOUT_SECONDS ))
  local controller_ok="false" prometheus_ok="false" rtmp_ok="false"
  local rtmp_port
  rtmp_port="$(rtmp_local_port)"

  log "Waiting for local port-forward endpoints for up to ${PORT_FORWARD_VERIFY_TIMEOUT_SECONDS}s..."

  while [[ "$(date +%s)" -le "$deadline" ]]; do
    if "$CURL_BIN" -fsS "${CONTROLLER_URL%/}/health" >/dev/null 2>&1; then
      controller_ok="true"
    else
      controller_ok="false"
    fi

    if [[ -z "$PROMETHEUS_URL" ]]; then
      prometheus_ok="skipped"
    elif "$CURL_BIN" -fsS "${PROMETHEUS_URL%/}/-/ready" >/dev/null 2>&1; then
      prometheus_ok="true"
    else
      prometheus_ok="false"
    fi

    if [[ -n "$rtmp_port" ]] && local_port_is_open "$rtmp_port"; then
      rtmp_ok="true"
    else
      rtmp_ok="false"
    fi

    if [[ "$controller_ok" == "true" && ( "$prometheus_ok" == "true" || "$prometheus_ok" == "skipped" ) && "$rtmp_ok" == "true" ]]; then
      log "Local endpoints ready: controller=${controller_ok} prometheus=${prometheus_ok} rtmp=${rtmp_ok}"
      return 0
    fi

    log "Local endpoints not ready: controller=${controller_ok} prometheus=${prometheus_ok} rtmp=${rtmp_ok}; sleeping ${PORT_FORWARD_VERIFY_INTERVAL_SECONDS}s"
    sleep "$PORT_FORWARD_VERIFY_INTERVAL_SECONDS"
  done

  log "Local endpoint readiness failed: controller=${controller_ok} prometheus=${prometheus_ok} rtmp=${rtmp_ok}"
  return 1
}

manage_port_forward() {
  local action="${1:-restart}"

  if ! bool_true "$MANAGE_PORT_FORWARD"; then
    return 0
  fi

  if ! port_forward_script_ok; then
    fail "MANAGE_PORT_FORWARD=true but PORT_FORWARD_SCRIPT is invalid: $PORT_FORWARD_SCRIPT"
  fi

  log "Managing port-forward: $PORT_FORWARD_SCRIPT $action"
  set +e
  "$PORT_FORWARD_SCRIPT" "$action" 2>&1 | tee -a "$CAMPAIGN_LOG"
  local rc=${PIPESTATUS[0]}
  set -e

  if [[ "$rc" -ne 0 ]]; then
    fail "port-forward script failed with rc=${rc}: $PORT_FORWARD_SCRIPT $action"
  fi

  sleep "$PORT_FORWARD_RESTART_COOLDOWN_SECONDS"
  wait_for_local_endpoints || fail "port-forward endpoints did not become ready after: $PORT_FORWARD_SCRIPT $action"
}

ensure_port_forward_ready() {
  if ! bool_true "$MANAGE_PORT_FORWARD"; then
    return 0
  fi

  if wait_for_local_endpoints; then
    return 0
  fi

  if bool_true "$PORT_FORWARD_WATCHDOG"; then
    log "Port-forward watchdog detected unavailable endpoint; restarting port-forwards."
    manage_port_forward restart
  else
    fail "Port-forward endpoint unavailable and PORT_FORWARD_WATCHDOG=false."
  fi
}

maybe_dump_port_forward_diagnostics() {
  if ! bool_true "${MANAGE_PORT_FORWARD:-false}"; then
    return 0
  fi
  if [[ -x "${PORT_FORWARD_SCRIPT:-}" ]]; then
    log "Port-forward status:"
    "$PORT_FORWARD_SCRIPT" status 2>&1 | tee -a "$CAMPAIGN_LOG" || true
    log "Port-forward logs:"
    "$PORT_FORWARD_SCRIPT" logs 2>&1 | tee -a "$CAMPAIGN_LOG" || true
  fi
}

make_keys() {
  local count="$1"
  local prefix="$2"
  local repetition_seed="$3"
  local file="$BASE_OUTPUT_DIR/keys/${prefix}-${count}-${repetition_seed}.txt"

  : > "$file"
  for i in $(seq 1 "$count"); do
    printf "%s-%02d-%s-%s\n" "$prefix" "$i" "$repetition_seed" "$(date +%s%N)" >> "$file"
  done

  echo "$file"
}

prom_query_json() {
  local query="$1"
  "$CURL_BIN" -fsS -G "${PROMETHEUS_URL%/}/api/v1/query" --data-urlencode "query=$query"
}

prom_query_len() {
  local query="$1"
  prom_query_json "$query" | "$JQ_BIN" '.data.result | length'
}

wait_for_media_targets() {
  local deadline=$(( $(date +%s) + WAIT_TARGETS_SECONDS ))
  local controller_count=0 proxy_count=0

  log "Waiting for Prometheus targets in namespace '$NAMESPACE' for up to ${WAIT_TARGETS_SECONDS}s..."

  while [[ "$(date +%s)" -le "$deadline" ]]; do
    controller_count=$("$CURL_BIN" -fsS "${PROMETHEUS_URL%/}/api/v1/targets" \
      | "$JQ_BIN" '[.data.activeTargets[] | select(.labels.namespace == "'"$NAMESPACE"'" and .labels.job == "controller" and .health == "up")] | length' 2>/dev/null || echo 0)
    proxy_count=$("$CURL_BIN" -fsS "${PROMETHEUS_URL%/}/api/v1/targets" \
      | "$JQ_BIN" '[.data.activeTargets[] | select(.labels.namespace == "'"$NAMESPACE"'" and .labels.job == "proxy" and .health == "up")] | length' 2>/dev/null || echo 0)

    if [[ "${controller_count:-0}" -ge 1 && "${proxy_count:-0}" -ge 1 ]]; then
      log "Prometheus targets ready: controller=$controller_count proxy=$proxy_count"
      return 0
    fi

    log "Prometheus targets not ready yet: controller=${controller_count:-0} proxy=${proxy_count:-0}; sleeping ${WAIT_TARGETS_INTERVAL_SECONDS}s"
    sleep "$WAIT_TARGETS_INTERVAL_SECONDS"
  done

  "$CURL_BIN" -s "${PROMETHEUS_URL%/}/api/v1/targets" \
    | "$JQ_BIN" '.data.activeTargets[] | select(.labels.namespace == "'"$NAMESPACE"'") | {job:.labels.job, service:.labels.service, pod:.labels.pod, endpoint:.labels.endpoint, health, lastError}' \
    | tee -a "$CAMPAIGN_LOG" || true

  fail "Prometheus did not expose healthy controller/proxy targets for namespace '$NAMESPACE'."
}

preflight() {
  log "Running preflight checks..."

  have_cmd "$PYTHON_BIN" || fail "PYTHON_BIN not found: $PYTHON_BIN"
  have_cmd "$KUBECTL_BIN" || fail "kubectl not found: $KUBECTL_BIN"
  have_cmd "$CURL_BIN" || fail "curl not found: $CURL_BIN"
  have_cmd "$JQ_BIN" || fail "jq not found: $JQ_BIN"
  have_cmd ffmpeg || fail "ffmpeg not found in PATH"

  [[ -f tools/experiments/run_experiment.py ]] || fail "tools/experiments/run_experiment.py not found. Run from project root."

  "$PYTHON_BIN" --version | tee -a "$CAMPAIGN_LOG"
  ffmpeg -version | head -1 | tee -a "$CAMPAIGN_LOG"

  log "Checking Kubernetes namespace and pods..."
  "$KUBECTL_BIN" get namespace "$NAMESPACE" >/dev/null
  "$KUBECTL_BIN" -n "$NAMESPACE" get pods -o wide | tee -a "$CAMPAIGN_LOG" || true

  if bool_true "$MANAGE_PORT_FORWARD"; then
    ensure_port_forward_ready
  fi

  log "Checking controller endpoint: ${CONTROLLER_URL%/}/health"
  "$CURL_BIN" -fsS "${CONTROLLER_URL%/}/health" | tee -a "$CAMPAIGN_LOG" || fail "Controller health endpoint failed. Check port-forward svc/controller 8000:8000."
  echo | tee -a "$CAMPAIGN_LOG" >/dev/null

  if [[ -z "$PROMETHEUS_URL" ]]; then
    if bool_true "$REQUIRE_PROMETHEUS_ANALYSIS"; then
      fail "REQUIRE_PROMETHEUS_ANALYSIS=true but PROMETHEUS_URL is empty."
    fi
    log "PROMETHEUS_URL is empty; skipping Prometheus readiness, target, and metric preflight checks."
    log "Preflight passed with reduced observability."
    return 0
  fi

  log "Checking Prometheus readiness: ${PROMETHEUS_URL%/}/-/ready"
  "$CURL_BIN" -fsS "${PROMETHEUS_URL%/}/-/ready" | tee -a "$CAMPAIGN_LOG" || fail "Prometheus not ready. Check port-forward to 9090."
  echo | tee -a "$CAMPAIGN_LOG" >/dev/null

  wait_for_media_targets

  log "Checking required metrics..."
  local controller_samples proxy_samples cpu_samples memory_samples
  controller_samples=$(prom_query_len 'controller_active_streams' || echo 0)
  proxy_samples=$(prom_query_len 'proxy_rtmp_stats_up' || echo 0)
  cpu_samples=$(prom_query_len "sum by (pod) (rate(container_cpu_usage_seconds_total{namespace=\"$NAMESPACE\", container!=\"POD\", pod=~\"(proxy-lb|proxy|worker|controller)-.*\"}[1m]))" || echo 0)
  memory_samples=$(prom_query_len "sum by (pod) (container_memory_working_set_bytes{namespace=\"$NAMESPACE\", container!=\"POD\", pod=~\"(proxy-lb|proxy|worker|controller)-.*\"})" || echo 0)

  log "metric sample counts: controller_active_streams=$controller_samples proxy_rtmp_stats_up=$proxy_samples cpu_by_pod=$cpu_samples memory_by_pod=$memory_samples"

  [[ "${controller_samples:-0}" -ge 1 ]] || fail "controller_active_streams has no samples."
  if [[ "${proxy_samples:-0}" -lt 1 ]]; then
    log "proxy_rtmp_stats_up has no samples; continuing with reduced proxy metrics."
  fi
  if [[ "${memory_samples:-0}" -lt 1 ]]; then
    log "memory by pod has no samples; continuing with reduced infrastructure metrics."
  fi
  if [[ "${cpu_samples:-0}" -lt 1 ]]; then
    log "cpu by pod has no samples; continuing with reduced infrastructure metrics."
  fi

  log "Proxy verification is limited to CPU/memory/controller metrics; network traffic metrics are not required in simplified mode."

  log "Preflight passed."
}

build_flags() {
  local flags=()
  bool_true "$PATCH_PROXY_CONTEXT" && flags+=(--patch-proxy-context)
  bool_true "$REQUIRE_PROMETHEUS_ANALYSIS" && flags+=(--require-prometheus-analysis)
  bool_true "$REQUIRE_DESTINATION_RECEIVED" && flags+=(--require-destination-received)
  bool_true "$ALLOW_PARTIAL" && flags+=(--allow-partial)
  bool_true "$ALLOW_INCONCLUSIVE" && flags+=(--allow-inconclusive)
  bool_true "$ALLOW_RESTORE_FAILURE" && flags+=(--allow-restore-failure)
  bool_true "$ALLOW_UNSCOPED_CONTEXT" && flags+=(--allow-unscoped-context)
  bool_true "$CONSTANT_BITRATE" && flags+=(--constant-bitrate)
  [[ -n "$TEE_RTMP_URLS" ]] && flags+=(--tee-rtmp-urls "$TEE_RTMP_URLS")
  bool_true "$ALLOW_WORKER_CLEANUP" && flags+=(--allow-worker-cleanup)
  printf '%s\n' "${flags[@]}"
}

summarize_level() {
  local report_dir="$1"
  if [[ ! -f "$report_dir/report.json" ]]; then
    log "No report.json found for level: $report_dir"
    return 0
  fi

  "$PYTHON_BIN" - "$report_dir/report.json" <<'PY' | tee -a "$CAMPAIGN_LOG" || true
import json, sys
p = sys.argv[1]
data = json.load(open(p))
summary = data.get("summary", {})
metadata = data.get("metadata", {})
activation = data.get("metrics", {}).get("activation", {})
print("--- level summary ---")
for key in ["experiment_id", "scenario", "duration_seconds", "repetitions"]:
    print(f"{key}:", metadata.get(key))
print("stream_keys:", len(metadata.get("stream_keys") or []))
for key in [
    "automation_status",
    "automation_failure_reasons",
    "publisher_success_count",
    "publisher_failure_count",
    "publisher_nonzero_process_count",
    "valid_activation_samples",
    "worker_observed_samples",
    "prometheus_analysis_ready",
    "prometheus_incomplete_metrics",
]:
    print(f"{key}:", summary.get(key))
for k in ("total_activation_seconds_per_stream", "worker_ready_seconds_per_stream", "stream_lifecycle_phase_seconds_p95"):
    row = activation.get(k, {}) or {}
    print(f"{k}: samples={row.get('samples', 0)} p50={row.get('p50')} p95={row.get('p95')} p99={row.get('p99')}")
print("note: controller simplificado; métricas de handover, recovery e lifecycle detalhado podem estar ausentes.")
PY
}

check_elapsed_guard() {
  local stream_count="$1"
  local repetitions="$2"
  local duration="$3"
  local elapsed="$4"
  local expected_min

  expected_min=$("$PYTHON_BIN" - "$repetitions" "$duration" "$MIN_LEVEL_DURATION_RATIO" <<'PY'
import sys
print(int(float(sys.argv[1]) * float(sys.argv[2]) * float(sys.argv[3])))
PY
)

  log "Elapsed guard for ${stream_count} streams: elapsed=${elapsed}s expected_min=${expected_min}s ratio=${MIN_LEVEL_DURATION_RATIO}"

  if bool_true "$REQUIRE_MIN_DURATION" && [[ "$elapsed" -lt "$expected_min" ]]; then
    log "SHORT RUN DETECTED for ${stream_count} streams. This usually means FFmpeg publishers failed early or run_experiment returned before waiting for duration."
    if bool_true "$CONTINUE_ON_SHORT_RUN"; then
      log "CONTINUE_ON_SHORT_RUN=true; continuing campaign despite short run."
      return 0
    fi
    fail "Level ${stream_count} streams finished too quickly (${elapsed}s < ${expected_min}s). Inspect $BASE_OUTPUT_DIR logs/reports before continuing."
  fi
}

run_experiment_once() {
  local stream_count="$1"
  local repetition_index="$2"
  local duration="$3"
  local scenario="$4"
  local timestamp experiment_id report_dir key_file

  timestamp="$(date +%Y%m%d-%H%M%S)"
  if bool_true "$UNIQUE_KEYS_PER_REPETITION"; then
    experiment_id="stress-${stream_count}streams-r${repetition_index}-${timestamp}"
    key_file="$(make_keys "$stream_count" "stress-${stream_count}streams-r${repetition_index}" "$timestamp")"
  else
    experiment_id="stress-${stream_count}streams-${timestamp}"
    key_file="$(make_keys "$stream_count" "stress-${stream_count}streams" "$timestamp")"
  fi
  report_dir="$BASE_OUTPUT_DIR/$experiment_id"
  mkdir -p "$report_dir"

  log "Experiment: $experiment_id"
  log "Report dir:  $report_dir"
  log "Keys file:   $key_file"

  local flags=()
  while IFS= read -r flag; do
    [[ -n "$flag" ]] && flags+=("$flag")
  done < <(build_flags)

  local cmd=(
    "$PYTHON_BIN" tools/experiments/run_experiment.py
    --scenario "$scenario"
    --stream-keys-file "$key_file"
    --duration-seconds "$duration"
    --repetitions 1
    --cooldown-seconds 0
    --startup-interval-seconds 0
    --controller-url "$CONTROLLER_URL"
    --namespace "$NAMESPACE"
    --rtmp-url "$RTMP_URL"
    --output-dir "$report_dir"
    --experiment-id "$experiment_id"
    --run-id "stress-${stream_count}streams-r${repetition_index}"
    --bitrate "$BITRATE"
    --testsrc-size "$TESTSRC_SIZE"
    --testsrc-rate "$TESTSRC_RATE"
    --audio-bitrate "$AUDIO_BITRATE"
    "${flags[@]}"
    --overwrite
  )

  if [[ -n "$PROMETHEUS_URL" ]]; then
    cmd+=(--prometheus-url "$PROMETHEUS_URL")
  fi
  if [[ -n "$SECONDARY_RTMP_URL" ]]; then
    cmd+=(--secondary-rtmp-url "$SECONDARY_RTMP_URL")
  fi

  printf '%q ' "${cmd[@]}" > "$report_dir.command.txt"
  echo >> "$report_dir.command.txt"
  cat "$report_dir.command.txt" | tee -a "$CAMPAIGN_LOG"

  if bool_true "$DRY_RUN"; then
    log "DRY_RUN=true; not executing ${stream_count} streams repetition ${repetition_index}."
    return 0
  fi

  local start end elapsed rc
  start=$(date +%s)
  set +e
  "${cmd[@]}" 2>&1 | tee -a "$CAMPAIGN_LOG"
  rc=${PIPESTATUS[0]}
  set -e
  end=$(date +%s)
  elapsed=$(( end - start ))

  log "Experiment ${experiment_id} finished with rc=$rc elapsed=${elapsed}s"
  summarize_level "$report_dir"

  if [[ "$rc" -ne 0 ]]; then
    fail "run_experiment.py failed for ${stream_count} streams repetition ${repetition_index} with rc=$rc. See $CAMPAIGN_LOG"
  fi

  check_elapsed_guard "$stream_count" 1 "$duration" "$elapsed"
}

run_stress_level() {
  local stream_count="$1"
  local repetitions="$2"
  local duration="$3"
  local cooldown="$4"
  local scenario="concurrency"

  log "================================================================================"
  log "Starting stress level: streams=$stream_count repetitions=$repetitions duration=${duration}s cooldown=${cooldown}s unique_keys_per_repetition=${UNIQUE_KEYS_PER_REPETITION}"
  log "================================================================================"

  if bool_true "$UNIQUE_KEYS_PER_REPETITION"; then
    local rep
    for rep in $(seq 1 "$repetitions"); do
      if bool_true "$PORT_FORWARD_RESTART_BEFORE_LEVEL"; then
        manage_port_forward restart
      elif bool_true "$PORT_FORWARD_WATCHDOG"; then
        ensure_port_forward_ready
      fi

      run_experiment_once "$stream_count" "$rep" "$duration" "$scenario"

      if bool_true "$PORT_FORWARD_RESTART_AFTER_LEVEL"; then
        manage_port_forward restart
      elif bool_true "$PORT_FORWARD_WATCHDOG"; then
        ensure_port_forward_ready || true
      fi

      if [[ "$rep" -lt "$repetitions" && "$cooldown" -gt 0 ]]; then
        if bool_true "$DRY_RUN"; then
          log "DRY_RUN=true; skipping cooldown after ${stream_count} streams repetition ${rep}: ${cooldown}s"
        else
          log "Cooldown after ${stream_count} streams repetition ${rep}: ${cooldown}s"
          sleep "$cooldown"
        fi
      fi
    done
  else
    # Legacy behavior: a single run_experiment.py call with reused stream keys across repetitions.
    # This is kept for backwards compatibility but is not recommended for scientific stress campaigns,
    # because delayed publish_done callbacks can collide with later repetitions using the same stream key.
    if bool_true "$PORT_FORWARD_RESTART_BEFORE_LEVEL"; then
      manage_port_forward restart
    elif bool_true "$PORT_FORWARD_WATCHDOG"; then
      ensure_port_forward_ready
    fi

    local timestamp experiment_id report_dir key_file
    timestamp="$(date +%Y%m%d-%H%M%S)"
    experiment_id="stress-${stream_count}streams-${timestamp}"
    report_dir="$BASE_OUTPUT_DIR/$experiment_id"
    key_file="$(make_keys "$stream_count" "stress-${stream_count}streams" "$timestamp")"
    mkdir -p "$report_dir"

    log "Experiment: $experiment_id"
    log "Report dir:  $report_dir"
    log "Keys file:   $key_file"

    local flags=()
    while IFS= read -r flag; do
      [[ -n "$flag" ]] && flags+=("$flag")
    done < <(build_flags)

    local cmd=(
      "$PYTHON_BIN" tools/experiments/run_experiment.py
      --scenario "$scenario"
      --stream-keys-file "$key_file"
      --duration-seconds "$duration"
      --repetitions "$repetitions"
      --cooldown-seconds "$cooldown"
      --startup-interval-seconds 0
      --controller-url "$CONTROLLER_URL"
      --namespace "$NAMESPACE"
      --rtmp-url "$RTMP_URL"
      --output-dir "$report_dir"
      --experiment-id "$experiment_id"
      --run-id "stress-${stream_count}streams"
      --bitrate "$BITRATE"
      --testsrc-size "$TESTSRC_SIZE"
      --testsrc-rate "$TESTSRC_RATE"
      --audio-bitrate "$AUDIO_BITRATE"
      "${flags[@]}"
      --overwrite
    )
    if [[ -n "$PROMETHEUS_URL" ]]; then
      cmd+=(--prometheus-url "$PROMETHEUS_URL")
    fi
    if [[ -n "$SECONDARY_RTMP_URL" ]]; then
      cmd+=(--secondary-rtmp-url "$SECONDARY_RTMP_URL")
    fi
    printf '%q ' "${cmd[@]}" > "$report_dir.command.txt"
    echo >> "$report_dir.command.txt"
    cat "$report_dir.command.txt" | tee -a "$CAMPAIGN_LOG"
    if bool_true "$DRY_RUN"; then
      log "DRY_RUN=true; not executing level $stream_count."
      return 0
    fi
    local start end elapsed rc
    start=$(date +%s)
    set +e
    "${cmd[@]}" 2>&1 | tee -a "$CAMPAIGN_LOG"
    rc=${PIPESTATUS[0]}
    set -e
    end=$(date +%s)
    elapsed=$(( end - start ))
    log "Level ${stream_count} streams finished with rc=$rc elapsed=${elapsed}s"
    summarize_level "$report_dir"
    if bool_true "$PORT_FORWARD_RESTART_AFTER_LEVEL"; then
      manage_port_forward restart
    elif bool_true "$PORT_FORWARD_WATCHDOG"; then
      ensure_port_forward_ready || true
    fi
    if [[ "$rc" -ne 0 ]]; then
      fail "run_experiment.py failed for ${stream_count} streams with rc=$rc. See $CAMPAIGN_LOG"
    fi
    check_elapsed_guard "$stream_count" "$repetitions" "$duration" "$elapsed"
  fi

  if [[ "$WAIT_AFTER_LEVEL_SECONDS" -gt 0 ]]; then
    if bool_true "$DRY_RUN"; then
      log "DRY_RUN=true; skipping wait after level ${stream_count}: ${WAIT_AFTER_LEVEL_SECONDS}s"
    else
      log "Waiting ${WAIT_AFTER_LEVEL_SECONDS}s after level ${stream_count}..."
      sleep "$WAIT_AFTER_LEVEL_SECONDS"
    fi
  fi
}

write_campaign_metadata() {
  cat > "$BASE_OUTPUT_DIR/campaign-metadata.txt" <<EOF_META
started_at=$(date -Is)
namespace=$NAMESPACE
prometheus_url=$PROMETHEUS_URL
controller_url=$CONTROLLER_URL
rtmp_url=$RTMP_URL
testsrc_size=$TESTSRC_SIZE
testsrc_rate=$TESTSRC_RATE
bitrate=$BITRATE
audio_bitrate=$AUDIO_BITRATE
constant_bitrate=$CONSTANT_BITRATE
tee_rtmp_urls=$TEE_RTMP_URLS
profile=YouTube-like ${TESTSRC_SIZE}@${TESTSRC_RATE}fps H.264 ${BITRATE} video + AAC ${AUDIO_BITRATE}
require_destination_received=$REQUIRE_DESTINATION_RECEIVED
allow_partial=$ALLOW_PARTIAL
allow_inconclusive=$ALLOW_INCONCLUSIVE
patch_proxy_context=$PATCH_PROXY_CONTEXT
require_min_duration=$REQUIRE_MIN_DURATION
continue_on_short_run=$CONTINUE_ON_SHORT_RUN
min_level_duration_ratio=$MIN_LEVEL_DURATION_RATIO
manage_port_forward=$MANAGE_PORT_FORWARD
port_forward_script=$PORT_FORWARD_SCRIPT
port_forward_watchdog=$PORT_FORWARD_WATCHDOG
port_forward_restart_before_preflight=$PORT_FORWARD_RESTART_BEFORE_PREFLIGHT
port_forward_restart_before_level=$PORT_FORWARD_RESTART_BEFORE_LEVEL
port_forward_restart_after_level=$PORT_FORWARD_RESTART_AFTER_LEVEL
unique_keys_per_repetition=$UNIQUE_KEYS_PER_REPETITION
EOF_META
}

print_estimated_duration() {
  "$PYTHON_BIN" - <<'PY' | tee -a "$CAMPAIGN_LOG"
levels = [
    (1, 3, 30, 30),
    (2, 3, 30, 30),
    (3, 3, 30, 30),
    (5, 3, 30, 30),
    (8, 3, 30, 30),
    (13, 3, 30, 30),
    (21, 3, 30, 30),
    (34, 3, 30, 30),
]
seconds = 0
for streams, reps, duration, cooldown in levels:
    seconds += reps * duration + max(reps - 1, 0) * cooldown
print("=== Planned stress levels ===")
for streams, reps, duration, cooldown in levels:
    print(f"{streams:>2} streams | repetitions={reps:<2} | duration={duration:>3}s | cooldown={cooldown:>3}s")
print(f"Estimated lower-bound wall time: {seconds/60:.1f} minutes ({seconds/3600:.2f} hours), excluding pod startup/cleanup/overhead")
PY
}

main() {
  write_campaign_metadata
  log "Campaign output dir: $BASE_OUTPUT_DIR"
  print_estimated_duration

  if bool_true "$PORT_FORWARD_RESTART_BEFORE_PREFLIGHT"; then
    manage_port_forward restart
  elif bool_true "$MANAGE_PORT_FORWARD"; then
    manage_port_forward start
  fi

  if ! bool_true "$SKIP_PREFLIGHT"; then
    preflight
  else
    log "SKIP_PREFLIGHT=true; skipping preflight checks."
  fi

  run_stress_level 1  3 30 30
  run_stress_level 2  3 30 30
  run_stress_level 3  3 30 30
  run_stress_level 5  3 30 30
  run_stress_level 8  3 30 30
  run_stress_level 13 3 30 30
  run_stress_level 21 3 30 30
  run_stress_level 34 3 30 30

  log "Campaign finished successfully. Output: $BASE_OUTPUT_DIR"
}

main "$@"
