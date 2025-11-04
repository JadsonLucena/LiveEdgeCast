# Nginx Prometheus Exporter - Métricas Disponíveis

## 📊 Visão Geral

O projeto LiveEdgeCast usa o **nginx-prometheus-exporter oficial** (mantido pela NGINX Inc.) para exportar métricas via protocolo HTTP/TCP padrão do Prometheus.

**Imagem Docker**: `nginx/nginx-prometheus-exporter:latest`  
**Porta**: `9113` (metrics endpoint)  
**Endpoint**: `http://pod-ip:9113/metrics`

---

## 🔌 Métricas Disponíveis

### **1. Conexões TCP (via stub_status)**

Métricas extraídas do endpoint `/nginx_status`:

```promql
# Total de conexões ativas (incluindo waiting/keepalive)
nginx_connections_active

# Conexões em estado waiting (keepalive idle)
nginx_connections_waiting

# Conexões realmente processando dados (active - waiting)
nginx_connections_active - nginx_connections_waiting

# Total de conexões aceitas
nginx_connections_accepted

# Total de conexões tratadas
nginx_connections_handled

# Conexões lendo request
nginx_connections_reading

# Conexões escrevendo response
nginx_connections_writing
```

### **2. Requests HTTP**

```promql
# Total de requests HTTP processados
nginx_http_requests_total
```

### **3. Tráfego de Rede (via cAdvisor/Kubelet)**

```promql
# Bytes recebidos (inbound traffic)
container_network_receive_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}

# Rate de bytes recebidos (últimos 1min)
rate(container_network_receive_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])

# Bytes enviados (outbound traffic)
container_network_transmit_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}

# Rate de bytes enviados (últimos 1min)
rate(container_network_transmit_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])
```

### **4. CPU e Memória (via cAdvisor/Kubelet)**

```promql
# CPU usage
rate(container_cpu_usage_seconds_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])

# Memória em uso
container_memory_usage_bytes{namespace="media",pod=~"rtmp-proxy-.*"}
```

---

## 🎯 Queries Úteis para KEDA Scaling

### **Worker Scaling (baseado em conexões ativas)**

```promql
# Conexões ativas no proxy (excluindo keepalive)
sum(nginx_connections_active{namespace="media",pod=~"rtmp-proxy-.*"} - nginx_connections_waiting{namespace="media",pod=~"rtmp-proxy-.*"}) or vector(0)
```

**Uso no ScaledObject**:
```yaml
triggers:
  - type: prometheus
    metadata:
      serverAddress: http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090
      metricName: rtmp_active_connections
      threshold: "1"
      query: |
        sum(nginx_connections_active{namespace="media",pod=~"rtmp-proxy-.*"} - nginx_connections_waiting{namespace="media",pod=~"rtmp-proxy-.*"}) or vector(0)
```

---

### **Proxy Scaling (baseado em tráfego inbound)**

```promql
# Tráfego inbound em Gbps
sum(rate(container_network_receive_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])) / (1000*1000*1000)
```

**Uso no ScaledObject**:
```yaml
triggers:
  - type: prometheus
    metadata:
      serverAddress: http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090
      metricName: rtmp_proxy_inbound_gbps
      threshold: "0.8"  # 800 Mbps
      query: |
        sum(rate(container_network_receive_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])) / (1000*1000*1000) or vector(0)
```

---

### **Proxy Scaling (baseado em conexões TCP)**

```promql
# Total de conexões ativas (fallback metric)
sum(nginx_connections_active{namespace="media",pod=~"rtmp-proxy-.*"}) - sum(nginx_connections_waiting{namespace="media",pod=~"rtmp-proxy-.*"}) or vector(0)
```

**Uso no ScaledObject**:
```yaml
triggers:
  - type: prometheus
    metadata:
      serverAddress: http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090
      metricName: rtmp_proxy_active_connections
      threshold: "50"  # 50 conexões por proxy
      query: |
        sum(nginx_connections_active{namespace="media",pod=~"rtmp-proxy-.*"}) - sum(nginx_connections_waiting{namespace="media",pod=~"rtmp-proxy-.*"}) or vector(0)
```

---

### **CPU Usage (safety net)**

```promql
# CPU usage em percentual
sum(rate(container_cpu_usage_seconds_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])) / sum(container_spec_cpu_quota{namespace="media",pod=~"rtmp-proxy-.*"}/container_spec_cpu_period{namespace="media",pod=~"rtmp-proxy-.*"}) * 100 or vector(0)
```

**Uso no ScaledObject**:
```yaml
triggers:
  - type: prometheus
    metadata:
      serverAddress: http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090
      metricName: rtmp_proxy_cpu_usage
      threshold: "80"  # 80% CPU
      query: |
        sum(rate(container_cpu_usage_seconds_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])) / sum(container_spec_cpu_quota{namespace="media",pod=~"rtmp-proxy-.*"}/container_spec_cpu_period{namespace="media",pod=~"rtmp-proxy-.*"}) * 100 or vector(0)
```

---

## 🔍 Verificando Métricas

### **Via kubectl port-forward**

```bash
# Port-forward do Prometheus
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090

# Acesse: http://localhost:9090
# Execute queries no Prometheus UI
```

### **Via kubectl port-forward (nginx-exporter direto)**

```bash
# Port-forward de um pod específico
kubectl port-forward -n media <rtmp-proxy-pod-name> 9113:9113

# Acesse: http://localhost:9113/metrics
# Veja as métricas em formato Prometheus
```

### **Exemplo de saída do nginx-exporter**

```
# HELP nginx_connections_active Active connections
# TYPE nginx_connections_active gauge
nginx_connections_active 8

# HELP nginx_connections_waiting Waiting connections
# TYPE nginx_connections_waiting gauge
nginx_connections_waiting 5

# HELP nginx_connections_accepted Accepted connections
# TYPE nginx_connections_accepted counter
nginx_connections_accepted 12345

# HELP nginx_http_requests_total Total HTTP requests
# TYPE nginx_http_requests_total counter
nginx_http_requests_total 98765
```

---

## 🎯 Vantagens do nginx-prometheus-exporter

1. ✅ **Oficial** - Mantido pela NGINX Inc.
2. ✅ **Protocolo TCP/HTTP** - Padrão Prometheus (não RTMP)
3. ✅ **Leve** - Container ~10MB
4. ✅ **Confiável** - Usado em produção globalmente
5. ✅ **Métricas ricas** - Conexões, requests, upstream
6. ✅ **KEDA nativo** - Integração direta via Prometheus scaler
7. ✅ **Sem dependências** - Não precisa de scripts Python customizados
8. ✅ **Real-time** - Scrape a cada poucos segundos

---

## 📚 Referências

- [nginx-prometheus-exporter GitHub](https://github.com/nginxinc/nginx-prometheus-exporter)
- [KEDA Prometheus Scaler](https://keda.sh/docs/latest/scalers/prometheus/)
- [Nginx stub_status module](http://nginx.org/en/docs/http/ngx_http_stub_status_module.html)
