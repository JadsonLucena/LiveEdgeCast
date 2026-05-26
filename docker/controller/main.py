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
import copy
from typing import Dict, Optional
from xml.etree import ElementTree as ET
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

allocation_lock = threading.Lock()

stream_to_worker: Dict[str, str] = {}

worker_to_stream: Dict[str, str] = {}

stream_to_proxy: Dict[str, str] = {}
stream_generation: Dict[str, int] = {}
stream_registry: Dict[str, Dict[str, float]] = {}

registry_health_task: Optional[asyncio.Task] = None
worker_health_task: Optional[asyncio.Task] = None
worker_orphan_sweeper_task: Optional[asyncio.Task] = None
PROXY_HEALTHCHECK_INTERVAL_SECONDS = 3
PROXY_HEALTHCHECK_MAX_FAILURES = 3
PROXY_HEALTHCHECK_TIMEOUT_SECONDS = 2
PROXY_HEALTHCHECK_MAX_CONCURRENCY = 20
PROXY_HEALTHCHECK_JITTER_SECONDS = 1.5
WORKER_HEALTHCHECK_INTERVAL_SECONDS = 3
WORKER_HEALTHCHECK_MAX_FAILURES = 3
WORKER_HEALTHCHECK_JITTER_SECONDS = 1.5
WORKER_READY_HEALTH_DELAY_SECONDS = 3  # Wait after worker Ready before worker /health probes.
WORKER_ORPHAN_SWEEP_INTERVAL_SECONDS = 60
PROXY_READY_HEALTH_DELAY_SECONDS = 3  # Wait after proxy Ready before proxy /health probes.
proxy_health_failures: Dict[str, int] = {}
worker_ready_since: Dict[str, float] = {}
worker_health_failures: Dict[str, int] = {}
worker_pod_uid_by_name: Dict[str, str] = {}
proxy_ready_since: Dict[str, float] = {}

STATE_CONFIGMAP_NAME = "controller-state"
STATE_CONFIGMAP_KEY = "state.json"
STATE_SCHEMA_VERSION = 2

metrics_collection_task: Optional[asyncio.Task] = None

pod_cpu_usage_percent = Gauge('pod_cpu_usage_percent','Pod CPU usage percentage (0-100)',['pod','namespace'])
pod_memory_usage_bytes = Gauge('pod_memory_usage_bytes','Pod memory usage in bytes',['pod','namespace'])
pod_memory_usage_percent = Gauge('pod_memory_usage_percent','Pod memory usage as percent of limit',['pod','namespace'])
pod_network_io_bytes_total = Counter('pod_network_io_bytes_total','Total network I/O bytes',['pod','direction'])
pod_ready_status = Gauge('pod_ready_status','Is pod ready (0 or 1)',['pod','namespace'])
proxy_active_connections = Gauge('proxy_active_connections','Active RTMP connections to proxy',['proxy_pod'])
proxy_bandwidth_mbps = Gauge('proxy_bandwidth_mbps','Current proxy bandwidth in Mbps',['proxy_pod'])
worker_pods_available = Gauge('worker_pods_available','Available worker pods for allocation',['namespace'])
stream_proxy_handover_counter = Counter('stream_proxy_handover_total','Total proxy handovers accepted by controller',['stream'])
handover_attempts_total = Counter('handover_attempts_total', 'Total proxy handover attempts', ['stream'])
handover_success_total = Counter('handover_success_total', 'Total successful proxy handovers', ['stream'])
handover_conflict_total = Counter('handover_conflict_total', 'Total conflicting proxy handovers denied', ['stream'])

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

    pod_spec = copy.deepcopy(template.spec)
    pod_metadata = copy.deepcopy(template.metadata) if template.metadata else client.V1ObjectMeta()

    pod_spec.restart_policy = "Always"

    for c in pod_spec.containers:
        env = list(c.env or [])
        env = [e for e in env if e.name not in ("STREAM_KEY", "PROXY_DNS")]
        env.append(client.V1EnvVar(name="STREAM_KEY", value=stream))
        env.append(client.V1EnvVar(name="PROXY_DNS", value=proxy_dns))
        c.env = env

    labels = dict(pod_metadata.labels or {})
    labels.update({"app": "worker", "stream": stream})

    logger.debug(
        f"[Worker Pod Create] pod_name='{pod_name}' stream='{stream}' proxy_dns='{proxy_dns}'"
    )

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

def register_or_refresh_stream(stream: str, proxy_pod: str):
    """
    Creates or refreshes canonical stream ownership on proxy.
    """
    if stream not in stream_generation:
        stream_generation[stream] = 1
    stream_registry[stream] = {
        "proxy_pod": proxy_pod,
    }
    stream_to_proxy[stream] = proxy_pod
    proxy_health_failures[proxy_pod] = 0
    return None




def is_stream_active_on_proxy(proxy_pod: str, stream: str) -> bool:
    """Checks NGINX RTMP stats to confirm whether stream is still active on a proxy."""
    try:
        target = resolve_proxy_address(proxy_pod)
        response = requests.get(f"http://{target}:8080/stats", timeout=PROXY_HEALTHCHECK_TIMEOUT_SECONDS)
        if response.status_code != 200:
            logger.warning(
                f"[Handover] Unable to verify stream activity on proxy '{proxy_pod}'. "
                f"/stats status_code={response.status_code}"
            )
            return True

        root = ET.fromstring(response.text)
        for node in root.findall('.//application/live/stream/name'):
            if (node.text or '').strip() == stream:
                return True
        return False
    except Exception as e:
        logger.warning(f"[Handover] Failed parsing /stats for proxy '{proxy_pod}': {e}")
        # Fail-safe: if we cannot prove stream is gone, keep ownership with current proxy.
        return True

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
        owner_unhealthy = get_proxy_health_status(current_owner) == "unhealthy"

    owner_stream_active = is_stream_active_on_proxy(current_owner, stream)

    if owner_unhealthy or not owner_stream_active:
        logger.info(
            f"[Handover] Stream '{stream}' ownership moved from '{current_owner}' "
            f"to '{candidate_proxy_pod}' (owner_unhealthy={owner_unhealthy}, owner_stream_active={owner_stream_active})"
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
        f"to '{candidate_proxy_pod}' (owner_unhealthy={owner_unhealthy}, owner_stream_active={owner_stream_active})"
    )
    return False



async def monitor_stream_registry_health():
    """
    Controller-driven health monitoring:
    - A cada 3s verifica /health de cada proxy com stream ativa
    - Após 3 falhas consecutivas, expira todas as streams daquele proxy
    """
    semaphore = asyncio.Semaphore(PROXY_HEALTHCHECK_MAX_CONCURRENCY)

    async def run_proxy_check(proxy_pod: str):
        if PROXY_HEALTHCHECK_JITTER_SECONDS > 0:
            await asyncio.sleep(random.uniform(0, PROXY_HEALTHCHECK_JITTER_SECONDS))

        async with semaphore:
            health_status = await asyncio.to_thread(get_proxy_health_status, proxy_pod)

        with allocation_lock:
            if health_status == "healthy":
                proxy_health_failures[proxy_pod] = 0
            elif health_status == "warming_up":
                logger.debug(
                    f"[ProxyHealth] Proxy '{proxy_pod}' is warming up; "
                    "waiting before counting /health probe failures."
                )
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
                            old_uid = worker_pod_uid_by_name.pop(worker_name, None)
                            if old_uid:
                                worker_health_failures.pop(old_uid, None)
                            try:
                                core.delete_namespaced_pod(name=worker_name, namespace=NAMESPACE, grace_period_seconds=0)
                            except Exception as e:
                                logger.warning(f"[ProxyHealth] Failed deleting worker {worker_name}: {e}")

                    proxy_health_failures.pop(proxy_pod, None)
                    proxy_ready_since.pop(proxy_pod, None)
                    persist_state_locked()

    while True:
        await asyncio.sleep(PROXY_HEALTHCHECK_INTERVAL_SECONDS)

        with allocation_lock:
            proxies = {entry.get("proxy_pod") for entry in stream_registry.values() if entry.get("proxy_pod")}

        if proxies:
            await asyncio.gather(*(run_proxy_check(proxy_pod) for proxy_pod in proxies))


def resolve_proxy_address(proxy_pod: str) -> str:
    """Retorna o IP atual do pod proxy owner da stream."""
    if not proxy_pod:
        raise RuntimeError("proxy_pod is required to resolve proxy address")

    pod = core.read_namespaced_pod(name=proxy_pod, namespace=NAMESPACE)
    pod_ip = (pod.status.pod_ip or "").strip() if pod and pod.status else ""
    if not pod_ip:
        raise RuntimeError(f"proxy pod '{proxy_pod}' has no assigned pod IP")
    return pod_ip


def get_proxy_health_status(proxy_pod: str) -> str:
    """Returns proxy health status without counting NotReady/warm-up as probe failures."""
    try:
        pod = core.read_namespaced_pod(name=proxy_pod, namespace=NAMESPACE)
        ready = any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))
        if not ready:
            proxy_ready_since.pop(proxy_pod, None)
            return "not_ready"

        now = time.time()
        first_ready_at = proxy_ready_since.get(proxy_pod)
        if first_ready_at is None:
            proxy_ready_since[proxy_pod] = now
            logger.debug(
                f"[ProxyHealth] Proxy '{proxy_pod}' became Ready. Starting proxy delay timer "
                f"({PROXY_READY_HEALTH_DELAY_SECONDS}s) before /health probe."
            )
            return "warming_up"
        if (now - first_ready_at) < PROXY_READY_HEALTH_DELAY_SECONDS:
            logger.debug(
                f"[ProxyHealth] Waiting {PROXY_READY_HEALTH_DELAY_SECONDS}s after Ready for '{proxy_pod}' "
                f"before probing /health ({now - first_ready_at:.1f}s elapsed)."
            )
            return "warming_up"

        target = resolve_proxy_address(proxy_pod)
        response = requests.get(f"http://{target}:8080/health", timeout=PROXY_HEALTHCHECK_TIMEOUT_SECONDS)
        return "healthy" if response.status_code == 200 else "unhealthy"
    except ApiException as e:
        if e.status == 404:
            return "unhealthy"
        logger.warning(f"[ProxyHealth] Failed to read proxy pod '{proxy_pod}': {e}")
        return "unhealthy"
    except Exception as e:
        logger.warning(f"[ProxyHealth] Failed to check /health for proxy '{proxy_pod}': {e}")
        return "unhealthy"


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

        # Sem estado persistido: não reaproveitar pods já existentes.
        # No modelo por-env (STREAM_KEY/PROXY_DNS), reuso pode carregar config obsoleta.
        logger.info("[State Recovery] No persisted state found. Skipping worker auto-recovery to avoid stale env reuse.")


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
    """Worker health monitor using worker-specific Ready-to-/health delay before probing."""
    """Controller-driven worker healthcheck every 3s, with 3 consecutive failures threshold."""
    while True:
        await asyncio.sleep(WORKER_HEALTHCHECK_INTERVAL_SECONDS)
        to_replace = []

        with allocation_lock:
            allocations = list(stream_to_worker.items())
            stream_owner_snapshot = {
                stream: entry.get("proxy_pod")
                for stream, entry in stream_registry.items()
            }

        # Health check for allocated workers
        for stream, worker_pod in allocations:
            owner_proxy = stream_owner_snapshot.get(stream)
            if not owner_proxy:
                logger.debug(
                    f"[WorkerHealth] Stream '{stream}' has no owner proxy in snapshot; skipping worker '{worker_pod}' health action."
                )
                continue

            if WORKER_HEALTHCHECK_JITTER_SECONDS > 0:
                await asyncio.sleep(random.uniform(0, WORKER_HEALTHCHECK_JITTER_SECONDS))

            healthy = False
            current_uid = ""
            try:
                pod = core.read_namespaced_pod(name=worker_pod, namespace=NAMESPACE)
                current_uid = ((pod.metadata.uid or "").strip() if pod and pod.metadata else "")
                prev_uid = worker_pod_uid_by_name.get(worker_pod)
                if prev_uid and current_uid and prev_uid != current_uid:
                    worker_health_failures.pop(prev_uid, None)
                    worker_health_failures[current_uid] = 0
                    worker_ready_since.pop(worker_pod, None)
                if current_uid:
                    worker_pod_uid_by_name[worker_pod] = current_uid

                ready = any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))
                if not ready:
                    worker_ready_since.pop(worker_pod, None)
                    if current_uid:
                        worker_health_failures.pop(current_uid, None)
                    continue

                now = time.time()
                first_ready_at = worker_ready_since.get(worker_pod)
                if first_ready_at is None:
                    worker_ready_since[worker_pod] = now
                    if current_uid:
                        worker_health_failures[current_uid] = 0
                    logger.debug(f"[WorkerHealth] Worker '{worker_pod}' became Ready. Starting worker delay timer ({WORKER_READY_HEALTH_DELAY_SECONDS}s) before /health probe.")
                    continue

                if (now - first_ready_at) < WORKER_READY_HEALTH_DELAY_SECONDS:
                    logger.debug(
                        f"[WorkerHealth] Waiting worker delay of {WORKER_READY_HEALTH_DELAY_SECONDS}s after Ready for '{worker_pod}' "
                        f"before probing /health ({now - first_ready_at:.1f}s elapsed)."
                    )
                    continue

                owner_proxy = stream_registry.get(stream, {}).get("proxy_pod")
                if not owner_proxy:
                    logger.debug(f"[WorkerHealth] Stream '{stream}' has no proxy owner; skipping worker check.")
                    if current_uid:
                        worker_health_failures.pop(current_uid, None)
                    continue

                owner_proxy_health = get_proxy_health_status(owner_proxy)
                if owner_proxy_health != "healthy":
                    logger.debug(
                        f"[WorkerHealth] Skipping worker '{worker_pod}' check because owner proxy "
                        f"'{owner_proxy}' is {owner_proxy_health}."
                    )
                    if current_uid:
                        worker_health_failures.pop(current_uid, None)
                    continue

                healthy = check_worker_health(worker_pod, pod.status.pod_ip)
            except Exception:
                healthy = False

            if healthy:
                if current_uid:
                    worker_health_failures[current_uid] = 0
                continue

            failures = worker_health_failures.get(current_uid, 0) + 1 if current_uid else 1
            if current_uid:
                worker_health_failures[current_uid] = failures
            logger.warning(
                f"[WorkerHealth] Worker '{worker_pod}' failed healthcheck for stream '{stream}' "
                f"({failures}/{WORKER_HEALTHCHECK_MAX_FAILURES})"
            )
            if failures >= WORKER_HEALTHCHECK_MAX_FAILURES:
                to_replace.append((stream, worker_pod))

        # Não reaproveitar pods prontos para pendências; sempre criar pod novo na alocação explícita.

        # Handle unhealthy workers
        if to_replace:
            with allocation_lock:
                for stream, worker_pod in to_replace:
                    allocated = stream_to_worker.get(stream)
                    if allocated != worker_pod:
                        continue

                    owner_proxy = stream_registry.get(stream, {}).get("proxy_pod")
                    if not owner_proxy:
                        logger.warning(
                            f"[WorkerHealth] Cannot replace worker '{worker_pod}' for stream '{stream}' "
                            "because the stream has no proxy owner."
                        )
                        continue

                    if get_proxy_health_status(owner_proxy) != "healthy":
                        logger.info(
                            f"[WorkerHealth] Delaying replacement of worker '{worker_pod}' for stream '{stream}' "
                            f"because owner proxy '{owner_proxy}' is not healthy."
                        )
                        continue

                    logger.warning(f"[WorkerHealth] Worker '{worker_pod}' unhealthy for stream '{stream}'. Replacing.")
                    try:
                        proxy_dns = resolve_proxy_address(owner_proxy)
                        new_worker = create_worker_pod_for_stream(stream=stream, proxy_dns=proxy_dns)
                    except Exception as e:
                        logger.warning(
                            f"[WorkerHealth] Failed to create replacement worker for stream '{stream}': {e}"
                        )
                        continue

                    stream_to_worker[stream] = new_worker
                    worker_to_stream.pop(worker_pod, None)
                    worker_to_stream[new_worker] = stream
                    worker_ready_since.pop(worker_pod, None)
                    old_uid = worker_pod_uid_by_name.pop(worker_pod, None)
                    if old_uid:
                        worker_health_failures.pop(old_uid, None)
            
                    try:
                        core.delete_namespaced_pod(name=worker_pod, namespace=NAMESPACE, grace_period_seconds=0)
                    except Exception as e:
                        logger.warning(f"[WorkerHealth] Failed to delete pod {worker_pod}: {e}")

                    logger.info(
                        f"[WorkerHealth] Replaced unhealthy worker for stream '{stream}': "
                        f"old='{worker_pod}' new='{new_worker}' proxy='{owner_proxy}'"
                    )
                persist_state_locked()


async def sweep_orphan_workers():
    """Safety-net: periodically delete worker pods that are not mapped in controller state."""
    while True:
        await asyncio.sleep(WORKER_ORPHAN_SWEEP_INTERVAL_SECONDS)

        try:
            pods = core.list_namespaced_pod(namespace=NAMESPACE, label_selector="app=worker").items
        except Exception as e:
            logger.warning(f"[OrphanSweeper] Failed to list worker pods: {e}")
            continue

        with allocation_lock:
            mapped_workers = set(stream_to_worker.values())

        for pod in pods:
            pod_name = pod.metadata.name if pod and pod.metadata else None
            if not pod_name:
                continue
            if pod_name in mapped_workers:
                continue

            # Double-check under lock right before deletion to avoid race with recent allocations.
            with allocation_lock:
                still_orphan = pod_name not in set(stream_to_worker.values())

            if not still_orphan:
                logger.debug(f"[OrphanSweeper] Pod '{pod_name}' became mapped before deletion; skipping.")
                continue

            logger.warning(f"[OrphanSweeper] Deleting orphan worker pod '{pod_name}'")
            try:
                core.delete_namespaced_pod(name=pod_name, namespace=NAMESPACE, grace_period_seconds=0)
            except Exception as e:
                logger.warning(f"[OrphanSweeper] Failed deleting orphan worker pod '{pod_name}': {e}")

@app.on_event("startup")
async def startup_event():
    global registry_health_task, worker_health_task, worker_orphan_sweeper_task, metrics_collection_task
    time.sleep(5)
    recover_state()
    registry_health_task = asyncio.create_task(monitor_stream_registry_health())
    worker_health_task = asyncio.create_task(monitor_worker_health())
    worker_orphan_sweeper_task = asyncio.create_task(sweep_orphan_workers())
    metrics_collection_task = asyncio.create_task(collect_infrastructure_metrics())


@app.on_event("shutdown")
async def shutdown_event():
    global registry_health_task, worker_health_task, worker_orphan_sweeper_task, metrics_collection_task
    if registry_health_task and not registry_health_task.done():
        registry_health_task.cancel()
    if worker_health_task and not worker_health_task.done():
        worker_health_task.cancel()
    if worker_orphan_sweeper_task and not worker_orphan_sweeper_task.done():
        worker_orphan_sweeper_task.cancel()
    if metrics_collection_task and not metrics_collection_task.done():
        metrics_collection_task.cancel()


@app.get("/health")
def health():
    return {"status": "ok"}


def allocate_worker(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(None, description="Proxy pod name for pull-only architecture"),
    ownership_already_verified: bool = False,
):
    """
    Aloca um worker dedicado para uma stream.
    Controller é a ÚNICA fonte da verdade para scale-up.

    Estratégia de concorrência:
    - usar lock apenas para decisões/mutação de estado interno
    - executar I/O Kubernetes fora do lock
    - revalidar estado ao voltar do I/O para evitar corridas
    """

    with allocation_lock:
        if proxy_pod and not ownership_already_verified:
            if not try_handover_stream_owner(stream, proxy_pod):
                owner = stream_registry.get(stream, {}).get("proxy_pod")
                persist_state_locked()
                raise HTTPException(
                    status_code=409,
                    detail=f"stream '{stream}' owned by proxy '{owner}'"
                )
            persist_state_locked()

        existing_worker = stream_to_worker.get(stream)
        owner_proxy = stream_registry.get(stream, {}).get("proxy_pod")
        generation_snapshot = stream_generation.get(stream)

    if not owner_proxy:
        raise HTTPException(status_code=409, detail=f"stream '{stream}' has no proxy owner")

    proxy_address = resolve_proxy_address(owner_proxy)

    if existing_worker:
        logger.info(
            f"[Allocate] Idempotent replay for stream '{stream}' "
            f"existing worker={existing_worker} proxy={proxy_address}"
        )
        return {
            "pod": f"{existing_worker}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local",
            "name": existing_worker,
            "proxy": proxy_address,
            "status": "idempotent_replay"
        }

    pod_name = create_worker_pod_for_stream(stream=stream, proxy_dns=proxy_address)

    with allocation_lock:
        current_worker = stream_to_worker.get(stream)
        current_owner = stream_registry.get(stream, {}).get("proxy_pod")
        current_generation = stream_generation.get(stream)

        if current_worker:
            logger.info(
                f"[Allocate] Concurrent allocation detected for stream '{stream}'. "
                f"Discarding newly created worker '{pod_name}' and keeping '{current_worker}'."
            )
            try:
                core.delete_namespaced_pod(name=pod_name, namespace=NAMESPACE, grace_period_seconds=0)
            except ApiException as e:
                logger.warning(f"[Allocate] Failed deleting extra worker pod {pod_name}: {e}")
            return {
                "pod": f"{current_worker}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local",
                "name": current_worker,
                "proxy": proxy_address,
                "status": "idempotent_replay"
            }

        if current_owner != owner_proxy or current_generation != generation_snapshot:
            logger.warning(
                f"[Allocate] Ownership changed while creating worker for stream '{stream}'. "
                f"expected_owner='{owner_proxy}' current_owner='{current_owner}'. Deleting '{pod_name}'."
            )
            try:
                core.delete_namespaced_pod(name=pod_name, namespace=NAMESPACE, grace_period_seconds=0)
            except ApiException as e:
                logger.warning(f"[Allocate] Failed deleting stale worker pod {pod_name}: {e}")
            raise HTTPException(status_code=409, detail=f"stream '{stream}' ownership changed during allocation")

        stream_to_worker[stream] = pod_name
        worker_to_stream[pod_name] = stream
        persist_state_locked()

    worker_dns = f"{pod_name}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local"
    logger.info(f"[Allocate] Created dedicated worker pod {pod_name} for stream '{stream}'")
    return {"pod": worker_dns, "name": pod_name, "proxy": proxy_address, "worker": pod_name, "status": "created"}


async def release_worker(stream: str = Query(..., description="Stream name to release")):
    """
    Libera worker alocado para uma stream e SEMPRE limpa estado canônico residual.
    Idempotente: se não houver worker, ainda remove ownership/mapeamentos restantes.
    """

    worker_name = None
    changed = False
    response_status = "not_found"

    with allocation_lock:
        worker_name = stream_to_worker.pop(stream, None)

        if worker_name:
            changed = True
            response_status = "released"
            worker_to_stream.pop(worker_name, None)
            worker_ready_since.pop(worker_name, None)
            old_uid = worker_pod_uid_by_name.pop(worker_name, None)
            if old_uid:
                worker_health_failures.pop(old_uid, None)

        if stream_to_proxy.pop(stream, None) is not None:
            changed = True
        if stream_registry.pop(stream, None) is not None:
            changed = True
        if stream_generation.pop(stream, None) is not None:
            changed = True

        if changed:
            persist_state_locked()

    if worker_name:
        logger.info(f"[Release] Released worker {worker_name} from stream '{stream}'")
        try:
            core.delete_namespaced_pod(name=worker_name, namespace=NAMESPACE, grace_period_seconds=0)
        except ApiException as e:
            logger.warning(f"[Release] Failed deleting worker pod {worker_name}: {e}")
        return {
            "status": response_status,
            "stream": stream,
            "worker": worker_name
        }

    if changed:
        logger.info(f"[Release] Cleaned residual state for stream '{stream}' without active worker")
        return {"status": "state_cleaned", "stream": stream}

    logger.info(f"[Release] Idempotent replay: stream '{stream}' not found")
    return {"status": "not_found", "stream": stream}


def register_stream(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(..., description="Proxy pod name")
):
    with allocation_lock:
        current = stream_registry.get(stream)
        was_replay = current and current.get("proxy_pod") == proxy_pod

        if not try_handover_stream_owner(stream, proxy_pod):
            current_owner = stream_registry.get(stream, {}).get("proxy_pod")
            raise HTTPException(
                status_code=409,
                detail=f"stream '{stream}' already owned by proxy '{current_owner}'"
            )

        persist_state_locked()

        if was_replay:
            logger.info(f"[Register] Idempotent replay for stream '{stream}' on proxy '{proxy_pod}'")
            status = "idempotent_replay"
        else:
            logger.info(f"[Register] State changed for stream '{stream}' owner='{proxy_pod}'")
            status = "registered"

        return {
            "status": status,
            "stream": stream,
            "proxy_pod": proxy_pod,
            "healthcheck_interval_seconds": PROXY_HEALTHCHECK_INTERVAL_SECONDS,
            "max_failed_healthchecks": PROXY_HEALTHCHECK_MAX_FAILURES
        }









@app.post("/streams/started")
def stream_started(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(..., description="Proxy pod that received publish")
):
    """Single controller entrypoint when proxy publish starts.
    Controller performs register/allocation/start orchestration.
    """
    registration = register_stream(stream=stream, proxy_pod=proxy_pod)
    allocation = allocate_worker(stream=stream, proxy_pod=proxy_pod, ownership_already_verified=True)

    replay = registration.get("status") == "idempotent_replay" and allocation.get("status") == "idempotent_replay"
    event_status = "idempotent_replay" if replay else "started_event_processed"
    log_prefix = "Idempotent replay" if replay else "State changed"
    logger.info(f"[StreamsStarted] {log_prefix} for stream '{stream}' proxy='{proxy_pod}'")

    return {
        "status": event_status,
        "registration": registration,
        "stream": stream,
        "allocation": allocation,
    }
@app.post("/streams/ended")
async def stream_ended(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(None, description="Proxy pod that ended publish"),
    generation: int = Query(None, description="Optional stream generation tied to publish session")
):
    """Single controller entrypoint when proxy publish ends.
    Cleanup is applied only if the ended event still matches current stream ownership/session.
    """
    with allocation_lock:
        current = stream_registry.get(stream)
        current_owner = current.get("proxy_pod") if current else None
        current_generation = stream_generation.get(stream)

    owner_mismatch = bool(proxy_pod and current_owner and current_owner != proxy_pod)
    generation_mismatch = bool(generation is not None and current_generation is not None and generation != current_generation)

    if owner_mismatch or generation_mismatch:
        logger.warning(
            f"[StreamsEnded] Ignoring stale ended event for stream '{stream}' "
            f"proxy='{proxy_pod}' generation='{generation}' "
            f"current_owner='{current_owner}' current_generation='{current_generation}'"
        )
        return {
            "status": "stale_event_ignored",
            "stream": stream,
            "current_owner": current_owner,
            "current_generation": current_generation,
        }

    with allocation_lock:
        if current_owner and (not proxy_pod or current_owner == proxy_pod):
            stream_registry.pop(stream, None)
            stream_to_proxy.pop(stream, None)

    release_result = await release_worker(stream=stream)
    replay = release_result.get("status") == "not_found"
    event_status = "idempotent_replay" if replay else "ended"
    logger.info(
        f"[StreamsEnded] {'Idempotent replay' if replay else 'State changed'} for stream '{stream}' "
        f"proxy='{proxy_pod}' generation='{generation}' release_status='{release_result.get('status')}'"
    )
    return {"status": event_status, "stream": stream, "release": release_result}

@app.get('/metrics')
def metrics():
    return Response(generate_latest(), media_type='text/plain; version=0.0.4; charset=utf-8')
