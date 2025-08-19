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

if kubectl get secret liveedgecast-secrets >/dev/null 2>&1; then
    echo "Deleting secret liveedgecast-secrets..."
    kubectl delete secret liveedgecast-secrets
else
    echo "No liveedgecast-secrets secret found. Skipping secret deletion."
fi

echo "Cleanup completed!"
