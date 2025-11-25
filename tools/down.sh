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

# Step 1: Stop any port-forward processes (if any)
print_step "Checking for port-forward processes..."
pkill -f "kubectl.*port-forward.*1935" 2>/dev/null && print_warning "Found and stopped RTMP port-forward (not needed with NodePort)" || true
pkill -f "kubectl.*port-forward.*8080" 2>/dev/null && print_warning "Found and stopped HTTP port-forward" || true
pkill -f "kubectl.*port-forward.*9090" 2>/dev/null && print_success "Prometheus port-forward stopped" || true
print_success "No active port-forwards (using NodePort 31935)"

# Step 2: Delete Kubernetes resources in correct order
print_step "Deleting Kubernetes resources..."

# 2.1: Delete ScaledObjects first (stop KEDA from scaling)
print_step "Step 1/7: Deleting KEDA ScaledObjects..."
kubectl delete scaledobject rtmp-worker-scaler -n media 2>/dev/null && print_success "Worker ScaledObject deleted" || print_warning "No Worker ScaledObject found"
kubectl delete scaledobject rtmp-proxy-scaler -n media 2>/dev/null && print_success "Proxy ScaledObject deleted" || print_warning "No Proxy ScaledObject found"

# 2.2: Scale deployments to 0 first (graceful shutdown)
print_step "Step 2/7: Scaling deployments to 0..."
if kubectl get deployment rtmp-worker -n media >/dev/null 2>&1; then
    kubectl scale deployment/rtmp-worker --replicas=0 -n media
    print_success "Worker scaled to 0"
fi

if kubectl get deployment rtmp-proxy -n media >/dev/null 2>&1; then
    kubectl scale deployment/rtmp-proxy --replicas=0 -n media
    print_success "Proxy scaled to 0"
fi

if kubectl get deployment rtmp-controller -n media >/dev/null 2>&1; then
    kubectl scale deployment/rtmp-controller --replicas=0 -n media
    print_success "Controller scaled to 0"
fi

# Wait a moment for graceful shutdown
sleep 3

# 2.3: Delete Deployments
print_step "Step 3/7: Deleting Deployments..."
kubectl delete deployment rtmp-worker -n media 2>/dev/null && print_success "Worker deployment deleted" || print_warning "No Worker deployment found"
kubectl delete deployment rtmp-proxy -n media 2>/dev/null && print_success "Proxy deployment deleted" || print_warning "No Proxy deployment found"
kubectl delete deployment rtmp-controller -n media 2>/dev/null && print_success "Controller deployment deleted" || print_warning "No Controller deployment found"

# Wait for deployments to be fully deleted
print_step "Waiting for all deployments to terminate..."
kubectl wait --for=delete deployment/rtmp-worker -n media --timeout=60s 2>/dev/null || print_warning "Worker deployment deletion timed out"
kubectl wait --for=delete deployment/rtmp-proxy -n media --timeout=60s 2>/dev/null || print_warning "Proxy deployment deletion timed out"
kubectl wait --for=delete deployment/rtmp-controller -n media --timeout=60s 2>/dev/null || print_warning "Controller deployment deletion timed out"
print_success "All deployments terminated"

# 2.4: Delete Services
print_step "Step 4/7: Deleting Services..."
kubectl delete service rtmp-worker -n media 2>/dev/null && print_success "Worker service deleted" || print_warning "No Worker service found"
kubectl delete service rtmp-proxy -n media 2>/dev/null && print_success "Proxy service deleted" || print_warning "No Proxy service found"
kubectl delete service rtmp-proxy-headless -n media 2>/dev/null && print_success "Proxy headless service deleted" || print_warning "No Proxy headless service found"
kubectl delete service rtmp-controller -n media 2>/dev/null && print_success "Controller service deleted" || print_warning "No Controller service found"

# 2.5: Delete ServiceMonitors (Prometheus)
print_step "Step 5/7: Deleting ServiceMonitors..."
kubectl delete servicemonitor rtmp-worker-metrics -n media 2>/dev/null && print_success "Worker ServiceMonitor deleted" || print_warning "No Worker ServiceMonitor found"
kubectl delete servicemonitor rtmp-proxy-metrics -n media 2>/dev/null && print_success "Proxy ServiceMonitor deleted" || print_warning "No Proxy ServiceMonitor found"

# 2.6: Delete ConfigMaps
print_step "Step 6/7: Deleting ConfigMaps..."
kubectl delete configmap rtmp-worker-nginx-conf -n media 2>/dev/null && print_success "Worker ConfigMap deleted" || print_warning "No Worker ConfigMap found"
kubectl delete configmap rtmp-proxy-nginx-conf -n media 2>/dev/null && print_success "Proxy ConfigMap deleted" || print_warning "No Proxy ConfigMap found"

# 2.7: Delete RBAC (last, after all pods are gone)
print_step "Step 7/7: Deleting RBAC resources..."
kubectl delete rolebinding rtmp-controller-binding -n media 2>/dev/null && print_success "Controller RoleBinding deleted" || print_warning "No Controller RoleBinding found"
kubectl delete role rtmp-controller-role -n media 2>/dev/null && print_success "Controller Role deleted" || print_warning "No Controller Role found"
kubectl delete serviceaccount rtmp-controller -n media 2>/dev/null && print_success "Controller ServiceAccount deleted" || print_warning "No Controller ServiceAccount found"

print_success "All Kubernetes resources deleted in correct order"

# Step 3: Clean up any remaining pods (force if needed)
print_step "Checking for remaining pods..."
CONTROLLER_PODS=$(kubectl get pods -l app=rtmp-controller -n media --no-headers 2>/dev/null | wc -l)
PROXY_PODS=$(kubectl get pods -l app=rtmp-proxy -n media --no-headers 2>/dev/null | wc -l)
WORKER_PODS=$(kubectl get pods -l app=rtmp-worker -n media --no-headers 2>/dev/null | wc -l)

if [ "$CONTROLLER_PODS" -gt 0 ]; then
    print_step "Force deleting remaining controller pods..."
    kubectl delete pods -l app=rtmp-controller -n media --force --grace-period=0 2>/dev/null || true
    print_success "Controller pods cleaned up"
fi

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

if [ "$CONTROLLER_PODS" -eq 0 ] && [ "$PROXY_PODS" -eq 0 ] && [ "$WORKER_PODS" -eq 0 ]; then
    print_success "No remaining pods found"
fi

# Step 4: Verify cleanup
print_step "Verifying cleanup..."
echo ""
echo "📊 Final status:"
echo ""

# Check all resources
SCALEDOBJECTS=$(kubectl get scaledobject -n media --no-headers 2>/dev/null | wc -l)
DEPLOYMENTS=$(kubectl get deployment -n media --no-headers 2>/dev/null | wc -l)
SERVICES=$(kubectl get service -n media --no-headers 2>/dev/null | wc -l)
SERVICEMONITORS=$(kubectl get servicemonitor -n media --no-headers 2>/dev/null | wc -l)
CONFIGMAPS=$(kubectl get configmap -n media --no-headers 2>/dev/null | wc -l)
PODS=$(kubectl get pods -n media --no-headers 2>/dev/null | wc -l)
RBAC_ROLES=$(kubectl get role -n media --no-headers 2>/dev/null | grep rtmp-controller | wc -l)
RBAC_ROLEBINDINGS=$(kubectl get rolebinding -n media --no-headers 2>/dev/null | grep rtmp-controller | wc -l)
RBAC_SA=$(kubectl get serviceaccount -n media --no-headers 2>/dev/null | grep rtmp-controller | wc -l)

if [ "$SCALEDOBJECTS" -eq 0 ]; then
    print_success "ScaledObjects: 0 (all deleted)"
else
    print_warning "ScaledObjects: $SCALEDOBJECTS remaining"
    kubectl get scaledobject -n media
fi

if [ "$DEPLOYMENTS" -eq 0 ]; then
    print_success "Deployments: 0 (all deleted)"
else
    print_warning "Deployments: $DEPLOYMENTS remaining"
    kubectl get deployment -n media
fi

if [ "$SERVICES" -eq 0 ]; then
    print_success "Services: 0 (all deleted)"
else
    print_warning "Services: $SERVICES remaining"
    kubectl get service -n media
fi

if [ "$SERVICEMONITORS" -eq 0 ]; then
    print_success "ServiceMonitors: 0 (all deleted)"
else
    print_warning "ServiceMonitors: $SERVICEMONITORS remaining"
fi

if [ "$CONFIGMAPS" -eq 0 ]; then
    print_success "ConfigMaps: 0 (all deleted)"
else
    print_warning "ConfigMaps: $CONFIGMAPS remaining"
fi

if [ "$PODS" -eq 0 ]; then
    print_success "Pods: 0 (all deleted)"
else
    print_warning "Pods: $PODS remaining"
    kubectl get pods -n media
fi

if [ "$RBAC_ROLES" -eq 0 ] && [ "$RBAC_ROLEBINDINGS" -eq 0 ] && [ "$RBAC_SA" -eq 0 ]; then
    print_success "RBAC: All deleted (Role, RoleBinding, ServiceAccount)"
else
    print_warning "RBAC: Some resources remaining (Role:$RBAC_ROLES, RoleBinding:$RBAC_ROLEBINDINGS, SA:$RBAC_SA)"
fi

echo ""
if [ "$SCALEDOBJECTS" -eq 0 ] && [ "$DEPLOYMENTS" -eq 0 ] && [ "$SERVICES" -eq 0 ] && [ "$PODS" -eq 0 ]; then
    print_success "🎉 LiveEdgeCast shutdown completed successfully!"
else
    print_warning "⚠️  Some resources may still be terminating. Run 'kubectl get all -n media' to verify."
fi