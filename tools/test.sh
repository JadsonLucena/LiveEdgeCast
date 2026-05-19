#!/usr/bin/env bash

set -euo pipefail

NAMESPACE="${NAMESPACE:-media}"
CONTROLLER_DEPLOYMENT="${CONTROLLER_DEPLOYMENT:-controller}"
PROXY_SELECTOR="${PROXY_SELECTOR:-app=proxy}"
WORKER_SELECTOR="${WORKER_SELECTOR:-app=worker}"
STREAM_NAME="${STREAM_NAME:-}"
SINCE="${SINCE:-10m}"
TAIL_LINES="${TAIL_LINES:-120}"
VERBOSE="${VERBOSE:-0}"
MAX_MATCHES="${MAX_MATCHES:-40}"

usage() {
  cat <<USAGE
Usage: tools/test.sh [options]

Options:
  -n, --namespace <name>            Kubernetes namespace (default: ${NAMESPACE})
  -c, --controller <deployment>     Controller deployment name (default: ${CONTROLLER_DEPLOYMENT})
  -p, --proxy-selector <selector>   Label selector for proxy pods (default: ${PROXY_SELECTOR})
  -w, --worker-selector <selector>  Label selector for worker pods (default: ${WORKER_SELECTOR})
  -s, --stream-name <name>          Stream name filter (also accepts STREAM_NAME env)
      --since <duration>            Log window passed to kubectl logs (default: ${SINCE})
      --tail-lines <n>              Tail lines per pod when no filter match (default: ${TAIL_LINES})
      --max-matches <n>             Max filtered lines per section (default: ${MAX_MATCHES})
  -v, --verbose                     Print extra details (yaml/describe full sections)
  -h, --help                        Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--namespace) NAMESPACE="$2"; shift 2 ;;
    -c|--controller) CONTROLLER_DEPLOYMENT="$2"; shift 2 ;;
    -p|--proxy-selector) PROXY_SELECTOR="$2"; shift 2 ;;
    -w|--worker-selector) WORKER_SELECTOR="$2"; shift 2 ;;
    -s|--stream-name) STREAM_NAME="$2"; shift 2 ;;
    --since) SINCE="$2"; shift 2 ;;
    --tail-lines) TAIL_LINES="$2"; shift 2 ;;
    --max-matches) MAX_MATCHES="$2"; shift 2 ;;
    -v|--verbose) VERBOSE="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red() { printf "\033[31m%s\033[0m\n" "$*"; }

check() {
  local title="$1"
  shift
  echo ""
  echo "=== $title ==="
  if "$@"; then
    green "OK: $title"
  else
    red "FAIL: $title"
    return 1
  fi
}

check_warn() {
  local title="$1"
  shift
  echo ""
  echo "=== $title ==="
  if "$@"; then
    green "OK: $title"
    return 0
  fi
  yellow "WARN: $title"
  return 0
}

log_snippet() {
  local pod="$1"
  local pattern="${2:-}"
  if [[ -n "$pattern" ]]; then
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" 2>/dev/null | grep -En "$pattern" || true
  else
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" 2>/dev/null | tail -n "$TAIL_LINES" || true
  fi
}

filter_stream() {
  local pattern="$1"
  grep -Ein "$pattern" | head -n "$MAX_MATCHES" || true
}

to_epoch() {
  local ts="$1"
  date -u -d "$ts" +%s 2>/dev/null || true
}

print_bootstrap_metrics() {
  local ctrl_pod="$1"
  local worker_pod="$2"

  echo ""
  echo "=== Bootstrap timing metrics (best-effort) ==="

  if [[ -z "$ctrl_pod" || -z "$worker_pod" ]]; then
    yellow "WARN: missing controller/worker pod; skipping bootstrap timing"
    return 0
  fi

  local alloc_secs
  alloc_secs="$(kubectl logs -n "$NAMESPACE" "$ctrl_pod" --since="$SINCE" 2>/dev/null \
    | grep -E "Removed stream .* from pending queue \(allocated in [0-9.]+s\)" \
    | tail -n 1 \
    | sed -E 's/.*allocated in ([0-9.]+)s.*/\1/' || true)"
  if [[ -n "$alloc_secs" ]]; then
    echo "metric allocate->streams/started ~= ${alloc_secs}s (controller pending queue timing)"
  else
    yellow "WARN: could not derive allocate->streams/started timing from controller logs"
  fi

  local spawn_ts first_input_ts first_push_ts
  spawn_ts="$(kubectl logs -n "$NAMESPACE" "$worker_pod" --since="$SINCE" 2>/dev/null \
    | grep -E "\[worker_recovery\] Starting FFmpeg" \
    | tail -n 1 \
    | sed -E "s/^\[([^]]+)\].*/\1/" || true)"
  first_input_ts="$(kubectl logs -n "$NAMESPACE" "$worker_pod" --since="$SINCE" 2>/dev/null \
    | grep -E "ffmpeg].*(Input #0|Stream mapping|frame=|video:|audio:)" \
    | head -n 1 \
    | sed -E "s/^\[([^]]+)\].*/\1/" || true)"
  first_push_ts="$(kubectl logs -n "$NAMESPACE" "$worker_pod" --since="$SINCE" 2>/dev/null \
    | grep -E "ffmpeg].*(Output #0|rtmp://a\.rtmp\.youtube\.com|av_interleaved_write_frame|Non-monotonous DTS|frame=)" \
    | head -n 1 \
    | sed -E "s/^\[([^]]+)\].*/\1/" || true)"

  if [[ -n "$spawn_ts" && -n "$first_input_ts" ]]; then
    local spawn_epoch input_epoch
    spawn_epoch="$(to_epoch "$spawn_ts")"
    input_epoch="$(to_epoch "$first_input_ts")"
    if [[ -n "$spawn_epoch" && -n "$input_epoch" ]]; then
      echo "metric ffmpeg_spawn->first_packet ~= $((input_epoch - spawn_epoch))s"
    fi
  else
    yellow "WARN: could not derive ffmpeg spawn->first packet (increase ffmpeg verbosity if needed)"
  fi

  if [[ -n "$first_input_ts" && -n "$first_push_ts" ]]; then
    local input_epoch push_epoch
    input_epoch="$(to_epoch "$first_input_ts")"
    push_epoch="$(to_epoch "$first_push_ts")"
    if [[ -n "$input_epoch" && -n "$push_epoch" ]]; then
      echo "metric first_packet->push_youtube ~= $((push_epoch - input_epoch))s"
    fi
  else
    yellow "WARN: could not derive first packet->push youtube (increase ffmpeg verbosity if needed)"
  fi
}

resolve_ip_owner() {
  local ip="$1"
  if [[ -z "$ip" ]]; then
    return 0
  fi

  local node
  node="$(kubectl get nodes -o wide --no-headers 2>/dev/null | awk -v ip="$ip" '$0 ~ ip {print $1; exit}')"
  if [[ -n "$node" ]]; then
    echo "node/$node"
    return 0
  fi

  local pod
  pod="$(kubectl get pods -A -o wide --no-headers 2>/dev/null | awk -v ip="$ip" '$7==ip {print $1"/"$2; exit}')"
  if [[ -n "$pod" ]]; then
    echo "pod/$pod"
    return 0
  fi

  echo "unknown"
}

echo "Runtime checklist"
echo "- namespace: $NAMESPACE"
echo "- stream_name filter: ${STREAM_NAME:-<none>}"
echo "- logs since: $SINCE"

check "kubectl connectivity" kubectl version --client >/dev/null
check "namespace exists" kubectl get ns "$NAMESPACE" >/dev/null
check "controller deployment exists" kubectl get deploy -n "$NAMESPACE" "$CONTROLLER_DEPLOYMENT" >/dev/null
check "proxy pods present" bash -lc "kubectl get pods -n '$NAMESPACE' -l '$PROXY_SELECTOR' --no-headers | wc -l | awk '{exit !(\$1>0)}'"
check_warn "worker pods present" bash -lc "kubectl get pods -n '$NAMESPACE' -l '$WORKER_SELECTOR' --no-headers | wc -l | awk '{exit !(\$1>0)}'"

echo ""
echo "=== Pod status ==="
kubectl get pods -n "$NAMESPACE" -l "$PROXY_SELECTOR"
kubectl get pods -n "$NAMESPACE" -l "$WORKER_SELECTOR"
kubectl get deploy -n "$NAMESPACE" "$CONTROLLER_DEPLOYMENT"

WORKER_COUNT="$(kubectl get pods -n "$NAMESPACE" -l "$WORKER_SELECTOR" --no-headers 2>/dev/null | wc -l | tr -d ' ')"
WORKER_DESIRED="$(kubectl get deploy -n "$NAMESPACE" worker -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "unknown")"

if [[ "$WORKER_COUNT" == "0" ]]; then
  echo ""
  echo "=== Worker not created: focused diagnostics ==="
  echo "No worker pods matched selector '$WORKER_SELECTOR'. Collecting likely causes..."

  echo ""
  echo "--- Worker deployment status (replicas/conditions) ---"
  kubectl get deploy -n "$NAMESPACE" worker -o wide 2>/dev/null || yellow "WARN: deployment/worker not found"
  if [[ "$VERBOSE" == "1" ]]; then
    kubectl describe deploy -n "$NAMESPACE" worker 2>/dev/null | tail -n 120 || true
  fi
  if [[ "$WORKER_DESIRED" == "0" ]]; then
    yellow "DIAG: deployment/worker está com spec.replicas=0. O worker não foi solicitado para escalar ainda."
    yellow "DIAG: provável causa -> fluxo canônico de start não chegou ao controller (/streams/started) para essa stream."
  fi

  echo ""
  echo "--- HPA / KEDA objects related to worker ---"
  kubectl get scaledobject -n "$NAMESPACE" 2>/dev/null | filter_stream "worker|NAME|NAMESPACE"
  kubectl get hpa -n "$NAMESPACE" 2>/dev/null | filter_stream "worker|NAME"

  echo ""
  echo "--- Recent namespace events (worker/controller/proxy) ---"
  kubectl get events -n "$NAMESPACE" --sort-by=.lastTimestamp 2>/dev/null | tail -n 120 | filter_stream "worker|worker|controller|proxy|Failed|Warning|BackOff|Insufficient|Forbidden|Error"
fi

echo ""
echo "=== Proxy -> Controller path diagnostics ==="
echo "--- Controller service and endpoints ---"
kubectl get svc -n "$NAMESPACE" controller -o wide 2>/dev/null || yellow "WARN: service/controller not found"
kubectl get endpoints -n "$NAMESPACE" controller -o wide 2>/dev/null || yellow "WARN: endpoints/controller not found"

echo ""
echo "--- Proxy deployment env (controller-related) ---"
  if [[ "$VERBOSE" == "1" ]]; then
    kubectl get deploy -n "$NAMESPACE" proxy -o yaml 2>/dev/null | filter_stream "name:|value:|controller|api|controller|publish|hook"
  else
    kubectl get deploy -n "$NAMESPACE" proxy -o jsonpath='{range .spec.template.spec.containers[*].env[*]}{.name}={.value}{"\n"}{end}' 2>/dev/null | filter_stream "controller|api|rtmp|publish|hook"
  fi

PROXY_POD_FOR_NET="$(kubectl get pods -n "$NAMESPACE" -l "$PROXY_SELECTOR" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -n "$PROXY_POD_FOR_NET" ]]; then
  echo ""
  echo "--- In-proxy connectivity checks to controller ---"
  check_warn "proxy resolves controller service DNS" kubectl exec -n "$NAMESPACE" "$PROXY_POD_FOR_NET" -- sh -lc "getent hosts controller.${NAMESPACE}.svc.cluster.local >/dev/null 2>&1 || nslookup controller.${NAMESPACE}.svc.cluster.local >/dev/null 2>&1"
  check_warn "proxy reaches controller /health" kubectl exec -n "$NAMESPACE" "$PROXY_POD_FOR_NET" -- sh -lc "if command -v curl >/dev/null 2>&1; then curl -fsS http://controller.${NAMESPACE}.svc.cluster.local:8000/health; elif command -v wget >/dev/null 2>&1; then wget -qO- http://controller.${NAMESPACE}.svc.cluster.local:8000/health; else echo 'missing curl/wget'; exit 1; fi | grep -Eq 'ok'"
  check_warn "proxy nginx has on_publish hooks" kubectl exec -n "$NAMESPACE" "$PROXY_POD_FOR_NET" -- sh -lc "grep -Ein 'on_publish|on_publish_done|exec_publish' /etc/nginx/nginx.conf"
  check_warn "proxy has curl and jq for publish hooks" kubectl exec -n "$NAMESPACE" "$PROXY_POD_FOR_NET" -- sh -lc "command -v curl >/dev/null 2>&1 && command -v jq >/dev/null 2>&1"
  check_warn "on_publish_start script targets current namespace controller" kubectl exec -n "$NAMESPACE" "$PROXY_POD_FOR_NET" -- sh -lc "grep -En 'CONTROLLER_API=' /scripts/on_publish_start.sh | grep -Eq 'controller\\.${NAMESPACE}\\.svc\\.cluster\\.local|controller\\.media\\.svc\\.cluster\\.local'"
  if [[ -n "$STREAM_NAME" ]]; then
    check_warn "manual streams/started probe from proxy (stream)" kubectl exec -n "$NAMESPACE" "$PROXY_POD_FOR_NET" -- sh -lc "PROXY_POD=\$(hostname); curl -sS -o /tmp/alloc_probe_${STREAM_NAME}.json -w '%{http_code}' \"http://controller.${NAMESPACE}.svc.cluster.local:8000/streams/started?stream=${STREAM_NAME}&proxy_pod=\$PROXY_POD\" | grep -Eq '200'"
  fi
else
  yellow "WARN: no proxy pod resolved for connectivity checks"
fi

echo ""
echo "=== Controller /health and /metrics ==="
CTRL_POD="$(kubectl get pod -n "$NAMESPACE" -l app="$CONTROLLER_DEPLOYMENT" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "$CTRL_POD" ]]; then
  CTRL_POD="$(kubectl get pod -n "$NAMESPACE" -l app=controller -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
fi
if [[ -n "$CTRL_POD" ]]; then
  check_warn "controller /health" kubectl exec -n "$NAMESPACE" "$CTRL_POD" -- sh -lc "if command -v curl >/dev/null 2>&1; then curl -fsS http://127.0.0.1:8000/health; elif command -v wget >/dev/null 2>&1; then wget -qO- http://127.0.0.1:8000/health; else echo 'missing curl/wget'; exit 1; fi | grep -Eq 'ok'"
  check_warn "controller /metrics has base gauges" kubectl exec -n "$NAMESPACE" "$CTRL_POD" -- sh -lc "if command -v curl >/dev/null 2>&1; then curl -fsS http://127.0.0.1:8000/metrics; elif command -v wget >/dev/null 2>&1; then wget -qO- http://127.0.0.1:8000/metrics; else echo 'missing curl/wget'; exit 1; fi | grep -Eq 'worker_pods_available|handover_attempts_total'"
  check_warn "controller OpenAPI has /streams/started route" kubectl exec -n "$NAMESPACE" "$CTRL_POD" -- sh -lc "if command -v curl >/dev/null 2>&1; then curl -fsS http://127.0.0.1:8000/openapi.json; elif command -v wget >/dev/null 2>&1; then wget -qO- http://127.0.0.1:8000/openapi.json; else echo 'missing curl/wget'; exit 1; fi | grep -Eq '\"/streams/started\"'"
else
  yellow "WARN: could not resolve controller pod by label; skipping in-pod endpoint checks"
fi

echo ""
echo "=== Controller logs (allocation lifecycle) ==="
if [[ -n "$CTRL_POD" ]]; then
  if [[ -n "$STREAM_NAME" ]]; then
    log_snippet "$CTRL_POD" "$STREAM_NAME|Allocate|Release|StartWorker|streams/started|Delivery|Recovery|allocation|Registry|ProxyHealth|/streams/started"
  else
    log_snippet "$CTRL_POD" "Allocate|Release|StartWorker|streams/started|Delivery|Recovery|allocation|Registry|ProxyHealth|/streams/started"
  fi
fi

echo ""
echo "=== Controller streams/started troubleshooting timeline ==="
if [[ -n "$CTRL_POD" ]]; then
  if [[ -n "$STREAM_NAME" ]]; then
    kubectl logs -n "$NAMESPACE" "$CTRL_POD" --since="$SINCE" \
      | filter_stream "$STREAM_NAME|\\[StartWorker\\]\\[Timeline\\]|/streams/started|worker_mismatch|already_started"
  else
    kubectl logs -n "$NAMESPACE" "$CTRL_POD" --since="$SINCE" \
      | filter_stream "\\[StartWorker\\]\\[Timeline\\]|/streams/started|worker_mismatch|already_started"
  fi
fi

echo ""
echo "=== Controller diagnostics: worker creation errors ==="
if [[ -n "$CTRL_POD" ]]; then
  if [[ -n "$STREAM_NAME" ]]; then
    kubectl logs -n "$NAMESPACE" "$CTRL_POD" --since="$SINCE" \
      | grep -Eiv "\\[DEBUG\\] \\[request\\] response body" \
      | filter_stream "$STREAM_NAME|error|exception|traceback|failed|forbidden|timeout|streams/started|allocate|created dedicated worker|released worker"
  else
    kubectl logs -n "$NAMESPACE" "$CTRL_POD" --since="$SINCE" \
      | grep -Eiv "\\[DEBUG\\] \\[request\\] response body" \
      | filter_stream "error|exception|traceback|failed|forbidden|timeout|streams/started|allocate|created dedicated worker|released worker"
  fi
fi

echo ""
echo "--- Kubernetes events related to worker creation ---"
kubectl get events -n "$NAMESPACE" --sort-by=.lastTimestamp 2>/dev/null | tail -n 200 | filter_stream "worker|replicaset|FailedCreate|FailedScheduling|FailedMount|ErrImage|BackOff|Forbidden|denied|insufficient|quota|oom|pull"

if [[ "$WORKER_COUNT" == "0" ]]; then
  echo ""
  echo "=== Quick interpretation ==="
  if [[ "$WORKER_DESIRED" == "0" ]]; then
    yellow "Sem worker porque replicas desejadas do deployment continuam em 0."
    yellow "Próximo passo: verificar se proxy chamou o endpoint /streams/started no controller para a stream."
  else
    yellow "Deployment pede worker (replicas > 0), mas pods não subiram. Verifique eventos de scheduling/imagem."
  fi
fi

echo ""
echo "=== Proxy logs (publish hooks + routing) ==="
for pod in $(kubectl get pods -n "$NAMESPACE" -l "$PROXY_SELECTOR" -o name); do
  echo "--- $pod ---"
  if [[ -n "$STREAM_NAME" ]]; then
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | filter_stream "on_publish_start|on_publish_done|$STREAM_NAME|streams/started|streams/ended|worker|error|warn"
  else
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | filter_stream "on_publish_start|on_publish_done|streams/started|streams/ended|worker|error|warn"
  fi
done

echo ""
echo "=== Proxy source lifecycle (who stopped first?) ==="
for pod in $(kubectl get pods -n "$NAMESPACE" -l "$PROXY_SELECTOR" -o name); do
  echo "--- $pod ---"
  check_warn "proxy stdout has publish/connect/disconnect timeline" kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | filter_stream "publish|on_publish|on_publish_done|play|connect|disconnect|close|deleteStream|$STREAM_NAME"
  check_warn "proxy nginx error/access logs (in-pod files)" kubectl exec -n "$NAMESPACE" "${pod#pod/}" -- sh -lc "for f in /var/log/nginx/error.log /var/log/nginx/access.log; do [ -f \"\$f\" ] && echo \"### \$f\" && tail -n 120 \"\$f\"; done" | filter_stream "publish|on_publish|on_publish_done|play|connect|disconnect|close|deleteStream|$STREAM_NAME|error|warn|fail"
done

echo ""
echo "=== Proxy reconnect clients (RTMP churn summary) ==="
for pod in $(kubectl get pods -n "$NAMESPACE" -l "$PROXY_SELECTOR" -o name); do
  echo "--- $pod ---"
  CLIENT_IPS="$(kubectl logs -n "$NAMESPACE" "${pod#pod/}" --since="$SINCE" 2>/dev/null \
    | grep -Eo "client connected '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+'" \
    | sed -E "s/.*'([0-9.]+)'.*/\1/" \
    | sort | uniq -c | sort -nr | head -n 8 || true)"

  if [[ -z "$CLIENT_IPS" ]]; then
    yellow "WARN: no RTMP client connection lines found in window"
    continue
  fi

  while read -r count ip; do
    [[ -z "${ip:-}" ]] && continue
    owner="$(resolve_ip_owner "$ip")"
    echo "connections=$count client_ip=$ip owner=$owner"
  done <<< "$CLIENT_IPS"
done

echo ""
echo "=== Worker logs (recovery/ffmpeg) ==="
for pod in $(kubectl get pods -n "$NAMESPACE" -l "$WORKER_SELECTOR" -o name); do
  echo "--- $pod ---"
  if [[ -n "$STREAM_NAME" ]]; then
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | filter_stream "$STREAM_NAME|worker_recovery|ffmpeg|recovery-report|heartbeat|error|warn|youtube|tcp"
  else
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" | filter_stream "worker_recovery|ffmpeg|recovery-report|heartbeat|error|warn|youtube|tcp"
  fi
done

echo ""
echo "=== Worker troubleshooting timeline ==="
for pod in $(kubectl get pods -n "$NAMESPACE" -l "$WORKER_SELECTOR" -o name); do
  echo "--- $pod ---"
  if [[ -n "$STREAM_NAME" ]]; then
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" \
      | filter_stream "$STREAM_NAME|\\[worker_publish\\]\\[timeline\\]|\\[worker_recovery\\] \\[timeline\\]|Recovery manager already running|Starting FFmpeg|FFmpeg exited"
  else
    kubectl logs -n "$NAMESPACE" "$pod" --since="$SINCE" \
      | filter_stream "\\[worker_publish\\]\\[timeline\\]|\\[worker_recovery\\] \\[timeline\\]|Recovery manager already running|Starting FFmpeg|FFmpeg exited"
  fi
done

if [[ "$WORKER_COUNT" != "0" ]]; then
  echo ""
  echo "=== Worker process checks (manager/ffmpeg) ==="
  for pod in $(kubectl get pods -n "$NAMESPACE" -l "$WORKER_SELECTOR" -o name); do
    echo "--- $pod ---"
    check_warn "worker has ffmpeg manager process" kubectl exec -n "$NAMESPACE" "${pod#pod/}" -- sh -lc "ps aux | grep -E 'worker_stream_runner|ffmpeg' | grep -v grep"
    check_warn "worker manager logs mention stream/start" kubectl exec -n "$NAMESPACE" "${pod#pod/}" -- sh -lc "ls /tmp/ffmpeg_*.log >/dev/null 2>&1 && grep -Ein 'start|stream|ffmpeg|error|recovery' /tmp/ffmpeg_*.log | tail -n 40"
    check_warn "ffmpeg runtime log has no immediate input/push errors" kubectl exec -n "$NAMESPACE" "${pod#pod/}" -- sh -lc "if ls /tmp/ffmpeg_*.log >/dev/null 2>&1; then tail -n 80 /tmp/ffmpeg_*.log | grep -Ein 'Error demuxing input|I/O error|Connection reset|Connection refused|timed out|broken pipe|forbidden|401|403|Invalid argument|Server error' && exit 1 || exit 0; else echo 'ffmpeg log not found yet'; exit 1; fi"
    check_warn "worker can resolve youtube ingest DNS" kubectl exec -n "$NAMESPACE" "${pod#pod/}" -- sh -lc "getent hosts a.rtmp.youtube.com >/dev/null 2>&1 || nslookup a.rtmp.youtube.com >/dev/null 2>&1"
  done
fi

WORKER_POD_FOR_TIMING="$(kubectl get pods -n "$NAMESPACE" -l "$WORKER_SELECTOR" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
print_bootstrap_metrics "$CTRL_POD" "$WORKER_POD_FOR_TIMING"

echo ""
echo "=== Objective checklist (manual confirmation) ==="
cat <<'CHECKLIST'
[ ] Proxy log has "on_publish_start" for your stream.
[ ] Controller log has "Allocate" and either "allocated" or "scaled deployment".
[ ] Controller log has "StartWorker" success for selected worker.
[ ] Worker log has recovery loop start and FFmpeg start for stream.
[ ] Worker log shows no repeated FFmpeg/network errors for your stream.
[ ] Controller /metrics includes worker_pods_available and handover_attempts_total.
[ ] On stream stop, proxy has "on_publish_done" and controller has "Release".
CHECKLIST

green "Checklist script finished."
