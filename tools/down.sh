#!/bin/bash

set -e

echo "Cleaning up LiveEdgeCast Kubernetes resources..."
command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found. Install kubectl first."; exit 1; }

echo "1. Scaler (KEDA)"
kubectl delete -f k8s/scaler/ --ignore-not-found
kubectl wait --for=delete --timeout=120s scaledobject/liveedgecast-rtmp-scaler || true

echo "2. Worker"
kubectl delete -f k8s/worker/ --ignore-not-found
kubectl rollout status deployment/rtmp-worker --timeout=120s || true

echo "3. Edge"
kubectl delete -f k8s/edge/ --ignore-not-found
kubectl rollout status deployment/rtmp-edge --timeout=120s || true

echo "4. Exporter"
kubectl delete -f k8s/exporter/ --ignore-not-found
kubectl rollout status deployment/rtmp-metrics-exporter --timeout=120s || true

echo "5. Prometheus"
kubectl delete -f k8s/prometheus/ --ignore-not-found
kubectl rollout status deployment/prometheus --timeout=120s || true

echo "Deleting secret liveedgecast-secrets (if exists)..."
kubectl delete secret liveedgecast-secrets --ignore-not-found

echo "Cleanup completed!"
