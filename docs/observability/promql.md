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
- Antes de publicar mudanças neste arquivo, execute
  `python3 tools/validate-promql-docs.py` para checar fences Markdown e armadilhas
  conhecidas dos snippets. Quando `promtool` estiver disponível, valide também as
  expressões finais no Prometheus/Grafana alvo, principalmente consultas com
  placeholders como `$window`.

## Cold start P50/P95/P99

Para cold start sem destino externo, use o histograma
`stream_lifecycle_phase_seconds` com `phase="proxy_to_first_progress"`, pois mede
do hook de publicação no proxy até o primeiro progresso do FFmpeg. Para separar
atrasos do controller, use `phase="controller_to_first_progress"`. Para
experimentos com receptor externo instrumentado, use as fases com destino
(`proxy_to_destination`, `controller_to_destination` e
`ffmpeg_first_progress_to_destination`). Essas fases só são válidas quando
`t_destination_received` é observado por um receptor experimental habilitado no
controller; sem receptor, use as consultas sem destino externo.

### Percentis sem destino externo

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

### Percentis com destino externo experimental

Use estas consultas somente quando o experimento habilitar
`CONTROLLER_DESTINATION_CALLBACK_ENABLED=true` e o receptor externo enviar
`/streams/destination-received`.

```promql
histogram_quantile(
  0.50,
  sum by (le) (
    rate(stream_lifecycle_phase_seconds_bucket{phase="proxy_to_destination"}[5m])
  )
)
```

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(stream_lifecycle_phase_seconds_bucket{phase="proxy_to_destination"}[5m])
  )
)
```

```promql
histogram_quantile(
  0.99,
  sum by (le) (
    rate(stream_lifecycle_phase_seconds_bucket{phase="proxy_to_destination"}[5m])
  )
)
```

### Percentis por ambiente ou região

Sem destino externo:

```promql
histogram_quantile(
  0.95,
  sum by (le, environment, region) (
    rate(stream_lifecycle_phase_seconds_bucket{phase="proxy_to_first_progress"}[5m])
  )
)
```

Com destino externo experimental:

```promql
histogram_quantile(
  0.95,
  sum by (le, environment, region) (
    rate(stream_lifecycle_phase_seconds_bucket{phase="proxy_to_destination"}[5m])
  )
)
```

### Decomposição do cold start por fase

Use esta consulta para decompor apenas o caminho até o primeiro progresso do
FFmpeg, sem incluir destino externo.

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

### Extensão experimental com destino externo por fase

Use esta consulta somente quando houver receptor experimental instrumentado; ela
mostra o trecho adicional após o primeiro progresso e os agregados até destino.

```promql
histogram_quantile(
  0.95,
  sum by (le, phase) (
    rate(stream_lifecycle_phase_seconds_bucket{
      phase=~"ffmpeg_first_progress_to_destination|proxy_to_destination|controller_to_destination"
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

```promql
sum by (phase, timestamp, reason) (
  increase(stream_lifecycle_missing_timestamp_total[$window])
)
```

```promql
sum by (timestamp, source) (
  increase(stream_lifecycle_approximate_timestamp_total[$window])
)
```

Use a segunda consulta para reportar medições que chegaram à avaliação de fase,
mas não entraram no histograma por endpoint aproximado ou duração negativa por
skew de relógio. Use `stream_lifecycle_missing_timestamp_total` para medir
endpoints que nunca chegaram antes do cleanup de lifecycle e
`stream_lifecycle_approximate_timestamp_total` para quantificar timestamps
aceitos como aproximações. Em execuções sem receptor externo instrumentado,
`t_destination_received` ausente em fases com destino é esperado e não deve ser
tratado como falha da pipeline principal.

## MTTR de worker

No LiveEdgeCast, MTTR operacional de worker é aproximado pela duração das
tentativas de recuperação de worker não saudável registradas em
`worker_recovery_duration_seconds`. O histograma não possui labels de `status` ou
`reason`; portanto ele mede a duração de todas as tentativas observadas. Use
`worker_recovery_total{status="success",reason="replaced"}` como métrica
complementar para separar taxa de sucesso e volume de substituições concluídas.

### MTTR médio

```promql
sum(rate(worker_recovery_duration_seconds_sum[5m]))
/
clamp_min(sum(rate(worker_recovery_duration_seconds_count[5m])), 1e-9)
```

### MTTR P95

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(worker_recovery_duration_seconds_bucket[5m])
  )
)
```

### Taxa de recuperação bem-sucedida

```promql
sum(rate(worker_recovery_total{status="success",reason="replaced"}[5m]))
/
clamp_min(sum(rate(worker_recovery_total[5m])), 1e-9)
```

### Recuperações com erro por causa

```promql
sum by (reason) (
  increase(worker_recovery_total{status!="success"}[$window])
)
```

## Handover rate

A taxa de handover deve distinguir avaliações de ownership, mudanças reais de
owner e conflitos. O contador `handover_attempts_total` é incrementado antes de
saber se a chamada é primeiro registro, refresh do mesmo owner ou tentativa real
de troca de proxy; portanto ele não é denominador de taxa de aceite de handover
efetivo. `stream_proxy_handover_total` representa handovers aceitos entre proxies
e `handover_conflict_total` representa tentativas de troca negadas por owner
saudável.

### Handovers efetivos por segundo

```promql
sum(rate(stream_proxy_handover_total[5m]))
```

### Taxa de aceite de ownership

`handover_success_total` inclui tanto handovers aceitos quanto o primeiro registro
de ownership quando ainda não existe owner anterior. Use esta consulta para medir
aceite geral de ownership; para handover efetivo entre proxies, use
`stream_proxy_handover_total`.

```promql
sum(rate(handover_success_total[5m]))
/
clamp_min(sum(rate(handover_attempts_total[5m])), 1e-9)
```

### Taxa de aceite de troca real entre proxies

Esta razão usa como denominador apenas eventos que indicam tentativa de mudança
de owner observável (`stream_proxy_handover_total + handover_conflict_total`):
handover aceito entre proxies ou conflito negado. Ela evita
diluir o resultado com primeiro registro de stream e refresh idempotente do mesmo
owner. A implementação atual não exporta um contador separado para tentativas de
troca que falham por exceção antes de aceitar/negar; se esse detalhe for
necessário para o artigo, instrumente um contador dedicado de tentativa de troca
de owner.

```promql
sum(rate(stream_proxy_handover_total[5m]))
/
clamp_min(
  sum(rate(stream_proxy_handover_total[5m]))
  + sum(rate(handover_conflict_total[5m])),
  1e-9
)
```

### Taxa de conflito em tentativas reais de troca

```promql
sum(rate(handover_conflict_total[5m]))
/
clamp_min(
  sum(rate(stream_proxy_handover_total[5m]))
  + sum(rate(handover_conflict_total[5m])),
  1e-9
)
```

### Handovers aceitos normalizados por avaliações de ownership

Use esta consulta apenas como métrica de volume relativo à carga de avaliações de
ownership. Ela não mede taxa de aceite de handover efetivo, pois o denominador
inclui registros iniciais e refreshes do mesmo owner.

```promql
sum(rate(stream_proxy_handover_total[5m]))
/
clamp_min(sum(rate(handover_attempts_total[5m])), 1e-9)
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
clamp_min(sum(rate(stream_allocation_total[5m])), 1e-9)
```

### Taxa de sucesso excluindo replay idempotente

```promql
sum(rate(stream_allocation_total{status="success",reason!~"^(idempotent_replay|concurrent_idempotent_replay)$"}[5m]))
/
clamp_min(sum(rate(stream_allocation_total{reason!~"^(idempotent_replay|concurrent_idempotent_replay)$"}[5m])), 1e-9)
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
valor JSON `null` no campo `stream`.

### Workers vivos sem FFmpeg saudável

```promql
sum(kube_pod_status_phase{namespace="media",phase="Running",pod=~"worker-.*"})
-
(
  sum(worker_ffmpeg_health_state{namespace="media"})
  or on() vector(0)
)
```

### Workers em execução menos streams ativos observados no proxy

```promql
sum(kube_pod_status_phase{namespace="media",phase="Running",pod=~"worker-.*"})
-
sum(proxy_rtmp_active_streams)
```

### Pods de worker não prontos por mais de 5 minutos

Use kube-state-metrics para este painel, pois as séries `pod_ready_status`
emitidas pelo controller podem permanecer expostas com o último valor se um Pod
desaparecer antes de uma nova coleta. A série abaixo fica stale quando o Pod é
removido do cluster, trata `Ready=False` e `Ready=Unknown` como não prontos ao
testar ausência de `Ready=True`, e exige idade mínima de 5 minutos para não
contar workers que ainda estão no cold start normal.

```promql
sum by (namespace, pod) (
  (1 - max_over_time(kube_pod_status_ready{namespace="media",pod=~"worker-.*",condition="true"}[5m]))
  * on (namespace, pod) group_left()
    (time() - kube_pod_created{namespace="media",pod=~"worker-.*"} > bool 300)
)
```

### Exclusões de órfãos por logs

Prometheus não consulta logs. Em Loki, valide a sintaxe exata do parser usado no
cluster: o controller serializa ausência de stream como JSON `null`, não como
string vazia. Uma consulta típica é parsear o JSON para obter `status` e aplicar
um filtro de linha para o campo nulo:

```logql
sum by (status) (
  count_over_time({namespace="media",app="controller"} | json | event_type="worker_deleted" |= "\"stream\": null" [$window])
)
```

Se o backend de logs normalizar `null` como campo ausente ou string vazia, ajuste
o predicado de nulidade e mantenha a validação contra amostras reais antes de
usar a consulta no artigo.

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

### Workers Ready observados

`worker_pods_available` conta Pods de worker `Ready=True` observados pelo
controller. Ela não subtrai workers já associados a streams e, portanto, não deve
ser interpretada como capacidade livre de alocação.

```promql
sum by (namespace) (worker_pods_available)
```

### Workers com FFmpeg saudável

```promql
sum(worker_ffmpeg_health_state{namespace="media"})
```

### Proxies com scrape RTMP saudável

```promql
sum(proxy_rtmp_stats_up)
```

## Recursos por componente

A estratégia primária de coleta de recursos não depende de valores simulados nem
de gauges auxiliares do controller. Use cAdvisor/kubelet para CPU, memória e rede,
e kube-state-metrics para contagem, criação, fase e idade de Pods. As consultas
abaixo agregam por componente inferido pelo nome do Pod; os exemplos por regex
funcionam com os manifests atuais e separam `proxy-lb-*` de `proxy-*` para não
misturar o HAProxy de entrada com os Pods RTMP. Se o cluster tiver labels de
workload estáveis exportadas pelo kube-state-metrics, elas podem substituir a
inferência por regex, desde que preservem a mesma separação de componentes.

Os manifests em `k8s/observability/liveedgecast-resource-rules.yaml` materializam
recording rules para os principais agregados com as séries nativas
`container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`,
`container_network_receive_bytes_total`, `container_network_transmit_bytes_total`,
`kube_pod_status_phase` e `kube_pod_created`.

### CPU por componente usando cAdvisor

```promql
sum by (component) (
  label_replace(
    rate(container_cpu_usage_seconds_total{namespace="media",container!="",container!="POD",pod=~"(proxy-lb|proxy|worker|controller)-.*"}[5m]),
    "component", "$1", "pod", "^(proxy-lb|proxy|worker|controller)-.*"
  )
)
```

### Memória working set por componente

```promql
sum by (component) (
  label_replace(
    container_memory_working_set_bytes{namespace="media",container!="",container!="POD",pod=~"(proxy-lb|proxy|worker|controller)-.*"},
    "component", "$1", "pod", "^(proxy-lb|proxy|worker|controller)-.*"
  )
)
```

### Tráfego de rede recebido por componente

```promql
sum by (component) (
  label_replace(
    rate(container_network_receive_bytes_total{namespace="media",pod=~"(proxy-lb|proxy|worker|controller)-.*"}[5m]),
    "component", "$1", "pod", "^(proxy-lb|proxy|worker|controller)-.*"
  )
)
```

### Tráfego de rede transmitido por componente

```promql
sum by (component) (
  label_replace(
    rate(container_network_transmit_bytes_total{namespace="media",pod=~"(proxy-lb|proxy|worker|controller)-.*"}[5m]),
    "component", "$1", "pod", "^(proxy-lb|proxy|worker|controller)-.*"
  )
)
```

### Rede RX/TX por componente em uma única tabela

Use esta consulta quando o painel precisar de uma dimensão `direction` comum para
entrada e saída. A fonte continua sendo cAdvisor/kubelet; o `label_replace` final
só adiciona a label de direção para facilitar legendas e tabelas.

```promql
label_replace(
  sum by (component) (
    label_replace(
      rate(container_network_receive_bytes_total{namespace="media",pod=~"(proxy-lb|proxy|worker|controller)-.*"}[5m]),
      "component", "$1", "pod", "^(proxy-lb|proxy|worker|controller)-.*"
    )
  ),
  "direction", "rx", "component", ".*"
)
or
label_replace(
  sum by (component) (
    label_replace(
      rate(container_network_transmit_bytes_total{namespace="media",pod=~"(proxy-lb|proxy|worker|controller)-.*"}[5m]),
      "component", "$1", "pod", "^(proxy-lb|proxy|worker|controller)-.*"
    )
  ),
  "direction", "tx", "component", ".*"
)
```

### Pods ativos por componente usando kube-state-metrics

Aqui, "ativo" significa Pod ainda em ciclo de vida operacional (`Pending` ou
`Running`), excluindo Pods terminados (`Succeeded` ou `Failed`). A consulta usa
`kube_pod_status_phase`, não métricas sintéticas do controller.

```promql
sum by (component) (
  label_replace(
    max by (namespace, pod) (
      kube_pod_status_phase{namespace="media",phase=~"Pending|Running",pod=~"(proxy-lb|proxy|worker|controller)-.*"} == 1
    ),
    "component", "$1", "pod", "^(proxy-lb|proxy|worker|controller)-.*"
  )
)
```

### Fase/status de Pods por componente

```promql
sum by (component, phase) (
  label_replace(
    kube_pod_status_phase{namespace="media",pod=~"(proxy-lb|proxy|worker|controller)-.*"},
    "component", "$1", "pod", "^(proxy-lb|proxy|worker|controller)-.*"
  )
)
```

### Tempo de criação e idade de Pods

`kube_pod_created` é um timestamp Unix em segundos. A primeira consulta mostra o
momento de criação; a segunda mostra a idade dos Pods ainda ativos.

```promql
kube_pod_created{namespace="media",pod=~"(proxy-lb|proxy|worker|controller)-.*"}
```

```promql
(time() - kube_pod_created{namespace="media",pod=~"(proxy-lb|proxy|worker|controller)-.*"})
* on (namespace, pod) group_left()
  max by (namespace, pod) (
    kube_pod_status_phase{namespace="media",phase=~"Pending|Running",pod=~"(proxy-lb|proxy|worker|controller)-.*"} == 1
  )
```

### Pod lifetime para janelas experimentais

Para comparar repetições, integre Pod-segundos no intervalo do experimento. Em
Grafana, substitua `$__range_s` pela duração da janela em segundos quando não
estiver usando a variável nativa.

```promql
sum by (component) (
  avg_over_time(
    (
      label_replace(
        max by (namespace, pod) (
          kube_pod_status_phase{namespace="media",phase=~"Pending|Running",pod=~"(proxy-lb|proxy|worker|controller)-.*"} == 1
        ),
        "component", "$1", "pod", "^(proxy-lb|proxy|worker|controller)-.*"
      )
    )[$__range:]
  ) * $__range_s
)
```

Para uma visualização instantânea de lifetime por Pod ativo:

```promql
label_replace(
  (time() - kube_pod_created{namespace="media",pod=~"(proxy-lb|proxy|worker|controller)-.*"})
  * on (namespace, pod) group_left()
    max by (namespace, pod) (
      kube_pod_status_phase{namespace="media",phase=~"Pending|Running",pod=~"(proxy-lb|proxy|worker|controller)-.*"} == 1
    ),
  "component", "$1", "pod", "^(proxy-lb|proxy|worker|controller)-.*"
)
```

### Custo relativo aproximado por Pod-segundo

Esta consulta produz uma unidade relativa, não moeda. Ajuste os pesos para a
infraestrutura do experimento; por padrão, o exemplo abaixo usa peso `1` por
Pod-segundo para comparar cenários sem depender de preços externos ou simulados.

```promql
sum by (component) (
  avg_over_time(
    (
      label_replace(
        max by (namespace, pod) (
          kube_pod_status_phase{namespace="media",phase=~"Pending|Running",pod=~"(proxy-lb|proxy|worker|controller)-.*"} == 1
        ),
        "component", "$1", "pod", "^(proxy-lb|proxy|worker|controller)-.*"
      )
    )[$__range:]
  ) * $__range_s
)
```

### Custo relativo aproximado por core-segundo

`increase(container_cpu_usage_seconds_total[$__range])` já retorna core-segundos
consumidos. Multiplique o resultado por um peso local se quiser converter para
uma pontuação relativa comum entre CPU e Pod lifetime.

```promql
sum by (component) (
  label_replace(
    increase(container_cpu_usage_seconds_total{namespace="media",container!="",container!="POD",pod=~"(proxy-lb|proxy|worker|controller)-.*"}[$__range]),
    "component", "$1", "pod", "^(proxy-lb|proxy|worker|controller)-.*"
  )
)
```

### Recording rules opcionais

Se `k8s/observability/liveedgecast-resource-rules.yaml` estiver aplicado, os
painéis podem usar as séries pré-agregadas abaixo em vez de repetir as expressões
longas nos dashboards:

```promql
liveedgecast:component:cpu_cores:rate5m
```

```promql
liveedgecast:component:memory_working_set_bytes
```

```promql
liveedgecast:component:network_receive_bytes_per_second:rate5m
```

```promql
liveedgecast:component:network_transmit_bytes_per_second:rate5m
```

```promql
liveedgecast:component:active_pods
```

```promql
liveedgecast:pod:age_seconds
```

### Métricas de recurso emitidas pelo controller

As séries `pod_cpu_usage_percent`, `pod_memory_usage_percent` e
`pod_network_io_bytes_total` são **deprecated como fonte primária** e devem ser
tratadas somente como auxiliares enquanto não houver coletor real equivalente a
cAdvisor/kubelet. A implementação atual do coletor do controller popula readiness
e memória aproximada, mas não popula CPU nem rede. Assim, use estas séries apenas
como smoke test de disponibilidade de Pod e prefira cAdvisor/kubelet e
kube-state-metrics para resultados do artigo.

```promql
avg by (pod, namespace) (pod_memory_usage_percent{namespace="media"})
```

```promql
avg by (pod, namespace) (pod_ready_status{namespace="media"})
```

Não use `pod_cpu_usage_percent`, `pod_memory_usage_percent` nem
`pod_network_io_bytes_total` como fonte de resultado enquanto elas permanecerem
declaradas, aproximadas ou dependentes de coletor não implementado.

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
