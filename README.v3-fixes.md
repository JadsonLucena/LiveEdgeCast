# LiveEdgeCast worker lifecycle fixes v3

This patch addresses the remaining worker lifecycle issues found during review:

- The controller no longer skips allocated workers solely because the Pod is NotReady. It now explicitly evaluates terminal Pod phases and prolonged NotReady states, then either releases stale allocations or replaces workers for active streams.
- Workers receive both `PROXY_DNS` and `PROXY_POD`: `PROXY_DNS` is used by FFmpeg to pull from the proxy IP/address, while `PROXY_POD` is used for controller `/streams/status` ownership checks.
- Worker FFmpeg exit metrics now use a dedicated event log file (`*.exit_events`) plus a separate last-exit file, avoiding double-counting of a single FFmpeg exit.
- FFmpeg exit `0` after progress is now classified using controller stream status: inactive streams exit cleanly, active streams are treated as an early unexpected exit, and unknown status is not treated as success unless explicitly allowed.
- The worker entrypoint now terminates the container after the runner exits successfully by default, avoiding idle worker Pods without FFmpeg. Set `KEEP_WORKER_ALIVE_AFTER_RUNNER_EXIT=true` only for debugging.
- Worker processing readiness events are now named `worker_processing_ready_observed` to distinguish processing readiness from basic Pod/container startup.

Validation performed:

```bash
python3 -m py_compile docker/controller/main.py docker/worker/metrics_exporter.py tools/experiments/run_experiment.py
bash -n docker/worker/worker_stream_runner.sh docker/worker/entrypoint.sh docker/proxy/on_publish_start.sh docker/proxy/on_publish_done.sh liveedge-run-stress-30.sh tools/port-forward.sh
pytest -q
```

Result:

```text
105 passed, 1 skipped
```
