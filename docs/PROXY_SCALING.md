# Proxy Auto-Scaling Configuration

## Overview
O sistema LiveEdgeCast implementa uma arquitetura híbrida de scaling:

### 🔄 **Proxy: Always-On com Auto-Scaling**
- **Mínimo**: 1 réplica sempre ativa (não é serverless)
- **Máximo**: 10 réplicas para alta carga
- **Função**: Recebe conexões e faz load balancing

### ⚡ **Workers: True Serverless**
- **Mínimo**: 0 réplicas (verdadeiro serverless)
- **Máximo**: 100+ réplicas conforme demanda
- **Função**: Processa streams individuais (1:1 mapping)

## Proxy Scaling Triggers

O proxy escala automaticamente baseado principalmente no tráfego de entrada, considerando a capacidade de rede de cada nó:

### 1. 🌐 Tráfego Inbound (Primary Trigger)
- **Threshold**: 0.8 Gbps por proxy (80% de 1Gbps disponível por nó)
- **Métrica**: `container_network_receive_bytes_total` (rate 1m) em Gbps
- **Comportamento**: Escala quando > 800Mbps de tráfego de entrada
- **Prioridade**: ⭐ Principal trigger de escalação

### 2. � Conexões Ativas (Secondary Trigger)
- **Threshold**: 50 conexões ativas por proxy  
- **Métrica**: `nginx_connections_active - nginx_connections_waiting`
- **Comportamento**: Backup trigger quando muitas conexões simultâneas
- **Prioridade**: 🔄 Trigger secundário

### 3. 💻 CPU Usage (Safety Net)
- **Threshold**: 80% de utilização de CPU
- **Métrica**: CPU usage percentage
- **Comportamento**: Safety net para situações extremas
- **Prioridade**: 🛡️ Proteção contra sobrecarga

## Scaling Behavior

### Scale Up (Adicionar Proxies)
- **Estabilização**: 30 segundos
- **Velocidade**: 100% em 15 segundos
- **Trigger**: Qualquer métrica acima do threshold

### Scale Down (Remover Proxies)  
- **Estabilização**: 3 minutos
- **Velocidade**: 25% em 60 segundos
- **Cooldown**: 2 minutos

## Capacidades

### Proxy Configuration (Always-On)
- **Min Replicas**: 1 (sempre pelo menos 1 ativo)
- **Max Replicas**: 10 
- **Network Capacity per Node**: 1 Gbps
- **Scaling Threshold**: 800 Mbps (80% utilização)
- **Total Network Capacity**: 
  - 1 proxy: 800 Mbps máximo
  - 10 proxies: 8 Gbps máximo (10 nós × 800 Mbps)
- **Responsabilidade**: Load balancing e entrada de tráfego

### Worker Configuration (Serverless)
- **Min Replicas**: 0 (verdadeiro serverless)
- **Max Replicas**: 100+
- **Scaling**: Baseado em demanda de streams (1:1 mapping)
- **Responsabilidade**: Processamento individual de streams

### Performance Estimation por Nó (1Gbps)
- **RTMP Stream típico**: ~2-5 Mbps
- **Streams por proxy**: ~160-400 streams simultâneas
- **Throughput por proxy**: 800 Mbps (80% de 1Gbps)
- **Latência de Proxy**: < 200ms de scaling
- **Latência de Worker**: < 30s de cold start
- **Throughput Total (10 nós)**: ~8 Gbps

## Arquitetura de Scaling

### Por que Proxy não é Serverless?
1. **Disponibilidade**: Deve estar sempre pronto para receber conexões
2. **Load Balancing**: Distribui tráfego entre workers
3. **Buffering**: Mantém streams durante worker cold start
4. **Service Discovery**: Gerencia roteamento dinâmico

### Por que Workers são Serverless?
1. **Eficiência de Recursos**: Só consome recursos quando necessário  
2. **Isolamento**: 1 worker = 1 stream (sem interferência)
3. **Escalabilidade**: Pode escalar para centenas de streams
4. **Cost Optimization**: Zero custo quando sem streams ativas

## Monitoring Queries

### Verificar Tráfego Inbound (Primary Metric)
```promql
# Tráfego em Gbps por proxy
sum(rate(container_network_receive_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])) by (pod) / (1000*1000*1000)

# Tráfego total em Gbps
sum(rate(container_network_receive_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])) / (1000*1000*1000)
```

### Verificar Conexões Ativas (Secondary Metric)
```promql
sum(nginx_connections_active{namespace="media",pod=~"rtmp-proxy-.*"}) - 
sum(nginx_connections_waiting{namespace="media",pod=~"rtmp-proxy-.*"})
```

### Verificar CPU Usage (Safety Net)
```promql
sum(rate(container_cpu_usage_seconds_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])) / 
sum(container_spec_cpu_quota{namespace="media",pod=~"rtmp-proxy-.*"}/container_spec_cpu_period{namespace="media",pod=~"rtmp-proxy-.*"}) * 100
```

## Troubleshooting

### Proxy não está escalando
1. Verificar se KEDA está instalado
2. Verificar se Prometheus está coletando métricas
3. Verificar logs do ScaledObject: `kubectl logs -n keda-system deployment/keda-operator`

### Métricas não aparecem
1. Verificar se nginx-exporter está rodando
2. Verificar endpoint `/nginx_status` no proxy
3. Verificar ServiceMonitor configuration

### Load Balancer não distribui tráfego
1. Verificar se Service tem múltiplos endpoints
2. Verificar se todos os proxies estão Ready
3. Verificar configuração de sessionAffinity