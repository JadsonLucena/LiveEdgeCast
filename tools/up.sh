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

kubectl apply -f k8s/namespaces.yaml
kubectl wait --for=jsonpath='{.status.phase}'=Active namespace/media --timeout=30s

PROXY_IMAGE="liveedgecast-proxy:latest"
WORKER_IMAGE="liveedgecast-worker:latest"
CONTROLLER_IMAGE="liveedgecast-controller:latest"

print_step "Building container images..."
docker build -t "$PROXY_IMAGE" -f docker/proxy/Dockerfile docker/proxy
docker build -t "$WORKER_IMAGE" -f docker/worker/Dockerfile docker/worker
docker build -t "$CONTROLLER_IMAGE" -f docker/controller/Dockerfile docker/controller
print_success "Container images built"

CONTEXT=$(kubectl config current-context)
if [[ $CONTEXT =~ kind ]]; then
    print_step "Loading images into the kind cluster..."
    kind load docker-image "$PROXY_IMAGE" "$WORKER_IMAGE" "$CONTROLLER_IMAGE"
elif [[ ! $CONTEXT =~ (docker-desktop|localhost|127\.0\.0\.1) ]]; then
    print_warning "Remote cluster detected: $CONTEXT"
    print_warning "Ensure the LiveEdgeCast images are available in its container registry."
fi

print_step "Applying Kubernetes resources..."
kubectl apply -f k8s/

print_step "Waiting for core deployments..."
kubectl wait --for=condition=available deployment/controller -n media --timeout=120s
kubectl wait --for=condition=available deployment/proxy -n media --timeout=120s
kubectl wait --for=condition=available deployment/proxy-lb -n media --timeout=120s

NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
print_success "LiveEdgeCast is ready"
echo ""
echo "RTMP ingest: rtmp://${NODE_IP}:31935/live/{stream-key}"
echo ""
echo "Useful commands:"
echo "  kubectl get pods -n media"
echo "  kubectl logs -l app=controller -n media -f"
echo "  kubectl logs -l app=proxy -n media -f"
echo "  kubectl logs -l app=worker -n media -f"
