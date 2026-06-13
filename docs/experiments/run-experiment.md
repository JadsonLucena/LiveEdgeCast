# Executando experimentos com `run_experiment.py`

O runner unificado executa publishers RTMP com FFmpeg, coleta evidências de Kubernetes/Prometheus, consolida métricas e gera um relatório científico em `report.md` e `report.json`.

## Pré-requisitos

- Cluster Kubernetes acessível via `kubectl`.
- Namespace `media` com proxy, controller e workers do LiveEdgeCast.
- FFmpeg instalado localmente para gerar publishers sintéticos.
- Prometheus acessível por URL HTTP, normalmente via port-forward.
- Permissão para ler pods/events/logs. A propagação de contexto via `kubectl set env` é opcional e só ocorre quando `--patch-proxy-context` é informado.

Exemplo de port-forward do Prometheus:

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090
```

## Arquivo de streamKeys

Crie um arquivo com uma chave por linha:

```text
key-001
key-002
key-003
```

Linhas vazias e linhas iniciadas por `#` são ignoradas.

## Execução básica

```bash
python tools/experiments/run_experiment.py \
  --stream-keys-file ./tools/experiments/stream_keys.txt \
  --scenario cold-start \
  --rtmp-url rtmp://127.0.0.1:1935/live \
  --source-file ./media/sample.mp4 \
  --duration-seconds 120 \
  --repetitions 30 \
  --prometheus-url http://localhost:9090 \
  --namespace media \
  --experiment-id exp-rtmp-coldstart-001 \
  --output-dir ./reports
```

Para cenários `handover` e `duplicate-streamkey`, use `--secondary-rtmp-url` quando quiser direcionar a segunda publicação para outro proxy ou outro caminho RTMP. Se a segunda publicação não puder ser comprovada em outro proxy por eventos do controller, o relatório marca o cenário como inconclusivo para a hipótese de handover/conflito entre proxies.

Também é possível informar as streamKeys diretamente:

```bash
python tools/experiments/run_experiment.py \
  --stream-keys key1,key2,key3 \
  --scenario concurrency \
  --duration-seconds 180 \
  --prometheus-url http://localhost:9090 \
  --namespace media \
  --experiment-id exp-concurrency-001 \
  --output-dir ./reports
```

## Cenários suportados

- `cold-start`: mede ativação sob demanda a partir de zero workers. Antes de iniciar cada repetição, o runner verifica pods `app=worker`; por padrão, ele falha se houver workers ativos. A remoção de resíduos só ocorre quando `--allow-worker-cleanup` é informado explicitamente.
- `concurrency`: inicia múltiplas transmissões simultâneas.
- `release`: encerra publishers e observa liberação de workers/estado.
- `worker-failure`: injeta falha de worker e observa recuperação.
- `proxy-failure`: injeta falha de proxy e observa limitação do RTMP/reconexão.
- `handover`: encerra uma publicação e inicia outra com a mesma chave para avaliar transferência de propriedade. Para validar handover entre proxies, informe `--secondary-rtmp-url` ou confirme no relatório que `secondary_proxy_observed=true`.
- `duplicate-streamkey`: tenta publicar a mesma chave simultaneamente para validar rejeição de conflito. A rejeição só é considerada sustentada quando há evidência do controller; falhas genéricas do FFmpeg não são tratadas como prova de conflito.
- `pilot-capacity`: aumenta a concorrência progressivamente usando níveis como `1, 5, 10, 15, N`, sempre incluindo o máximo de streamKeys informado.

## Observabilidade e correlação

Por padrão, o runner **não altera deployments** do cluster. Para propagar contexto experimental aos hooks do proxy e às métricas/logs do controller, execute com `--patch-proxy-context`. Nesse modo, o runner aplica temporariamente variáveis de ambiente nos deployments `proxy` e `controller`, aguarda rollout e tenta restaurar os valores anteriores ao final:

```bash
kubectl set env deployment/proxy EXPERIMENT_ID=<id> SCENARIO=<scenario> RUN_ID=<run> -n <namespace>
kubectl set env deployment/controller LIVEEDGECAST_EXPERIMENT_ID=<id> LIVEEDGECAST_SCENARIO=<scenario> LIVEEDGECAST_RUN_ID=<run> LIVEEDGECAST_TENANT=<id> LIVEEDGECAST_ENVIRONMENT=<scenario> LIVEEDGECAST_REGION=<run> -n <namespace>
```

O patch é opt-in porque reinicia os deployments e pode interromper sessões RTMP ativas. Use preferencialmente um namespace dedicado por experimento. O runner captura o estado anterior das variáveis antes de alterar cada deployment; se o snapshot falhar para algum deployment, esse deployment não é alterado para evitar restauração destrutiva. Para segurança, o patch também é recusado quando uma variável-alvo já existe com `valueFrom` de Secret/ConfigMap, pois `kubectl set env` não restaura esse tipo de referência de forma confiável a partir de um valor escalar. Em deployments com sidecars ou múltiplos containers, informe `--proxy-container` e/ou `--controller-container`; sem isso, o runner recusa o patch para evitar alterar o container errado. O resultado do patch e da restauração fica em `raw/proxy_context_patch.json` e `raw/proxy_context_restore.json`. O escopo de métricas do controller só é usado quando o deployment `controller` foi efetivamente alterado e o rollout concluiu com sucesso; se o patch do controller for ignorado ou falhar, as queries do Prometheus não aplicam labels de experimento para evitar falso vazio. Quando `--patch-proxy-context` é solicitado e o patch não fica plenamente efetivo, o experimento passa a ser tratado como parcial/inválido para automação, salvo uso explícito de `--allow-unscoped-context`.

O runner também coleta logs estruturados e gera:

- `raw/controller_events.jsonl`
- `raw/proxy_events.jsonl`
- `raw/worker_events.jsonl`

Esses eventos são usados para derivar métricas per-stream de ativação, release, handover e correção arquitetural quando os logs contêm timestamps suficientes. As métricas de ativação e release são consolidadas por `repetition` + `streamKey`, preservando amostras independentes quando `--repetitions` é maior que 1.

## Saída

Cada execução gera:

```text
reports/<experiment-id>/
  metadata.json
  execution.json
  raw/
  metrics/
  logs/
  charts/
  report.md
  report.json
```

O runner não altera a documentação do repositório durante a execução. Todos os artefatos do experimento são gravados apenas no diretório informado em `--output-dir`.

## Limitações esperadas

- `t_destination_received` só é observável se houver callback do destino ou receptor experimental.
- Métricas per-stream dependem de logs estruturados do controller/worker.
- Métricas FFmpeg exportadas pelos workers são escopadas por padrão com `namespace="$namespace"`. Ajuste `--worker-metric-label-selector` ou `LIVEEDGECAST_WORKER_METRIC_LABEL_SELECTOR` quando o Prometheus usa outro nome de label; use string vazia apenas se esses exporters realmente não carregarem labels de scrape.
- Gráficos só são gerados quando há amostras reais; caso contrário, o runner cria um `.txt` explicando a ausência de dados.
- A estimativa é uma redução relativa de atividade por pod-seconds, não uma cobrança real de provedor de nuvem. O relatório chama essa seção de “Atividade relativa de recursos”. A referência `always_on_worker_pod_seconds_reference` é calculada somando cada janela `run_id + repetition`, usando a quantidade de streamKeys ativa naquela janela; isso evita distorções em `pilot-capacity` e `--resume`.

## Validade do experimento

- O cenário `cold-start` falha o comando quando não consegue confirmar zero workers antes da execução.
- Repetições com erro geram evento `run_failed`, fechando a janela temporal da repetição para evitar contaminação de métricas posteriores.
- O cenário `release` aguarda `--release-after-seconds` antes de encerrar publishers, permitindo que a stream fique ativa antes da medição de limpeza.
- Para evitar contaminação em métricas de cAdvisor/kube-state-metrics, execute apenas um experimento por namespace ou use namespaces isolados por execução.
- Cenários `handover` e `duplicate-streamkey` são marcados como inconclusivos quando a segunda publicação não é observada em outro proxy após o timestamp da segunda tentativa. A rejeição do controller e a validade entre proxies são reportadas separadamente. Por padrão, uma hipótese inconclusiva nesses cenários retorna código de saída `1`; use `--allow-inconclusive` apenas quando deseja preservar o relatório sem tratar a execução como evidência conclusiva.
- No cenário `duplicate-streamkey`, saída não-zero do segundo publisher sem rejeição observada pelo controller também torna a amostra inválida/inconclusiva para automação. O processo FFmpeg e a rejeição arquitetural são avaliados separadamente.
- `--resume` agrega evidências no mesmo diretório; use um `run_id` único para cada retomada e interprete o relatório como agregado, não como apenas a execução mais recente. Evidências Prometheus são salvas por `run_id` em `raw/prometheus_range_queries.run.<run-id>.json`, evitando sobrescrever séries de retomadas anteriores. O arquivo legado `raw/prometheus_range_queries.json` representa apenas a coleta mais recente para compatibilidade. `report.json.summary.prometheus_evidence_files_complete` informa se todo `run_id` observado nas janelas de execução possui arquivo Prometheus correspondente. `report.json.summary.prometheus_resume_safe`/`prometheus_analysis_ready` só ficam `true` quando, além dos arquivos, as métricas necessárias possuem amostras utilizáveis; lacunas aparecem em `prometheus_missing_run_ids` e em `metrics/prometheus_metric_coverage.csv`.

## Segurança de diretórios e limpeza

Por padrão, o runner recusa executar quando `reports/<experiment-id>/` já existe e contém arquivos, pois os arquivos JSONL são evidência bruta append-only. Use uma das opções abaixo de forma explícita:

- `--overwrite`: apaga o diretório anterior antes de executar.
- `--resume`: permite continuar e anexar evidências ao diretório existente. Use um `--run-id` novo para cada retomada; as métricas são separadas por `run_id + repetition`. O runner recusa a execução se detectar que o mesmo `run_id + repetition` já existe no diretório.

O cenário `cold-start` também é conservador por padrão. Se houver workers ativos, o experimento falha em vez de apagar pods automaticamente. Para permitir a limpeza de workers residuais em um namespace dedicado ao experimento, use:

```bash
--allow-worker-cleanup
```

Evite essa opção em namespaces compartilhados.

## Códigos de saída

- `0`: experimento válido, `partial` aceito explicitamente com `--allow-partial`, patch de contexto incompleto aceito explicitamente com `--allow-unscoped-context`, ou hipótese inconclusiva aceita explicitamente com `--allow-inconclusive`.
- `1`: experimento `failed`, `partial` sem `--allow-partial`, patch de contexto solicitado mas inefetivo sem `--allow-unscoped-context`, falha de restauração de contexto após `--patch-proxy-context` sem `--allow-restore-failure`, cenário `handover`/`duplicate-streamkey` inconclusivo sem `--allow-inconclusive`, ou Prometheus configurado com `--require-prometheus-analysis` sem amostras obrigatórias suficientes.

Use `--allow-partial` apenas quando deseja gerar relatório mesmo com falhas parciais sem quebrar automações/CI. Use `--allow-unscoped-context` somente quando aceita que logs/métricas do controller podem não estar correlacionados por labels de experimento. Use `--allow-restore-failure` somente após confirmar limpeza manual do cluster, pois a restauração malsucedida pode deixar deployments com variáveis de ambiente experimentais. Use `--allow-inconclusive` apenas quando a execução será analisada manualmente e não será tratada como evidência conclusiva automática. Para coleta de dados de artigo, use `--require-prometheus-analysis` junto com `--prometheus-url`; assim o comando falha quando os arquivos Prometheus existem, mas as séries obrigatórias (`workers_active`, `proxies_active`, `pod_cpu_rate`) não possuem amostras utilizáveis.

## Logs por fase

Além dos logs finais, o runner pode gravar subpastas em `logs/`, por exemplo:

```text
logs/r1-before-release/
logs/r1-after-run/
logs/final-before-restore/
```

Quando `--patch-proxy-context` é usado, os logs finais são coletados antes da restauração dos deployments para reduzir o risco de perder logs por rollout.


## Smoke test end-to-end

Há um script opcional para validação manual em cluster real:

```bash
LIVEEDGECAST_RTMP_URL=rtmp://127.0.0.1:1935/live \
PROMETHEUS_URL=http://127.0.0.1:9090 \
NAMESPACE=media \
./tools/experiments/smoke_k8s_experiment.sh
```

O script executa um `cold-start` mínimo em namespace dedicado ou controlado e valida se `report.md`, `activation_metrics.csv` e `correctness_metrics.csv` foram gerados. Além da existência dos arquivos, o smoke test exige pelo menos uma amostra com `total_activation_seconds` finito e uma linha de correção com worker observado; linhas parciais ou apenas `not_observable` não são suficientes para aprovar o teste.

## Checklist de validação pré-artigo

Antes de coletar dados finais para o artigo, execute o smoke test e arquive integralmente o diretório `reports/<experiment-id>/`. A coleta deve ser considerada válida para análise quantitativa apenas quando `report.json.summary` indicar:

- `controller_events_observed=true`;
- `observable_activation_samples > 0`;
- `worker_observed_samples > 0`;
- `prometheus_samples_observed=true`, quando a análise de recursos/atividade relativa for usada;
- `prometheus_resume_safe=true`, quando `--resume` for usado;
- `prometheus_missing_run_ids=[]`;
- `prometheus_incomplete_metrics=[]` ou apenas métricas que não sustentam a hipótese avaliada;
- `resource_baseline_window_aware=true`;
- `automation_status=passed` e `automation_exit_code=0`.

Se qualquer item necessário estiver ausente, trate o relatório como evidência exploratória ou qualitativa, não como resultado final da avaliação.
