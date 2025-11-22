from fastapi import FastAPI
from kubernetes import client, config
import random
import string

app = FastAPI()

NAMESPACE = "media"
WORKER_DEPLOYMENT = "rtmp-worker"

# Load Kubernetes credentials (inside cluster)
try:
    config.load_incluster_config()
except:
    config.load_kube_config()

apps = client.AppsV1Api()
core = client.CoreV1Api()


def random_suffix():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/allocate")
def allocate_worker():
    """
    Returns a ready worker pod.
    If none exists, force a scale-up (replicas=1).
    """

    # List worker pods
    pods = core.list_namespaced_pod(
        namespace=NAMESPACE,
        label_selector="app=rtmp-worker"
    ).items

    # Filter only ready pods
    ready = []
    for p in pods:
        if not p.status.conditions:
            continue
        cond = {c.type: c.status for c in p.status.conditions}
        if cond.get("Ready") == "True":
            ready.append(p)

    # If a ready worker exists, return it
    if ready:
        pod = ready[0]
        return {
            "pod": f"{pod.metadata.name}.{NAMESPACE}.svc.cluster.local"
        }

    # If none exists → force a scale-up
    body = { "spec": { "replicas": 1 } }

    apps.patch_namespaced_deployment_scale(
        name=WORKER_DEPLOYMENT,
        namespace=NAMESPACE,
        body=body
    )

    return { "pod": None, "status": "starting" }
