# LiveEdgeCast

🚀 **RTMP Edge Proxy for Low-Latency Live Stream Retransmission**

LiveEdgeCast runs on Kubernetes at the edge and focuses on retransmitting live streams with minimal latency. The proxy replica count is statically configured by the `replicas` field in `k8s/proxy-deployment.yaml`.

## 🎯 Project Goals

- **Low-Latency Retransmission**: Minimize delay in live stream forwarding
- **Edge Computing**: Deploy close to users for optimal performance  
- **High Availability**: Fault-tolerant stream proxy architecture
- **Predictable Proxy Capacity**: A static replica count configured through the Proxy Deployment


# How to Start and Stop the Project

To start the project, use the provided script:

```sh
./tools/up.sh
pkill -f "kubectl.*port-forward.*1935" 2>/dev/null; pkill -f "kubectl.*port-forward.*8080" 2>/dev/null; sleep 2 && kubectl port-forward -n media svc/proxy 1935:1935 >/dev/null 2>&1 & kubectl port-forward -n media svc/proxy 8080:8080 >/dev/null 2>&1 & sleep 3 && echo "Port-forwards configurados!" && netstat -tlnp 2>/dev/null | grep -E "1935|8080" | grep LISTEN || ss -tlnp | grep -E "1935|8080" & kubectl port-forward -n monitoring svc/prometheus-kube-prometheus-prometheus 9090:9090 >/dev/null 2>&1 & sleep 3 && echo "Prometheus port-forward configurado!"
```

To stop the project, use:

```sh
./tools/down.sh
```

# Running Directly with Docker

Alternatively, you can run the project directly using Docker Compose:

```sh
RTMP_PUSH_URL=rtmp://a.rtmp.youtube.com/live2/2tww-t6fv-z2mh-0rsq-4z8t docker-compose up -d
```

To stop and remove the containers:

```sh
docker-compose down
```

# Requirements
- **Docker**: Ensure Docker is installed and running.
- **Kubernetes**: A Kubernetes cluster is required for deployment.
- **kubectl**: Command-line tool for interacting with Kubernetes clusters.
- **Docker Compose**: For running the project directly with Docker.
# Environment Variables
- **RTMP_PUSH_URL**: The RTMP URL to which the stream will be pushed. It should be in the format `rtmp://upstream.example.com/live/yourStreamKey`.
