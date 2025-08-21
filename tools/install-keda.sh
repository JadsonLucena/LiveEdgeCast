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
kubectl create namespace keda --dry-run=client -o yaml | kubectl apply -f -
helm install keda kedacore/keda --namespace keda
echo "KEDA installed successfully!"
echo "Checking pods in keda namespace..."
kubectl get pods -n keda