# 🧪 Testing & Monitoring Tools

Scripts para testar e monitorar as métricas do LiveEdgeCast.

## 📋 Scripts Disponíveis

### 1. `test-metrics.sh` - Teste Completo de Métricas

Executa uma bateria de testes para verificar se o Prometheus está coletando métricas corretamente.

**Uso:**
```bash
./tools/test-metrics.sh
```

**O que testa:**
- ✅ Pods estão rodando (proxy e worker)
- ✅ Nginx `stub_status` endpoint (`/nginx_status`)
- ✅ Nginx-exporter está expondo métricas (porta 9113)
- ✅ Prometheus está saudável e respondendo
- ✅ Queries de métricas nginx funcionando
- ✅ Queries de tráfego de rede funcionando
- ✅ ServiceMonitor configurado (se existir)

**Saída exemplo:**
```
▶ Test 1: Checking if pods are running...
✓ Proxy pod found: rtmp-proxy-abc123
⚠ No worker pods running (this is expected for serverless)

▶ Test 2: Testing nginx stub_status endpoint...
Active connections: 2 
server accepts handled requests
 142 142 89 
Reading: 0 Writing: 1 Waiting: 1
✓ Nginx stub_status is working

▶ Test 3: Testing nginx-exporter metrics endpoint...
nginx_connections_active 2
nginx_connections_waiting 1
✓ Nginx-exporter is responding

▶ Test 5: Querying nginx_connections_active...
✓ Query successful
Response: {"status":"success","data":{"result":[...]}}
```

---

### 2. `monitor-metrics.sh` - Monitor em Tempo Real

Dashboard interativo em tempo real das métricas principais.

**Uso:**
```bash
./tools/monitor-metrics.sh
```

**Métricas exibidas:**
- 📦 Status dos pods (proxy e worker)
- 🔌 Conexões TCP (active, waiting, processing)
- 🌐 Tráfego de rede (inbound/outbound em Mbps)
- 💻 Uso de CPU (%)
- 💾 Uso de memória (MB)
- ⚖️ Status dos KEDA ScaledObjects
- 🎯 Métricas de scaling (thresholds)

**Screenshot (texto):**
```
╔════════════════════════════════════════════════════════════════╗
║       LiveEdgeCast - Real-time Metrics Dashboard              ║
╚════════════════════════════════════════════════════════════════╝

⏰ 2025-11-03 14:30:45

📦 Pod Status:
  Proxy pods:  1
  Worker pods: 0

🔌 TCP Connections (nginx metrics):
  Active connections:      3
  Waiting (keepalive):     2
  Processing (active):     1
  Total accepted:          145

🌐 Network Traffic:
  Inbound (receive):       12.50 Mbps
  Outbound (transmit):     8.75 Mbps

💻 CPU Usage:
  Proxy CPU usage:         5.23 %

💾 Memory Usage:
  Proxy memory usage:      128.45 MB

⚖️  KEDA Scaling Status:
  Proxy scaler:            True
  Worker scaler:           True

🎯 Current Scaling Metrics:
  Inbound traffic:         0.012 Gbps (threshold: 0.8 Gbps)
  ✓ Below threshold: 1.5%

  ✓ No active connections (workers at 0)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Refreshing in 5 seconds... (Ctrl+C to stop)
```

---

## 🎯 Fluxo de Teste Recomendado

### 1. Deploy inicial
```bash
./tools/up.sh
```

### 2. Verificar métricas
```bash
./tools/test-metrics.sh
```

### 3. Monitorar em tempo real
```bash
./tools/monitor-metrics.sh
```

### 4. Testar com stream RTMP
```bash
# Em outro terminal
ffmpeg -re -i video.mp4 -c copy -f flv rtmp://localhost:1935/live/test
```

### 5. Observar scaling
```bash
# Em outro terminal
watch -n 1 'kubectl get pods -n media'
```

---

## 🔍 Queries Prometheus Úteis

Acesse http://localhost:9090 após rodar `test-metrics.sh` ou `monitor-metrics.sh`.

### Conexões
```promql
# Total de conexões ativas
nginx_connections_active{namespace="media"}

# Conexões processando (sem keepalive)
nginx_connections_active{namespace="media"} - nginx_connections_waiting{namespace="media"}

# Total por pod
sum by (pod) (nginx_connections_active{namespace="media"})
```

### Tráfego de Rede
```promql
# Taxa de recebimento em bytes/s
rate(container_network_receive_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])

# Taxa em Gbps
sum(rate(container_network_receive_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])) / 1e9

# Taxa em Mbps
sum(rate(container_network_receive_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])) / 1e6 * 8
```

### CPU e Memória
```promql
# CPU usage
rate(container_cpu_usage_seconds_total{namespace="media",pod=~"rtmp-proxy-.*",container="nginx"}[1m])

# CPU em porcentagem
rate(container_cpu_usage_seconds_total{namespace="media",pod=~"rtmp-proxy-.*",container="nginx"}[1m]) * 100

# Memória em MB
container_memory_usage_bytes{namespace="media",pod=~"rtmp-proxy-.*",container="nginx"} / 1024 / 1024
```

### Requests HTTP
```promql
# Total de requests
nginx_http_requests_total{namespace="media"}

# Taxa de requests por segundo
rate(nginx_http_requests_total{namespace="media"}[1m])
```

---

## 🐛 Troubleshooting

### Problema: "No metrics found"
**Solução:**
1. Aguarde 30 segundos após o deploy (primeiro scrape)
2. Verifique se o nginx-exporter está rodando:
   ```bash
   kubectl get pods -n media -o wide
   kubectl logs -n media <pod-name> -c nginx-exporter
   ```

### Problema: "Prometheus not responding"
**Solução:**
1. Verifique se o Prometheus está rodando:
   ```bash
   kubectl get pods -n monitoring
   ```
2. Recrie o port-forward:
   ```bash
   pkill -f "kubectl.*port-forward.*9090"
   kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090
   ```

### Problema: "nginx_status returns 404"
**Solução:**
1. Verifique se o ConfigMap está correto:
   ```bash
   kubectl get configmap rtmp-proxy-nginx-conf -n media -o yaml | grep nginx_status
   ```
2. Deve conter:
   ```nginx
   location /nginx_status {
       stub_status on;
       access_log off;
       allow all;
   }
   ```

### Problema: "Worker metrics not available"
**Solução:**
Isso é esperado! Workers são serverless (0 replicas). Eles só aparecem quando há tráfego:
```bash
# Envie uma stream para criar workers
ffmpeg -re -i test.mp4 -c copy -f flv rtmp://localhost:1935/live/test

# Aguarde ~30s para workers escalarem
kubectl get pods -n media -w
```

---

## 📊 Visualizando no Grafana (Opcional)

Se você instalou o kube-prometheus-stack completo, pode acessar o Grafana:

```bash
# Port-forward do Grafana
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80

# Acesse: http://localhost:3000
# User: admin
# Password: (obter com comando abaixo)
kubectl get secret -n monitoring kube-prometheus-stack-grafana -o jsonpath="{.data.admin-password}" | base64 --decode ; echo
```

### Dashboards Úteis
- **Node Exporter**: Métricas dos nodes
- **Kubernetes / Compute Resources / Namespace (Pods)**: Métricas por namespace
- Criar dashboard customizado com as queries acima

---

## 📚 Referências

- [Prometheus Query Basics](https://prometheus.io/docs/prometheus/latest/querying/basics/)
- [Nginx Prometheus Exporter](https://github.com/nginxinc/nginx-prometheus-exporter)
- [KEDA Prometheus Scaler](https://keda.sh/docs/latest/scalers/prometheus/)
- [PromQL Examples](https://prometheus.io/docs/prometheus/latest/querying/examples/)
