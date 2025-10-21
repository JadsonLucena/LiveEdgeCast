# Network Capacity Planning - LiveEdgeCast

## 🌐 Node Network Specifications

### Per Node Capacity
- **Available Bandwidth**: 1 Gbps (1000 Mbps)
- **Scaling Threshold**: 800 Mbps (80% utilization)
- **Safety Margin**: 200 Mbps (20% buffer)

## 📊 RTMP Stream Estimations

### Typical RTMP Stream Bitrates
- **Low Quality**: 1-2 Mbps (720p @ 30fps)
- **Medium Quality**: 3-5 Mbps (1080p @ 30fps)  
- **High Quality**: 6-10 Mbps (1080p @ 60fps)
- **Ultra Quality**: 10-15 Mbps (4K scenarios)

### Streams per Proxy (800 Mbps available)
- **Low Quality**: ~400 streams (800 ÷ 2)
- **Medium Quality**: ~200 streams (800 ÷ 4)
- **High Quality**: ~100 streams (800 ÷ 8)
- **Mixed Load**: ~160 streams (average 5 Mbps)

## 🎯 Scaling Behavior

### Single Node (1 Proxy)
```
Inbound Traffic: 0-800 Mbps
Streams: 0-160 (mixed quality)
Status: Single proxy handles load
```

### Multi-Node (2-10 Proxies)
```
Inbound Traffic: 800+ Mbps  
Streams: 160+ concurrent
Action: KEDA scales new proxy on different node
Load Distribution: Kubernetes LoadBalancer
```

## 🔍 Monitoring Thresholds

### Primary Trigger: Network Inbound
```promql
# Current inbound traffic in Gbps
sum(rate(container_network_receive_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])) / (1000*1000*1000)

# Threshold: 0.8 Gbps (800 Mbps)
```

### Scaling Decision Matrix
| Inbound Traffic | Proxies | Action |
|----------------|---------|--------|
| 0-800 Mbps | 1 | Maintain |
| 800-1600 Mbps | 2 | Scale +1 |
| 1600-2400 Mbps | 3 | Scale +1 |
| ... | ... | ... |
| 7200-8000 Mbps | 10 | Maximum |

## 🛡️ Safety Mechanisms

### Secondary Triggers
1. **Connection Count**: >50 active connections
2. **CPU Usage**: >80% utilization  
3. **Memory Pressure**: Container limits

### Protection Features
- **Anti-flapping**: 30s stabilization on scale up
- **Graceful scale down**: 3min stabilization
- **Resource limits**: CPU/Memory constraints
- **Load balancing**: Traffic distribution across proxies

## 🎛️ Performance Tuning

### Network Optimizations
```yaml
# Container network settings
resources:
  requests:
    memory: "256Mi"
    cpu: "250m" 
  limits:
    memory: "1Gi"
    cpu: "1000m"  # 1 CPU core per proxy
```

### Nginx Tuning for High Throughput
```nginx
worker_processes auto;
worker_connections 2048;  # Higher for more concurrent connections
sendfile on;
tcp_nopush on;
tcp_nodelay on;
keepalive_timeout 65;
```

## 📈 Capacity Planning Examples

### Scenario 1: Small Event (50 streams @ 4 Mbps avg)
- **Total Traffic**: 200 Mbps
- **Proxies Needed**: 1
- **Node Utilization**: 20%

### Scenario 2: Medium Event (160 streams @ 5 Mbps avg)  
- **Total Traffic**: 800 Mbps
- **Proxies Needed**: 1
- **Node Utilization**: 80% (threshold)

### Scenario 3: Large Event (500 streams @ 4 Mbps avg)
- **Total Traffic**: 2000 Mbps (2 Gbps)
- **Proxies Needed**: 3
- **Node Utilization**: ~67% per node

### Scenario 4: Massive Event (1000 streams @ 6 Mbps avg)
- **Total Traffic**: 6000 Mbps (6 Gbps)  
- **Proxies Needed**: 8
- **Node Utilization**: 75% per node