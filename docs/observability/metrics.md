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
| `t_ffmpeg_first_progress` | Worker one-shot callback after reading the first FFmpeg `-progress` line locally. | The worker runs FFmpeg with a local progress FIFO and notifies the controller once; duplicate callbacks are ignored for the milestone. |

The controller watches worker Pod events with `label_selector=app=worker` and
extracts scheduling/start/readiness milestones from Pod status. Worker Pods are
annotated with `liveedgecast.io/stream`, `liveedgecast.io/generation`, and
`liveedgecast.io/proxy-pod` so Kubernetes events can be correlated without using
stream names as Prometheus labels.

Derived Prometheus metrics:

| Metric | Labels | Meaning |
| --- | --- | --- |
| `stream_lifecycle_timestamp_observed_total` | `timestamp`, `source` plus controlled metadata | Count of lifecycle timestamp observations accepted by the controller. |
| `stream_lifecycle_phase_seconds` | `phase`, `start_timestamp`, `end_timestamp` plus controlled metadata | Histogram of derived phase durations once both timestamps are present and neither endpoint is approximate. |
| `stream_lifecycle_phase_observations_total` | `phase`, `status`, `reason` plus controlled metadata | Count of derived phase observations, including ignored negative durations caused by clock skew. |

Derived phases are intentionally low-cardinality and do **not** expose `stream`,
`generation`, `proxy_pod`, or `worker_pod` as metric labels. Those identifiers
remain in structured logs and in the controller's in-memory timestamp model for
correlation. Canonical phase histograms skip approximate endpoints so later exact
Kubernetes timestamps do not leave uncorrectable approximate observations in
Prometheus histograms.

Current approximations and observability limits:

- Proxy hook time and controller time may be on different clocks; negative
  derived durations are ignored and counted with `reason="negative_duration"`.
- `t_ffmpeg_started` is the worker script notification immediately before the
  `ffmpeg` command is launched, not a kernel-level process start timestamp.
- `t_ffmpeg_first_progress` is observed when the worker reads the first line from
  FFmpeg `-progress` and sends a one-shot callback to the controller; it is later
  than first input byte processing and depends on FFmpeg's progress emission
  cadence and worker/controller HTTP delivery.
- Kubernetes does not expose an exact image pull completion or user-process start
  timestamp in this controller, so `t_worker_container_started` is the container
  runtime `startedAt` value from `containerStatuses`.
