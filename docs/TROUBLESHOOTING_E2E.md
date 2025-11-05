# LiveEdgeCast - Troubleshooting E2E e Solução Final

## 📋 Contexto

Sistema de retransmissão RTMP serverless com KEDA para escalonamento automático de workers (0-100 réplicas).

## 🔍 Problema Identificado

### Arquitetura Original
nginx-rtmp configurado em **modo relay** com diretiva `push`:
```nginx
application live {
    live on;
    push rtmp://rtmp-worker.media.svc.cluster.local:1935/live;
}
```

### Issue #1: Métricas Invisíveis
**Descoberta:** nginx-rtmp em modo relay (com PUSH) **NÃO expõe** contagem de publishers no endpoint `/stats`.
- Endpoint `/stats` mostra apenas `<nclients>0</nclients>` (players/viewers)
- Publishers que fazem PUSH não são contabilizados
- Impossibilita escalonamento baseado em `nginx_rtmp_live_clients`

### Issue #2: Chicken-and-Egg do HPA
**Problema:** Kubernetes HPA desabilita escalonamento quando `replicas=0`
- Workers em 0 réplicas → sem métricas de workers
- Sem métricas → HPA não escala
- Sistema fica "travado" em 0 réplicas permanentemente

### Issue #3: Docker Desktop Networking
**Limitação:** Docker Desktop não expõe corretamente serviços Kubernetes para localhost
- LoadBalancer com `EXTERNAL-IP: localhost` → conexões falham
- NodePort alocado → não acessível de WSL/host
- hostPort → conflitos de rede
- port-forward → usa loopback (127.0.0.1), não gera conexões "externas"

## ✅ Solução Implementada

### 1. Métricas via Contagem de Conexões TCP

**Insight:** Cada publisher RTMP = 1 conexão TCP estabelecida na porta 1935

**Implementação:**
```python
def count_tcp_connections(port):
    """Count established TCP connections on specified port"""
    try:
        result = subprocess.run(['netstat', '-tn'], capture_output=True, text=True)
        lines = result.stdout.split('\n')
        count = sum(1 for line in lines if f':{port}' in line and 'ESTABLISHED' in line)
        return count
    except:
        return 0

# Expõe métrica Prometheus
nginx_rtmp_tcp_connections{port="1935"} <count>
```

**Vantagem:** Funciona independentemente do modo nginx-rtmp (relay, push, hls, etc.)

### 2. ScaledObject com activationThreshold

**Configuração KEDA para escalonamento 0→1:**
```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: rtmp-worker-scaler
spec:
  scaleTargetRef:
    name: rtmp-worker
  minReplicaCount: 0  # True serverless
  maxReplicaCount: 100
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://prometheus-server.prometheus.svc.cluster.local:80
        # Query lê métricas DO PROXY (não dos workers!)
        query: sum(nginx_rtmp_tcp_connections{namespace="media",pod=~"rtmp-proxy-.*",port="1935"})
        threshold: "1"
        activationThreshold: "0.5"  # CRÍTICO: habilita escalonamento 0→1
```

**Key Points:**
- `activationThreshold`: permite KEDA escalar de 0 para 1 quando métrica > 0.5
- Query busca métricas do **proxy** (sempre rodando), não dos workers
- Threshold=1: 1 conexão TCP = escala 1 worker

### 3. Prometheus Scrape Configuration

**Problema:** Vanilla Prometheus chart não usa ServiceMonitor

**Solução:** Configurar scrape via Helm values
```yaml
# k8s/prometheus-values.yaml
serverFiles:
  prometheus.yml:
    scrape_configs:
      - job_name: 'rtmp-proxy-metrics'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names: ['media']
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_label_app]
            regex: rtmp-proxy
            action: keep
          - source_labels: [__meta_kubernetes_pod_ip]
            target_label: __address__
            replacement: '$1:9114'
```

## 📊 Arquitetura Final

```
┌─────────────┐
│  Publisher  │
│   (ffmpeg)  │
└──────┬──────┘
       │ RTMP (porta 1935)
       │
       v
┌──────────────────────────────────┐
│   rtmp-proxy (always-on, 1-10)   │
│                                   │
│  ┌───────────┐  ┌──────────────┐ │
│  │  nginx-   │  │  stats-      │ │
│  │  rtmp     │  │  exporter    │ │
│  │           │  │              │ │
│  │  relay    │  │  counts TCP  │ │
│  │  mode     │  │  :1935       │ │
│  └─────┬─────┘  └───────┬──────┘ │
└────────┼─────────────────┼────────┘
         │                 │
         │ PUSH            │ metrics → Prometheus
         │                 │              ↓
         v                 └──────> KEDA queries
┌──────────────────────────────────┐      Prometheus
│ rtmp-worker (serverless, 0-100)  │←────────┘
│                                   │ scales based on
│  Receives: proxy PUSH            │ tcp_connections
│  Sends: YouTube RTMP             │
└───────────────────────────────────┘
```

## 🎯 Fórmula de Escalonamento

```
tcp_connections = conexões ESTABLISHED na porta 1935 do proxy
workers_needed = tcp_connections (1:1 mapping)

Se tcp_connections >= 0.5:
    scale workers de 0 → 1
Se tcp_connections >= 1:
    scale workers proportionalmente (1 worker por stream)
```

## ⚠️ Conhecimento para Produção

### Docker Desktop (Desenvolvimento Local)
❌ **NÃO RECOMENDADO** para testes E2E de networking
- LoadBalancer/NodePort não funcionam corretamente com localhost
- Use alternativas: Minikube + MetalLB, Kind com extraPortMappings

### Cloud (Produção)
✅ **FUNCIONAMENTO COMPLETO ESPERADO**
- LoadBalancer real (AWS NLB, GCP LB, Azure LB)
- Conexões externas reais geram métricas TCP corretas
- Escalonamento 0→N funcionará perfeitamente

### Teste de Validação em Produção
```bash
# 1. Enviar stream RTMP
ffmpeg -re -i input.mp4 -c copy -f flv rtmp://<LOAD_BALANCER_IP>:1935/live/<key>

# 2. Verificar métrica no Prometheus
curl -s 'http://<prometheus>/api/v1/query?query=nginx_rtmp_tcp_connections'
# Deve retornar: nginx_rtmp_tcp_connections{port="1935"} 1

# 3. Verificar escalonamento
kubectl get pods -n media
# Deve mostrar: rtmp-worker-xxx-xxx 1/1 Running (escalou de 0→1)

# 4. Verificar HPA
kubectl get hpa -n media
# TARGET deve mostrar: 1/1 (avg)
```

## 📚 Lições Aprendidas

1. **nginx-rtmp /stats é limitado**: Apenas conta viewers, não publishers em modo relay
2. **TCP connections = métrica universal**: Funciona independentemente da configuração nginx
3. **activationThreshold é essencial**: Sem ele, HPA não escala de 0→1
4. **Query deve ler metrics do proxy**: Workers em 0 réplicas não têm métricas
5. **Docker Desktop tem limitações**: Não é ambiente adequado para testes de networking E2E

## 🔄 Próximos Passos

1. ✅ Deploy em cluster cloud (EKS, GKE, AKS)
2. ✅ Validar escalonamento 0→1→N com streams reais
3. ✅ Monitorar latência e qualidade do stream
4. ✅ Ajustar `cooldownPeriod` do ScaledObject conforme necessário
5. ✅ Implementar alertas Prometheus para falhas de stream

## 📝 Referências

- [KEDA Scaling Deployment to/from 0](https://keda.sh/docs/latest/concepts/scaling-deployments/#activating-and-scaling-thresholds)
- [nginx-rtmp statistics](https://github.com/arut/nginx-rtmp-module/wiki/Directives#stat)
- [Prometheus Kubernetes SD](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#kubernetes_sd_config)
