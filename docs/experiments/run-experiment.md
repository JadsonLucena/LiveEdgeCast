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

- `cold-start`: mede ativação sob demanda a partir de zero workers. Antes de iniciar cada repetição, o runner verifica pods `app=worker`, remove resíduos ativos no namespace do experimento e falha a repetição se não conseguir confirmar zero workers.
- `concurrency`: inicia múltiplas transmissões simultâneas.
- `release`: encerra publishers e observa liberação de workers/estado.
- `worker-failure`: injeta falha de worker e observa recuperação.
- `proxy-failure`: injeta falha de proxy e observa limitação do RTMP/reconexão.
- `handover`: encerra uma publicação e inicia outra com a mesma chave para avaliar transferência de propriedade.
- `duplicate-streamkey`: tenta publicar a mesma chave simultaneamente para validar rejeição de conflito.
- `pilot-capacity`: aumenta a concorrência progressivamente usando níveis como `1, 5, 10, 15, N`, sempre incluindo o máximo de streamKeys informado.

## Observabilidade e correlação

Por padrão, o runner **não altera deployments** do cluster. Para propagar contexto experimental aos hooks do proxy e às métricas/logs do controller, execute com `--patch-proxy-context`. Nesse modo, o runner aplica temporariamente variáveis de ambiente nos deployments `proxy` e `controller`, aguarda rollout e tenta restaurar os valores anteriores ao final:

```bash
kubectl set env deployment/proxy EXPERIMENT_ID=<id> SCENARIO=<scenario> RUN_ID=<run> -n <namespace>
kubectl set env deployment/controller LIVEEDGECAST_EXPERIMENT_ID=<id> LIVEEDGECAST_SCENARIO=<scenario> LIVEEDGECAST_RUN_ID=<run> LIVEEDGECAST_TENANT=<id> LIVEEDGECAST_ENVIRONMENT=<scenario> LIVEEDGECAST_REGION=<run> -n <namespace>
```

O patch é opt-in porque reinicia os deployments e pode interromper sessões RTMP ativas. Use preferencialmente um namespace dedicado por experimento. O resultado do patch e da restauração fica em `raw/proxy_context_patch.json` e `raw/proxy_context_restore.json`.

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
- Gráficos só são gerados quando há amostras reais; caso contrário, o runner cria um `.txt` explicando a ausência de dados.
- A estimativa de custo é relativa e baseada em pod-seconds, não em cobrança real de provedor de nuvem.

## Validade do experimento

- O cenário `cold-start` falha o comando quando não consegue confirmar zero workers antes da execução.
- Repetições com erro geram evento `run_failed`, fechando a janela temporal da repetição para evitar contaminação de métricas posteriores.
- O cenário `release` aguarda `--release-after-seconds` antes de encerrar publishers, permitindo que a stream fique ativa antes da medição de limpeza.
- Para evitar contaminação em métricas de cAdvisor/kube-state-metrics, execute apenas um experimento por namespace ou use namespaces isolados por execução.
