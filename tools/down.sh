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
pkill -f "kubectl.*port-forward.*8080" 2>/dev/null && print_success "Port-forward on 8080 stopped" || print_warning "No port-forward on 8080 found"
pkill -f "kubectl.*port-forward.*keda.*8080" 2>/dev/null && print_success "KEDA port-forward stopped" || true

# Step 2: Delete Kubernetes resources
print_step "Deleting Kubernetes resources..."

# Delete HTTPScaledObject first (important for KEDA)
if kubectl get httpscaledobject nginx-http-scaledobject >/dev/null 2>&1; then
    print_step "Deleting HTTPScaledObject..."
    kubectl delete httpscaledobject nginx-http-scaledobject
    print_success "HTTPScaledObject deleted"
else
    print_warning "No HTTPScaledObject found"
fi

# Delete other resources
if kubectl get deployment nginx-deployment >/dev/null 2>&1; then
    print_step "Deleting remaining Kubernetes resources..."
    kubectl delete -f k8s/ 2>/dev/null || true
    
    # Wait for deployment to be fully deleted
    print_step "Waiting for deployment to be cleaned up..."
    kubectl wait --for=delete deployment/nginx-deployment --timeout=60s 2>/dev/null || print_warning "Deployment deletion timed out"
    
    print_success "Kubernetes resources deleted"
else
    print_warning "No nginx deployment found. Skipping manifest deletion."
fi

# Step 3: Clean up any remaining pods
print_step "Checking for remaining pods..."
REMAINING_PODS=$(kubectl get pods -l app=nginx-deployment --no-headers 2>/dev/null | wc -l)
if [ "$REMAINING_PODS" -gt 0 ]; then
    print_step "Force deleting remaining nginx pods..."
    kubectl delete pods -l app=nginx-deployment --force --grace-period=0 2>/dev/null || true
    print_success "Remaining pods cleaned up"
else
    print_success "No remaining pods found"
fi

# Step 4: Verify cleanup
print_step "Verifying cleanup..."
echo ""
echo "📊 Final status:"
kubectl get httpscaledobject 2>/dev/null || echo "  No HTTPScaledObjects found ✅"
kubectl get deployment nginx-deployment 2>/dev/null || echo "  No nginx deployment found ✅"
kubectl get svc nginx-service 2>/dev/null || echo "  No nginx service found ✅"
kubectl get pods -l app=nginx-deployment 2>/dev/null || echo "  No nginx pods found ✅"

echo ""
print_success "🎉 LiveEdgeCast shutdown completed!"