# Executando experimentos com `run_experiment.py`

O runner unificado executa publishers RTMP com FFmpeg, coleta evidências de Kubernetes/Prometheus, consolida métricas e gera um relatório científico em `report.md` e `report.json`.

## Pré-requisitos

- Cluster Kubernetes acessível via `kubectl`.
- Namespace `media` com proxy, controller e workers do LiveEdgeCast.
- FFmpeg instalado localmente para gerar publishers sintéticos.
- Prometheus acessível por URL HTTP, normalmente via port-forward.
- Permissão para ler pods/events/logs e, opcionalmente, executar `kubectl set env deployment/proxy` para propagar `EXPERIMENT_ID`, `SCENARIO` e `RUN_ID` aos hooks do proxy.

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

Antes de executar o experimento, o runner tenta propagar o contexto para o deployment `proxy` com:

```bash
kubectl set env deployment/proxy EXPERIMENT_ID=<id> SCENARIO=<scenario> RUN_ID=<run> -n <namespace>
```

Isso permite que os hooks `exec_publish` enviem metadados ao controller. Caso essa etapa falhe, o experimento continua, mas o relatório registra a limitação em `raw/proxy_context_patch.json`.

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
