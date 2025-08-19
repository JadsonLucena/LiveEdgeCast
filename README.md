# LiveEdgeCast

🚀 **Serverless RTMP Edge Proxy for Low-Latency Live Stream Retransmission**

LiveEdgeCast implements a serverless architecture on edge computing environments, focusing on the retransmission of live streams with minimal latency. The solution ensures high availability, responsiveness, and efficient resource utilization by dynamically provisioning compute resources only when needed.

## 🎯 Project Goals

- **Low-Latency Retransmission**: Minimize delay in live stream forwarding
- **Edge Computing**: Deploy close to users for optimal performance  
- **Serverless Execution**: Dynamic resource provisioning and cost optimization
- **High Availability**: Fault-tolerant stream proxy architecture
- **Efficient Resource Utilization**: Scale up/down based on demand


# How to Start and Stop the Project

To start the project, use the provided script:

```sh
./tools/up.sh
```

To stop the project, use:

```sh
./tools/down.sh
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
- **Docker**: Ensure Docker is installed and running.
- **Kubernetes**: A Kubernetes cluster is required for deployment.
- **kubectl**: Command-line tool for interacting with Kubernetes clusters.
- **Docker Compose**: For running the project directly with Docker.
# Environment Variables
- **RTMP_PUSH_URL**: The RTMP URL to which the stream will be pushed. It should be in the format `rtmp://upstream.example.com/live/yourStreamKey`.