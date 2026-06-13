# Formato do relatório experimental

Cada execução do runner cria uma pasta `reports/<experiment-id>/` com evidências brutas, métricas consolidadas, gráficos e relatórios.

```text
reports/<experiment-id>/
  metadata.json
  execution.json
  raw/
    streams.jsonl
    publishers.jsonl
    controller_events.jsonl
    proxy_events.jsonl
    worker_events.jsonl
    kubernetes_events.jsonl
    pod_snapshots.jsonl
    prometheus_range_queries.json              # última coleta, compatibilidade
    prometheus_range_queries.run.<run-id>.json     # coleta Prometheus por run_id, segura para --resume
    prometheus_range_queries.__index__.json        # índice das coletas Prometheus
    proxy_context_patch.json
    controller_http_before.json
    controller_http_after.json
  metrics/
    activation_metrics.csv
    release_metrics.csv
    resilience_metrics.csv
    resource_usage.csv
    correctness_metrics.csv
    duplicate_streamkey_metrics.csv
    prometheus_metric_coverage.csv
    resource_activity.csv
    cost_estimation.csv  # alias compatível; contém a mesma estimativa de atividade relativa
  logs/
    final-before-restore/
      controller.log
      proxy.log
      worker.log
      publishers.log
    r<repetition>-after-run/
      controller.log
      proxy.log
      worker.log
      publishers.log
  charts/
    *.png ou *.txt
  report.md
  report.json
```

## Arquivos principais

- `metadata.json`: parâmetros do experimento, horários de início/fim e diretórios.
- `execution.json`: resumo das execuções, falhas injetadas, disponibilidade de Prometheus e coleta de logs.
- `raw/*.jsonl`: evidências brutas para auditoria e reprocessamento.
- `metrics/*.csv`: dados tabulares usados no relatório e na discussão do artigo.
- `report.md`: relatório legível com resumo, tabelas, limitações e texto-base para Discussão dos Resultados.
- `report.json`: versão estruturada do relatório.

## Interpretação dos CSVs

### `activation_metrics.csv`

Contém timestamps e durações por `run_id + repetition + streamKey`. O campo `status` informa se os valores foram derivados de logs estruturados ou se não eram observáveis.

### `release_metrics.csv`

Contém tempos entre fim do publisher, recebimento do evento de encerramento e término/deleção do worker por `run_id + repetition + streamKey`.

### `resilience_metrics.csv`

Registra falhas injetadas e, quando possível, tempo de recuperação observado.

### `resource_usage.csv`

Resume séries do Prometheus para CPU, memória e rede, incluindo média, mediana, P50, P95, P99, mínimo, máximo e intervalo de confiança.

### `correctness_metrics.csv`

Resume indícios de correção por `run_id + repetition + streamKey`. O arquivo separa `worker_observed_for_stream`, `at_most_one_worker_per_stream` e `one_worker_per_stream`, evitando considerar uma stream sem worker observado como válida. Duplicidade combina snapshots de pods com eventos estruturados do controller para reduzir o risco de perder sobreposições transitórias; substituições históricas entre repetições não são tratadas como duplicidade.

### `duplicate_streamkey_metrics.csv`

Registra explicitamente se houve tentativa de publicar uma `streamKey` duplicada, se o controller observou rejeição por conflito/handover negado e se houve aceitação inesperada. O arquivo também separa `duplicate_publisher_statuses` de `duplicate_publisher_process_statuses`; a primeira coluna é interpretação arquitetural, enquanto a segunda descreve o processo FFmpeg, como `nonzero_exit`, `success` ou `expected_stopped`. A coluna `duplicate_publisher_nonzero_without_controller_rejection=true` indica que o segundo publisher terminou com erro de processo sem rejeição observada do controller; por padrão isso invalida/parcializa a hipótese automatizada porque pode representar erro de RTMP/FFmpeg, e não proteção arquitetural. Esse arquivo deve ser usado na discussão sobre prevenção de duplicidade ou roubo de sessão.


### `prometheus_metric_coverage.csv`

Arquivo de cobertura por `run_id` e métrica Prometheus. Cada linha separa `query_success` de `samples_observed`: uma query Prometheus pode retornar `success` com resultado vazio e, nesse caso, `available_for_analysis=false`. A coluna `expected_by_run_windows` indica se aquele `run_id` pertence às janelas de execução esperadas ou se veio de evidência extra/stale preservada em `raw/`. Em execuções com `--resume`, use esse arquivo para verificar se a agregação comparou janelas com cobertura equivalente e amostras suficientes.

### `resource_activity.csv` e `cost_estimation.csv`

Calculam uma estimativa relativa de atividade por pod-seconds. A coluna `source` indica se o valor veio do Prometheus, da duração do experimento ou de uma estimativa derivada. Esses arquivos não representam cobrança financeira real de provedor de nuvem. O arquivo `resource_activity.csv` é o artefato primário; `cost_estimation.csv` é gerado apenas como alias legado do plano original e contém uma linha de aviso de depreciação. O relatório usa linguagem mais conservadora de “atividade relativa de recursos”. A referência `always_on_worker_pod_seconds_reference` é consciente das janelas de execução: soma `streamKeys_ativas_na_janela * duração_da_janela` para cada `run_id + repetition`, em vez de usar uma única duração global.

## Gráficos

O runner só gera gráficos com dados reais. Quando as amostras necessárias não existem, ele grava um arquivo `.txt` na pasta `charts/` explicando a limitação. Isso evita gráficos placeholder que poderiam ser confundidos com evidência experimental.

## Integridade do relatório

O runner não mistura execuções por padrão. Se o diretório de saída já existir, a execução falha, exceto quando `--overwrite` ou `--resume` for informado. Essa regra evita que evidências antigas contaminem métricas novas. Em modo `--resume`, as séries Prometheus são preservadas em arquivos por `run_id`; o agregador lê `raw/prometheus_range_queries.run.<run-id>.json` e evita depender do arquivo legado `raw/prometheus_range_queries.json`, que contém apenas a coleta mais recente. Arquivos Prometheus extras são reportados em `prometheus_extra_run_ids`, mas não entram nos numeradores de atividade relativa quando não pertencem às janelas de execução esperadas.

Os resultados por streamKey no `report.md` são calculados por `run_id + repetition + streamKey`, usando as janelas de execução registradas em `raw/streams.jsonl`. O uso de `--resume` deve ser acompanhado de um `--run-id` novo; o runner recusa retomar quando encontra colisão de `run_id + repetition` já existente no diretório e também recusa identificadores internos reservados como `index`, `latest` e `__index__`.


## Validade de handover e streamKey duplicada

Os relatórios incluem campos como `primary_proxy_pod`, `secondary_proxy_pod`, `secondary_proxy_observed`, `same_proxy_detected` e `scenario_inconclusive`. Para sustentar conclusões sobre handover ou rejeição de `streamKey` duplicada entre proxies, `secondary_proxy_observed` deve ser `true` ou a execução deve usar uma URL secundária que direcione a segunda publicação a outro proxy observável.

A coluna `duplicate_streamkey_rejected` depende de evidência explícita do controller, como eventos `handover_denied`, status `denied/conflict/rejected` ou mensagem de conflito. Encerramento genérico do FFmpeg não é prova suficiente de rejeição arquitetural. O relatório separa `controller_rejection_status` de `between_proxy_validity_status`: uma rejeição pode ser válida como comportamento do controller, mas a hipótese entre proxies só deve ser sustentada quando `second_attempt_proxy_correlated=true` e `secondary_proxy_observed=true`.


## Validade operacional do cluster

Quando `--patch-proxy-context` é usado, `report.json.summary.restore_ok=false` indica que a restauração das variáveis de ambiente falhou. Nesse caso, o comando retorna código diferente de zero por padrão, e o cluster deve ser inspecionado manualmente antes de nova coleta.

`report.json.summary.context_scope_ok=false` indica que o patch de contexto foi solicitado, mas não ficou totalmente efetivo. Por padrão, isso também faz o comando retornar código diferente de zero, pois a correlação por experimento pode estar incompleta. `report.json.summary.controller_scope_effective=true` indica que o escopo de labels do controller foi efetivamente aplicado nas consultas Prometheus. Quando esse campo é falso, as métricas do controller são consultadas sem labels de experimento para evitar falsos negativos.

`report.json.summary.scenario_inconclusive=true` em `handover` ou `duplicate-streamkey` indica que a hipótese entre proxies não foi sustentada automaticamente, mesmo que a execução técnica tenha terminado. Por padrão isso retorna código diferente de zero, salvo `--allow-inconclusive`. `report.json.summary.duplicate_publisher_nonzero_without_controller_rejection=true` indica erro de processo do segundo publisher sem rejeição observada pelo controller e também deve ser tratado como evidência inválida/inconclusiva para a hipótese de proteção contra duplicidade.


## Checklist de validade em `report.json`

O campo `report.json.summary` inclui marcadores para auditoria metodológica:

- `prometheus_resume_safe`: alias compatível de `prometheus_evidence_files_complete`; indica que todas as janelas `run_id + repetition` possuem arquivo Prometheus correspondente;
- `prometheus_missing_run_ids`: `run_id`s esperados pelas janelas, mas sem evidência Prometheus;
- `prometheus_incomplete_metrics`: métricas ausentes em pelo menos um `run_id`;
- `prometheus_samples_observed`: pelo menos uma série Prometheus retornou amostras;
- `resource_baseline_window_aware`: a referência de atividade relativa usa janelas `run_id + repetition`;
- `observable_activation_samples`: quantidade de linhas com `total_activation_seconds` finito;
- `worker_observed_samples`: quantidade de linhas de correção com worker observado;
- `controller_events_observed`: há eventos estruturados do controller disponíveis;
- `automation_status`, `automation_exit_code` e `automation_failure_reasons`: veredito final usado pela automação/CI.

Esses marcadores devem ser arquivados junto com o relatório antes de usar os dados na discussão final do artigo.
