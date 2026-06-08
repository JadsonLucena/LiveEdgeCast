# LiveEdgeCast experiment scripts

This directory contains runnable experiment drivers for load and failure scenarios. Existing scripts can remain alongside these files; all new drivers share the same required CLI parameters so each run is traceable and comparable:

- `--experiment-id`
- `--scenario`
- `--run-id`
- `--concurrency`
- `--duration-seconds`
- `--bitrate`

Every script validates arguments before starting, writes execution logs, and creates per-run artifacts under:

```text
tools/experiments/artifacts/<experiment_id>/<scenario>/<run_id>/
```

Common artifacts include `config.json`, `run.log`, per-process FFmpeg stdout/stderr logs, and `summary.json`. Failure-injection scripts also collect Kubernetes pod and event snapshots when `kubectl` is available.

## Scenarios

### Incremental pilot until saturation

Runs increasing concurrency levels until `--concurrency` is reached or the observed FFmpeg failure rate reaches `--saturation-error-rate`.

```sh
./tools/experiments/incremental_pilot.py \
  --experiment-id exp-001 \
  --scenario incremental_pilot \
  --run-id run-001 \
  --concurrency 10 \
  --duration-seconds 60 \
  --bitrate 800k
```

Useful optional parameters:

- `--step-size`: increment between concurrency levels.
- `--saturation-error-rate`: failure-rate threshold from `0` to `1`.

### Kill worker

Starts the requested streams, waits `--kill-after-seconds`, deletes the worker pod for the first stream when it can identify it by `liveedgecast.io/stream`, and records whether publishers survive.

```sh
./tools/experiments/kill_worker.py \
  --experiment-id exp-001 \
  --scenario kill_worker \
  --run-id run-001 \
  --concurrency 3 \
  --duration-seconds 90 \
  --bitrate 800k
```

### Kill proxy

Starts the requested streams, waits `--kill-after-seconds`, deletes one proxy pod selected by `--proxy-selector`, and records publisher outcomes.

```sh
./tools/experiments/kill_proxy.py \
  --experiment-id exp-001 \
  --scenario kill_proxy \
  --run-id run-001 \
  --concurrency 3 \
  --duration-seconds 90 \
  --bitrate 800k
```

### Reconnect and duplicate `streamKey`

Starts a primary publisher, launches a second publisher with the same `streamKey`, waits for it, then performs reconnect attempts using the same key. Extra concurrent background streams are started when `--concurrency` is greater than `1`.

```sh
./tools/experiments/duplicate_stream_key_reconnect.py \
  --experiment-id exp-001 \
  --scenario duplicate_stream_key_reconnect \
  --run-id run-001 \
  --concurrency 2 \
  --duration-seconds 60 \
  --bitrate 800k
```

Useful optional parameters:

- `--duplicate-attempt-delay-seconds`
- `--reconnect-delay-seconds`
- `--reconnect-attempts`

## Shared runtime options

- `--rtmp-url`: RTMP application URL. Defaults to `rtmp://127.0.0.1:1935/live` or `LIVEEDGECAST_RTMP_URL`.
- `--namespace`: Kubernetes namespace. Defaults to `media` or `NAMESPACE`.
- `--proxy-selector`: proxy pod selector. Defaults to `app=proxy` or `PROXY_SELECTOR`.
- `--worker-selector`: worker pod selector. Defaults to `app=worker` or `WORKER_SELECTOR`.
- `--ffmpeg-path`: FFmpeg executable. Defaults to `ffmpeg` or `FFMPEG`.
- `--kubectl-path`: kubectl executable. Defaults to `kubectl` or `KUBECTL`.
