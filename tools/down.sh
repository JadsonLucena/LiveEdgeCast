#!/bin/bash

set -e

echo "Cleaning up LiveEdgeCast Kubernetes resources..."

command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found. Install kubectl first."; exit 1; }

if kubectl get deployment liveedgecast-deployment >/dev/null 2>&1; then
    echo "Deleting Kubernetes resources..."
    kubectl delete -f k8s/
else
    echo "No LiveEdgeCast deployment found. Skipping manifest deletion."
fi

echo "Cleaning up KEDA resources..."
if kubectl get scaledobject liveedgecast-scaledobject >/dev/null 2>&1; then
    echo "Deleting ScaledObject..."
    kubectl delete scaledobject liveedgecast-scaledobject
else
    echo "No ScaledObject found. Skipping ScaledObject deletion."
fi

if kubectl get hpa liveedgecast-hpa >/dev/null 2>&1; then
    echo "Deleting HPA..."
    kubectl delete hpa liveedgecast-hpa
else
    echo "No HPA found. Skipping HPA deletion."
fi

echo "Cleaning up KEDA metrics..."
kubectl delete --ignore-not-found=true \
    --selector=app=liveedgecast \
    --all-namespaces \
    --field-selector=metadata.name!=liveedgecast-deployment \
    --field-selector=metadata.name!=liveedgecast-service \
    --field-selector=metadata.name!=liveedgecast-secrets

if kubectl get secret liveedgecast-secrets >/dev/null 2>&1; then
    echo "Deleting secret liveedgecast-secrets..."
    kubectl delete secret liveedgecast-secrets
else
    echo "No liveedgecast-secrets secret found. Skipping secret deletion."
fi

echo "Cleanup completed!"
