# LiveEdgeCast Architecture

## Overview

LiveEdgeCast é um sistema de streaming RTMP serverless que utiliza Kubernetes e KEDA para auto-scaling baseado em demanda. O sistema consiste em dois componentes principais:

1. **Edge Proxy** - Recebe streams RTMP e distribui para workers
2. **Worker Nodes** - Processam streams e enviam para upstream

## Architecture Components

### 1. Edge Proxy (rtmp-edge)

**Função**: Gateway principal que recebe streams RTMP e roteia para workers

**Configuração**:
- Recebe streams na porta 1935
- Aplicações RTMP: `live` (para streams) e `worker` (para comunicação entre componentes)
- Redireciona streams automaticamente para workers usando `push rtmp://rtmp-worker:1935/live/stream`
- Exporta métricas RTMP para KEDA via `/rtmp_stat`

**Localização**: `k8s/edge/`

### 2. Worker Nodes (rtmp-worker)

**Função**: Processa streams RTMP e envia para upstream configurado

**Configuração**:
- Recebe streams do edge
- Processa e envia para `RTMP_PUSH_URL` configurado
- Escala de 0 a 10 réplicas baseado em demanda
- Exporta métricas para monitoramento

**Localização**: `k8s/worker/`

### 3. KEDA Scaler

**Função**: Auto-scaling baseado em métricas RTMP

**Triggers**:
- `nginx_rtmp_nclients` - Número de clientes RTMP conectados
- `rtmp_streams_active` - Streams RTMP ativos

**Configuração**:
- Polling a cada 10 segundos
- Cooldown de 30 segundos
- Escala de 0 a 10 workers

**Localização**: `k8s/scaler/`

### 4. Metrics Pipeline

**Prometheus**: Coleta métricas do edge e workers
**Exporter**: Converte métricas nginx para formato Prometheus
**KEDA**: Usa métricas para decidir scaling

## Data Flow

```
RTMP Client → Edge Proxy → Worker Node → Upstream RTMP Server
     ↓              ↓           ↓              ↓
   Stream      Distribution  Processing    Final Output
     ↓              ↓           ↓              ↓
   Input       Load Balance   Scaling      Upstream
```

## Scaling Logic

1. **Stream Detection**: Edge detecta novo stream RTMP
2. **Metrics Export**: Métricas são enviadas para Prometheus
3. **KEDA Trigger**: KEDA detecta métricas acima do threshold
4. **Worker Creation**: Pod do worker é criado
5. **Stream Routing**: Edge redireciona stream para worker
6. **Processing**: Worker processa e envia para upstream

## Configuration Files

### Edge Configuration
- **Docker**: `docker/edge/Dockerfile` e `docker/edge/nginx.conf`
- **K8s**: `k8s/edge/deployment.yaml`, `k8s/edge/service.yaml`, `k8s/edge/configmap.yaml`

### Worker Configuration
- **Docker**: `docker/worker/Dockerfile` e `docker/worker/nginx.conf`
- **K8s**: `k8s/worker/deployment.yaml` e `k8s/worker/service.yaml`

### Scaling Configuration
- **KEDA**: `k8s/scaler/scaledobject.yaml`
- **Metrics**: `k8s/prometheus/` e `k8s/exporter/`

## Troubleshooting

### Common Issues

1. **Workers não são criados**:
   - Verificar se KEDA está funcionando: `kubectl get scaledobject`
   - Verificar métricas: `kubectl port-forward svc/prometheus 9090:9090`
   - Verificar logs do edge: `kubectl logs -l app=rtmp-edge`

2. **Streams não são redirecionados**:
   - Verificar configuração nginx do edge
   - Verificar conectividade entre edge e worker
   - Verificar logs de ambos os componentes

3. **Métricas não aparecem**:
   - Verificar se exporter está rodando
   - Verificar configuração do Prometheus
   - Verificar endpoints `/rtmp_stat`

### Debug Commands

```bash
# Verificar status geral
./tools/test-stream.sh

# Monitorar scaling
kubectl get pods -l app=liveedgecast -w

# Verificar logs
kubectl logs -l app=rtmp-edge --tail=50
kubectl logs -l app=rtmp-worker --tail=50

# Verificar métricas
kubectl port-forward svc/prometheus 9090:9090
# Abrir http://localhost:9090 no browser
```

## Performance Considerations

- **Edge**: 1 réplica fixa (stateless)
- **Workers**: 0-10 réplicas (auto-scaling)
- **Polling**: 10 segundos para KEDA
- **Cooldown**: 30 segundos para evitar flapping
- **Resources**: 128Mi-256Mi RAM, 100m-200m CPU por pod

## Security

- **RTMP**: Sem autenticação (desenvolvimento)
- **HTTP**: Endpoints de health e métricas
- **Network**: Comunicação interna via ClusterIP
- **Secrets**: RTMP_PUSH_URL armazenado em Kubernetes Secret
