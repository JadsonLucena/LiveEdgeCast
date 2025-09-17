#!/bin/bash

echo "🚀 Deploying LiveEdgeCast with KEDA HTTP Add-on..."

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

# Check if KEDA HTTP Add-on is installed
if ! kubectl get pods -n keda -l app.kubernetes.io/component=interceptor >/dev/null 2>&1; then
    print_error "KEDA HTTP Add-on not found. Please install it first:"
    echo "  helm install http-add-on kedacore/keda-add-ons-http --namespace keda"
    exit 1
fi
print_success "KEDA and HTTP Add-on are installed"

# Step 3: Check/Load Docker image
print_step "Checking Docker image..."
IMAGE_NAME="nginx:latest"

if ! docker image inspect $IMAGE_NAME >/dev/null 2>&1; then
    print_step "Pulling Docker image: $IMAGE_NAME"
    docker pull $IMAGE_NAME || { print_error "Failed to pull image"; exit 1; }
fi
print_success "Docker image $IMAGE_NAME is available"

# Handle Docker image for cluster
CONTEXT=$(kubectl config current-context)
if [[ ! $CONTEXT =~ (docker-desktop|localhost|127\.0\.0\.1|kind) ]]; then
    print_warning "Remote/managed cluster detected: $CONTEXT"
    print_warning "Ensure the $IMAGE_NAME image is available in the cluster."
    echo -n "Continue with deployment? (y/n): "
    read -r continue_deploy
    if [[ ! $continue_deploy =~ ^[Yy]$ ]]; then
        print_error "Deployment cancelled. Please ensure image is available in your cluster."
        exit 1
    fi
fi

# Step 4: Deploy to Kubernetes
print_step "Deploying to Kubernetes..."
kubectl apply -f k8s/ || { print_error "Deployment failed"; exit 1; }
print_success "Kubernetes manifests applied"

# Step 5: Wait for deployment and HTTPScaledObject to be ready
print_step "Waiting for deployment to be ready..."
kubectl wait --for=condition=available deployment/nginx-deployment --timeout=60s || {
    print_error "Deployment failed to become available"
    exit 1
}

print_step "Verifying HTTPScaledObject..."
# Wait a bit for HTTPScaledObject to initialize
sleep 5
if kubectl get httpscaledobject nginx-http-scaledobject >/dev/null 2>&1; then
    # Check if it's marked as ACTIVE
    if kubectl get httpscaledobject nginx-http-scaledobject --no-headers | grep -q "True"; then
        print_success "HTTPScaledObject is active and ready"
    else
        print_warning "HTTPScaledObject created but may still be initializing"
    fi
else
    print_error "HTTPScaledObject not found"
    exit 1
fi

print_step "Checking pod status..."
POD_COUNT=$(kubectl get pods -l app=nginx-deployment --no-headers 2>/dev/null | wc -l)
if [ "$POD_COUNT" -eq 0 ]; then
    print_warning "No pods running (KEDA scaling from 0 - pods will be created on first request)"
else
    print_success "$POD_COUNT pod(s) are running"
    kubectl wait --for=condition=ready pod -l app=nginx-deployment --timeout=30s 2>/dev/null || {
        print_warning "Some pods may still be starting"
    }
fi

# Step 6: Check KEDA HTTP Add-on status
print_step "Checking KEDA HTTP Add-on status..."
kubectl get pods -n keda -l app.kubernetes.io/component=interceptor --no-headers | grep Running >/dev/null || {
    print_warning "KEDA HTTP Interceptor may not be ready yet"
}

# Step 7: Setup port-forward
print_step "Setting up port-forward..."

# Kill any existing port-forward on port 8080
pkill -f "kubectl.*port-forward.*8080" 2>/dev/null || true
sleep 2

# Start port-forward in background
print_step "Starting port-forward to KEDA HTTP Interceptor on localhost:8080..."
kubectl port-forward -n keda svc/keda-add-ons-http-interceptor-proxy 8080:8080 >/dev/null 2>&1 &
PORT_FORWARD_PID=$!

print_success "Deployment completed!"

# Display status
echo ""
print_step "Deployment Status:"
kubectl get httpscaledobject
echo ""
kubectl get pods -l app=nginx-deployment
echo ""
kubectl get svc nginx-service

echo ""
print_success "🎉 LiveEdgeCast is ready!"
echo ""
echo -e "${GREEN}📡 Access your application:${NC}"
echo "  🌐 Web: http://localhost:8080"
echo ""
echo -e "${BLUE}🔧 Useful commands:${NC}"
echo "  📊 Check scaling: kubectl get pods -l app=nginx-deployment -w"
echo "  📋 View logs: kubectl logs -l app=nginx-deployment -f"
echo "  🔍 KEDA status: kubectl get httpscaledobject"
echo "  🛑 Stop port-forward: kill $PORT_FORWARD_PID"
echo ""
echo -e "${YELLOW}💡 Note: With KEDA, pods will scale to 0 after 10 seconds of inactivity${NC}"
echo -e "${YELLOW}   Make a request to http://localhost:8080 to scale up automatically${NC}"