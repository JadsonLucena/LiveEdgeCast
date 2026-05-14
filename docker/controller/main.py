from fastapi import FastAPI, Query, HTTPException
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
import random
import string
import threading
import requests
import time
import logging
import asyncio
import json
from typing import Dict, Optional
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from fastapi.responses import Response

logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [%(levelname)s] [%(funcName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = FastAPI()

NAMESPACE = "media"
WORKER_DEPLOYMENT = "worker"
WORKER_SERVICE = "worker"
PROXY_SERVICE_DNS = "proxy.media.svc.cluster.local"
PROXY_HEADLESS_SERVICE = "proxy-headless"
SCALE_DOWN_DELAY = 180

allocation_lock = threading.Lock()

stream_to_worker: Dict[str, str] = {}

worker_to_stream: Dict[str, str] = {}

stream_to_proxy: Dict[str, str] = {}
stream_generation: Dict[str, int] = {}
stream_registry: Dict[str, Dict[str, float]] = {}

streams_pending_allocation: Dict[str, float] = {}

last_release_time: Optional[float] = None
scale_down_task: Optional[asyncio.Task] = None
registry_health_task: Optional[asyncio.Task] = None
worker_health_task: Optional[asyncio.Task] = None
PROXY_HEALTHCHECK_INTERVAL_SECONDS = 3
PROXY_HEALTHCHECK_MAX_FAILURES = 3
PROXY_HEALTHCHECK_TIMEOUT_SECONDS = 2
PROXY_HEALTHCHECK_MAX_CONCURRENCY = 20
PROXY_HEALTHCHECK_JITTER_SECONDS = 1.5
WORKER_READY_HEALTH_DELAY_SECONDS = 3
STREAM_TTL_SECONDS = 180
proxy_health_failures: Dict[str, int] = {}
worker_ready_since: Dict[str, float] = {}
proxy_ready_since: Dict[str, float] = {}

STATE_CONFIGMAP_NAME = "controller-state"
STATE_CONFIGMAP_KEY = "state.json"
STATE_SCHEMA_VERSION = 2

stream_start_time: Dict[str, float] = {}
stream_interruptions: Dict[str, int] = {}
stream_downtime: Dict[str, float] = {}

recovery_attempts: Dict[str, int] = {}
recovery_successes: Dict[str, int] = {}

metrics_collection_task: Optional[asyncio.Task] = None

stream_delivery_errors = Counter('stream_delivery_errors_total','Total delivery errors to YouTube',['reason'])
stream_bitrate_output = Gauge('stream_bitrate_output_mbps','Bitrate being sent to YouTube in Mbps',['stream'])
stream_bitrate_input = Gauge('stream_bitrate_input_mbps','Bitrate received from proxy in Mbps',['stream'])
stream_delivery_status = Gauge('stream_delivery_status','Delivery status: 0=error, 1=warning, 2=ok',['stream'])
stream_start_timestamp = Gauge('stream_start_time','Unix timestamp when stream started',['stream'])
stream_uptime = Gauge('stream_uptime_seconds','How long stream has been active in seconds',['stream'])
stream_session_duration = Gauge('stream_session_duration_seconds','Total stream session duration in seconds',['stream'])
stream_interruptions_counter = Counter('stream_interruptions_total','Total number of times stream was interrupted',['stream'])
stream_downtime_gauge = Gauge('stream_downtime_total_seconds','Total accumulated downtime in seconds',['stream'])
stream_current_downtime = Gauge('stream_current_downtime_seconds','Current downtime if stream is down, 0 otherwise',['stream'])
ffmpeg_restart_counter = Counter('ffmpeg_restart_total','Total FFmpeg process restarts',['stream'])
recovery_attempt_counter = Counter('recovery_attempt_total','Total recovery attempts',['stream'])
recovery_successful_counter = Counter('recovery_successful_total','Successful recovery attempts',['stream'])
recovery_time_histogram = Histogram('recovery_time_seconds','Time taken to recover from failure',['stream'],buckets=(1,2,5,10,15,20,30,60))
recovery_success_rate = Gauge('recovery_success_rate','Success rate of recovery (0-1)',['stream'])
ffmpeg_exit_code_counter = Counter('ffmpeg_exit_code','FFmpeg exit codes',['stream','code'])
ffmpeg_process_running = Gauge('ffmpeg_process_running','Is FFmpeg currently running (0 or 1)',['stream'])
stream_last_error_reason_gauge = Gauge('stream_last_error_reason','Last error reason code',['stream'])
pod_cpu_usage_percent = Gauge('pod_cpu_usage_percent','Pod CPU usage percentage (0-100)',['pod','namespace'])
pod_memory_usage_bytes = Gauge('pod_memory_usage_bytes','Pod memory usage in bytes',['pod','namespace'])
pod_memory_usage_percent = Gauge('pod_memory_usage_percent','Pod memory usage as percent of limit',['pod','namespace'])
pod_network_io_bytes_total = Counter('pod_network_io_bytes_total','Total network I/O bytes',['pod','direction'])
pod_ready_status = Gauge('pod_ready_status','Is pod ready (0 or 1)',['pod','namespace'])
proxy_active_connections = Gauge('proxy_active_connections','Active RTMP connections to proxy',['proxy_pod'])
proxy_bandwidth_mbps = Gauge('proxy_bandwidth_mbps','Current proxy bandwidth in Mbps',['proxy_pod'])
worker_pods_available = Gauge('worker_pods_available','Available worker pods for allocation',['namespace'])
allocation_queue_length = Gauge('allocation_queue_length','Number of streams waiting for worker allocation')
stream_proxy_handover_counter = Counter('stream_proxy_handover_total','Total proxy handovers accepted by controller',['stream'])
handover_attempts_total = Counter('handover_attempts_total', 'Total proxy handover attempts', ['stream'])
handover_success_total = Counter('handover_success_total', 'Total successful proxy handovers', ['stream'])
handover_conflict_total = Counter('handover_conflict_total', 'Total conflicting proxy handovers denied', ['stream'])
stream_assignment_info = Gauge('stream_assignment_info', 'Current stream assignment by proxy/worker', ['stream','proxy_pod','worker_pod','generation'])

try:
    config.load_incluster_config()
    logger.info("Loaded in-cluster Kubernetes config")
except:
    config.load_kube_config()
    logger.info("Loaded local kubeconfig")

apps = client.AppsV1Api()
core = client.CoreV1Api()


def persist_state_locked() -> None:
    """
    Persists critical controller state to a ConfigMap to survive pod restart/crash.
    Must be called only while holding allocation_lock.
    """
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "stream_to_worker": stream_to_worker,
        "worker_to_stream": worker_to_stream,
        "stream_to_proxy": stream_to_proxy,
        "stream_registry": stream_registry,
        "stream_generation": stream_generation,
        "streams_pending_allocation": streams_pending_allocation,
    }
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=STATE_CONFIGMAP_NAME, namespace=NAMESPACE),
        data={STATE_CONFIGMAP_KEY: json.dumps(payload)}
    )
    try:
        core.patch_namespaced_config_map(
            name=STATE_CONFIGMAP_NAME,
            namespace=NAMESPACE,
            body=body
        )
    except ApiException as e:
        if e.status == 404:
            core.create_namespaced_config_map(namespace=NAMESPACE, body=body)
        else:
            raise


def restore_persisted_state_locked() -> bool:
    """
    Restores persisted state from the ConfigMap.
    Returns True if state was restored.
    Must be called only while holding allocation_lock.
    """
    try:
        cm = core.read_namespaced_config_map(name=STATE_CONFIGMAP_NAME, namespace=NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return False
        logger.warning(f"[State Recovery] Failed to read state ConfigMap: {e}")
        return False

    raw = (cm.data or {}).get(STATE_CONFIGMAP_KEY)
    if not raw:
        return False

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[State Recovery] Invalid JSON in persisted state, ignoring.")
        return False

    stream_to_worker.clear()
    stream_to_worker.update(data.get("stream_to_worker", {}))
    worker_to_stream.clear()
    worker_to_stream.update(data.get("worker_to_stream", {}))
    stream_to_proxy.clear()
    stream_to_proxy.update(data.get("stream_to_proxy", {}))
    stream_registry.clear()
    stream_registry.update(data.get("stream_registry", {}))
    stream_generation.clear()
    stream_generation.update(data.get("stream_generation", {}))
    streams_pending_allocation.clear()
    streams_pending_allocation.update(data.get("streams_pending_allocation", {}))
    return True


def random_suffix():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))




def create_worker_pod_for_stream(stream: str, proxy_dns: str) -> str:
    """
    Cria Pod por stream reaproveitando o template do worker Deployment.
    Apenas STREAM_KEY e PROXY_DNS são injetados dinamicamente.
    """
    pod_name = f"worker-{stream.lower().replace('_','-')[:40]}-{random_suffix()}"

    deployment = apps.read_namespaced_deployment(name=WORKER_DEPLOYMENT, namespace=NAMESPACE)
    template = deployment.spec.template
    if not template or not template.spec or not template.spec.containers:
        raise RuntimeError("worker deployment template is invalid or has no containers")

    pod_spec = template.spec
    pod_spec.restart_policy = "Always"

    for c in pod_spec.containers:
        env = list(c.env or [])
        env = [e for e in env if e.name not in ("STREAM_KEY", "PROXY_DNS")]
        env.append(client.V1EnvVar(name="STREAM_KEY", value=stream))
        env.append(client.V1EnvVar(name="PROXY_DNS", value=proxy_dns))
        c.env = env

    labels = dict(template.metadata.labels or {}) if template.metadata else {}
    labels.update({"app": "worker", "stream": stream})

    pod_manifest = client.V1Pod(
        metadata=client.V1ObjectMeta(name=pod_name, namespace=NAMESPACE, labels=labels),
        spec=pod_spec,
    )

    core.create_namespaced_pod(namespace=NAMESPACE, body=pod_manifest)
    return pod_name


def replace_worker_pod_for_stream_locked(stream: str, proxy_dns: str) -> Optional[str]:
    """Recria o worker da stream para aplicar novo PROXY_DNS (env imutável em Pod existente)."""
    old_worker = stream_to_worker.get(stream)
    if not old_worker:
        return None

    new_worker = create_worker_pod_for_stream(stream=stream, proxy_dns=proxy_dns)
    stream_to_worker[stream] = new_worker
    worker_to_stream.pop(old_worker, None)
    worker_to_stream[new_worker] = stream

    try:
        core.delete_namespaced_pod(name=old_worker, namespace=NAMESPACE, grace_period_seconds=0)
    except ApiException as e:
        logger.warning(f"[Handover] Failed deleting old worker pod {old_worker}: {e}")

    logger.info(
        f"[Handover] Replaced worker pod for stream '{stream}' due to proxy change: "
        f"old='{old_worker}' new='{new_worker}' proxy_dns='{proxy_dns}'"
    )
    return new_worker
def cleanup_expired_streams() -> None:
    """
    Removes expired streams from the ephemeral registry.
    Also removes stream_to_proxy to keep state consistent.
    """
    now = time.time()
    expired = [stream for stream, entry in stream_registry.items() if entry.get("expires_at", 0) <= now]

    for stream in expired:
        stream_registry.pop(stream, None)
        stream_to_proxy.pop(stream, None)
        logger.info(f"[Registry] Stream '{stream}' expired after inactivity window")


def register_or_refresh_stream(stream: str, proxy_pod: str):
    """
    Creates or refreshes the stream ephemeral registry on proxy.
    """
    expires_at = time.time() + STREAM_TTL_SECONDS
    if stream not in stream_generation:
        stream_generation[stream] = 1
    stream_registry[stream] = {
        "proxy_pod": proxy_pod,
        "expires_at": expires_at
    }
    stream_to_proxy[stream] = proxy_pod
    proxy_health_failures[proxy_pod] = 0
    return expires_at


def register_or_refresh_stream_if_owner_matches(stream: str, proxy_pod: str):
    """
    Refreshes registry only if:
    - stream does not exist yet, or
    - stream already belongs to the same proxy_pod
    """
    current = stream_registry.get(stream)
    if current and current.get("proxy_pod") != proxy_pod:
        return None
    return register_or_refresh_stream(stream, proxy_pod)


def try_handover_stream_owner(stream: str, candidate_proxy_pod: str) -> bool:
    """
    Ownership rule with safe handover:
    - idempotent: if already owned by candidate_proxy_pod, just refresh
    - handover allowed if previous owner is ineligible by any criterion:
      proxy unhealthy/dead
    """
    handover_attempts_total.labels(stream=stream).inc()
    current = stream_registry.get(stream)
    if not current:
        register_or_refresh_stream(stream, candidate_proxy_pod)
        handover_success_total.labels(stream=stream).inc()
        return True

    current_owner = current.get("proxy_pod")
    if current_owner == candidate_proxy_pod:
        register_or_refresh_stream(stream, candidate_proxy_pod)
        return True

    owner_unhealthy = proxy_health_failures.get(current_owner, 0) >= PROXY_HEALTHCHECK_MAX_FAILURES
    if not owner_unhealthy:
        owner_unhealthy = not check_proxy_health(current_owner)

    if owner_unhealthy:
        logger.info(
            f"[Handover] Stream '{stream}' ownership moved from '{current_owner}' "
            f"to '{candidate_proxy_pod}' (owner_unhealthy={owner_unhealthy})"
        )
        stream_generation[stream] = stream_generation.get(stream, 1) + 1
        register_or_refresh_stream(stream, candidate_proxy_pod)

        # PROXY_DNS é env de Pod; para atualizar em reconexão/handover, recria o worker.
        proxy_dns = resolve_proxy_address(candidate_proxy_pod)
        if stream in stream_to_worker:
            replace_worker_pod_for_stream_locked(stream=stream, proxy_dns=proxy_dns)

        handover_success_total.labels(stream=stream).inc()
        stream_proxy_handover_counter.labels(stream=stream).inc()
        return True

    handover_conflict_total.labels(stream=stream).inc()
    logger.warning(
        f"[Handover] Denied ownership change for stream '{stream}' from '{current_owner}' "
        f"to '{candidate_proxy_pod}' (owner_unhealthy={owner_unhealthy})"
    )
    return False


async def schedule_scale_down_if_idle():
    """
    Mantido por compatibilidade: no modelo atual (1 pod por stream),
    não há scale-down de Deployment para executar.
    """
    await asyncio.sleep(SCALE_DOWN_DELAY)
    logger.info("[AutoScaleDown] Skipped: worker Deployment scaling is disabled (per-stream pods).")


async def monitor_stream_registry_health():
    """
    Controller-driven health monitoring:
    - A cada 5s verifica /health de cada proxy com stream ativa
    - Após 3 falhas consecutivas, expira todas as streams daquele proxy
    """
    semaphore = asyncio.Semaphore(PROXY_HEALTHCHECK_MAX_CONCURRENCY)

    async def run_proxy_check(proxy_pod: str):
        if PROXY_HEALTHCHECK_JITTER_SECONDS > 0:
            await asyncio.sleep(random.uniform(0, PROXY_HEALTHCHECK_JITTER_SECONDS))

        async with semaphore:
            is_healthy = await asyncio.to_thread(check_proxy_health, proxy_pod)

        with allocation_lock:
            if is_healthy:
                proxy_health_failures[proxy_pod] = 0
                for stream, entry in stream_registry.items():
                    if entry.get("proxy_pod") == proxy_pod:
                        entry["expires_at"] = time.time() + STREAM_TTL_SECONDS
            else:
                failures = proxy_health_failures.get(proxy_pod, 0) + 1
                proxy_health_failures[proxy_pod] = failures
                logger.warning(
                    f"[ProxyHealth] Proxy '{proxy_pod}' failed healthcheck "
                    f"({failures}/{PROXY_HEALTHCHECK_MAX_FAILURES})"
                )

                if failures >= PROXY_HEALTHCHECK_MAX_FAILURES:
                    impacted_streams = [
                        stream for stream, entry in stream_registry.items()
                        if entry.get("proxy_pod") == proxy_pod
                    ]
                    for stream in impacted_streams:
                        stream_registry.pop(stream, None)
                        stream_to_proxy.pop(stream, None)
                        logger.info(
                            f"[Registry] Stream '{stream}' expired after "
                            f"{PROXY_HEALTHCHECK_MAX_FAILURES} failed proxy healthchecks"
                        )
                        worker_name = stream_to_worker.pop(stream, None)
                        if worker_name:
                            worker_to_stream.pop(worker_name, None)
                            worker_ready_since.pop(worker_name, None)
                            try:
                                core.delete_namespaced_pod(name=worker_name, namespace=NAMESPACE, grace_period_seconds=0)
                            except Exception as e:
                                logger.warning(f"[ProxyHealth] Failed deleting worker {worker_name}: {e}")
                    
                    proxy_health_failures.pop(proxy_pod, None)
                    proxy_ready_since.pop(proxy_pod, None)

    while True:
        await asyncio.sleep(PROXY_HEALTHCHECK_INTERVAL_SECONDS)

        with allocation_lock:
            cleanup_expired_streams()
            proxies = {entry.get("proxy_pod") for entry in stream_registry.values() if entry.get("proxy_pod")}

        if proxies:
            await asyncio.gather(*(run_proxy_check(proxy_pod) for proxy_pod in proxies))


def get_proxy_pod_ip(proxy_pod: str) -> str:
    """Compat helper (legacy)."""
    try:
        pod = core.read_namespaced_pod(name=proxy_pod, namespace=NAMESPACE)
        return pod.status.pod_ip
    except Exception as e:
        logger.error(f"[ProxyIP] Failed to get IP for {proxy_pod}: {e}")
        return None


def resolve_proxy_address(proxy_pod: Optional[str]) -> str:
    """Retorna DNS específico do pod proxy (via service headless) quando disponível."""
    if proxy_pod:
        return f"{proxy_pod}.{PROXY_HEADLESS_SERVICE}.{NAMESPACE}.svc.cluster.local"
    return PROXY_SERVICE_DNS


def check_proxy_health(proxy_pod: str) -> bool:
    try:
        pod = core.read_namespaced_pod(name=proxy_pod, namespace=NAMESPACE)
        ready = any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))
        if not ready:
            proxy_ready_since.pop(proxy_pod, None)
            return False

        now = time.time()
        first_ready_at = proxy_ready_since.get(proxy_pod)
        if first_ready_at is None:
            proxy_ready_since[proxy_pod] = now
            return False
        if (now - first_ready_at) < WORKER_READY_HEALTH_DELAY_SECONDS:
            return False

        target = resolve_proxy_address(proxy_pod)
        response = requests.get(f"http://{target}:8080/health", timeout=PROXY_HEALTHCHECK_TIMEOUT_SECONDS)
        return response.status_code == 200
    except Exception:
        return False


def check_worker_health(pod_name: str, pod_ip: Optional[str] = None) -> bool:
    try:
        target = pod_ip if pod_ip else f"{pod_name}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local"
        response = requests.get(f"http://{target}:8080/health", timeout=2)
        return response.status_code == 200
    except Exception as e:
        logger.warning(f"Failed to check /health for {pod_name}: {e}")
        return False


def recover_state(
):
    """
    Recupera estado de alocações após reinício do controller.
    Verifica quais workers estão realmente ocupados consultando suas métricas RTMP.
    """
    logger.info("[State Recovery] Starting state recovery...")
    
    with allocation_lock:
        restored = restore_persisted_state_locked()
        if restored:
            logger.info(
                f"[State Recovery] Restored persisted state with {len(stream_to_worker)} active stream allocations."
            )
            return

        pods = core.list_namespaced_pod(
            namespace=NAMESPACE,
            label_selector="app=worker"
        ).items
        
        recovered_count = 0
        
        for pod in pods:
            if not pod.status.conditions:
                continue
                
            cond = {c.type: c.status for c in pod.status.conditions}
            if cond.get("Ready") != "True":
                continue
            
            pod_name = pod.metadata.name
            
            is_healthy = check_worker_health(pod_name, pod.status.pod_ip)

            if is_healthy:
                stream_name = f"recovered_stream_{random_suffix()}"
                stream_to_worker[stream_name] = pod_name
                worker_to_stream[pod_name] = stream_name
                recovered_count += 1
                logger.info(f"[State Recovery] Worker {pod_name} responded /health, marked as allocated")
        
        persist_state_locked()
        logger.info(f"[State Recovery] Completed. Recovered {recovered_count} active workers.")



def record_stream_start(stream: str):
    now = time.time()
    stream_start_time[stream] = now
    stream_interruptions[stream] = 0
    stream_downtime[stream] = 0.0
    recovery_attempts[stream] = 0
    recovery_successes[stream] = 0
    stream_start_timestamp.labels(stream=stream).set(now)
    stream_uptime.labels(stream=stream).set(0)
    stream_downtime_gauge.labels(stream=stream).set(0)

def record_stream_end(stream: str):
    start = stream_start_time.get(stream)
    if not start:
        return
    duration = time.time() - start
    stream_session_duration.labels(stream=stream).set(duration)

def update_stream_uptime(stream: str):
    start = stream_start_time.get(stream)
    if not start:
        return
    uptime = (time.time() - start) - stream_downtime.get(stream, 0.0)
    stream_uptime.labels(stream=stream).set(max(0, uptime))

def record_interruption(stream: str):
    stream_interruptions[stream] = stream_interruptions.get(stream, 0) + 1
    stream_interruptions_counter.labels(stream=stream).inc()

def record_recovery_attempt(stream: str, success: bool, recovery_time_sec: float, exit_code: int = 0):
    recovery_attempts[stream] = recovery_attempts.get(stream, 0) + 1
    recovery_attempt_counter.labels(stream=stream).inc()
    ffmpeg_restart_counter.labels(stream=stream).inc()
    ffmpeg_exit_code_counter.labels(stream=stream, code=str(exit_code)).inc()
    if success:
        recovery_successes[stream] = recovery_successes.get(stream, 0) + 1
        recovery_successful_counter.labels(stream=stream).inc()
        recovery_time_histogram.labels(stream=stream).observe(recovery_time_sec)
    attempts = recovery_attempts.get(stream, 0)
    if attempts:
        recovery_success_rate.labels(stream=stream).set(recovery_successes.get(stream, 0) / attempts)

def get_pod_metrics(pod_name: str, namespace: str) -> dict:
    try:
        pod = core.read_namespaced_pod(name=pod_name, namespace=namespace)
        memory_limit = 0
        for container in pod.spec.containers or []:
            limits = container.resources.limits if container.resources else None
            if limits and limits.get('memory'):
                mem = str(limits.get('memory'))
                if mem.endswith('Mi'):
                    memory_limit += int(mem[:-2]) * 1024 * 1024
        ready = any(c.type == 'Ready' and c.status == 'True' for c in (pod.status.conditions or []))
        return {'memory_limit': memory_limit, 'ready': ready}
    except Exception as e:
        logger.warning(f'Failed to get metrics for {pod_name}: {e}')
        return {}

def collect_pod_metrics():
    try:
        pods = core.list_namespaced_pod(namespace=NAMESPACE,label_selector='app in (proxy, worker)').items
        for pod in pods:
            name = pod.metadata.name
            m = get_pod_metrics(name, NAMESPACE)
            pod_ready_status.labels(pod=name, namespace=NAMESPACE).set(1 if m.get('ready') else 0)
            memory_limit = m.get('memory_limit', 0)
            if memory_limit > 0:
                pod_memory_usage_bytes.labels(pod=name, namespace=NAMESPACE).set(memory_limit * 0.5)
                pod_memory_usage_percent.labels(pod=name, namespace=NAMESPACE).set(50)
    except Exception as e:
        logger.warning(f'Failed to collect pod metrics: {e}')

def collect_allocation_metrics():
    with allocation_lock:
        allocation_queue_length.set(len(streams_pending_allocation))
        try:
            pods = core.list_namespaced_pod(namespace=NAMESPACE, label_selector="app=worker").items
            ready = 0
            for pod in pods:
                conditions = {c.type: c.status for c in (pod.status.conditions or [])}
                if conditions.get("Ready") == "True":
                    ready += 1
            worker_pods_available.labels(namespace=NAMESPACE).set(ready)
        except Exception:
            pass

async def collect_infrastructure_metrics():
    while True:
        await asyncio.sleep(30)
        collect_pod_metrics()
        collect_allocation_metrics()




async def monitor_worker_health():
    """Controller-driven worker healthcheck every 3s + pending allocation retry."""
    while True:
        await asyncio.sleep(3)
        to_replace = []

        with allocation_lock:
            allocations = list(stream_to_worker.items())
            pending_streams = list(streams_pending_allocation.keys())

        # Health check for allocated workers
        for stream, worker_pod in allocations:
            healthy = False
            try:
                pod = core.read_namespaced_pod(name=worker_pod, namespace=NAMESPACE)
                ready = any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))
                if not ready:
                    worker_ready_since.pop(worker_pod, None)
                    continue

                now = time.time()
                first_ready_at = worker_ready_since.get(worker_pod)
                if first_ready_at is None:
                    worker_ready_since[worker_pod] = now
                    logger.debug(f"[WorkerHealth] Worker '{worker_pod}' became Ready. Starting /health delay timer.")
                    continue

                if (now - first_ready_at) < WORKER_READY_HEALTH_DELAY_SECONDS:
                    logger.debug(
                        f"[WorkerHealth] Waiting {WORKER_READY_HEALTH_DELAY_SECONDS}s after Ready for '{worker_pod}' "
                        f"before probing /health ({now - first_ready_at:.1f}s elapsed)."
                    )
                    continue

                healthy = check_worker_health(worker_pod, pod.status.pod_ip)
            except Exception:
                healthy = False

            if not healthy:
                to_replace.append((stream, worker_pod))

        # Try to allocate workers for pending streams
        for stream in pending_streams:
            with allocation_lock:
                if stream in stream_to_worker:
                    continue  # Already allocated

            try:
                pods = core.list_namespaced_pod(
                    namespace=NAMESPACE,
                    label_selector="app=worker"
                ).items

                for pod in pods:
                    if not pod.status.conditions:
                        continue

                    pod_name = pod.metadata.name

                    cond = {c.type: c.status for c in pod.status.conditions}
                    if cond.get("Ready") != "True":
                        continue

                    with allocation_lock:
                        if pod_name in worker_to_stream:
                            continue  # Worker already has a stream

                        # Allocate this worker to the pending stream
                        stream_time = streams_pending_allocation.get(stream, time.time())
                        stream_to_worker[stream] = pod_name
                        worker_to_stream[pod_name] = stream
                        streams_pending_allocation.pop(stream, None)
                        persist_state_locked()

                        owner_proxy = stream_registry.get(stream, {}).get("proxy_pod")
                        if owner_proxy:
                            stream_to_proxy[stream] = owner_proxy

                        logger.info(
                            f"[PendingAllocation] Allocated worker '{pod_name}' to pending stream '{stream}' "
                            f"(waiting since {time.time() - stream_time:.1f}s)"
                        )
                        break
            except Exception as e:
                logger.warning(f"[PendingAllocation] Error allocating workers for stream '{stream}': {e}")

        # Handle unhealthy workers
        if to_replace:
            with allocation_lock:
                for stream, worker_pod in to_replace:
                    allocated = stream_to_worker.get(stream)
                    if allocated != worker_pod:
                        continue
                    stream_to_worker.pop(stream, None)
                    worker_to_stream.pop(worker_pod, None)
                    worker_ready_since.pop(worker_pod, None)
                    streams_pending_allocation.pop(stream, None)
                    logger.warning(f"[WorkerHealth] Worker '{worker_pod}' unhealthy for stream '{stream}'. Replacing.")
                    try:
                        core.delete_namespaced_pod(name=worker_pod, namespace=NAMESPACE, grace_period_seconds=0)
                    except Exception as e:
                        logger.warning(f"[WorkerHealth] Failed to delete pod {worker_pod}: {e}")
                persist_state_locked()

@app.on_event("startup")
async def startup_event():
    global registry_health_task, worker_health_task, metrics_collection_task
    time.sleep(5)
    recover_state()
    registry_health_task = asyncio.create_task(monitor_stream_registry_health())
    worker_health_task = asyncio.create_task(monitor_worker_health())
    metrics_collection_task = asyncio.create_task(collect_infrastructure_metrics())


@app.on_event("shutdown")
async def shutdown_event():
    global registry_health_task, worker_health_task, metrics_collection_task
    if registry_health_task and not registry_health_task.done():
        registry_health_task.cancel()
    if worker_health_task and not worker_health_task.done():
        worker_health_task.cancel()
    if metrics_collection_task and not metrics_collection_task.done():
        metrics_collection_task.cancel()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/allocate")
def allocate_worker(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(None, description="Proxy pod name for pull-only architecture")
):
    """
    Aloca um worker dedicado para uma stream.
    Controller é a ÚNICA fonte da verdade para scale-up.
    
    Args:
        stream: Nome da stream (YouTube key)
        proxy_pod: Nome do pod do proxy que recebeu a stream (para Pull-Only)
    
    Retorna worker DNS + proxy DNS se disponível, ou None se ainda está escalando.
    """
    global scale_down_task
    
    with allocation_lock:
        cleanup_expired_streams()

        if proxy_pod:
            if not try_handover_stream_owner(stream, proxy_pod):
                owner = stream_registry.get(stream, {}).get("proxy_pod")
                persist_state_locked()
                raise HTTPException(
                    status_code=409,
                    detail=f"stream '{stream}' owned by proxy '{owner}'"
                )
            persist_state_locked()

        if scale_down_task and not scale_down_task.done():
            scale_down_task.cancel()
            logger.info("[Allocate] Cancelled pending scale-down task (new allocation request)")
        
        if stream in stream_to_worker:
            existing_worker = stream_to_worker[stream]
            
            owner_proxy = stream_registry.get(stream, {}).get("proxy_pod")
            proxy_address = resolve_proxy_address(owner_proxy)
            
            logger.info(f"[Allocate] Stream '{stream}' already has worker: {existing_worker} - Proxy: {proxy_address}")
            return {
                "pod": f"{existing_worker}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local",
                "name": existing_worker,
                "proxy": proxy_address,
                "status": "existing"
            }
        
        # Modelo 1:1 (pod por stream): nunca reaproveitar worker já existente.
        # Isso evita reuso indevido entre sessões e simplifica o ciclo de vida.
        streams_pending_allocation.pop(stream, None)

        owner_proxy = stream_registry.get(stream, {}).get("proxy_pod")
        proxy_address = resolve_proxy_address(owner_proxy)

        pod_name = create_worker_pod_for_stream(stream=stream, proxy_dns=proxy_address)
        stream_to_worker[stream] = pod_name
        worker_to_stream[pod_name] = stream
        persist_state_locked()

        worker_dns = f"{pod_name}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local"
        logger.info(f"[Allocate] Created dedicated worker pod {pod_name} for stream '{stream}'")
        return {"pod": worker_dns, "name": pod_name, "proxy": proxy_address, "worker": pod_name, "status": "created"}


@app.post("/release")
async def release_worker(stream: str = Query(..., description="Stream name to release")):
    """
    Libera worker alocado para uma stream.
    Remove mapeamento stream→worker.
    Agenda scale-down automático se todos workers ficarem idle.
    """
    global scale_down_task
    
    with allocation_lock:
        if stream not in stream_to_worker:
            logger.warning(f"[Release] Stream '{stream}' not found in allocations")
            
            if stream in streams_pending_allocation:
                del streams_pending_allocation[stream]
                logger.info(f"[Release] Removed stream '{stream}' from pending allocation queue (never allocated)")
                persist_state_locked()
            
            return {"status": "not_found", "stream": stream}
        
        worker_name = stream_to_worker[stream]
        
        del stream_to_worker[stream]
        del worker_to_stream[worker_name]
        worker_ready_since.pop(worker_name, None)
        
        if stream in stream_to_proxy:
            del stream_to_proxy[stream]
        if stream in stream_registry:
            del stream_registry[stream]
        stream_generation.pop(stream, None)
        
        if stream in streams_pending_allocation:
            del streams_pending_allocation[stream]
            logger.info(f"[Release] Removed stream '{stream}' from pending allocation queue")
        
        persist_state_locked()
        
        record_stream_end(stream)
        logger.info(f"[Release] Released worker {worker_name} from stream '{stream}'")
        try:
            core.delete_namespaced_pod(name=worker_name, namespace=NAMESPACE, grace_period_seconds=0)
        except ApiException as e:
            logger.warning(f"[Release] Failed deleting worker pod {worker_name}: {e}")

        if scale_down_task and not scale_down_task.done():
            scale_down_task.cancel()
        
        if len(stream_to_worker) == 0:
            scale_down_task = asyncio.create_task(schedule_scale_down_if_idle())
            logger.info(f"[Release] Scheduled auto scale-down in {SCALE_DOWN_DELAY}s")
        
        return {
            "status": "released",
            "stream": stream,
            "worker": worker_name
        }


@app.post("/streams/register")
def register_stream(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(..., description="Proxy pod name")
):
    with allocation_lock:
        cleanup_expired_streams()
        if not try_handover_stream_owner(stream, proxy_pod):
            current_owner = stream_registry.get(stream, {}).get("proxy_pod")
            raise HTTPException(
                status_code=409,
                detail=f"stream '{stream}' already owned by proxy '{current_owner}'"
            )
        expires_at = stream_registry.get(stream, {}).get("expires_at")
        persist_state_locked()
        return {
            "status": "registered",
            "stream": stream,
            "proxy_pod": proxy_pod,
            "ttl_seconds": STREAM_TTL_SECONDS,
            "healthcheck_interval_seconds": PROXY_HEALTHCHECK_INTERVAL_SECONDS,
            "max_failed_healthchecks": PROXY_HEALTHCHECK_MAX_FAILURES,
            "expires_at": expires_at
        }


@app.post("/streams/heartbeat")
def heartbeat_stream(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(..., description="Proxy pod name")
):
    raise HTTPException(status_code=410, detail="heartbeat disabled: controller performs healthchecks")


@app.get("/streams/resolve")
def resolve_stream(stream: str = Query(..., description="Stream name")):
    proxy_pod = None
    expires_at = None

    with allocation_lock:
        cleanup_expired_streams()
        entry = stream_registry.get(stream)

        if not entry:
            raise HTTPException(status_code=404, detail=f"stream '{stream}' not found")

        proxy_pod = entry.get("proxy_pod")
        expires_at = entry.get("expires_at")

    proxy_address = resolve_proxy_address(proxy_pod)

    return {
        "stream": stream,
        "proxyPod": proxy_pod,
        "proxyAddress": proxy_address,
        "expiresAt": expires_at
    }


@app.post("/streams/release")
def release_stream_registry(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(None, description="Proxy pod name")
):
    with allocation_lock:
        cleanup_expired_streams()
        current = stream_registry.get(stream)
        if not current:
            return {"status": "not_found", "stream": stream}

        if proxy_pod and current.get("proxy_pod") != proxy_pod:
            raise HTTPException(
                status_code=409,
                detail=f"stream '{stream}' owned by another proxy '{current.get('proxy_pod')}'"
            )

        stream_registry.pop(stream, None)
        stream_to_proxy.pop(stream, None)
        persist_state_locked()
        return {"status": "released", "stream": stream}






@app.post("/streams/started")
def stream_started(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(..., description="Proxy pod that received publish")
):
    """Single controller entrypoint when proxy publish starts.
    Controller performs register/allocation/start orchestration.
    """
    register_stream(stream=stream, proxy_pod=proxy_pod)
    allocation = allocate_worker(stream=stream, proxy_pod=proxy_pod)

    return {
        "status": "started_event_processed",
        "stream": stream,
        "allocation": allocation,
    }
@app.post("/streams/ended")
async def stream_ended(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(None, description="Proxy pod that ended publish")
):
    """Single controller entrypoint when proxy publish ends.
    Controller performs full cleanup (registry + worker release).
    """
    with allocation_lock:
        if proxy_pod:
            current = stream_registry.get(stream)
            if current and current.get("proxy_pod") == proxy_pod:
                stream_registry.pop(stream, None)
                stream_to_proxy.pop(stream, None)

    release_result = await release_worker(stream=stream)
    return {"status": "ended", "stream": stream, "release": release_result}
@app.get("/status")
def get_status():
    """
    Returns current allocation state.
    Useful for debugging and monitoring.
    """
    with allocation_lock:
        cleanup_expired_streams()
        return {
            "active_streams": len(stream_to_worker),
            "registry_streams": len(stream_registry),
            "allocations": [
                {
                    "stream": stream,
                    "worker": worker
                }
                for stream, worker in stream_to_worker.items()
            ],
            "registry": [
                {
                    "stream": stream,
                    "proxy_pod": data.get("proxy_pod"),
                    "expires_at": data.get("expires_at")
                }
                for stream, data in stream_registry.items()
            ]
        }


@app.post("/streams/delivery-status")
def report_delivery_status(
    stream: str = Query(...),
    proxy_pod: str = Query(...),
    status: str = Query(...),
    reason: str = Query(None),
    bitrate_input: float = Query(0),
    bitrate_output: float = Query(0),
):
    now = time.time()
    bitrate_input_mbps = bitrate_input / 1024.0
    bitrate_output_mbps = bitrate_output / 1024.0
    stream_delivery_status.labels(stream=stream).set({'ok':2,'warning':1,'error':0}.get(status,0))
    if bitrate_input_mbps > 0:
        stream_bitrate_input.labels(stream=stream).set(bitrate_input_mbps)
    if bitrate_output_mbps > 0:
        stream_bitrate_output.labels(stream=stream).set(bitrate_output_mbps)
    if status == 'error':
        stream_delivery_errors.labels(reason=reason or 'unknown').inc()
        ffmpeg_process_running.labels(stream=stream).set(0)
        stream_last_error_reason_gauge.labels(stream=stream).set(1)
        record_interruption(stream)
    else:
        ffmpeg_process_running.labels(stream=stream).set(1)
    update_stream_uptime(stream)
    with allocation_lock:
        register_or_refresh_stream(stream, proxy_pod)
        persist_state_locked()
    return {'acknowledged': True, 'status': status}

@app.post('/streams/recovery-report')
def report_recovery(stream: str = Query(...), success: bool = Query(...), recovery_time: float = Query(...), exit_code: int = Query(0)):
    record_recovery_attempt(stream, success, recovery_time, exit_code)
    return {'acknowledged': True}

@app.get('/metrics')
def metrics():
    return Response(generate_latest(), media_type='text/plain; version=0.0.4; charset=utf-8')

@app.get('/debug/streams')
def debug_streams():
    result = {}
    with allocation_lock:
        for stream in stream_registry:
            start = stream_start_time.get(stream, 0)
            result[stream] = {
                'status': 'unknown',
                'uptime_seconds': time.time() - start if start else 0,
                'interruptions': stream_interruptions.get(stream, 0),
                'recovery_attempts': recovery_attempts.get(stream, 0),
                'recovery_successes': recovery_successes.get(stream, 0),
            }
    return result


@app.get("/stream-key")
def get_stream_key(stream: str = Query(..., description="Stream name")):
    """
    Retorna a chave do YouTube E proxy DNS para uma stream específica.
    
    Esta função implementa a lógica de mapeamento stream → YouTube key + proxy.
    Por padrão, usa o próprio stream name como chave.
    
    Em produção, você pode:
    - Consultar um banco de dados
    - Usar variáveis de ambiente
    - Implementar lógica de mapeamento customizada
    """
    youtube_key = stream
    
    with allocation_lock:
        cleanup_expired_streams()
        proxy_pod = stream_to_proxy.get(stream)
        proxy_address = resolve_proxy_address(proxy_pod)

    logger.debug(f"[StreamKey] Returning info for stream '{stream}': YouTube={youtube_key}, Proxy={proxy_address}")
    
    return {
        "stream": stream,
        "youtubeKey": youtube_key,
        "proxyDns": proxy_address,
        "proxyPod": proxy_pod,
        "generation": stream_generation.get(stream, 1)
    }
