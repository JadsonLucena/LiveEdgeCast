from fastapi import FastAPI, Query
from kubernetes import client, config
import random
import string
import threading
import requests
import time
from typing import Dict, Optional

app = FastAPI()

NAMESPACE = "media"
WORKER_DEPLOYMENT = "rtmp-worker"
WORKER_SERVICE = "rtmp-worker"

# Lock para evitar race conditions
allocation_lock = threading.Lock()

# Mapeamento: stream_name → worker_pod_name
stream_to_worker: Dict[str, str] = {}

# Mapeamento inverso: worker_pod_name → stream_name
worker_to_stream: Dict[str, str] = {}

# Load Kubernetes credentials (inside cluster)
try:
    config.load_incluster_config()
except:
    config.load_kube_config()

apps = client.AppsV1Api()
core = client.CoreV1Api()


def random_suffix():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))


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
        print(f"Warning: Failed to check metrics for {pod_name}: {e}")
        return 0


def recover_state():
    """
    Recupera estado de alocações após reinício do controller.
    Verifica quais workers estão realmente ocupados consultando suas métricas RTMP.
    """
    print("[State Recovery] Starting state recovery...")
    
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
                print(f"[State Recovery] Worker {pod_name} is busy ({nclients} clients), marked as allocated")
        
        print(f"[State Recovery] Completed. Recovered {recovered_count} active workers.")


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
def allocate_worker(stream: str = Query(..., description="Stream name")):
    """
    Aloca um worker dedicado para uma stream.
    Controller é a ÚNICA fonte da verdade para scale-up.
    
    Retorna worker DNS se disponível, ou None se ainda está escalando.
    """
    
    with allocation_lock:
        # Verificar se já existe alocação para essa stream
        if stream in stream_to_worker:
            existing_worker = stream_to_worker[stream]
            print(f"[Allocate] Stream '{stream}' already has worker: {existing_worker}")
            return {
                "pod": f"{existing_worker}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local",
                "name": existing_worker,
                "status": "existing"
            }
        
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
            
            # Criar mapeamento bidirecional
            stream_to_worker[stream] = pod_name
            worker_to_stream[pod_name] = stream
            
            print(f"[Allocate] Allocated worker {pod_name} for stream '{stream}'")
            
            return {
                "pod": f"{pod_name}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local",
                "name": pod_name,
                "status": "allocated"
            }

        # Nenhum worker disponível → escalar deployment
        current = apps.read_namespaced_deployment_scale(
            name=WORKER_DEPLOYMENT,
            namespace=NAMESPACE
        )
        
        new_replicas = current.spec.replicas + 1
        
        body = { "spec": { "replicas": new_replicas } }

        apps.patch_namespaced_deployment_scale(
            name=WORKER_DEPLOYMENT,
            namespace=NAMESPACE,
            body=body
        )
        
        print(f"[Allocate] No workers available. Scaled deployment to {new_replicas} replicas.")

        return { "pod": None, "status": "scaling" }


@app.post("/release")
def release_worker(stream: str = Query(..., description="Stream name to release")):
    """
    Libera worker alocado para uma stream.
    Remove mapeamento stream→worker.
    """
    with allocation_lock:
        if stream not in stream_to_worker:
            print(f"[Release] WARNING: Stream '{stream}' not found in allocations")
            return {"status": "not_found", "stream": stream}
        
        worker_name = stream_to_worker[stream]
        
        # Remover mapeamentos
        del stream_to_worker[stream]
        del worker_to_stream[worker_name]
        
        print(f"[Release] Released worker {worker_name} from stream '{stream}'")
        
        return {
            "status": "released",
            "stream": stream,
            "worker": worker_name
        }


@app.get("/status")
def get_status():
    """
    Retorna estado atual de alocações.
    Útil para debug e monitoramento.
    """
    with allocation_lock:
        return {
            "active_streams": len(stream_to_worker),
            "allocations": [
                {
                    "stream": stream,
                    "worker": worker
                }
                for stream, worker in stream_to_worker.items()
            ]
        }


@app.get("/stream-key")
def get_stream_key(stream: str = Query(..., description="Stream name")):
    """
    Retorna a chave do YouTube para uma stream específica.
    
    Esta função implementa a lógica de mapeamento stream → YouTube key.
    Por padrão, usa o próprio stream name como chave.
    
    Em produção, você pode:
    - Consultar um banco de dados
    - Usar variáveis de ambiente
    - Implementar lógica de mapeamento customizada
    """
    # Para testes/desenvolvimento: usar o stream name como chave
    # Em produção, substituir por lógica real de mapeamento
    youtube_key = stream
    
    print(f"[StreamKey] Returning YouTube key for stream '{stream}': {youtube_key}")
    
    return {
        "stream": stream,
        "youtubeKey": youtube_key
    }
