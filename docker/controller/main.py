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
STREAM_TTL_SECONDS = 15
STREAM_HEARTBEAT_INTERVAL_SECONDS = 5

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
    Também remove stream_to_proxy para manter consistência.
    """
    now = time.time()
    expired = []

    for stream, entry in stream_registry.items():
        if entry.get("expires_at", 0) <= now:
            expired.append(stream)

    for stream in expired:
        stream_registry.pop(stream, None)
        stream_to_proxy.pop(stream, None)
        logger.info(f"[Registry] Stream '{stream}' expired (missing heartbeat)")


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


def check_worker_metrics(pod_name: str) -> int:
    """
    Verifica métricas RTMP do worker para determinar se está ocupado.
    Retorna número de clientes RTMP ativos.
    """
    try:
        # Tentar acessar /stats do worker
        stats_url = f"http://{pod_name}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local:8080/stats"
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
            nclients = check_worker_metrics(pod_name)
            
            if nclients > 0:
                # Worker está ocupado, mas não sabemos qual stream
                # Marcar como alocado com stream desconhecido
                stream_name = f"recovered_stream_{random_suffix()}"
                stream_to_worker[stream_name] = pod_name
                worker_to_stream[pod_name] = stream_name
                recovered_count += 1
                logger.info(f"[State Recovery] Worker {pod_name} is busy ({nclients} clients), marked as allocated")
        
        logger.info(f"[State Recovery] Completed. Recovered {recovered_count} active workers.")


# Recuperar estado ao iniciar
@app.on_event("startup")
async def startup_event():
    # Aguardar 5 segundos para Kubernetes estabilizar
    time.sleep(5)
    recover_state()


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
                nclients = check_worker_metrics(pod_name)
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
            "heartbeat_interval_seconds": STREAM_HEARTBEAT_INTERVAL_SECONDS,
            "expires_at": expires_at
        }


@app.post("/streams/heartbeat")
def heartbeat_stream(
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
        "proxyDns": proxy_ip
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
