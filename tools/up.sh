#!/bin/bash

echo "Deploying LiveEdgeCast to Kubernetes..."

set -e

command -v kubectl >/dev/null 2>&1 || { echo "❌ kubectl not found. Install kubectl first."; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "❌ docker not found. Install Docker first."; exit 1; }

if ! kubectl cluster-info >/dev/null 2>&1; then
    echo "Cannot connect to Kubernetes cluster!"
    kubectl config current-context 2>/dev/null || echo "  No active context found"
    exit 1
fi

# Get RTMP push URL
if [ -z "$RTMP_PUSH_URL" ]; then
    read -p "Enter RTMP push URL/key: " RTMP_PUSH_URL
    [ -z "$RTMP_PUSH_URL" ] && { echo "RTMP URL required"; exit 1; }
    export RTMP_PUSH_URL
fi

if ! [[ "$RTMP_PUSH_URL" =~ ^rtmp://.+/.+ ]]; then
    echo "Invalid RTMP_PUSH_URL. It must start with rtmp:// and contain a stream key."
    exit 1
fi

docker build -f docker/Dockerfile -t liveedgecast:latest . || { echo "Build failed"; exit 1; }

# Handle Docker image for cluster
CONTEXT=$(kubectl config current-context)
if [[ ! $CONTEXT =~ (docker-desktop|localhost|127\.0\.0\.1) ]]; then
    echo "Remote/managed cluster: ensure the liveedgecast:latest image is available in the cluster."
    echo -n "Continuar com o deploy? (y/n): "
    read -r continue_deploy
    if [[ ! $continue_deploy =~ ^[Yy]$ ]]; then
        echo "Deployment cancelled. Please ensure image is available in your cluster."
        exit 1
    fi
fi

kubectl delete secret liveedgecast-secrets --ignore-not-found
kubectl create secret generic liveedgecast-secrets \
    --from-literal=rtmp-push-url="$RTMP_PUSH_URL" \
    --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f k8s/

kubectl wait --for=condition=ready pod -l app=liveedgecast --timeout=120s

echo "Deployment completed!"
kubectl get pods,svc -l app=liveedgecast

# Show access information
EXTERNAL_IP=$(kubectl get svc liveedgecast-service -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)
[ -z "$EXTERNAL_IP" ] && EXTERNAL_IP=$(kubectl get svc liveedgecast-service -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null)

if [ -n "$EXTERNAL_IP" ]; then
    echo ""
    echo "🔗 Access endpoints (LoadBalancer):"
    echo "  RTMP: rtmp://$EXTERNAL_IP:1935/live"
    echo "  Health: http://$EXTERNAL_IP:8080/health"
    echo "  Stats: http://$EXTERNAL_IP:8080/stats"
else
    # Get NodePort information for alternative access
    RTMP_NODEPORT=$(kubectl get svc liveedgecast-service -o jsonpath='{.spec.ports[?(@.name=="rtmp")].nodePort}' 2>/dev/null)
    HTTP_NODEPORT=$(kubectl get svc liveedgecast-service -o jsonpath='{.spec.ports[?(@.name=="http")].nodePort}' 2>/dev/null)
    
    echo ""
    echo "⏳ External IP not available (this is normal for most local/development clusters)"
    echo ""
    echo "🔗 Access options:"
    echo "  1. Port forwarding (recommended for development):"
    echo "     kubectl port-forward svc/liveedgecast-service 1935:1935 8080:8080"
    echo "     Then access:"
    echo "       RTMP: rtmp://localhost:1935/live"
    echo "       Health: http://localhost:8080/health"
    echo "       Stats: http://localhost:8080/stats"
    
    if [ -n "$RTMP_NODEPORT" ] && [ -n "$HTTP_NODEPORT" ]; then
        echo ""
        echo "  2. NodePort access (if cluster nodes are accessible):"
        echo "     RTMP: rtmp://<node-ip>:$RTMP_NODEPORT/live"
        echo "     Health: http://<node-ip>:$HTTP_NODEPORT/health"
        echo "     Stats: http://<node-ip>:$HTTP_NODEPORT/stats"
        echo "     (Replace <node-ip> with any cluster node IP)"
    fi
fi