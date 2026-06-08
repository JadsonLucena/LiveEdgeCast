# Experimental Test Plan
Scenarios: concurrency 1/5/10/15/20, saturation ramp, worker kill, proxy kill, duplicate stream reconnect.
Independent vars: concurrency, bitrate, duration, failure mode.
Dependent vars: cold start, ready time, error rate, handover success, resource usage, orphan cleanup.
Controls: cluster size, image versions, RTMP target, node type.
Repetitions: minimum 10 runs per scenario.
Collected data: Prometheus metrics + structured logs with experiment_id/scenario/run_id.
Acceptance: no leaked workers, idempotent replay handled, handover safe, metrics scrape stable.
Limitations: some timestamps rely on nearest observable controller/k8s events when exact FFmpeg internals unavailable.
