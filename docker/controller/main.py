from fastapi import FastAPI, Query, HTTPException
from kubernetes import client, config
import random
import string
import threading
import requests
import time
import logging
import asyncio
from typing import Dict, Optional
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from fastapi.responses import Response

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [%(levelname)s] [%(funcName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = FastAPI()

NAMESPACE = "media"
WORKER_DEPLOYMENT = "rtmp-worker"
WORKER_SERVICE = "rtmp-worker"
SCALE_DOWN_DELAY = 180  # 3 minutos após último release

# Lock para evitar race conditions
allocation_lock = threading.Lock()

# Mapeamento: stream_name → worker_pod_name
stream_to_worker: Dict[str, str] = {}

# Mapeamento inverso: worker_pod_name → stream_name
worker_to_stream: Dict[str, str] = {}

# Mapeamento: stream_name → proxy_pod_name (Pull-Only Architecture)
stream_to_proxy: Dict[str, str] = {}
# Registro efêmero de streams no proxy com TTL
# Mapeia: stream_name -> {"proxy_pod": str, "expires_at": float}
stream_registry: Dict[str, Dict[str, float]] = {}

# Rastreia streams aguardando worker (evita múltiplas escalações para mesma stream)
# Mapeia: stream_name → timestamp da primeira solicitação
streams_pending_allocation: Dict[str, float] = {}

# Timestamp do último release (para scale-down automático)
last_release_time: Optional[float] = None
scale_down_task: Optional[asyncio.Task] = None
registry_health_task: Optional[asyncio.Task] = None
PROXY_HEALTHCHECK_INTERVAL_SECONDS = 5
PROXY_HEALTHCHECK_MAX_FAILURES = 3
PROXY_HEALTHCHECK_TIMEOUT_SECONDS = 2
PROXY_HEALTHCHECK_MAX_CONCURRENCY = 20
PROXY_HEALTHCHECK_JITTER_SECONDS = 1.5
STREAM_TTL_SECONDS = PROXY_HEALTHCHECK_INTERVAL_SECONDS * PROXY_HEALTHCHECK_MAX_FAILURES * PROXY_HEALTHCHECK_TIMEOUT_SECONDS * PROXY_HEALTHCHECK_JITTER_SECONDS
proxy_health_failures: Dict[str, int] = {}

# Rastreia último heartbeat de cada stream (para renovar TTL mesmo se healthcheck falhar)
# Mapeia: stream_name → timestamp do último heartbeat
stream_last_heartbeat: Dict[str, float] = {}

# Stream lifecycle tracking
stream_start_time: Dict[str, float] = {}
stream_interruptions: Dict[str, int] = {}
stream_downtime: Dict[str, float] = {}

# Recovery tracking
recovery_attempts: Dict[str, int] = {}
recovery_successes: Dict[str, int] = {}

# Background tasks
metrics_collection_task: Optional[asyncio.Task] = None

# Tier 1/2/3 Prometheus metrics
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

# Load Kubernetes credentials (inside cluster)
try:
    config.load_incluster_config()
    logger.info("Loaded in-cluster Kubernetes config")
except:
    config.load_kube_config()
    logger.info("Loaded local kubeconfig")

apps = client.AppsV1Api()
core = client.CoreV1Api()


def random_suffix():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))


def cleanup_expired_streams() -> None:
    """
    Remove streams expiradas do registro efêmero.
    Streams com heartbeat recente (últimos 10s) não expiram mesmo se TTL passou.
    Também remove stream_to_proxy para manter consistência.
    """
    now = time.time()
    expired = []
    HEARTBEAT_GRACE_PERIOD = 10  # Streams com heartbeat recente não expiram

    for stream, entry in stream_registry.items():
        last_heartbeat = stream_last_heartbeat.get(stream, 0)
        time_since_heartbeat = now - last_heartbeat
        
        # Expirar se TTL passou E não há heartbeat recente
        if entry.get("expires_at", 0) <= now and time_since_heartbeat > HEARTBEAT_GRACE_PERIOD:
            expired.append(stream)

    for stream in expired:
        stream_registry.pop(stream, None)
        stream_to_proxy.pop(stream, None)
        stream_last_heartbeat.pop(stream, None)
        logger.info(f"[Registry] Stream '{stream}' expired (no heartbeat for {HEARTBEAT_GRACE_PERIOD}s)")


def register_or_refresh_stream(stream: str, proxy_pod: str):
    """
    Cria ou renova registro efêmero da stream no proxy.
    """
    expires_at = time.time() + STREAM_TTL_SECONDS
    stream_registry[stream] = {
        "proxy_pod": proxy_pod,
        "expires_at": expires_at
    }
    stream_to_proxy[stream] = proxy_pod
    proxy_health_failures[proxy_pod] = 0
    return expires_at


def register_or_refresh_stream_if_owner_matches(stream: str, proxy_pod: str):
    """
    Renova registro apenas se:
    - stream não existe ainda, ou
    - stream já pertence ao mesmo proxy_pod
    """
    current = stream_registry.get(stream)
    if current and current.get("proxy_pod") != proxy_pod:
        return None
    return register_or_refresh_stream(stream, proxy_pod)


async def schedule_scale_down_if_idle():
    """
    Agenda scale-down automático dos workers se ficarem idle por tempo suficiente.
    Aguarda SCALE_DOWN_DELAY (10min) após último release antes de reduzir para 0.
    """
    global last_release_time, scale_down_task
    
    last_release_time = time.time()
    
    # Aguardar delay
    await asyncio.sleep(SCALE_DOWN_DELAY)
    
    # Verificar se ainda não há workers alocados
    with allocation_lock:
        if len(stream_to_worker) == 0:
            try:
                current = apps.read_namespaced_deployment_scale(
                    name=WORKER_DEPLOYMENT,
                    namespace=NAMESPACE
                )
                
                current_replicas = current.spec.replicas if current.spec.replicas is not None else 0
                
                if current_replicas > 0:
                    # Reduzir para 0 (todos workers idle)
                    body = {"spec": {"replicas": 0}}
                    apps.patch_namespaced_deployment_scale(
                        name=WORKER_DEPLOYMENT,
                        namespace=NAMESPACE,
                        body=body
                    )
                    logger.info(f"[AutoScaleDown] Reduced workers to 0 (idle for {SCALE_DOWN_DELAY}s)")
            except Exception as e:
                logger.error(f"[AutoScaleDown] Failed to scale down: {e}")


def check_proxy_health(proxy_pod: str) -> bool:
    """
    Check proxy health using TCP connection to RTMP port (1935).
    TCP check is more reliable than HTTP for detecting port availability.
    Detects actual RTMP availability, not just HTTP endpoint availability.
    """
    import socket
    
    proxy_ip = get_proxy_pod_ip(proxy_pod)
    if not proxy_ip:
        return False

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(PROXY_HEALTHCHECK_TIMEOUT_SECONDS)
        result = sock.connect_ex((proxy_ip, 1935))
        sock.close()
        
        if result == 0:
            logger.debug(f"[ProxyHealth] TCP check passed for {proxy_pod}:{proxy_ip}:1935")
            return True
        else:
            logger.warning(f"[ProxyHealth] TCP check failed for {proxy_pod}:{proxy_ip}:1935 (errno={result})")
            return False
    except Exception as e:
        logger.warning(f"[ProxyHealth] TCP healthcheck failed for {proxy_pod}: {e}")
        return False


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
                # Proxy online: renovar validade de todas as streams deste proxy
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
                    now = time.time()
                    HEARTBEAT_GRACE_PERIOD = 10
                    
                    for stream in impacted_streams:
                        last_heartbeat = stream_last_heartbeat.get(stream, 0)
                        time_since_heartbeat = now - last_heartbeat
                        
                        # Expirar apenas se não há heartbeat recente
                        if time_since_heartbeat > HEARTBEAT_GRACE_PERIOD:
                            stream_registry.pop(stream, None)
                            stream_to_proxy.pop(stream, None)
                            logger.info(
                                f"[Registry] Stream '{stream}' expired after "
                                f"{PROXY_HEALTHCHECK_MAX_FAILURES} failed proxy healthchecks "
                                f"(no heartbeat for {time_since_heartbeat:.1f}s)"
                            )
                        else:
                            logger.info(
                                f"[Registry] Stream '{stream}' protected by heartbeat "
                                f"({time_since_heartbeat:.1f}s since last beat)"
                            )
                    
                    proxy_health_failures.pop(proxy_pod, None)

    while True:
        await asyncio.sleep(PROXY_HEALTHCHECK_INTERVAL_SECONDS)

        with allocation_lock:
            cleanup_expired_streams()
            proxies = {entry.get("proxy_pod") for entry in stream_registry.values() if entry.get("proxy_pod")}

        if proxies:
            await asyncio.gather(*(run_proxy_check(proxy_pod) for proxy_pod in proxies))


def get_proxy_pod_ip(proxy_pod: str) -> str:
    """
    Obter IP do pod proxy para conexão direta do worker.
    
    Substitui DNS headless que não funciona sem StatefulSet/hostname.
    Worker se conecta diretamente ao IP do proxy pod via ClusterIP.
    """
    try:
        pod = core.read_namespaced_pod(name=proxy_pod, namespace=NAMESPACE)
        return pod.status.pod_ip
    except Exception as e:
        logger.error(f"[ProxyIP] Failed to get IP for {proxy_pod}: {e}")
        return None


def check_worker_metrics(pod_name: str, pod_ip: Optional[str] = None) -> int:
    """
    Verifica métricas RTMP do worker para determinar se está ocupado.
    Retorna número de clientes RTMP ativos.
    """
    try:
        # Tentar acessar /stats do worker.
        # Prioriza IP do pod (mais confiável que DNS por pod em Deployment sem headless/stateful hostname).
        target = pod_ip if pod_ip else f"{pod_name}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local"
        stats_url = f"http://{target}:8080/stats"
        response = requests.get(stats_url, timeout=2)
        
        if response.status_code == 200:
            # Parse XML simples procurando por <nclients>
            content = response.text
            if '<nclients>' in content:
                start = content.find('<nclients>') + 10
                end = content.find('</nclients>')
                if end > start:
                    return int(content[start:end])
        return 0
    except Exception as e:
        logger.warning(f"Failed to check metrics for {pod_name}: {e}")
        return 0


def recover_state():
    """
    Recupera estado de alocações após reinício do controller.
    Verifica quais workers estão realmente ocupados consultando suas métricas RTMP.
    """
    logger.info("[State Recovery] Starting state recovery...")
    
    with allocation_lock:
        pods = core.list_namespaced_pod(
            namespace=NAMESPACE,
            label_selector="app=rtmp-worker"
        ).items
        
        recovered_count = 0
        
        for pod in pods:
            if not pod.status.conditions:
                continue
                
            cond = {c.type: c.status for c in pod.status.conditions}
            if cond.get("Ready") != "True":
                continue
            
            pod_name = pod.metadata.name
            
            # Verificar se worker tem clientes RTMP ativos
            nclients = check_worker_metrics(pod_name, pod.status.pod_ip)
            
            if nclients > 0:
                # Worker está ocupado, mas não sabemos qual stream
                # Marcar como alocado com stream desconhecido
                stream_name = f"recovered_stream_{random_suffix()}"
                stream_to_worker[stream_name] = pod_name
                worker_to_stream[pod_name] = stream_name
                recovered_count += 1
                logger.info(f"[State Recovery] Worker {pod_name} is busy ({nclients} clients), marked as allocated")
        
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
        pods = core.list_namespaced_pod(namespace=NAMESPACE,label_selector='app in (rtmp-proxy, rtmp-worker)').items
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
            current = apps.read_namespaced_deployment_scale(name=WORKER_DEPLOYMENT,namespace=NAMESPACE)
            worker_pods_available.labels(namespace=NAMESPACE).set(current.status.available_replicas or 0)
        except Exception:
            pass

async def collect_infrastructure_metrics():
    while True:
        await asyncio.sleep(30)
        collect_pod_metrics()
        collect_allocation_metrics()


# Recuperar estado ao iniciar
@app.on_event("startup")
async def startup_event():
    global registry_health_task, metrics_collection_task
    # Aguardar 5 segundos para Kubernetes estabilizar
    time.sleep(5)
    recover_state()
    registry_health_task = asyncio.create_task(monitor_stream_registry_health())
    metrics_collection_task = asyncio.create_task(collect_infrastructure_metrics())


@app.on_event("shutdown")
async def shutdown_event():
    global registry_health_task, metrics_collection_task
    if registry_health_task and not registry_health_task.done():
        registry_health_task.cancel()
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
            expires_at = register_or_refresh_stream_if_owner_matches(stream, proxy_pod)
            if expires_at is None:
                logger.warning(
                    f"[Allocate] Stream '{stream}' already owned by proxy "
                    f"'{stream_registry.get(stream, {}).get('proxy_pod')}', ignoring proxy '{proxy_pod}'"
                )

        # Cancelar scale-down se houver nova solicitação de alocação
        if scale_down_task and not scale_down_task.done():
            scale_down_task.cancel()
            logger.info("[Allocate] Cancelled pending scale-down task (new allocation request)")
        
        # Verificar se já existe alocação para essa stream
        if stream in stream_to_worker:
            existing_worker = stream_to_worker[stream]
            
            # Construir IP do proxy (Pull-Only Architecture)
            # Usa IP direto do pod proxy (Headless DNS não funciona sem StatefulSet)
            proxy_ip = get_proxy_pod_ip(proxy_pod) if proxy_pod else None
            proxy_address = proxy_ip if proxy_ip else "rtmp-proxy.media.svc.cluster.local"
            
            logger.info(f"[Allocate] Stream '{stream}' already has worker: {existing_worker} - Proxy: {proxy_address}")
            return {
                "pod": f"{existing_worker}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local",
                "name": existing_worker,
                "proxy": proxy_address,
                "status": "existing"
            }
        
        # Verificar se stream já está aguardando alocação (evita múltiplas escalações)
        if stream in streams_pending_allocation:
            elapsed = time.time() - streams_pending_allocation[stream]
            logger.info(f"[Allocate] Stream '{stream}' is already pending allocation ({elapsed:.1f}s elapsed). Checking for ready workers...")
            # Não escala novamente, apenas verifica se algum worker ficou pronto
        else:
            # Primeira solicitação para essa stream
            streams_pending_allocation[stream] = time.time()
            logger.info(f"[Allocate] Stream '{stream}' added to pending allocation queue")
        
        # Listar workers disponíveis
        pods = core.list_namespaced_pod(
            namespace=NAMESPACE,
            label_selector="app=rtmp-worker"
        ).items

        # Filtrar workers ready E não alocados
        available_workers = []
        for p in pods:
            if not p.status.conditions:
                continue
            cond = {c.type: c.status for c in p.status.conditions}
            
            pod_name = p.metadata.name
            
            # Worker deve estar Ready E não alocado
            if cond.get("Ready") == "True" and pod_name not in worker_to_stream:
                # Dupla verificação: consultar métricas para garantir que está realmente livre
                nclients = check_worker_metrics(pod_name, p.status.pod_ip)
                if nclients == 0:
                    available_workers.append(p)

        # Se existe worker disponível, alocar
        if available_workers:
            pod = available_workers[0]
            pod_name = pod.metadata.name
            pod_ip = pod.status.pod_ip
            
            # Criar mapeamento bidirecional
            stream_to_worker[stream] = pod_name
            worker_to_stream[pod_name] = stream
            
            # Armazenar proxy para Pull-Only Architecture
            if proxy_pod:
                stream_to_proxy[stream] = proxy_pod
            
            # Remover de pending allocation (worker foi alocado)
            if stream in streams_pending_allocation:
                elapsed = time.time() - streams_pending_allocation[stream]
                del streams_pending_allocation[stream]
                logger.info(f"[Allocate] Removed stream '{stream}' from pending queue (allocated in {elapsed:.1f}s)")
            
            # Usar DNS estável via Headless Service em vez de IP direto
            # Formato: <pod-name>.<service-name>.<namespace>.svc.cluster.local
            worker_dns = f"{pod_name}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local"
            
            # Obter IP do proxy para retornar ao worker (Pull-Only Architecture)
            proxy_ip = get_proxy_pod_ip(proxy_pod) if proxy_pod else None
            proxy_address = proxy_ip if proxy_ip else "rtmp-proxy.media.svc.cluster.local"
            
            logger.info(f"[Allocate] Allocated worker {pod_name} (DNS: {worker_dns}) for stream '{stream}' - Proxy: {proxy_address}")
            record_stream_start(stream)
            
            return {
                "pod": worker_dns,  # DNS estável via Headless Service
                "name": pod_name,
                "proxy": proxy_address,  # IP do proxy para pull (Pull-Only)
                "worker": pod_name,
                "status": "allocated"
            }

        # Nenhum worker disponível → escalar deployment (APENAS se stream não está em pending)
        # Se já está em pending, significa que já escalou antes
        if stream not in streams_pending_allocation:
            # Nunca deveria chegar aqui (proteção dupla)
            streams_pending_allocation[stream] = time.time()
        
        # Verificar se precisa escalar (só escala na primeira vez)
        elapsed_since_pending = time.time() - streams_pending_allocation[stream]
        
        # Só escala se for a primeira tentativa (elapsed < 1s)
        if elapsed_since_pending < 1.0:
            current = apps.read_namespaced_deployment_scale(
                name=WORKER_DEPLOYMENT,
                namespace=NAMESPACE
            )
            
            # Handle None replicas (controlled by KEDA or set to 0)
            current_replicas = current.spec.replicas if current.spec.replicas is not None else 0
            new_replicas = current_replicas + 1
            
            body = { "spec": { "replicas": new_replicas } }

            apps.patch_namespaced_deployment_scale(
                name=WORKER_DEPLOYMENT,
                namespace=NAMESPACE,
                body=body
            )
            
            logger.info(f"[Allocate] No workers available. Scaled deployment to {new_replicas} replicas for stream '{stream}'.")
        else:
            logger.info(f"[Allocate] Stream '{stream}' still waiting for worker (pending for {elapsed_since_pending:.1f}s). Not scaling again.")

        return { 
            "pod": None, 
            "status": "scaling",
            "pending_seconds": elapsed_since_pending
        }


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
            
            # Limpar pending allocation se existir
            if stream in streams_pending_allocation:
                del streams_pending_allocation[stream]
                logger.info(f"[Release] Removed stream '{stream}' from pending allocation queue (never allocated)")
            
            return {"status": "not_found", "stream": stream}
        
        worker_name = stream_to_worker[stream]
        
        # Remover mapeamentos
        del stream_to_worker[stream]
        del worker_to_stream[worker_name]
        
        # Remover proxy mapping (Pull-Only)
        if stream in stream_to_proxy:
            del stream_to_proxy[stream]
        if stream in stream_registry:
            del stream_registry[stream]
        
        # Remover de pending allocation se ainda estiver lá
        if stream in streams_pending_allocation:
            del streams_pending_allocation[stream]
            logger.info(f"[Release] Removed stream '{stream}' from pending allocation queue")
        
        # Remove heartbeat tracking
        if stream in stream_last_heartbeat:
            del stream_last_heartbeat[stream]
        
        record_stream_end(stream)
        logger.info(f"[Release] Released worker {worker_name} from stream '{stream}'")
        
        # Auto scale-down: Reduzir deployment se não há workers alocados
        # Cancela task anterior e agenda nova
        if scale_down_task and not scale_down_task.done():
            scale_down_task.cancel()
        
        if len(stream_to_worker) == 0:
            # Nenhum worker alocado - agendar scale-down após delay
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
        current = stream_registry.get(stream)
        if current and current.get("proxy_pod") != proxy_pod:
            raise HTTPException(
                status_code=409,
                detail=f"stream '{stream}' already owned by proxy '{current.get('proxy_pod')}'"
            )
        expires_at = register_or_refresh_stream(stream, proxy_pod)
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
    """
    Worker sends heartbeat to prove stream is actively being pulled and pushed.
    This protects stream from expiring even if proxy healthchecks fail.
    """
    with allocation_lock:
        cleanup_expired_streams()
        current = stream_registry.get(stream)

        if current and current.get("proxy_pod") != proxy_pod:
            raise HTTPException(
                status_code=409,
                detail=f"stream '{stream}' already owned by proxy '{current.get('proxy_pod')}'"
            )

        # Record heartbeat timestamp - stream is alive!
        stream_last_heartbeat[stream] = time.time()
        
        # Also refresh TTL to be safe
        expires_at = register_or_refresh_stream(stream, proxy_pod)
        
        logger.debug(f"[Heartbeat] Stream '{stream}' heartbeat recorded. TTL renewed.")
        
        return {
            "status": "ok",
            "stream": stream,
            "proxy_pod": proxy_pod,
            "expires_at": expires_at
        }


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

    proxy_ip = get_proxy_pod_ip(proxy_pod) if proxy_pod else None

    if not proxy_ip:
        raise HTTPException(status_code=404, detail=f"proxy for stream '{stream}' unavailable")

    return {
        "stream": stream,
        "proxyPod": proxy_pod,
        "proxyAddress": proxy_ip,
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
        return {"status": "released", "stream": stream}


@app.get("/status")
def get_status():
    """
    Retorna estado atual de alocações.
    Útil para debug e monitoramento.
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
    stream_last_heartbeat[stream] = now
    register_or_refresh_stream(stream, proxy_pod)
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
    # Para testes/desenvolvimento: usar o stream name como chave
    # Em produção, substituir por lógica real de mapeamento
    youtube_key = stream
    
    with allocation_lock:
        cleanup_expired_streams()
        proxy_pod = stream_to_proxy.get(stream)
        proxy_ip = get_proxy_pod_ip(proxy_pod) if proxy_pod else None
    
    if not proxy_ip:
        raise HTTPException(status_code=404, detail=f"stream '{stream}' has no active proxy")
    
    logger.debug(f"[StreamKey] Returning info for stream '{stream}': YouTube={youtube_key}, Proxy={proxy_ip}")
    
    return {
        "stream": stream,
        "youtubeKey": youtube_key,
        "proxyDns": proxy_ip,
        "proxyPod": proxy_pod
    }


@app.get("/start-worker")
def start_worker(stream: str = Query(..., description="Stream name"), worker: str = Query(..., description="Worker pod name")):
    """
    Endpoint chamado pelo proxy para iniciar worker pull+push.
    Worker executa on_worker_publish_push.sh para iniciar FFmpeg.
    
    Pull-Only Architecture:
    - Proxy notifica controller após alocar worker
    - Controller executa script no worker via kubectl exec
    - Worker inicia FFmpeg pull do proxy específico + push YouTube
    """
    try:
        logger.info(f"[StartWorker] Starting worker '{worker}' for stream '{stream}'")
        
        # Executar on_worker_pull_push.sh no worker via kubectl exec
        import subprocess
        result = subprocess.run(
            [
                "kubectl", "exec", "-n", NAMESPACE, worker, "--",
                "/scripts/on_worker_pull_push.sh", stream
            ],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            logger.info(f"[StartWorker] Worker '{worker}' started successfully for stream '{stream}'")
            return {"status": "started", "worker": worker, "stream": stream}
        else:
            logger.error(f"[StartWorker] Failed to start worker '{worker}': {result.stderr}")
            return {"status": "error", "error": result.stderr}
            
    except Exception as e:
        logger.error(f"[StartWorker] Exception starting worker '{worker}': {str(e)}")
        return {"status": "error", "error": str(e)}
