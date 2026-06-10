# LiveEdgeCast

🚀 **Serverless RTMP Edge Proxy for Low-Latency Live Stream Retransmission**

LiveEdgeCast implements a serverless architecture on edge computing environments, focusing on the retransmission of live streams with minimal latency. The solution ensures high availability, responsiveness, and efficient resource utilization by dynamically provisioning compute resources only when needed.

## 🎯 Project Goals

- **Low-Latency Retransmission**: Minimize delay in live stream forwarding
- **Edge Computing**: Deploy close to users for optimal performance  
- **Serverless Execution**: Dynamic resource provisioning and cost optimization
- **High Availability**: Fault-tolerant stream proxy architecture
- **Efficient Resource Utilization**: Scale up/down based on demand


## How to Start and Stop the Project

To start the project, use the provided script:

```sh
./tools/up.sh
./tools/port-forward.sh
```

To stop the project, use:

```sh
./tools/down.sh
```

## Running Directly with Docker

Alternatively, you can run the project directly using Docker Compose:

```sh
RTMP_PUSH_URL=rtmp://a.rtmp.youtube.com/live2/2tww-t6fv-z2mh-0rsq-4z8t docker-compose up -d
```

To stop and remove the containers:

```sh
docker-compose down
```

## Requirements
- **Docker**: Ensure Docker is installed and running.
- **Kubernetes**: A Kubernetes cluster is required for deployment.
- **kubectl**: Command-line tool for interacting with Kubernetes clusters.
- **Docker Compose**: For running the project directly with Docker.

## Environment Variables
- **RTMP_PUSH_URL**: The RTMP URL to which the stream will be pushed. It should be in the format `rtmp://upstream.example.com/live/yourStreamKey`.

## Observability checks

Worker metrics are exposed through the `worker` Service in namespace `media` on
the named port `metrics` (`9113`) and discovered by the `worker-metrics`
ServiceMonitor in namespace `monitoring`. Because the worker Deployment starts
with zero replicas, start a stream or otherwise ensure at least one `app=worker`
Pod exists before expecting a `worker-metrics` target to be `UP`. If you already
ran `./tools/port-forward.sh`, the Prometheus port-forward is active. Otherwise,
run the helper or start only Prometheus manually:

```sh
./tools/port-forward.sh
# or, for Prometheus only:
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090
```

If your Prometheus Helm release uses a different Service name, discover it with
`kubectl -n monitoring get svc -o wide | grep -E 'prometheus|9090'` and
port-forward the Prometheus server Service that exposes port `9090`, or run
`PROMETHEUS_SERVICE=<service-name> ./tools/port-forward.sh`.

Then open `http://localhost:9090/targets` and confirm the `worker-metrics`
target belongs to the `media` namespace and uses the `metrics` port. Detailed
alignment notes for the worker Deployment, Service, and ServiceMonitor are
documented in `docs/observability/metrics.md`.
