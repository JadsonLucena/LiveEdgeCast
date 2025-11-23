#!/bin/bash

echo "🚀 Deploying LiveEdgeCast with KEDA RTMP Serverless Architecture..."

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

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

# Function to check if command exists
check_command() {
    if ! command -v $1 >/dev/null 2>&1; then
        print_error "$1 not found. Please install $1 first."
        exit 1
    fi
    print_success "$1 is installed"
}

# Step 1: Check prerequisites
print_step "Checking prerequisites..."
check_command "docker"
check_command "kubectl"
check_command "helm"

# Check if kubectl can connect to cluster
if ! kubectl cluster-info >/dev/null 2>&1; then
    print_error "Cannot connect to Kubernetes cluster!"
    kubectl config current-context 2>/dev/null || echo "  No active context found"
    exit 1
fi
print_success "kubectl can connect to cluster"

# Step 2: Check if KEDA is installed
print_step "Checking KEDA installation..."
if ! kubectl get namespace keda >/dev/null 2>&1; then
    print_error "KEDA namespace not found. Please install KEDA first:"
    echo "  helm repo add kedacore https://kedacore.github.io/charts"
    echo "  helm repo update"
    echo "  helm install keda kedacore/keda --namespace keda --create-namespace"
    exit 1
fi

kubectl apply -f k8s/namespaces.yaml || { print_error "Failed to create namespace"; exit 1; }

# wait for namespace to be active
kubectl wait --for jsonpath='{.status.phase}=Active' --timeout=30s namespace/media --timeout=30s || {
    print_error "Namespace 'media' failed to become active"
    exit 1
}
kubectl wait --for jsonpath='{.status.phase}=Active' --timeout=30s namespace/monitoring --timeout=30s || {
    print_error "Namespace 'keda' failed to become active"
    exit 1
}

# Check if Prometheus is installed (for KEDA metrics)
if ! kubectl get namespace monitoring >/dev/null 2>&1; then
    print_warning "Monitoring namespace not found. Installing Prometheus for KEDA metrics..."
    print_step "Installing Prometheus stack..."
    
    # Add Prometheus Helm repo
    helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
    helm repo update >/dev/null 2>&1
    
    # Install Prometheus
    helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
        --namespace monitoring --create-namespace \
        --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
        --wait --timeout=300s || {
        print_error "Failed to install Prometheus"
        exit 1
    }
    
    print_success "Prometheus installed successfully"
else
    print_success "Monitoring namespace exists"
fi

# Step 3: Build Docker images
print_step "Building LiveEdgeCast RTMP Proxy image..."
RTMP_IMAGE="liveedgecast:latest"

# Build the RTMP proxy image (with curl, jq, wget, bash)
docker build -t $RTMP_IMAGE -f docker/rtmp/Dockerfile docker/rtmp/ || { 
    print_error "Failed to build RTMP proxy image"; 
    exit 1; 
}
print_success "RTMP proxy image $RTMP_IMAGE built successfully"

print_step "Building RTMP Controller API image..."
CONTROLLER_IMAGE="rtmp-controller:latest"
docker build -t $CONTROLLER_IMAGE -f docker/controller/Dockerfile docker/controller/ || {
    print_error "Failed to build controller image";
    exit 1;
}
print_success "Controller image $CONTROLLER_IMAGE built successfully"

# Handle Docker images for cluster
CONTEXT=$(kubectl config current-context)
if [[ $CONTEXT =~ (kind) ]]; then
    print_step "Loading images to kind cluster..."
    kind load docker-image $RTMP_IMAGE || {
        print_error "Failed to load RTMP image to kind cluster"
        exit 1
    }
    kind load docker-image $CONTROLLER_IMAGE || {
        print_error "Failed to load controller image to kind cluster"
        exit 1
    }
    print_success "Images loaded to kind cluster"
elif [[ ! $CONTEXT =~ (docker-desktop|localhost|127\.0\.0\.1) ]]; then
    print_warning "Remote/managed cluster detected: $CONTEXT"
    print_warning "Ensure $RTMP_IMAGE and $CONTROLLER_IMAGE are available in the cluster registry."
    echo -n "Continue with deployment? (y/n): "
    read -r continue_deploy
    if [[ ! $continue_deploy =~ ^[Yy]$ ]]; then
        print_error "Deployment cancelled. Please push images to cluster registry first."
        exit 1
    fi
fi

# Step 4: Deploy to Kubernetes
print_step "Applying RBAC for Controller..."
kubectl apply -f k8s/controller-rbac.yaml || { print_error "RBAC setup failed"; exit 1; }
print_success "Controller RBAC configured"

print_step "Deploying to Kubernetes..."
kubectl apply -f k8s/ || { print_error "Deployment failed"; exit 1; }
print_success "Kubernetes manifests applied"

# Step 5: Wait for deployments to be ready
print_step "Waiting for RTMP Controller deployment to be ready..."
kubectl wait --for=condition=available deployment/rtmp-controller -n media --timeout=120s || {
    print_error "RTMP Controller deployment failed to become available"
    kubectl logs -l app=rtmp-controller -n media --tail=50 2>/dev/null || true
    exit 1
}
print_success "RTMP Controller is ready"

print_step "Waiting for RTMP Proxy deployment to be ready..."
kubectl wait --for=condition=available deployment/rtmp-proxy -n media --timeout=120s || {
    print_error "RTMP Proxy deployment failed to become available"
    kubectl logs -l app=rtmp-proxy -n media --tail=50 2>/dev/null || true
    exit 1
}
print_success "RTMP Proxy is ready"

print_step "Verifying KEDA ScaledObjects..."
sleep 5

# Check proxy scaler (should exist)
if kubectl get scaledobject rtmp-proxy-scaler -n media >/dev/null 2>&1; then
    print_success "RTMP Proxy ScaledObject is active"
else
    print_warning "RTMP Proxy ScaledObject not found"
fi

# Worker scaler is DISABLED in v2.0 (Controller manages scaling)
if kubectl get scaledobject rtmp-worker-scaler -n media >/dev/null 2>&1; then
    print_warning "RTMP Worker ScaledObject found (should be disabled - Controller manages workers)"
else
    print_success "RTMP Worker scaling managed by Controller (KEDA disabled as expected)"
fi

print_step "Checking pod status..."
CONTROLLER_PODS=$(kubectl get pods -l app=rtmp-controller -n media --no-headers 2>/dev/null | wc -l)
PROXY_PODS=$(kubectl get pods -l app=rtmp-proxy -n media --no-headers 2>/dev/null | wc -l)
WORKER_PODS=$(kubectl get pods -l app=rtmp-worker -n media --no-headers 2>/dev/null | wc -l)

print_success "RTMP Controller: $CONTROLLER_PODS pod(s) running"
print_success "RTMP Proxy: $PROXY_PODS pod(s) running"

if [ "$WORKER_PODS" -eq 0 ]; then
    print_warning "No worker pods running (KEDA serverless - workers will scale on demand)"
else
    print_success "RTMP Workers: $WORKER_PODS pod(s) running"
fi

# Step 6: Check KEDA status
print_step "Checking KEDA Operator status..."
kubectl get pods -n keda -l app=keda-operator --no-headers | grep Running >/dev/null || {
    print_warning "KEDA Operator may not be ready yet"
}

kubectl get pods -n keda -l app=keda-metrics-apiserver --no-headers | grep Running >/dev/null || {
    print_warning "KEDA Metrics API Server may not be ready yet"
}

# Step 7: Setup port-forward for RTMP
print_step "Setting up port-forward for RTMP access..."

# Kill any existing port-forwards
pkill -f "kubectl.*port-forward.*1935" 2>/dev/null || true
pkill -f "kubectl.*port-forward.*8080" 2>/dev/null || true
sleep 2

# Start RTMP port-forward
print_step "Starting port-forward to RTMP Proxy on localhost:1935..."
kubectl port-forward -n media svc/rtmp-proxy 1935:1935 >/dev/null 2>&1 &
RTMP_PORT_FORWARD_PID=$!

# Start HTTP port-forward for monitoring
print_step "Starting port-forward to RTMP Proxy HTTP on localhost:8080..."
kubectl port-forward -n media svc/rtmp-proxy 8080:8080 >/dev/null 2>&1 &
HTTP_PORT_FORWARD_PID=$!

print_success "Deployment completed!"

# Display status
echo ""
print_step "Deployment Status:"
echo ""
print_step "Controller API:"
kubectl get pods -l app=rtmp-controller -n media
echo ""
print_step "RTMP Proxy Pods:"
kubectl get pods -l app=rtmp-proxy -n media
echo ""
print_step "RTMP Worker Pods (Serverless):"
kubectl get pods -l app=rtmp-worker -n media
echo ""
print_step "KEDA ScaledObjects:"
kubectl get scaledobject -n media
echo ""
print_step "Services:"
kubectl get svc -n media

echo ""
print_success "🎉 LiveEdgeCast RTMP Serverless is ready!"
echo ""
echo -e "${GREEN}📡 RTMP Streaming:${NC}"
echo "  📺 Publish to: rtmp://localhost:1935/live/{your-stream-key}"
echo "  🌐 Monitor: http://localhost:8080/stats"
echo "  ❤️ Health: http://localhost:8080/health"
echo ""
echo -e "${BLUE}🔧 Useful commands:${NC}"
echo "  📊 Watch worker scaling: kubectl get pods -l app=rtmp-worker -n media -w"
echo "  📊 Controller status: curl http://localhost:8000/status (via port-forward)"
echo "  📋 Controller logs: kubectl logs -l app=rtmp-controller -n media -f"
echo "  📋 Proxy logs: kubectl logs -l app=rtmp-proxy -n media -f -c nginx"
echo "  📋 Worker logs: kubectl logs -l app=rtmp-worker -n media -f"
echo "  🔍 KEDA status: kubectl get scaledobject -n media"
echo "  📈 Metrics: kubectl top pods -n media"
echo "  🛑 Stop RTMP port-forward: kill $RTMP_PORT_FORWARD_PID"
echo "  🛑 Stop HTTP port-forward: kill $HTTP_PORT_FORWARD_PID"
echo ""
echo -e "${YELLOW}🎬 Testing Multi-Stream:${NC}"
echo "  # Stream 1"
echo "  ffmpeg -re -i video1.mp4 -f flv rtmp://localhost:1935/live/stream1"
echo "  # Stream 2 (simultaneous)"
echo "  ffmpeg -re -i video2.mp4 -f flv rtmp://localhost:1935/live/stream2"
echo "  # Stream 3 (simultaneous)"
echo "  ffmpeg -re -i video3.mp4 -f flv rtmp://localhost:1935/live/stream3"
echo ""
echo -e "${YELLOW}💡 Multi-Stream Serverless Architecture v2.0:${NC}"
echo -e "${YELLOW}   • Controller: Única fonte da verdade para workers (state recovery via métricas)${NC}"
echo -e "${YELLOW}   • Proxy: Suporte multi-stream via FFmpeg dedicado por publicação${NC}"
echo -e "${YELLOW}   • Workers: True Serverless (0 replicas) escalados 1:1 pelo Controller${NC}"
echo -e "${YELLOW}   • Garantia: 1 stream = 1 worker = 1 processo FFmpeg isolado${NC}"
echo -e "${YELLOW}   • Scripts: Embarcados na imagem Docker (on_publish_start/done.sh)${NC}"
echo -e "${YELLOW}   • Roteamento: NGINX → FFmpeg → Worker dedicado → YouTube${NC}"
echo ""
echo -e "${YELLOW}📊 Auto-Scaling Configuration:${NC}"
echo -e "${YELLOW}   Proxy Scaling (1-10 replicas via KEDA):${NC}"
echo -e "${YELLOW}   • Primary: >800 Mbps inbound (80% of 1Gbps per node)${NC}"
echo -e "${YELLOW}   • Secondary: >50 active connections or >80% CPU${NC}"
echo -e "${YELLOW}   Worker Scaling (0-100 replicas via Controller):${NC}"
echo -e "${YELLOW}   • Controller API: /allocate cria workers sob demanda${NC}"
echo -e "${YELLOW}   • Mapeamento bidirecional: stream ↔ worker${NC}"
echo -e "${YELLOW}   • State recovery: Automático via métricas RTMP ao reiniciar${NC}"
echo ""
echo -e "${BLUE}🔍 Testing Prometheus Metrics Collection:${NC}"
echo ""

# Test 1: Check if nginx-exporter is exposing metrics
print_step "Testing nginx-exporter endpoints..."
PROXY_POD=$(kubectl get pods -n media -l app=rtmp-proxy -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

if [ -n "$PROXY_POD" ]; then
    echo "  Testing nginx stub_status endpoint..."
    if kubectl exec -n media "$PROXY_POD" -c nginx -- wget -qO- http://localhost:8080/nginx_status 2>/dev/null | head -5; then
        print_success "✓ Nginx stub_status is working"
    else
        print_warning "✗ Nginx stub_status not responding"
    fi
    
    echo ""
    echo "  Testing nginx-exporter metrics endpoint..."
    if kubectl exec -n media "$PROXY_POD" -c nginx-exporter -- wget -qO- http://localhost:9113/metrics 2>/dev/null | grep "nginx_connections" | head -5; then
        print_success "✓ Nginx-exporter is exposing metrics"
    else
        print_warning "✗ Nginx-exporter not responding"
    fi
else
    print_warning "No proxy pod found for testing"
fi

echo ""
print_step "Setting up Prometheus port-forward for testing..."
# Kill any existing Prometheus port-forward
pkill -f "kubectl.*port-forward.*prometheus.*9090" 2>/dev/null || true
sleep 1

# Start Prometheus port-forward in background
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090 >/dev/null 2>&1 &
PROM_PORT_FORWARD_PID=$!
sleep 3

# Test Prometheus API
echo "  Testing Prometheus query API..."
PROM_QUERY="nginx_connections_active{namespace=\"media\"}"
PROM_RESULT=$(curl -s "http://localhost:9090/api/v1/query?query=$PROM_QUERY" 2>/dev/null)

if echo "$PROM_RESULT" | grep -q "nginx_connections_active"; then
    print_success "✓ Prometheus is scraping nginx metrics"
    echo ""
    echo "  📊 Current nginx_connections_active:"
    echo "$PROM_RESULT" | grep -o '"value":\[[^]]*\]' | head -3
else
    print_warning "✗ Prometheus may not have scraped metrics yet (wait 30s and check manually)"
fi

echo ""
echo -e "${GREEN}🚀 Ready to receive RTMP streams!${NC}"
echo ""
echo -e "${BLUE}🔍 Metrics Verification:${NC}"
echo "  🌐 Prometheus UI: http://localhost:9090"
echo "  📊 Query examples:"
echo "     - nginx_connections_active{namespace=\"media\"}"
echo "     - nginx_connections_active - nginx_connections_waiting"
echo "     - rate(container_network_receive_bytes_total{namespace=\"media\"}[1m])"
echo ""
echo "  🛑 Stop Prometheus port-forward: kill $PROM_PORT_FORWARD_PID"