# LiveEdgeCast - Teste Local E2E

## ✅ Pré-requisitos

- Kubernetes cluster (Docker Desktop, Minikube, Kind, etc.)
- KEDA instalado
- Prometheus instalado
- ffmpeg instalado localmente

## 🚀 Como Testar Localmente

### 1. Deploy do Sistema

```bash
cd LiveEdgeCast
./tools/up.sh
```

### 2. Configurar Port-Forward

```bash
# Port-forward para RTMP (porta 1935)
kubectl port-forward -n media svc/rtmp-proxy 1935:1935 &

# Port-forward para Prometheus (opcional, para visualização)
kubectl port-forward -n prometheus svc/prometheus-server 9090:80 &
```

### 3. Verificar Estado Inicial

```bash
# Verificar pods (deve ter apenas rtmp-proxy rodando)
kubectl get pods -n media

# Verificar HPA (workers devem estar em 0 réplicas)
kubectl get hpa -n media
```

**Saída esperada:**
```
NAME                         READY   STATUS    RESTARTS   AGE
rtmp-proxy-xxxxxxxxxx-xxxxx  2/2     Running   0          1m

keda-hpa-rtmp-worker-scaler  Deployment/rtmp-worker  <unknown>/1  1  100  0
```

### 4. Iniciar Stream RTMP

```bash
# Stream de teste com ffmpeg (60 segundos)
ffmpeg -re -t 60 \
  -f lavfi -i testsrc=size=640x480:rate=15 \
  -f lavfi -i sine=frequency=1000 \
  -vcodec libx264 -preset ultrafast \
  -tune zerolatency -pix_fmt yuv420p \
  -b:v 500k -acodec aac -b:a 64k \
  -f flv rtmp://localhost:1935/live/meustream
```

### 5. Monitorar Escalonamento

```bash
# Assistir pods sendo criados
watch kubectl get pods -n media

# Monitorar métricas
kubectl exec -n media deployment/rtmp-proxy -c rtmp-stats-exporter -- \
  python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:9114/metrics').read().decode())" | \
  grep nginx_rtmp_tcp_connections

# Verificar HPA
kubectl get hpa -n media
```

**Comportamento esperado:**
- ⏱️  **T+0s**: Workers = 0, TCP connections = 0
- ⏱️  **T+5s**: Workers = 1 (Pending/ContainerCreating), TCP connections = 1
- ⏱️  **T+10s**: Workers = 1 (Running), TCP connections = 1
- ⏱️  **T+60s**: Stream termina, TCP connections = 0
- ⏱️  **T+90s**: Workers escalam para 0 (cooldown period)

## 📊 Verificar Métricas no Prometheus

Acesse: http://localhost:9090

Execute queries:
```promql
# Conexões TCP ativas
nginx_rtmp_tcp_connections{namespace="media",port="1935"}

# Bandwidth inbound
nginx_rtmp_bandwidth_in_bytes_per_second

# Número de replicas workers
kube_deployment_status_replicas{namespace="media",deployment="rtmp-worker"}
```

## 🧪 Teste com Múltiplos Streams

Para testar escalonamento com múltiplas conexões:

```bash
# Terminal 1
ffmpeg -re -t 120 -f lavfi -i testsrc -vcodec libx264 -preset ultrafast \
  -f flv rtmp://localhost:1935/live/stream1 &

# Terminal 2  
ffmpeg -re -t 120 -f lavfi -i testsrc -vcodec libx264 -preset ultrafast \
  -f flv rtmp://localhost:1935/live/stream2 &

# Terminal 3
ffmpeg -re -t 120 -f lavfi -i testsrc -vcodec libx264 -preset ultrafast \
  -f flv rtmp://localhost:1935/live/stream3 &
```

**Resultado esperado:**
- 3 streams → `nginx_rtmp_tcp_connections = 3`
- KEDA escala para 3 workers (1 worker por stream)

## 🔧 Troubleshooting

### Port-forward não funciona

```bash
# Matar processos port-forward antigos
pkill -f "port-forward.*1935"

# Reiniciar
kubectl port-forward -n media svc/rtmp-proxy 1935:1935
```

### Métrica mostra 0 conexões durante stream ativo

```bash
# Verificar se ffmpeg está realmente conectado
ps aux | grep ffmpeg

# Verificar logs do ffmpeg
ffmpeg ... -loglevel info

# Verificar conexões TCP no proxy
kubectl exec -n media deployment/rtmp-proxy -c nginx -- \
  netstat -tn | grep ":1935.*ESTABLISHED"
```

### Workers não escalam

```bash
# Verificar ScaledObject
kubectl describe scaledobject rtmp-worker-scaler -n media

# Verificar logs do KEDA operator
kubectl logs -n keda deployment/keda-operator --tail=50

# Verificar se Prometheus está acessível
kubectl exec -n media deployment/rtmp-proxy -c rtmp-stats-exporter -- \
  wget -qO- http://prometheus-server.prometheus.svc.cluster.local:80/api/v1/query?query=up
```

## 🎯 Fórmula de Escalonamento

```
tcp_connections = Número de conexões ESTABLISHED na porta 1935
workers_needed = tcp_connections (1:1 mapping)

Regras:
- tcp_connections >= 0.5  →  Escala de 0 para 1
- tcp_connections >= N    →  Escala para N workers
- tcp_connections = 0     →  Após cooldown (30s), escala para 0
```

## 🎊 Teste Bem-Sucedido!

Se você ver:
- ✅ Métrica `nginx_rtmp_tcp_connections` aumentando durante stream
- ✅ Pods `rtmp-worker-xxx` sendo criados
- ✅ HPA mostrando `REPLICAS > 0`
- ✅ Após stream terminar, workers escalando para 0

**Seu sistema está funcionando perfeitamente!** 🚀

## 📚 Referências

- [KEDA Scaling Concepts](https://keda.sh/docs/latest/concepts/scaling-deployments/)
- [Prometheus Queries](https://prometheus.io/docs/prometheus/latest/querying/basics/)
- [nginx-rtmp Module](https://github.com/arut/nginx-rtmp-module)
- [FFmpeg Streaming Guide](https://trac.ffmpeg.org/wiki/StreamingGuide)
