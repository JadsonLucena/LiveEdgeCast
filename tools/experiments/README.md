# LiveEdgeCast experiment scripts

This directory contains runnable experiment drivers for load and failure scenarios. Existing scripts can remain alongside these files; all new drivers share the same required CLI parameters so each run is traceable and comparable:

- `--experiment-id` / `--experiment_id`
- `--scenario`
- `--run-id` / `--run_id`
- `--concurrency`
- `--duration-seconds` / `--duration_seconds`
- `--bitrate`

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
  --bitrate 800k
```

Useful optional parameters:

- `--step-size`: increment between concurrency levels; values above `--concurrency` run the target concurrency once.
- `--saturation-error-rate`: publisher failure-rate threshold from `0` to `1`.

### Kill worker

Starts the requested streams, waits `--kill-after-seconds`, deletes the explicit `--target-worker-pod` when provided, otherwise deletes the worker pod for the first stream when it can identify it by `liveedgecast.io/stream`, or falls back only when `--worker-selector` matches exactly one Running/Pending pod. Ambiguous worker selections are refused so the run does not accidentally delete a worker unrelated to the experiment traffic. `--kill-after-seconds` must be less than `--duration-seconds` so the worker is killed during the active run.

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

Starts the requested streams, waits `--kill-after-seconds`, deletes the explicit `--target-proxy-pod` when provided, or otherwise deletes a proxy only when `--proxy-selector` matches exactly one Running/Pending pod. Ambiguous selectors are refused so the run does not accidentally delete a proxy that is unrelated to the experiment traffic. `--kill-after-seconds` must be less than `--duration-seconds` so the proxy is killed during the active run.

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

Starts a primary publisher, launches a second publisher with the same `streamKey` while the primary is still active, waits for it, then performs reconnect attempts using the same key. Extra concurrent background streams are started when `--concurrency` is greater than `1`. `--duplicate-attempt-delay-seconds` must be less than `--duration-seconds` so the duplicate attempt overlaps the primary publisher.

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

- `--rtmp-url` / `--rtmp_url`: RTMP application URL. Defaults to `rtmp://127.0.0.1:1935/live` or `LIVEEDGECAST_RTMP_URL`.
- `--controller-url` / `--controller_url`: optional controller HTTP URL used to collect `/health` and `/metrics` artifacts.
- `--namespace`: Kubernetes namespace. Defaults to `media` or `NAMESPACE`.
- `--proxy-selector` / `--proxy_selector`: proxy pod selector. Defaults to `app=proxy` or `PROXY_SELECTOR`. In `kill_proxy`, this selector must match exactly one Running/Pending pod unless `--target-proxy-pod` is set.
- `--target-proxy-pod` / `--target_proxy_pod`: exact proxy pod to delete in `kill_proxy`. Defaults to `TARGET_PROXY_POD` when set.
- `--worker-selector` / `--worker_selector`: worker pod selector. Defaults to `app=worker` or `WORKER_SELECTOR`. In `kill_worker`, this selector is first filtered by `liveedgecast.io/stream`; fallback selector matching must be unambiguous unless `--target-worker-pod` is set.
- `--target-worker-pod` / `--target_worker_pod`: exact worker pod to delete in `kill_worker`. Defaults to `TARGET_WORKER_POD` when set.
- `--ffmpeg-path` / `--ffmpeg_path`: FFmpeg executable. Defaults to `ffmpeg` or `FFMPEG`.
- `--kubectl-path` / `--kubectl_path`: kubectl executable. Defaults to `kubectl` or `KUBECTL`.
