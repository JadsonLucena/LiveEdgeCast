# LiveEdgeCast experiment scripts

This directory contains runnable experiment drivers for load and failure scenarios. Existing scripts can remain alongside these files; all new drivers share the same required CLI parameters so each run is traceable and comparable:

- `--experiment-id` / `--experiment_id`
- `--scenario`
- `--run-id` / `--run_id`
- `--concurrency`
- `--duration-seconds` / `--duration_seconds`
- `--bitrate`
- `--audio-bitrate`
- `--constant-bitrate`
- `--tee-rtmp-urls`

Quando os scripts geram fonte sintética com FFmpeg/lavfi, o padrão recomendado para os testes atuais é `testsrc=size=1920x1080:rate=30` com `10000k` de bitrate de vídeo, CBR aproximado (`minrate=maxrate=10000k`, `bufsize=20000k`), áudio AAC `128k` em `44.1kHz`, GOP de 60 frames e saída FLV/RTMP por streamKey. Use `--constant-bitrate` para tornar o CBR explícito também no encoder x264 (`nal-hrd=cbr:force-cfr=1`) e calcular `bufsize` como 2x o bitrate informado; o script `liveedge-run-stress-30.sh` habilita essa opção por padrão via `CONSTANT_BITRATE=true`. Quando for necessário publicar o mesmo streamKey em destinos RTMP adicionais, use `--tee-rtmp-urls` (ou `TEE_RTMP_URLS` no script de stress) para gerar uma única codificação local e multiplexar as saídas com `ffmpeg -f tee` e `onfail=ignore`, reduzindo CPU local em comparação com múltiplos encoders independentes e evitando que uma falha isolada em um destino derrube todo o teste. Informe apenas URLs RTMP base, como `rtmp://proxy-a/live,rtmp://proxy-b/live`; as streamKeys continuam sendo geradas automaticamente pelo script de stress e são anexadas pelo runner a cada URL base.

Every script validates arguments before starting, writes execution logs, and creates per-run artifacts under:

```text
tools/experiments/artifacts/<experiment_id>/<scenario>/<run_id>/
```

Common artifacts include `config.json`, `run.log`, per-process FFmpeg stdout/stderr logs, and `summary.json`. RTMP and controller URLs are redacted in structured artifacts and agent-managed logs to avoid leaking credentials. FFmpeg's own stderr can still include protocol-level details, so treat raw per-process logs as sensitive when using production endpoints.

Before starting publishers, scripts validate required executables and run a TCP preflight against the RTMP host/port. Publisher waits are bounded by the configured duration plus startup/grace budget; timed-out publishers are stopped and marked with `timed_out` in `summary.json`. The `kill_worker` and `kill_proxy` scenarios require `kubectl`; they also collect Kubernetes pod and event snapshots with collection status metadata at the end of the run. Controller `/health` and `/metrics` artifacts are collected only when `--controller-url` is provided and are reported with collection status metadata. Pod target selection in kill scenarios records `selected`, `not_found`, `ambiguous`, `kubectl_failed`, or `parse_failed` statuses in `summary.json`.

## Scenarios

### Incremental pilot until publisher failure-rate saturation

Runs increasing concurrency levels until `--concurrency` is reached or the observed FFmpeg publisher failure rate reaches `--saturation-error-rate`. The saturation signal is intentionally named `publisher_failure_rate`; use controller, proxy, or Prometheus metrics alongside these artifacts if you need a broader system-saturation definition.

```sh
./tools/experiments/incremental_pilot.py \
  --experiment-id exp-001 \
  --scenario incremental_pilot \
  --run-id run-001 \
  --concurrency 10 \
  --duration-seconds 60 \
  --bitrate 10000k
```

Useful optional parameters:

- `--step-size`: increment between concurrency levels; values above `--concurrency` run the target concurrency once.
- `--saturation-error-rate`: publisher failure-rate threshold from `0` to `1`.

### Kill worker

Starts the requested streams, waits `--kill-after-seconds`, deletes the explicit `--target-worker-pod` when provided, otherwise deletes the worker pod for the first still-active stream when it can identify it by `liveedgecast.io/stream`, or falls back only when `--worker-selector` matches exactly one Running/Pending pod. If no publisher is still active after startup and kill delay, the worker kill is refused. Ambiguous worker selections are refused so the run does not accidentally delete a worker unrelated to the experiment traffic. `--kill-after-seconds` must be less than `--duration-seconds` so the worker is killed during the active run.

This scenario exercises Kubernetes Pod disruption/orphan behavior. Do not use it as the primary worker MTTR measurement unless the controller is changed to record recovery for deleted Pods. For `worker_recovery_duration_seconds` experiments, inject a controller-visible health failure such as a crashing/restarting worker container or an explicit nginx `/health` failure; freezing FFmpeg alone is insufficient while nginx continues to return healthy `/health` responses.

```sh
./tools/experiments/kill_worker.py \
  --experiment-id exp-001 \
  --scenario kill_worker \
  --run-id run-001 \
  --concurrency 3 \
  --duration-seconds 90 \
  --bitrate 10000k
```

### Kill proxy

Starts the requested streams, waits `--kill-after-seconds`, deletes the explicit `--target-proxy-pod` when provided, or otherwise deletes a proxy only when `--proxy-selector` matches exactly one Running/Pending pod. Ambiguous selectors are refused so the run does not accidentally delete a proxy that is unrelated to the experiment traffic. `--kill-after-seconds` must be less than `--duration-seconds` so the proxy is killed during the active run.

```sh
./tools/experiments/kill_proxy.py \
  --experiment-id exp-001 \
  --scenario kill_proxy \
  --run-id run-001 \
  --concurrency 3 \
  --duration-seconds 90 \
  --bitrate 10000k
```

### Reconnect and duplicate `streamKey`

Starts a primary publisher, launches a second publisher with the same `streamKey` while the primary is still active, waits for it, then performs reconnect attempts using the same key. Extra concurrent background streams are started when `--concurrency` is greater than `1`. `--duplicate-attempt-delay-seconds` must be less than `--duration-seconds` so the duplicate attempt overlaps the primary publisher.

```sh
./tools/experiments/duplicate_stream_key_reconnect.py \
  --experiment-id exp-001 \
  --scenario duplicate_stream_key_reconnect \
  --run-id run-001 \
  --concurrency 2 \
  --duration-seconds 60 \
  --bitrate 10000k
```

Useful optional parameters:

- `--duplicate-attempt-delay-seconds`
- `--reconnect-delay-seconds`
- `--reconnect-attempts`

## Shared runtime options

- `--rtmp-url` / `--rtmp_url`: RTMP application URL. Defaults to `rtmp://127.0.0.1:1935/live` or `LIVEEDGECAST_RTMP_URL`.
- `--controller-url` / `--controller_url`: optional controller HTTP URL used to collect `/health` and `/metrics` artifacts.
- `--namespace`: Kubernetes namespace. Defaults to `media` or `NAMESPACE`.
- `--proxy-selector` / `--proxy_selector`: proxy pod selector. Defaults to `app=proxy` or `PROXY_SELECTOR`. In `kill_proxy`, this selector must match exactly one Running/Pending pod unless `--target-proxy-pod` is set.
- `--target-proxy-pod` / `--target_proxy_pod`: exact proxy pod to delete in `kill_proxy`. Defaults to `TARGET_PROXY_POD` when set.
- `--worker-selector` / `--worker_selector`: worker pod selector. Defaults to `app=worker` or `WORKER_SELECTOR`. In `kill_worker`, this selector is first filtered by `liveedgecast.io/stream`; fallback selector matching must be unambiguous unless `--target-worker-pod` is set.
- `--target-worker-pod` / `--target_worker_pod`: exact worker pod to delete in `kill_worker`. Defaults to `TARGET_WORKER_POD` when set.
- `--ffmpeg-path` / `--ffmpeg_path`: FFmpeg executable. Defaults to `ffmpeg` or `FFMPEG`.
- `--kubectl-path` / `--kubectl_path`: kubectl executable. Defaults to `kubectl` or `KUBECTL`.

## Scientific validation switches

- Use `--require-prometheus-analysis` to fail automation when scenario-required Prometheus series are missing.
- Use `--require-network-metrics` only in clusters that expose Pod network counters such as `container_network_receive_bytes_total` and `container_network_transmit_bytes_total`; this makes proxy RX/TX mandatory evidence.
- Use `--require-destination-received` only when an instrumented destination receiver reports `t_destination_received`. It should remain disabled for YouTube/RTMPS targets without callback telemetry.

Correctness metrics combine Kubernetes snapshots with controller structured events, so short-lived workers observed via `worker_created`, `worker_ready_observed`, `ffmpeg_started`, or `ffmpeg_first_progress` are still counted even when they are absent from before/after snapshots. The Markdown stream table uses the same event-derived worker/proxy evidence so `initial_worker`, `final_worker`, and `proxy_owner` remain populated for short-lived workers.
- `tools/experiments/smoke_k8s_experiment.sh` passes `--controller-url` by default from `CONTROLLER_URL`/`LIVEEDGECAST_CONTROLLER_URL` or `http://127.0.0.1:8000`; set `CONTROLLER_URL=` to skip controller preflight artifacts.
- `PATCH_PROXY_CONTEXT=true` enables `--patch-proxy-context` in the smoke script. Use this for official multi-run campaigns when controller metrics must be scoped by experiment context.
- `REQUIRE_NETWORK_METRICS=true` enables `--require-network-metrics` in the smoke script for clusters that expose Pod network counters.
