# Executando experimentos com `run_experiment.py`

O runner unificado executa publishers RTMP com FFmpeg, coleta evidências de Kubernetes/Prometheus, consolida métricas e gera `report.md` e `report.json`.

Exemplo:

```bash
python tools/experiments/run_experiment.py \
  --stream-keys-file ./tools/experiments/stream_keys.txt \
  --scenario cold-start \
  --rtmp-url rtmp://127.0.0.1:1935/live \
  --duration-seconds 120 \
  --repetitions 30 \
  --prometheus-url http://localhost:9090 \
  --namespace media \
  --experiment-id exp-rtmp-coldstart-001 \
  --output-dir ./reports
```

Cenários suportados: `cold-start`, `concurrency`, `release`, `worker-failure`, `proxy-failure`, `handover`, `duplicate-streamkey` e `pilot-capacity`.

Quando uma métrica não estiver disponível, o runner registra `null` nos CSVs e declara a limitação no relatório. O script não inventa tempos não observáveis.
