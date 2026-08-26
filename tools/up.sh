#!/bin/bash

echo "🚀 Deploying LiveEdgeCast RTMP architecture..."

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_step() {
    echo -e "${BLUE}📋 $1${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

check_command() {
    if ! command -v $1 >/dev/null 2>&1; then
        print_error "$1 not found. Please install $1 first."
        exit 1
    fi
    print_success "$1 is installed"
}

print_step "Checking prerequisites..."
check_command "docker"
check_command "kubectl"
check_command "helm"

if ! kubectl cluster-info >/dev/null 2>&1; then
    print_error "Cannot connect to Kubernetes cluster!"
    kubectl config current-context 2>/dev/null || echo "  No active context found"
    exit 1
fi
print_success "kubectl can connect to cluster"

kubectl apply -f k8s/namespaces.yaml || { print_error "Failed to create namespace"; exit 1; }

kubectl wait --for jsonpath='{.status.phase}=Active' --timeout=30s namespace/media --timeout=30s || {
    print_error "Namespace 'media' failed to become active"
    exit 1
}
kubectl wait --for jsonpath='{.status.phase}=Active' --timeout=30s namespace/monitoring --timeout=30s || {
    print_error "Namespace 'monitoring' failed to become active"
    exit 1
}

if ! kubectl get service kube-prometheus-stack-prometheus -n monitoring >/dev/null 2>&1; then
    print_warning "Prometheus service not found in namespace 'monitoring'. Installing Prometheus stack..."
    print_step "Installing Prometheus stack..."
    
    helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
    helm repo update >/dev/null 2>&1
    
    helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
        --namespace monitoring --create-namespace \
        --set prometheus-node-exporter.enabled=false \
        --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
        --wait --timeout=300s || {
        print_error "Failed to install Prometheus"
        exit 1
    }
    
    print_success "Prometheus installed successfully"
else
    print_success "Prometheus stack already available in namespace 'monitoring'"
fi

print_step "Building LiveEdgeCast proxy image..."
PROXY_IMAGE="liveedgecast-proxy:latest"
docker build -t $PROXY_IMAGE -f docker/proxy/Dockerfile docker/proxy || {
    print_error "Failed to build proxy image";
    exit 1;
}
print_success "Proxy image $PROXY_IMAGE built successfully"

print_step "Building LiveEdgeCast worker image..."
WORKER_IMAGE="liveedgecast-worker:latest"
docker build -t $WORKER_IMAGE -f docker/worker/Dockerfile docker/worker || {
    print_error "Failed to build worker image";
    exit 1;
}
print_success "Worker image $WORKER_IMAGE built successfully"

print_step "Building RTMP Controller API image..."
CONTROLLER_IMAGE="liveedgecast-controller:latest"
docker build -t $CONTROLLER_IMAGE -f docker/controller/Dockerfile docker/controller/ || {
    print_error "Failed to build controller image";
    exit 1;
}
print_success "Controller image $CONTROLLER_IMAGE built successfully"

CONTEXT=$(kubectl config current-context)
if [[ $CONTEXT =~ (kind) ]]; then
    print_step "Loading images to kind cluster..."
    kind load docker-image $PROXY_IMAGE || {
        print_error "Failed to load proxy image to kind cluster"
        exit 1
    }
    kind load docker-image $WORKER_IMAGE || {
        print_error "Failed to load worker image to kind cluster"
        exit 1
    }
    kind load docker-image $CONTROLLER_IMAGE || {
        print_error "Failed to load controller image to kind cluster"
        exit 1
    }
    print_success "Images loaded to kind cluster"
elif [[ ! $CONTEXT =~ (docker-desktop|localhost|127\.0\.0\.1) ]]; then
    print_warning "Remote/managed cluster detected: $CONTEXT"
    print_warning "Ensure $PROXY_IMAGE, $WORKER_IMAGE and $CONTROLLER_IMAGE are available in the cluster registry."
    echo -n "Continue with deployment? (y/n): "
    read -r continue_deploy
    if [[ ! $continue_deploy =~ ^[Yy]$ ]]; then
        print_error "Deployment cancelled. Please push images to cluster registry first."
        exit 1
    fi
fi

print_step "Applying RBAC for Controller..."
kubectl apply -f k8s/controller-rbac.yaml || { print_error "RBAC setup failed"; exit 1; }
print_success "Controller RBAC configured"

print_step "Deploying to Kubernetes..."
kubectl apply -f k8s/ || { print_error "Deployment failed"; exit 1; }
print_success "Kubernetes manifests applied"

print_step "Waiting for RTMP Controller deployment to be ready..."
kubectl wait --for=condition=available deployment/controller -n media --timeout=120s || {
    print_error "RTMP Controller deployment failed to become available"
    kubectl logs -l app=controller -n media --tail=50 2>/dev/null || true
    exit 1
}
print_success "RTMP Controller is ready"

print_step "Waiting for RTMP Proxy deployment to be ready..."
kubectl wait --for=condition=available deployment/proxy -n media --timeout=120s || {
    print_error "RTMP Proxy deployment failed to become available"
    kubectl logs -l app=proxy -n media --tail=50 2>/dev/null || true
    exit 1
}
print_success "RTMP Proxy is ready"

print_step "Checking pod status..."
CONTROLLER_PODS=$(kubectl get pods -l app=controller -n media --no-headers 2>/dev/null | wc -l)
PROXY_PODS=$(kubectl get pods -l app=proxy -n media --no-headers 2>/dev/null | wc -l)
WORKER_PODS=$(kubectl get pods -l app=worker -n media --no-headers 2>/dev/null | wc -l)

print_success "RTMP Controller: $CONTROLLER_PODS pod(s) running"
print_success "RTMP Proxy: $PROXY_PODS pod(s) running"

if [ "$WORKER_PODS" -eq 0 ]; then
    print_warning "No worker pods are currently assigned to streams"
else
    print_success "RTMP Workers: $WORKER_PODS pod(s) running"
fi

print_step "Getting NodePort access information..."

NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
WSL_IP=$(ip addr show eth0 2>/dev/null | grep "inet " | awk '{print $2}' | cut -d/ -f1 || echo "N/A")

print_success "NodePort RTMP Service configured on port 31935"

print_success "Deployment completed!"

echo ""
print_step "Deployment Status:"
echo ""
print_step "Controller API:"
kubectl get pods -l app=controller -n media
echo ""
print_step "RTMP Proxy Pods:"
kubectl get pods -l app=proxy -n media
echo ""
print_step "RTMP Worker Pods:"
kubectl get pods -l app=worker -n media
echo ""
print_step "Services:"
kubectl get svc -n media

echo ""
print_success "🎉 LiveEdgeCast RTMP is ready!"
echo ""
echo -e "${GREEN}📡 RTMP Streaming (NodePort - External Access):${NC}"
echo "  📺 From Windows/OBS: rtmp://localhost:31935/live/{your-youtube-key}"
echo "  📺 From WSL: rtmp://${WSL_IP}:31935/live/{your-youtube-key}"
echo "  📺 From Network: rtmp://${NODE_IP}:31935/live/{your-youtube-key}"
echo ""
echo -e "${YELLOW}🎥 OBS Studio Configuration:${NC}"
echo "  1. Settings → Stream"
echo "  2. Service: Custom"
echo "  3. Server: rtmp://localhost:31935/live"
echo "  4. Stream Key: {your-youtube-stream-key}"
echo "  5. Click 'Start Streaming'"
echo ""
echo -e "${BLUE}🔧 Useful commands:${NC}"
echo "  📊 Watch worker pods: kubectl get pods -l app=worker -n media -w"
echo "  📊 Controller status: kubectl logs -l app=controller -n media --tail=50"
echo "  📋 Controller logs: kubectl logs -l app=controller -n media -f"
echo "  📋 Proxy logs: kubectl logs -l app=proxy -n media -f"
echo "  📋 Worker logs: kubectl logs -l app=worker -n media -f"
echo "  📈 Metrics: kubectl top pods -n media"
echo ""
echo -e "${YELLOW}🎬 Testing with FFmpeg (NodePort):${NC}"
echo "  # Test stream with YouTube key"
echo "  ffmpeg -re -f lavfi -i testsrc=size=1280x720:rate=30 \\"
echo "         -f lavfi -i sine=frequency=1000 \\"
echo "         -c:v libx264 -preset ultrafast -b:v 2500k \\"
echo "         -c:a aac -b:a 128k \\"
echo "         -f flv rtmp://localhost:31935/live/{your-youtube-key}"
echo ""
echo "  # Multiple streams simultaneously"
echo "  ffmpeg -re -i video1.mp4 -f flv rtmp://localhost:31935/live/key1 &"
echo "  ffmpeg -re -i video2.mp4 -f flv rtmp://localhost:31935/live/key2 &"
echo "  ffmpeg -re -i video3.mp4 -f flv rtmp://localhost:31935/live/key3 &"
echo ""
echo -e "${YELLOW}💡 Multi-Stream Architecture v2.0:${NC}"
echo -e "${YELLOW}   • Controller: Única fonte da verdade para workers (state recovery via métricas)${NC}"
echo -e "${YELLOW}   • Proxy: Suporte multi-stream via FFmpeg dedicado por publicação${NC}"
echo -e "${YELLOW}   • Workers: Um worker dedicado por stream, orquestrado pelo Controller${NC}"
echo -e "${YELLOW}   • Garantia: 1 stream = 1 worker = 1 processo FFmpeg isolado${NC}"
echo -e "${YELLOW}   • Scripts: Embarcados na imagem Docker (on_publish_start/done.sh)${NC}"
echo -e "${YELLOW}   • Roteamento: NGINX → FFmpeg → Worker dedicado → YouTube${NC}"
echo ""
echo -e "${YELLOW}📊 Capacity Configuration:${NC}"
echo -e "${YELLOW}   • Proxy: Contagem estática de replicas definida no Deployment${NC}"
echo -e "${YELLOW}   Worker Lifecycle:${NC}"
echo -e "${YELLOW}   • Controller API: /streams/started e /streams/ended orquestram workers${NC}"
echo -e "${YELLOW}   • Mapeamento bidirecional: stream ↔ worker${NC}"
echo -e "${YELLOW}   • State recovery: Automático via métricas RTMP ao reiniciar${NC}"
echo ""
echo -e "${GREEN}🚀 Ready to receive RTMP streams!${NC}"
echo ""
echo -e "${BLUE}🔍 Health Check:${NC}"
PROXY_POD=$(kubectl get pods -n media -l app=proxy -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$PROXY_POD" ]; then
    if kubectl exec -n media "$PROXY_POD" -- wget -qO- http://localhost:8080/health 2>/dev/null | grep -q "running"; then
        print_success "✓ RTMP Proxy is healthy and ready"
    else
        print_warning "⚠ RTMP Proxy may not be fully ready yet"
    fi
fi

CONTROLLER_POD=$(kubectl get pods -n media -l app=controller -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$CONTROLLER_POD" ]; then
    if kubectl exec -n media "$CONTROLLER_POD" -- wget -qO- http://localhost:8000/health 2>/dev/null | grep -q "ok"; then
        print_success "✓ Controller API is healthy and ready"
    else
        print_warning "⚠ Controller API may not be fully ready yet"
    fi
fi

echo ""
echo -e "${BLUE}📊 Monitoring (Optional):${NC}"
echo "  To access Prometheus UI:"
echo "    kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090"
echo "    Then open: http://localhost:9090"
echo ""
echo "  Useful Prometheus queries:"
echo "    - rate(container_network_receive_bytes_total{namespace=\"media\"}[1m])/1000000  # Mbps"
echo "    - container_memory_usage_bytes{namespace=\"media\",pod=~\"rtmp-.*\"}/1024/1024  # MB"
echo "    - rate(container_cpu_usage_seconds_total{namespace=\"media\"}[1m])*100  # CPU %"
