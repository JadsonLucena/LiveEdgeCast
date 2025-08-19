#!/bin/bash

echo "Installing KEDA on Kubernetes cluster..."

if ! command -v helm &> /dev/null; then
    echo "Installing Helm..."
    curl https://get.helm.sh/helm-v3.12.0-linux-amd64.tar.gz | tar xz
    mv linux-amd64/helm /usr/local/bin/
    rm -rf linux-amd64/
fi

helm repo add kedacore https://kedacore.github.io/charts
helm repo update

helm install keda kedacore/keda --namespace keda-system --create-namespace

echo "KEDA installed successfully!"
echo "Waiting for pods to be ready..."

kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=keda -n keda-system --timeout=300s

echo "KEDA is ready to use!"