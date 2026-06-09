# Consultas PromQL para observabilidade e artigo

Este documento reúne consultas canônicas para dashboards, alertas e coleta de
resultados experimentais do LiveEdgeCast. As consultas assumem Prometheus com as
métricas nativas do controller e do worker, além das métricas usuais de
Kubernetes/cAdvisor/kube-state-metrics instaladas pelo stack de monitoramento.

## Convenções

- Janela curta operacional: `5m`, usada para painéis quase em tempo real.
- Janela de experimento: substitua `$__range` ou `$window` pela duração exata da
  repetição, por exemplo `15m`, `30m` ou o intervalo delimitado por Grafana.
- Agrupamentos de metadados controlados: `tenant`, `environment` e `region` são
  seguros para `sum by (...)` quando preenchidos por ambiente controlado.
- Evite agrupar por `pod` em gráficos agregados do artigo; use `component` ou
  `namespace` para reduzir cardinalidade e tornar as séries comparáveis entre
  repetições.

## Cold start P50/P95/P99

A métrica mais completa para cold start fim-a-fim é o histograma
`stream_lifecycle_phase_seconds` com `phase="proxy_to_first_progress"`, pois mede
do hook de publicação no proxy até o primeiro progresso do FFmpeg. Para separar
atrasos do controller, use `phase="controller_to_first_progress"`. Para detalhar
componentes internos, use as demais fases descritas em `metrics.md`.

### Percentis fim-a-fim

```promql
histogram_quantile(
  0.50,
  sum by (le) (
    rate(stream_lifecycle_phase_seconds_bucket{phase="proxy_to_first_progress"}[5m])
  )
)
```

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(stream_lifecycle_phase_seconds_bucket{phase="proxy_to_first_progress"}[5m])
  )
)
```

```promql
histogram_quantile(
  0.99,
  sum by (le) (
    rate(stream_lifecycle_phase_seconds_bucket{phase="proxy_to_first_progress"}[5m])
  )
)
```

### Percentis por ambiente ou região

```promql
histogram_quantile(
  0.95,
  sum by (le, environment, region) (
    rate(stream_lifecycle_phase_seconds_bucket{phase="proxy_to_first_progress"}[5m])
  )
)
```

### Decomposição do cold start por fase

```promql
histogram_quantile(
  0.95,
  sum by (le, phase) (
    rate(stream_lifecycle_phase_seconds_bucket{
      phase=~"proxy_to_controller|controller_to_worker_create_request|worker_create_request_to_pod_created|pod_created_to_scheduled|scheduled_to_container_started|container_started_to_worker_ready|worker_ready_to_ffmpeg_started|ffmpeg_started_to_first_progress"
    }[5m])
  )
)
```

### Contagem de amostras válidas e fases descartadas

```promql
sum by (phase) (
  increase(stream_lifecycle_phase_seconds_count{phase="proxy_to_first_progress"}[$window])
)
```

```promql
sum by (phase, reason) (
  increase(stream_lifecycle_phase_observations_total{status!="observed"}[$window])
)
```

Use a segunda consulta para reportar quantas medições não entraram no histograma
por endpoint aproximado, timestamp ausente ou duração negativa por skew de relógio.

## MTTR de worker

No LiveEdgeCast, MTTR operacional de worker é aproximado pela duração das
tentativas de recuperação de worker não saudável registradas em
`worker_recovery_duration_seconds`. A consulta abaixo mede tempo até substituição
ou descarte da tentativa de recuperação, não tempo percebido pelo usuário final.

### MTTR médio

```promql
sum(rate(worker_recovery_duration_seconds_sum{status="success",reason="replaced"}[5m]))
/
sum(rate(worker_recovery_duration_seconds_count{status="success",reason="replaced"}[5m]))
```

### MTTR P95

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(worker_recovery_duration_seconds_bucket{status="success",reason="replaced"}[5m])
  )
)
```

### Taxa de recuperação bem-sucedida

```promql
sum(rate(worker_recovery_total{status="success",reason="replaced"}[5m]))
/
sum(rate(worker_recovery_total[5m]))
```

### Recuperações com erro por causa

```promql
sum by (reason) (
  increase(worker_recovery_total{status!="success"}[$window])
)
```

## Handover rate

A taxa de handover deve distinguir tentativas, aceitações e conflitos. O contador
`handover_attempts_total` inclui avaliações de ownership; `stream_proxy_handover_total`
representa handovers efetivos entre proxies; `handover_conflict_total` representa
negações por conflito de owner saudável.

### Handovers efetivos por segundo

```promql
sum(rate(stream_proxy_handover_total[5m]))
```

### Taxa de sucesso de handover

```promql
sum(rate(handover_success_total[5m]))
/
sum(rate(handover_attempts_total[5m]))
```

### Taxa de conflito de handover

```promql
sum(rate(handover_conflict_total[5m]))
/
sum(rate(handover_attempts_total[5m]))
```

### Handovers efetivos por 100 streams ativos

```promql
100 * sum(rate(stream_proxy_handover_total[5m]))
/
clamp_min(sum(proxy_rtmp_active_streams), 1)
```

## Allocation success

A alocação bem-sucedida é medida por `stream_allocation_total` com
`status="success"`. Replays idempotentes devem ser separados da criação real de
worker para não inflar a taxa de sucesso primária.

### Taxa de sucesso de alocação

```promql
sum(rate(stream_allocation_total{status="success"}[5m]))
/
sum(rate(stream_allocation_total[5m]))
```

### Taxa de sucesso excluindo replay idempotente

```promql
sum(rate(stream_allocation_total{status="success",reason!="idempotent_replay"}[5m]))
/
sum(rate(stream_allocation_total{reason!="idempotent_replay"}[5m]))
```

### Latência P95 de alocação

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(stream_allocation_duration_seconds_bucket[5m])
  )
)
```

### Falhas de criação de worker por causa

```promql
sum by (reason) (
  increase(worker_create_total{status!="success"}[$window])
)
```

## Órfãos

O sweeper de órfãos apaga Pods `app=worker` sem mapeamento ativo no estado do
controller, mas a métrica atual não exporta diretamente a cardinalidade de
órfãos. Para dashboards, use métricas de Kubernetes para detectar workers prontos
sem atividade de FFmpeg ou compare workers vivos com streams ativos. Para análise
forense, correlacione com logs estruturados `event_type="worker_deleted"` e
`stream=null`.

### Workers vivos sem FFmpeg saudável

```promql
sum(kube_pod_status_phase{namespace="media",phase="Running",pod=~"worker-.*"})
-
sum(worker_ffmpeg_health_state)
```

### Workers em execução menos streams ativos observados no proxy

```promql
sum(kube_pod_status_phase{namespace="media",phase="Running",pod=~"worker-.*"})
-
sum(proxy_rtmp_active_streams)
```

### Pods de worker não prontos por mais de 5 minutos

```promql
sum by (pod) (
  max_over_time((1 - pod_ready_status{namespace="media",pod=~"worker-.*"})[5m])
)
```

### Exclusões de órfãos por logs

Prometheus não consulta logs. Em Loki, use a consulta equivalente:

```logql
sum by (status) (
  count_over_time({namespace="media",app="controller"} | json | event_type="worker_deleted" | stream="" [$window])
)
```

## Ativos

### Streams ativos por proxy

```promql
sum by (proxy_pod) (proxy_rtmp_active_streams)
```

### Publishers ativos por proxy

```promql
sum by (proxy_pod) (proxy_rtmp_active_publishers)
```

### Clientes RTMP ativos por proxy

```promql
sum by (proxy_pod) (proxy_rtmp_active_clients)
```

### Workers disponíveis para alocação

```promql
sum by (namespace) (worker_pods_available)
```

### Workers com FFmpeg saudável

```promql
sum(worker_ffmpeg_health_state)
```

### Proxies com scrape RTMP saudável

```promql
sum(proxy_rtmp_stats_up)
```

## Recursos por componente

As consultas abaixo agregam por componente inferido pelo nome do Pod. Quando
possível, prefira labels de workload do kube-state-metrics; os exemplos por regex
funcionam com os manifests atuais (`proxy-*`, `worker-*`, `controller-*`).

### CPU por componente usando cAdvisor

```promql
sum by (component) (
  label_replace(
    rate(container_cpu_usage_seconds_total{namespace="media",container!="",pod=~"(proxy|worker|controller)-.*"}[5m]),
    "component", "$1", "pod", "^(proxy|worker|controller)-.*"
  )
)
```

### Memória working set por componente

```promql
sum by (component) (
  label_replace(
    container_memory_working_set_bytes{namespace="media",container!="",pod=~"(proxy|worker|controller)-.*"},
    "component", "$1", "pod", "^(proxy|worker|controller)-.*"
  )
)
```

### Tráfego de rede recebido por componente

```promql
sum by (component) (
  label_replace(
    rate(container_network_receive_bytes_total{namespace="media",pod=~"(proxy|worker|controller)-.*"}[5m]),
    "component", "$1", "pod", "^(proxy|worker|controller)-.*"
  )
)
```

### Tráfego de rede transmitido por componente

```promql
sum by (component) (
  label_replace(
    rate(container_network_transmit_bytes_total{namespace="media",pod=~"(proxy|worker|controller)-.*"}[5m]),
    "component", "$1", "pod", "^(proxy|worker|controller)-.*"
  )
)
```

### Métricas de recurso emitidas pelo controller

```promql
avg by (pod, namespace) (pod_cpu_usage_percent{namespace="media"})
```

```promql
avg by (pod, namespace) (pod_memory_usage_percent{namespace="media"})
```

```promql
sum by (pod, direction) (rate(pod_network_io_bytes_total[5m]))
```

As métricas de recurso do controller são úteis para smoke tests e dashboards
simples. Para resultados do artigo, prefira cAdvisor/kubelet para CPU, memória e
rede, porque `pod_memory_usage_percent` usa uma aproximação quando a coleta
detalhada não está disponível.

## SLOs e critérios resumidos para dashboards

- Cold start P95: comparar `proxy_to_first_progress` contra o limite definido no
  experimento.
- Allocation success: manter `stream_allocation_total{status="success"}` acima do
  limiar aceito, excluindo replays quando o objetivo for avaliar criação real.
- Handover conflict rate: investigar qualquer aumento persistente quando proxies
  estão saudáveis.
- Worker recovery P95: comparar `worker_recovery_duration_seconds` contra o MTTR
  alvo.
- Observabilidade: `proxy_rtmp_stats_up == 1` para proxies ativos e
  `worker_pod_lifecycle_watch_up == 1` durante execução do controller.
