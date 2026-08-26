from fastapi import FastAPI, Query, HTTPException
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
import random
import threading
import requests
import time
import logging
import asyncio
import json
from typing import Dict, Optional
from xml.etree import ElementTree as ET

logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [%(levelname)s] [%(funcName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = FastAPI()

NAMESPACE = "media"

allocation_lock = threading.Lock()

stream_to_proxy: Dict[str, str] = {}
stream_generation: Dict[str, int] = {}
stream_registry: Dict[str, Dict[str, float]] = {}

registry_health_task: Optional[asyncio.Task] = None
PROXY_HEALTHCHECK_INTERVAL_SECONDS = 3
PROXY_HEALTHCHECK_MAX_FAILURES = 3
PROXY_HEALTHCHECK_TIMEOUT_SECONDS = 2
PROXY_HEALTHCHECK_MAX_CONCURRENCY = 20
PROXY_HEALTHCHECK_JITTER_SECONDS = 1.5
PROXY_READY_HEALTH_DELAY_SECONDS = 3  # Wait after proxy Ready before proxy /health probes.
proxy_health_failures: Dict[str, int] = {}
proxy_ready_since: Dict[str, float] = {}

STATE_CONFIGMAP_NAME = "controller-state"
STATE_CONFIGMAP_KEY = "state.json"
STATE_SCHEMA_VERSION = 3

try:
    config.load_incluster_config()
    logger.info("Loaded in-cluster Kubernetes config")
except:
    config.load_kube_config()
    logger.info("Loaded local kubeconfig")

core = client.CoreV1Api()


def persist_state_locked() -> None:
    """
    Persists critical controller state to a ConfigMap to survive pod restart/crash.
    Must be called only while holding allocation_lock.
    """
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
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

    stream_to_proxy.clear()
    stream_to_proxy.update(data.get("stream_to_proxy", {}))
    stream_registry.clear()
    stream_registry.update(data.get("stream_registry", {}))
    stream_generation.clear()
    stream_generation.update(data.get("stream_generation", {}))
    return True


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
    current = stream_registry.get(stream)
    if not current:
        register_or_refresh_stream(stream, candidate_proxy_pod)
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

        return True

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


def recover_state():
    """Restore persisted stream ownership after a controller restart."""
    logger.info("[State Recovery] Starting state recovery...")
    with allocation_lock:
        restored = restore_persisted_state_locked()
        if restored:
            logger.info(
                f"[State Recovery] Restored persisted state with {len(stream_registry)} active streams."
            )
        else:
            logger.info("[State Recovery] No persisted state found.")


@app.on_event("startup")
async def startup_event():
    global registry_health_task
    time.sleep(5)
    recover_state()
    registry_health_task = asyncio.create_task(monitor_stream_registry_health())


@app.on_event("shutdown")
async def shutdown_event():
    global registry_health_task
    if registry_health_task and not registry_health_task.done():
        registry_health_task.cancel()


@app.get("/health")
def health():
    return {"status": "ok"}


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
    Controller records stream ownership only; worker reconciliation is intentionally out of scope for this cleanup phase.
    """
    registration = register_stream(stream=stream, proxy_pod=proxy_pod)

    replay = registration.get("status") == "idempotent_replay"
    event_status = "idempotent_replay" if replay else "started_event_processed"
    log_prefix = "Idempotent replay" if replay else "State changed"
    logger.info(f"[StreamsStarted] {log_prefix} for stream '{stream}' proxy='{proxy_pod}'")

    return {
        "status": event_status,
        "registration": registration,
        "stream": stream,
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
        generation_mismatch = bool(
            generation is not None and
            current_generation is not None and
            generation != current_generation
        )

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

        changed = False
        if stream_to_proxy.pop(stream, None) is not None:
            changed = True
        if stream_registry.pop(stream, None) is not None:
            changed = True
        if stream_generation.pop(stream, None) is not None:
            changed = True
        if changed:
            persist_state_locked()

    if changed:
        logger.info(f"[Release] Cleaned stream state for '{stream}'")
        release_result = {"status": "state_cleaned", "stream": stream}
    else:
        logger.info(f"[Release] Idempotent replay: stream '{stream}' not found")
        release_result = {"status": "not_found", "stream": stream}
    replay = release_result.get("status") == "not_found"
    event_status = "idempotent_replay" if replay else "ended"
    logger.info(
        f"[StreamsEnded] {'Idempotent replay' if replay else 'State changed'} for stream '{stream}' "
        f"proxy='{proxy_pod}' generation='{generation}' release_status='{release_result.get('status')}'"
    )
    return {"status": event_status, "stream": stream, "release": release_result}
