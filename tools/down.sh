#!/bin/bash

echo "🛑 Shutting down LiveEdgeCast with KEDA..."

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

# Check if kubectl is available
command -v kubectl >/dev/null 2>&1 || { 
    print_error "kubectl not found. Install kubectl first."
    exit 1
}

# Step 1: Stop port-forward processes
print_step "Stopping port-forward processes..."
pkill -f "kubectl.*port-forward.*1935" 2>/dev/null && print_success "RTMP port-forward on 1935 stopped" || print_warning "No RTMP port-forward found"
pkill -f "kubectl.*port-forward.*8080" 2>/dev/null && print_success "HTTP port-forward on 8080 stopped" || print_warning "No HTTP port-forward found"
pkill -f "kubectl.*port-forward.*9090" 2>/dev/null && print_success "Prometheus port-forward on 9090 stopped" || print_warning "No Prometheus port-forward found"

# Step 2: Delete Kubernetes resources
print_step "Deleting Kubernetes resources..."

# Delete ScaledObjects first (important for KEDA)
if kubectl get scaledobject rtmp-worker-scaler -n media >/dev/null 2>&1; then
    print_step "Deleting Worker ScaledObject..."
    kubectl delete scaledobject rtmp-worker-scaler -n media
    print_success "Worker ScaledObject deleted"
else
    print_warning "No Worker ScaledObject found"
fi

if kubectl get scaledobject rtmp-proxy-scaler -n media >/dev/null 2>&1; then
    print_step "Deleting Proxy ScaledObject..."
    kubectl delete scaledobject rtmp-proxy-scaler -n media
    print_success "Proxy ScaledObject deleted"
else
    print_warning "No Proxy ScaledObject found"
fi

# Delete deployments
if kubectl get deployment rtmp-proxy -n media >/dev/null 2>&1 || kubectl get deployment rtmp-worker -n media >/dev/null 2>&1; then
    print_step "Deleting LiveEdgeCast deployments..."
    kubectl delete -f k8s/ -n media 2>/dev/null || true
    
    # Wait for deployments to be fully deleted
    print_step "Waiting for deployments to be cleaned up..."
    kubectl wait --for=delete deployment/rtmp-proxy -n media --timeout=60s 2>/dev/null || print_warning "Proxy deployment deletion timed out"
    kubectl wait --for=delete deployment/rtmp-worker -n media --timeout=60s 2>/dev/null || print_warning "Worker deployment deletion timed out"
    
    print_success "Kubernetes resources deleted"
else
    print_warning "No LiveEdgeCast deployments found. Skipping manifest deletion."
fi

# Step 3: Clean up any remaining pods
print_step "Checking for remaining pods..."
PROXY_PODS=$(kubectl get pods -l app=rtmp-proxy -n media --no-headers 2>/dev/null | wc -l)
WORKER_PODS=$(kubectl get pods -l app=rtmp-worker -n media --no-headers 2>/dev/null | wc -l)

if [ "$PROXY_PODS" -gt 0 ]; then
    print_step "Force deleting remaining proxy pods..."
    kubectl delete pods -l app=rtmp-proxy -n media --force --grace-period=0 2>/dev/null || true
    print_success "Proxy pods cleaned up"
fi

if [ "$WORKER_PODS" -gt 0 ]; then
    print_step "Force deleting remaining worker pods..."
    kubectl delete pods -l app=rtmp-worker -n media --force --grace-period=0 2>/dev/null || true
    print_success "Worker pods cleaned up"
fi

if [ "$PROXY_PODS" -eq 0 ] && [ "$WORKER_PODS" -eq 0 ]; then
    print_success "No remaining pods found"
fi

# Step 4: Verify cleanup
print_step "Verifying cleanup..."
echo ""
echo "📊 Final status:"
kubectl get scaledobject -n media 2>/dev/null || echo "  No ScaledObjects found ✅"
kubectl get deployment rtmp-proxy -n media 2>/dev/null || echo "  No rtmp-proxy deployment found ✅"
kubectl get deployment rtmp-worker -n media 2>/dev/null || echo "  No rtmp-worker deployment found ✅"
kubectl get svc rtmp-proxy -n media 2>/dev/null || echo "  No rtmp-proxy service found ✅"
kubectl get svc rtmp-worker -n media 2>/dev/null || echo "  No rtmp-worker service found ✅"
kubectl get pods -n media 2>/dev/null || echo "  No pods found in media namespace ✅"

echo ""
print_success "🎉 LiveEdgeCast shutdown completed!"