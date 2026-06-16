#!/bin/bash

echo "🛑 Shutting down LiveEdgeCast simplified deployment..."

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

command -v kubectl >/dev/null 2>&1 || { 
    print_error "kubectl not found. Install kubectl first."
    exit 1
}

print_step "Checking for port-forward processes..."
pkill -f "kubectl.*port-forward.*1935" 2>/dev/null && print_warning "Found and stopped RTMP port-forward (not needed with NodePort)" || true
pkill -f "kubectl.*port-forward.*8080" 2>/dev/null && print_warning "Found and stopped HTTP port-forward" || true
pkill -f "kubectl.*port-forward.*9090" 2>/dev/null && print_success "Prometheus port-forward stopped" || true
print_success "No active port-forwards (using NodePort 31935)"

print_step "Deleting Kubernetes resources..."

print_step "Step 1/6: Scaling deployments to 0..."
if kubectl get deployment worker -n media >/dev/null 2>&1; then
    kubectl scale deployment/worker --replicas=0 -n media
    print_success "Worker scaled to 0"
fi

if kubectl get deployment proxy -n media >/dev/null 2>&1; then
    kubectl scale deployment/proxy --replicas=0 -n media
    print_success "Proxy scaled to 0"
fi
if kubectl get deployment proxy-lb -n media >/dev/null 2>&1; then
    kubectl scale deployment/proxy-lb --replicas=0 -n media
    print_success "Proxy LB scaled to 0"
fi

if kubectl get deployment controller -n media >/dev/null 2>&1; then
    kubectl scale deployment/controller --replicas=0 -n media
    print_success "Controller scaled to 0"
fi

sleep 3

print_step "Step 2/6: Deleting Deployments..."
kubectl delete deployment worker -n media 2>/dev/null && print_success "Worker deployment deleted" || print_warning "No Worker deployment found"
kubectl delete deployment proxy -n media 2>/dev/null && print_success "Proxy deployment deleted" || print_warning "No Proxy deployment found"
kubectl delete deployment proxy-lb -n media 2>/dev/null && print_success "Proxy LB deployment deleted" || print_warning "No Proxy LB deployment found"
kubectl delete deployment controller -n media 2>/dev/null && print_success "Controller deployment deleted" || print_warning "No Controller deployment found"

print_step "Waiting for all deployments to terminate..."
kubectl wait --for=delete deployment/worker -n media --timeout=60s 2>/dev/null || print_warning "Worker deployment deletion timed out"
kubectl wait --for=delete deployment/proxy -n media --timeout=60s 2>/dev/null || print_warning "Proxy deployment deletion timed out"
kubectl wait --for=delete deployment/proxy-lb -n media --timeout=60s 2>/dev/null || print_warning "Proxy LB deployment deletion timed out"
kubectl wait --for=delete deployment/controller -n media --timeout=60s 2>/dev/null || print_warning "Controller deployment deletion timed out"
print_success "All deployments terminated"

print_step "Step 3/6: Deleting Services..."
kubectl delete service worker -n media 2>/dev/null && print_success "Worker service deleted" || print_warning "No Worker service found"
kubectl delete service proxy -n media 2>/dev/null && print_success "Proxy service deleted" || print_warning "No Proxy service found"
kubectl delete service proxy-entry -n media 2>/dev/null && print_success "Proxy entry service deleted" || print_warning "No Proxy entry service found"
kubectl delete service proxy-headless -n media 2>/dev/null && print_success "Proxy headless service deleted" || print_warning "No Proxy headless service found"
kubectl delete service controller -n media 2>/dev/null && print_success "Controller service deleted" || print_warning "No Controller service found"
kubectl delete configmap proxy-lb-config -n media 2>/dev/null && print_success "Proxy LB config deleted" || print_warning "No Proxy LB config found"

print_step "Step 4/6: Deleting ServiceMonitors..."
kubectl delete servicemonitor worker-metrics -n monitoring 2>/dev/null && print_success "Worker ServiceMonitor deleted" || print_warning "No Worker ServiceMonitor found"
kubectl delete servicemonitor proxy-metrics -n monitoring 2>/dev/null && print_success "Proxy ServiceMonitor deleted" || print_warning "No Proxy ServiceMonitor found"
kubectl delete servicemonitor controller-metrics -n monitoring 2>/dev/null && print_success "Controller ServiceMonitor deleted" || print_warning "No Controller ServiceMonitor found"

print_step "Step 5/6: Deleting RBAC resources..."
kubectl delete rolebinding controller-binding -n media 2>/dev/null && print_success "Controller RoleBinding deleted" || print_warning "No Controller RoleBinding found"
kubectl delete role controller-role -n media 2>/dev/null && print_success "Controller Role deleted" || print_warning "No Controller Role found"
kubectl delete serviceaccount controller -n media 2>/dev/null && print_success "Controller ServiceAccount deleted" || print_warning "No Controller ServiceAccount found"

print_success "All Kubernetes resources deleted in correct order"

print_step "Step 6/6: Deleting namespaces from k8s/namespaces.yaml..."
kubectl delete -f k8s/namespaces.yaml --ignore-not-found=true 2>/dev/null || print_warning "Namespace deletion command returned warnings"

print_step "Checking for remaining pods..."
CONTROLLER_PODS=$(kubectl get pods -l app=controller -n media --no-headers 2>/dev/null | wc -l)
PROXY_PODS=$(kubectl get pods -l app=proxy -n media --no-headers 2>/dev/null | wc -l)
WORKER_PODS=$(kubectl get pods -l app=worker -n media --no-headers 2>/dev/null | wc -l)

if [ "$CONTROLLER_PODS" -gt 0 ]; then
    print_step "Force deleting remaining controller pods..."
    kubectl delete pods -l app=controller -n media --force --grace-period=0 2>/dev/null || true
    print_success "Controller pods cleaned up"
fi

if [ "$PROXY_PODS" -gt 0 ]; then
    print_step "Force deleting remaining proxy pods..."
    kubectl delete pods -l app=proxy -n media --force --grace-period=0 2>/dev/null || true
    print_success "Proxy pods cleaned up"
fi

if [ "$WORKER_PODS" -gt 0 ]; then
    print_step "Force deleting remaining worker pods..."
    kubectl delete pods -l app=worker -n media --force --grace-period=0 2>/dev/null || true
    print_success "Worker pods cleaned up"
fi

if [ "$CONTROLLER_PODS" -eq 0 ] && [ "$PROXY_PODS" -eq 0 ] && [ "$WORKER_PODS" -eq 0 ]; then
    print_success "No remaining pods found"
fi

print_step "Verifying cleanup..."
echo ""
echo "📊 Final status:"
echo ""

DEPLOYMENTS=$(kubectl get deployment -n media --no-headers 2>/dev/null | wc -l)
SERVICES=$(kubectl get service -n media --no-headers 2>/dev/null | wc -l)
SERVICEMONITORS=$(kubectl get servicemonitor -n monitoring --no-headers 2>/dev/null | wc -l)
PODS=$(kubectl get pods -n media --no-headers 2>/dev/null | wc -l)
RBAC_ROLES=$(kubectl get role -n media --no-headers 2>/dev/null | grep controller | wc -l)
RBAC_ROLEBINDINGS=$(kubectl get rolebinding -n media --no-headers 2>/dev/null | grep controller | wc -l)
RBAC_SA=$(kubectl get serviceaccount -n media --no-headers 2>/dev/null | grep controller | wc -l)

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
if [ "$DEPLOYMENTS" -eq 0 ] && [ "$SERVICES" -eq 0 ] && [ "$PODS" -eq 0 ]; then
    print_success "🎉 LiveEdgeCast shutdown completed successfully!"
else
    print_warning "⚠️  Some resources may still be terminating. Run 'kubectl get all -n media' to verify."
fi
