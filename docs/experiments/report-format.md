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
    prometheus_range_queries.json
    proxy_context_patch.json
    controller_http_before.json
    controller_http_after.json
  metrics/
    activation_metrics.csv
    release_metrics.csv
    resilience_metrics.csv
    resource_usage.csv
    correctness_metrics.csv
    cost_estimation.csv
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

Resume indícios de correção por `run_id + repetition + streamKey`. O arquivo separa `worker_observed_for_stream`, `at_most_one_worker_per_stream` e `one_worker_per_stream`, evitando considerar uma stream sem worker observado como válida. Duplicidade só considera workers simultâneos dentro da janela da repetição; substituições históricas entre repetições não são tratadas como duplicidade.

### `cost_estimation.csv`

Calcula uma estimativa relativa de atividade por pod-seconds. A coluna `source` indica se o valor veio do Prometheus, da duração do experimento ou de uma estimativa derivada. Esse arquivo não representa cobrança financeira real de provedor de nuvem.

## Gráficos

O runner só gera gráficos com dados reais. Quando as amostras necessárias não existem, ele grava um arquivo `.txt` na pasta `charts/` explicando a limitação. Isso evita gráficos placeholder que poderiam ser confundidos com evidência experimental.

## Integridade do relatório

O runner não mistura execuções por padrão. Se o diretório de saída já existir, a execução falha, exceto quando `--overwrite` ou `--resume` for informado. Essa regra evita que evidências antigas contaminem métricas novas.

Os resultados por streamKey no `report.md` são calculados por `run_id + repetition + streamKey`, usando as janelas de execução registradas em `raw/streams.jsonl`. O uso de `--resume` deve ser acompanhado de um `--run-id` novo para evitar colisão lógica entre retomadas.
