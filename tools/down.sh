#!/bin/bash

echo "🛑 Shutting down LiveEdgeCast..."

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

if ! command -v kubectl >/dev/null 2>&1; then
    print_error "kubectl not found. Install kubectl first."
    exit 1
fi

if ! kubectl cluster-info >/dev/null 2>&1; then
    print_error "Cannot connect to Kubernetes cluster!"
    kubectl config current-context 2>/dev/null || echo "  No active context found"
    exit 1
fi

print_step "Stopping local port forwards..."
pkill -f "kubectl.*port-forward.*1935" 2>/dev/null || true
pkill -f "kubectl.*port-forward.*8080" 2>/dev/null || true

if ! MEDIA_NAMESPACE_RESULT=$(kubectl get namespace media --ignore-not-found -o name); then
    print_error "Unable to determine whether the media namespace exists"
    exit 1
elif [[ -n $MEDIA_NAMESPACE_RESULT ]]; then
    print_step "Deleting Kubernetes resources..."
    kubectl delete -f k8s/ --ignore-not-found=true
else
    print_success "The media namespace is already absent"
fi

# Upgrade cleanup for clusters provisioned by earlier releases. Deleting the
# namespace removes the legacy chart release and its namespaced resources even
# when the media namespace has already been removed.
print_step "Deleting the legacy monitoring namespace, if present..."
kubectl delete namespace monitoring --ignore-not-found=true

print_success "LiveEdgeCast shutdown completed"
