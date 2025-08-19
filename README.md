# LiveEdgeCast

**Serverless RTMP Edge Proxy for Low-Latency Live Stream Retransmission**

LiveEdgeCast implements a serverless architecture on edge computing environments, focusing on the retransmission of live streams with minimal latency. The solution ensures high availability, responsiveness, and efficient resource utilization by dynamically provisioning compute resources only when needed.

## Project Goals

- **Low-Latency Retransmission**: Minimize delay in live stream forwarding
- **Edge Computing**: Deploy close to users for optimal performance  
- **Serverless Execution**: Dynamic resource provisioning and cost optimization
- **High Availability**: Fault-tolerant stream proxy architecture
- **Efficient Resource Utilization**: Scale up/down based on demand


# How to Start and Stop the Project

### Install KEDA
```sh
./tools/install-keda.sh
```

To start the project:

```sh
./tools/up.sh
```

To stop the project:

```sh
./tools/down.sh
```

## What is KEDA and How It Works

**KEDA (Kubernetes Event-driven Autoscaling)** is a Kubernetes-based event-driven autoscaling component that enables you to scale any container in Kubernetes based on the number of events needing to be processed.

### How KEDA Enhances LiveEdgeCast

1. **Smart Scaling**: Automatically scales pods based on RTMP stream activity
2. **Zero-Scale**: Scales to zero when no streams are active (cost optimization)
3. **Event-Driven**: Responds to real-time streaming metrics and events
4. **Resource Efficiency**: Only provisions resources when actually needed

### Scaling Triggers

- **RTMP Stream Count**: Scales based on active RTMP connections
- **CPU/Memory Usage**: Responds to resource consumption patterns
- **Custom Metrics**: Can scale based on custom streaming metrics
- **Time-based**: Can scale during peak streaming hours

### Architecture with KEDA

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   RTMP Client   │───▶│  LiveEdgeCast   │───▶│  Upstream RTMP  │
│                 │    │     Pods        │    │     Server      │
└─────────────────┘    └─────────────────┘    └─────────────────┘
                                │
                                ▼
                       ┌─────────────────┐
                       │   KEDA Metrics  │
                       │   & ScaledObject│
                       └─────────────────┘
                                │
                                ▼
                       ┌─────────────────┐
                       │  Auto-scaling   │
                       │   Controller    │
                       └─────────────────┘
```

# Running Directly with Docker

Alternatively, you can run the project directly using Docker Compose:

```sh
RTMP_PUSH_URL=rtmp://upstream.example.com/live/yourStreamKey docker-compose up -d
```

To stop and remove the containers:

```sh
docker-compose down
```

# Requirements

## Basic Requirements
- **Docker**: Ensure Docker is installed and running.
- **Kubernetes**: A Kubernetes cluster is required for deployment.
- **kubectl**: Command-line tool for interacting with Kubernetes clusters.
- **Docker Compose**: For running the project directly with Docker.

## KEDA Requirements (for auto-scaling)
- **KEDA Operator**: Must be installed in your Kubernetes cluster
- **Metrics Server**: For CPU/Memory-based scaling
- **Custom Metrics Adapter**: For RTMP stream-based scaling
- **RBAC Permissions**: Proper permissions for KEDA to manage scaling

# Environment Variables
- **RTMP_PUSH_URL**: The RTMP URL to which the stream will be pushed. It should be in the format `rtmp://upstream.example.com/live/yourStreamKey`.