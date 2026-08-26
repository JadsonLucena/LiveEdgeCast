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

print_step "Stopping local port forwards..."
pkill -f "kubectl.*port-forward.*1935" 2>/dev/null || true
pkill -f "kubectl.*port-forward.*8080" 2>/dev/null || true

if ! kubectl get namespace media >/dev/null 2>&1; then
    print_success "The media namespace is already absent"
    exit 0
fi

print_step "Deleting Kubernetes resources..."
# Legacy per-stream workers were created directly and are not covered by a manifest.
kubectl delete pods -l app=worker -n media --ignore-not-found=true
kubectl delete -f k8s/ --ignore-not-found=true

print_success "LiveEdgeCast shutdown completed"
