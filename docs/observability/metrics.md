# Controller metrics observability metadata

The controller attaches low-cardinality observability metadata to structured logs
and controlled metadata labels to controller-owned Prometheus metrics emitted by
`docker/controller/main.py`.

## Allowed metadata labels

Only these label names are propagated to metrics. Metric label values are
resolved only from environment variables or the controlled default; header and
query values are log-only.

| Label | Purpose | Controlled default |
| --- | --- | --- |
| `tenant` | Logical customer or tenant bucket. | `unknown` |
| `environment` | Deployment environment such as `dev`, `stage`, or `prod`. | `unknown` |
| `region` | Deployment region or locality bucket. | `unknown` |

Blank values are ignored during precedence resolution so the resolver can fall
back to the next source. Values are sanitized before use in labels and logs:
empty resolved values become `unknown`, unsupported characters are replaced with
`_`, and values are capped at 64 characters. This keeps labels bounded and
prevents accidental high-cardinality metadata from becoming Prometheus labels.

## Structured log metadata precedence

For each allowed label in structured logs, the controller resolves metadata in this exact order:

1. **HTTP headers**
   - `X-LiveEdgeCast-Tenant`, `X-Tenant`
   - `X-LiveEdgeCast-Environment`, `X-Environment`
   - `X-LiveEdgeCast-Region`, `X-Region`
2. **Query parameters**
   - `tenant` or `metadata_tenant`
   - `environment` or `metadata_environment`
   - `region` or `metadata_region`
3. **Environment variables**
   - `LIVEEDGECAST_TENANT` or `CONTROLLER_METADATA_TENANT`
   - `LIVEEDGECAST_ENVIRONMENT` or `CONTROLLER_METADATA_ENVIRONMENT`
   - `LIVEEDGECAST_REGION` or `CONTROLLER_METADATA_REGION`
4. **Controlled default**
   - `unknown`

The precedence is evaluated independently per field. For example, a request can
supply `X-Tenant` by header while `region` falls back to `LIVEEDGECAST_REGION`.

## Metrics and logs

Controller logs emitted by `docker/controller/main.py` are JSON-formatted and
include two metadata objects:

- `metadata`: the resolved `tenant`, `environment`, and `region` values using
  the full precedence above.
- `metadata_sources`: the source selected for each field (`header`, `query`,
  `env`, or `default`).

Controller metrics include the allowed metadata label names in addition to their
original metric-specific labels, but metric label values never come from HTTP
headers or query parameters. Metrics use only the controlled configuration branch
(environment variables) and then the controlled default (`unknown`), resolved once
per controller process for hot metric paths. This prevents
request-controlled values from creating unbounded Prometheus time series while
logs remain auditable for request-supplied metadata. If your log backend
automatically indexes JSON fields, do not promote request-sourced `metadata.*`
values (`metadata_sources` of `header` or `query`) to labels/indexes without an
allowlist.

## Proxy RTMP `/stats` metrics

The controller also starts a background asynchronous scraper for every pod labeled
`app=proxy` in the `media` namespace. Each interval it queries
`http://<proxy-pod-ip>:8080/stats`, parses either the default NGINX RTMP XML
output or a JSON representation of the same shape, and publishes per-proxy
aggregate gauges:

| Metric | Labels | Meaning |
| --- | --- | --- |
| `proxy_rtmp_active_streams` | `proxy_pod` plus controlled metadata | Number of active `<stream>` entries on the proxy. |
| `proxy_rtmp_active_publishers` | `proxy_pod` plus controlled metadata | Number of RTMP clients marked as publishers by the `/stats` payload. |
| `proxy_rtmp_active_clients` | `proxy_pod` plus controlled metadata | Number of RTMP `<client>` entries on the proxy. |
| `proxy_rtmp_stream_active` | `proxy_pod` plus controlled metadata | `1` when at least one stream is active on the proxy, otherwise `0`. |
| `proxy_rtmp_stats_up` | `proxy_pod` plus controlled metadata | `1` when the latest per-proxy `/stats` scrape succeeded, otherwise `0`. |
| `proxy_rtmp_stats_scrape_errors_total` | `proxy_pod` plus controlled metadata | Total failures fetching or parsing an individual proxy `/stats` response. |
| `proxy_rtmp_stats_discovery_errors_total` | Controlled metadata only | Total failures listing proxy pods before per-proxy scraping can start. |

The scraper intentionally aggregates by `proxy_pod` only. NGINX RTMP stream names
can contain the `streamKey`, so stream names are parsed only to count aggregate
activity and are never emitted as Prometheus labels. If a per-proxy scrape fails,
the controller sets `proxy_rtmp_stats_up` to `0`, increments
`proxy_rtmp_stats_scrape_errors_total`, and keeps the last successful activity
values instead of converting an unknown scrape result into zero active streams.
When a proxy pod disappears from Kubernetes discovery, the controller removes
that pod's RTMP metric series to avoid stale values after rollouts or
reschedules.

## Worker FFmpeg progress metrics

Each worker starts a lightweight standard-library exporter on port `9113` and
exposes `/metrics` for the worker ServiceMonitor. The exporter follows the local
FFmpeg `-progress` regular file, preserving the last coherent complete progress
record while a newer record is still being written. A first partial record may be
reported as best-effort until FFmpeg emits its first `progress=` delimiter; after
that, gauges remain anchored to the last complete record until the next complete
record arrives. This avoids false zero values during partial writes while keeping
`worker_ffmpeg_last_progress_timestamp_seconds` fresh whenever new complete
progress lines are observed.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `worker_ffmpeg_running` | Gauge | none | `1` when the PID file points to a currently running FFmpeg process, otherwise `0`. |
| `worker_ffmpeg_health_state` | Gauge | none | `1` when FFmpeg is running and progress has been observed within `FFMPEG_PROGRESS_STALE_SECONDS` (default `15`), otherwise `0`. |
| `worker_ffmpeg_last_progress_timestamp_seconds` | Gauge | none | Unix timestamp from the exporter clock when the latest complete progress line was observed. |
| `worker_ffmpeg_progress_age_seconds` | Gauge | none | Seconds elapsed since `worker_ffmpeg_last_progress_timestamp_seconds`; `0` until any progress is observed. |
| `worker_ffmpeg_out_time_seconds` | Gauge | none | Latest `out_time`/`out_time_us`/`out_time_ms` converted to seconds from the best available progress record. |
| `worker_ffmpeg_total_size_bytes` | Gauge | none | Latest FFmpeg `total_size` value in bytes from the best available progress record. |
| `worker_ffmpeg_speed` | Gauge | none | Latest FFmpeg `speed` multiplier with the trailing `x` removed. |
| `worker_ffmpeg_exit_total` | Counter | `exit_code` | Unique FFmpeg exits observed from the worker-local exit event file. |
| `worker_ffmpeg_exporter_errors_total` | Counter | `stage` | Exporter read/persistence errors; current stages are `progress_read` and `exit_state`. |

The worker runner appends a unique run id and exit code to the exit file after
FFmpeg exits. The exporter persists seen run ids in a small state file beside the
exit file so exporter restarts do not double-count already observed exits. Like
other process-local Prometheus counters, the exported counter can still reset if
the whole pod and its local filesystem are replaced. Exporter errors are counted
with low-cardinality `stage` labels so operators can distinguish FFmpeg progress
staleness from exporter file I/O or state persistence issues.

## Stream startup lifecycle timestamps

The controller models startup milestones per `(stream, generation)` in memory and
observes derived histograms only when both endpoints of a phase are available.
The timestamp fields are:

| Timestamp | Source | Notes |
| --- | --- | --- |
| `t_publish_start_proxy` | Proxy `on_publish_start` hook query parameter. | If the proxy timestamp is missing, the controller records `t_controller_received_event` time as a documented approximation. |
| `t_controller_received_event` | Controller `/streams/started` handler. | Wall-clock epoch seconds when the controller receives the publish-start event. |
| `t_worker_create_requested` | Controller before Kubernetes Pod create API call. | Records the controller-side create request boundary. |
| `t_worker_pod_created` | Kubernetes Pod metadata/create response. | Uses `metadata.creationTimestamp` when observable; records controller create-response time as an approximation only until exact metadata is observed. |
| `t_worker_scheduled` | Kubernetes Pod `status.conditions`. | Uses the `PodScheduled=True` `lastTransitionTime`. |
| `t_worker_container_started` | Kubernetes Pod `status.containerStatuses`. | Uses the first container `state.running.startedAt` observed for the worker Pod. |
| `t_worker_ready` | Kubernetes Pod `status.conditions`. | Uses the `Ready=True` `lastTransitionTime`. |
| `t_ffmpeg_started` | Worker HTTP notification immediately before launching FFmpeg. | Approximation of process start; the exact `execve` timestamp is not exposed by Kubernetes. |
| `t_ffmpeg_first_progress` | Worker one-shot callback after reading the first FFmpeg `-progress` line locally. | The worker runs FFmpeg with a local regular progress file and notifies the controller after the first complete `key=value` progress line is delivered; duplicate callbacks are ignored for the milestone. |

The controller watches worker Pod events with `label_selector=app=worker` and
extracts scheduling/start/readiness milestones from Pod status. Worker Pods are
annotated with `liveedgecast.io/stream`, `liveedgecast.io/generation`, and
`liveedgecast.io/proxy-pod` so Kubernetes events can be correlated without using
stream names as Prometheus labels. The watch uses a short bounded timeout
(`WORKER_POD_LIFECYCLE_WATCH_TIMEOUT_SECONDS`, default `5`) so controller
shutdown is not delayed for a long-running blocking Kubernetes watch thread.

Derived Prometheus metrics:

| Metric | Labels | Meaning |
| --- | --- | --- |
| `stream_lifecycle_timestamp_observed_total` | `timestamp`, `source` plus controlled metadata | Count of lifecycle timestamp observations accepted by the controller. |
| `stream_lifecycle_phase_seconds` | `phase`, `start_timestamp`, `end_timestamp` plus controlled metadata | Histogram of derived phase durations once both timestamps are present and neither endpoint is approximate. |
| `stream_lifecycle_phase_observations_total` | `phase`, `status`, `reason` plus controlled metadata | Count of derived phase observations, including pending phases with approximate endpoints and ignored negative durations caused by clock skew. |
| `worker_pod_lifecycle_watch_errors_total` | `status`, `reason` plus controlled metadata | Count of worker Pod lifecycle watch failures or per-event processing failures. |
| `worker_pod_lifecycle_watch_up` | Controlled metadata only | `1` when the worker Pod lifecycle watch loop is active, `0` after a watch failure until the next successful watch starts. |

Derived phases are intentionally low-cardinality and do **not** expose `stream`,
`generation`, `proxy_pod`, or `worker_pod` as metric labels. Those identifiers
remain in structured logs and in the controller's in-memory timestamp model for
correlation. The in-memory model is live-state only and is cleaned up when the
stream is released or expired; Prometheus metrics and structured logs are the
durable observability outputs. Canonical phase histograms skip approximate
endpoints so later exact Kubernetes timestamps do not leave uncorrectable
approximate observations in Prometheus histograms; skipped approximate phases are
counted once with `status="pending"` and `reason="approximate_endpoint"`.

Current approximations and observability limits:

- Proxy hook time and controller time may be on different clocks; negative
  derived durations are ignored and counted with `reason="negative_duration"`.
- `t_ffmpeg_started` is the worker script notification immediately before the
  `ffmpeg` command is launched, not a kernel-level process start timestamp.
- `t_ffmpeg_first_progress` is observed when the worker sees the first complete
  `key=value` line in the FFmpeg `-progress` regular file and successfully sends
  a one-shot callback to the controller; it is later than first input byte
  processing and depends on FFmpeg's progress emission cadence plus
  worker/controller HTTP delivery. Transient callback failures are retried by the
  worker until the first-progress notification succeeds or the FFmpeg process
  exits; the retry poll interval is controlled by `PROGRESS_NOTIFY_POLL_SECONDS`
  (default `0.2`).
- Kubernetes does not expose an exact image pull completion or user-process start
  timestamp in this controller, so `t_worker_container_started` is the container
  runtime `startedAt` value from `containerStatuses`.
