#!/bin/bash

echo "🚀 Deploying LiveEdgeCast..."

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_step() { echo -e "${BLUE}📋 $1${NC}"; }
print_success() { echo -e "${GREEN}✅ $1${NC}"; }
print_warning() { echo -e "${YELLOW}⚠️  $1${NC}"; }
print_error() { echo -e "${RED}❌ $1${NC}"; }

check_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        print_error "$1 not found. Please install $1 first."
        exit 1
    fi
    print_success "$1 is installed"
}

print_step "Checking prerequisites..."
check_command docker
check_command kubectl

if ! kubectl cluster-info >/dev/null 2>&1; then
    print_error "Cannot connect to Kubernetes cluster!"
    kubectl config current-context 2>/dev/null || echo "  No active context found"
    exit 1
fi

CONTEXT=$(kubectl config current-context)
if [[ $CONTEXT != kind-* && $CONTEXT != docker-desktop && $CONTEXT != desktop-linux ]]; then
    print_error "Unsupported Kubernetes context: $CONTEXT"
    print_error "Use a local kind or Docker Desktop cluster. Remote nodes cannot use the imagePullPolicy: Never images built by this script."
    exit 1
fi

kubectl apply -f k8s/namespaces.yaml
kubectl wait --for=jsonpath='{.status.phase}'=Active namespace/media --timeout=30s

PROXY_IMAGE="liveedgecast-proxy:latest"
CONTROLLER_IMAGE="liveedgecast-controller:latest"

print_step "Building container images..."
docker build -t "$PROXY_IMAGE" -f docker/proxy/Dockerfile docker/proxy
docker build -t "$CONTROLLER_IMAGE" -f docker/controller/Dockerfile docker/controller
print_success "Container images built"

if [[ $CONTEXT == kind-* ]]; then
    check_command kind
    print_step "Loading images into the kind cluster..."
    kind load docker-image "$PROXY_IMAGE" "$CONTROLLER_IMAGE"
fi

print_step "Removing resources retired by the cleanup phase..."
# Applying the retained manifests does not prune objects from older releases.
kubectl delete pods -l app=worker -n media --ignore-not-found=true
kubectl delete deployment/proxy-lb deployment/worker configmap/proxy-lb-config \
    service/controller service/proxy-entry service/worker -n media --ignore-not-found=true

print_step "Applying Kubernetes resources..."
kubectl apply -f k8s/

# Both images use local, mutable `latest` tags with imagePullPolicy: Never.
# Restart both Deployments so rerunning this script uses the images just loaded.
print_step "Restarting deployments to use the newly loaded images..."
kubectl rollout restart deployment/controller deployment/proxy -n media

print_step "Waiting for core deployments..."
kubectl rollout status deployment/controller -n media --timeout=120s
kubectl rollout status deployment/proxy -n media --timeout=120s

print_success "LiveEdgeCast is ready"
echo ""
if [[ $CONTEXT == kind-* ]]; then
    PORT_FORWARD_PID_FILE="/tmp/liveedgecast-proxy-port-forward.pid"
    if [[ -f $PORT_FORWARD_PID_FILE ]]; then
        OLD_PORT_FORWARD_PID=$(cat "$PORT_FORWARD_PID_FILE")
        if [[ $OLD_PORT_FORWARD_PID =~ ^[0-9]+$ ]] && \
            [[ $(ps -p "$OLD_PORT_FORWARD_PID" -o args= 2>/dev/null) == *"kubectl port-forward"* ]]; then
            kill "$OLD_PORT_FORWARD_PID" 2>/dev/null || true
        fi
    fi

    print_step "Starting the local RTMP port forward..."
    nohup kubectl port-forward -n media service/proxy 1935:1935 \
        >/tmp/liveedgecast-proxy-port-forward.log 2>&1 &
    PORT_FORWARD_PID=$!
    echo "$PORT_FORWARD_PID" >"$PORT_FORWARD_PID_FILE"
    sleep 1
    if ! kill -0 "$PORT_FORWARD_PID" 2>/dev/null; then
        print_error "Could not start the RTMP port forward. See /tmp/liveedgecast-proxy-port-forward.log."
        exit 1
    fi
    echo "RTMP ingest: rtmp://127.0.0.1:1935/live/{stream-key}"
else
    echo "RTMP ingest is exposed on TCP port 1935 by the proxy LoadBalancer Service."
    echo "Run 'kubectl get service proxy -n media' to find its external address."
fi
echo ""
echo "Useful commands:"
echo "  kubectl get pods -n media"
echo "  kubectl logs -l app=controller -n media -f"
echo "  kubectl logs -l app=proxy -n media -f"
