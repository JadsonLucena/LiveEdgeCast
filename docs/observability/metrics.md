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


## Worker structured JSON logs

Worker containers emit newline-delimited JSON logs from `docker/worker/entrypoint.sh`
and `docker/worker/worker_stream_runner.sh` for lifecycle correlation without
adding high-cardinality labels to Prometheus metrics. Each worker log record uses
the following schema:

| Field | Meaning | Source |
| --- | --- | --- |
| `timestamp` | UTC timestamp when the event was emitted. | Worker shell script wall clock. |
| `event_type` | Worker lifecycle event name. | One of the worker event types below. |
| `stream` | Stream key handled by this worker. | `STREAM_KEY`. |
| `generation` | Stream generation token. | `STREAM_GENERATION`. |
| `proxy_pod` | Proxy identifier associated with the stream. | `PROXY_POD` when set, otherwise `PROXY_DNS`. |
| `worker_pod` | Worker pod name. | `WORKER_POD` when set, otherwise `HOSTNAME`. |
| `experiment_id` | Experiment identifier for controlled test runs. | `EXPERIMENT_ID`. |
| `scenario` | Experiment scenario name. | `SCENARIO`. |
| `run_id` | Experiment run identifier. | `RUN_ID`. |
| `duration_ms` | Event duration in milliseconds. | Elapsed time from the event-specific start boundary: entrypoint start for worker entrypoint/shutdown/error events, FFmpeg launch for FFmpeg start/progress/exit events. |
| `status` | Low-cardinality event result or exit status. | Worker shell script. |

Minimum worker event types are:

| Event type | Emitted by | Meaning |
| --- | --- | --- |
| `worker_entrypoint_started` | `entrypoint.sh` | Entrypoint process started. |
| `ffmpeg_started` | `worker_stream_runner.sh` | FFmpeg was launched and its PID was recorded. |
| `ffmpeg_first_progress` | `worker_stream_runner.sh` | The first complete FFmpeg `-progress` line was observed locally. Controller callback failures are logged separately as `worker_error` and retried without suppressing this event. |
| `ffmpeg_exited` | `worker_stream_runner.sh` | FFmpeg process exited; `status` is `exit_<code>`. |
| `worker_shutdown` | `entrypoint.sh` | The worker container is stopping after normal completion, process exit, or signal handling. |
| `worker_error` | Both worker shell scripts | A subprocess, notification, startup validation, or FFmpeg failure occurred. |

`EXPERIMENT_ID`, `SCENARIO`, and `RUN_ID` are intentionally read only from the
worker environment so experiments can control these values without allowing
request-controlled cardinality in metrics. For experiment runs, configure these
variables on the worker Deployment template or have the controller inject them
when creating the per-stream worker Pod. The controller-created Pod path already
sets stream-specific variables such as `STREAM_KEY`, `STREAM_GENERATION`,
`PROXY_DNS`, and `CONTROLLER_API`; experiment metadata must be propagated through
the same controlled environment mechanism so the worker JSON logs can correlate
all events for a given run. Do not promote `stream`, `proxy_pod`, `worker_pod`,
`experiment_id`, `scenario`, or `run_id` to Prometheus labels unless an explicit
cardinality review approves it.

## Proxy publish hook structured JSON logs

Proxy RTMP hooks emit newline-delimited JSON from `docker/proxy/on_publish_start.sh`
and `docker/proxy/on_publish_done.sh` so publish lifecycle events can be
correlated with controller and worker logs without adding per-stream labels to
Prometheus. Each proxy hook log record uses this schema:

| Field | Meaning | Source |
| --- | --- | --- |
| `timestamp` | UTC timestamp when the hook event was emitted. | Proxy shell script wall clock. |
| `event_type` | Proxy hook event name. | One of the proxy event types below. |
| `stream` | RTMP stream name/key observed by NGINX RTMP. | Hook argument from `$name`. |
| `proxy_pod` | Proxy pod or host handling the publisher. | `PROXY_POD` when set, otherwise `hostname`. |
| `experiment_id` | Experiment identifier for controlled test runs. | `EXPERIMENT_ID`, default `unknown`. |
| `scenario` | Experiment scenario name. | `SCENARIO`, default `unknown`. |
| `run_id` | Experiment run identifier. | `RUN_ID`, default `unknown`. |
| `status` | Low-cardinality hook result. | Proxy shell script. |

Minimum proxy event types are:

| Event type | Emitted by | Meaning |
| --- | --- | --- |
| `proxy_publish_started` | `on_publish_start.sh` | NGINX RTMP invoked the publish-start hook. |
| `proxy_publish_start_notified` | `on_publish_start.sh` | The `/streams/started` callback returned a successful HTTP response. |
| `proxy_publish_start_notify_failed` | `on_publish_start.sh` | The `/streams/started` callback failed after the configured curl timeout. |
| `proxy_publish_ended` | `on_publish_done.sh` | NGINX RTMP invoked the publish-done hook. |
| `proxy_publish_done_notified` | `on_publish_done.sh` | The `/streams/ended` callback returned a successful HTTP response. |
| `proxy_publish_done_notify_failed` | `on_publish_done.sh` | The `/streams/ended` callback failed after the configured curl timeout. |

The start hook sends `t_publish_start_proxy` to the controller as epoch seconds
with sub-second precision when the runtime `date` implementation supports
`%N`; it falls back to whole epoch seconds otherwise. The controller stores this
proxy-origin timestamp as the `t_publish_start_proxy` lifecycle milestone and
records `t_controller_received_event` from the controller clock when the HTTP
callback is received. Because these two milestones are sampled on different
hosts/containers, derived proxy-to-controller latency is sensitive to clock skew
between the proxy and controller nodes. Keep node time synchronized with NTP/PTP
and treat negative or unexpectedly large proxy-to-controller durations as a
clock-synchronization signal before interpreting them as network or controller
latency.

Experimental context propagation from proxy to controller is controlled by proxy
environment variables only: `EXPERIMENT_ID`, `SCENARIO`, and `RUN_ID`. The hooks
sanitize these values to the same bounded character set used by controller log
context (`a-z`, `A-Z`, `0-9`, `_`, `.`, `:`, `-`, capped at 64 characters, with
`unknown` for empty values) before forwarding them in both
`X-LiveEdgeCast-Experiment-Id`, `X-LiveEdgeCast-Scenario`, and
`X-LiveEdgeCast-Run-Id` headers and the matching `experiment_id`, `scenario`, and
`run_id` query parameters so the controller log context can capture them. Hook
JSON is emitted through `jq` rather than hand-written string escaping so unusual
stream keys remain valid JSON log values. Do not derive experiment context from
the RTMP stream name, and do not promote `stream`/`streamKey`, `experiment_id`,
`scenario`, or `run_id` to Prometheus labels unless an explicit cardinality
review approves it.

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

The current Prometheus metrics exported from `/stats` must not be used as a
byte-level RTMP traffic source. With the NGINX RTMP module configuration used by
the proxy, the raw `/stats` payload can include byte/bandwidth fields depending
on the module version, but the controller currently parses and exports only
stream, client, and publisher activity counts. The proxy sidecar
`nginx-prometheus-exporter` is intentionally connected to `/nginx_status`
(`stub_status`) and exposes generic NGINX process/connection metrics, not RTMP
per-stream byte counters. Until a compatible NGINX RTMP exporter or parser is
selected and deployed, use cAdvisor `container_network_receive_bytes_total` and
`container_network_transmit_bytes_total` as a Pod-level network proxy for RTMP
traffic, with documentation explicitly noting that these counters include all
non-loopback network traffic for the Pod.

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
| `worker_ffmpeg_first_progress_timestamp_seconds` | Gauge | none | Unix timestamp from the exporter clock when this exporter first observed a complete FFmpeg progress line for the current progress file/run; resets to `0` after progress file truncation or rotation until new progress is observed. |
| `worker_ffmpeg_out_time_seconds` | Gauge | none | Latest `out_time`/`out_time_us`/`out_time_ms` converted to seconds from the best available progress record. |
| `worker_ffmpeg_total_size_bytes` | Gauge | none | Latest FFmpeg `total_size` value in bytes from the best available progress record. |
| `worker_ffmpeg_speed` | Gauge | none | Latest FFmpeg `speed` multiplier with the trailing `x` removed. |
| `worker_ffmpeg_bitrate_bits_per_second` | Gauge | none | Latest FFmpeg `bitrate` value converted to bits per second from unitless bits/s, `bits/s`, `kbits/s`, `Mbits/s`, or `Gbits/s`; negative, `N/A`, missing, or unparsable values are exported as `0`. |
| `worker_ffmpeg_exit_total` | Counter | `exit_code` | Unique FFmpeg exits observed from the worker-local exit event file. |
| `worker_ffmpeg_exporter_errors_total` | Counter | `stage` | Exporter read/persistence errors; current stages are `progress_read` and `exit_state`. |

The worker runner appends a unique run id and exit code to the exit file after
FFmpeg exits. The exporter persists seen run ids in a small state file beside the
exit file so exporter restarts do not double-count already observed exits. Like
other process-local Prometheus counters, the exported counter can still reset if
the whole pod and its local filesystem are replaced. The progress-derived gauges
(`worker_ffmpeg_first_progress_timestamp_seconds`,
`worker_ffmpeg_bitrate_bits_per_second`, and the existing progress gauges) expose
no exporter-side labels; cardinality is therefore one time series per scraped
worker target, plus the labels added externally by Prometheus service discovery.
Exporter errors are counted with low-cardinality `stage` labels so operators can
distinguish FFmpeg progress staleness from exporter file I/O or state persistence
issues.

## Worker metrics ServiceMonitor checks

Worker metrics are exposed by the worker container on the named container port
`metrics` (`9113`) and scraped through the headless `worker` Service in the
`media` namespace. Because the Service is headless, Prometheus target addresses
correspond to selected worker Pod endpoints rather than a single Service
ClusterIP. Keep these Kubernetes objects aligned when changing worker
observability:

- `k8s/worker-deployment.yaml`: Pod template labels must include `app: worker`
  and the worker container must expose `containerPort: 9113` with `name: metrics`.
- `k8s/worker-service.yaml`: Service metadata must stay in namespace `media`,
  its selector must be `app: worker`, and its `metrics` Service port must route
  to `targetPort: metrics` so it follows the named container port.
- `k8s/worker-service-monitor.yaml`: The ServiceMonitor can live in namespace
  `monitoring`, but its `namespaceSelector.matchNames` must include `media`, its
  selector must match `app: worker`, and its endpoint must scrape port `metrics`
  at path `/metrics`.

After applying the manifests, confirm whether worker Pods exist before
investigating application-level metrics. The worker Deployment intentionally
starts with `replicas: 0`; in an idle stack, no worker endpoint or `UP`
`worker-metrics` target is expected until a stream causes the controller to
create a worker Pod.

Use these commands to inspect the Kubernetes objects and establish a Prometheus
port-forward:

```sh
kubectl -n media get deploy/worker svc/worker -o wide
kubectl -n media get pods -l app=worker -o wide
kubectl -n monitoring get servicemonitor worker-metrics -o yaml
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090
```

If the Prometheus Service name differs in your cluster, discover the installed
Service with
`kubectl -n monitoring get svc -o wide | grep -E 'prometheus|9090'` and
port-forward the Prometheus server Service that exposes port `9090` instead, or run
`PROMETHEUS_SERVICE=<service-name> ./tools/port-forward.sh`.

Then open `http://localhost:9090/targets`. If at least one `app=worker` Pod
exists, verify the `worker-metrics` target is `UP`, points at a `media` namespace
worker endpoint, and shows the `metrics` port. If the target is missing while
worker Pods exist, first check the Service labels and ServiceMonitor
selector/namespace selector; if it is present but down, check the worker Pod
readiness and scrape `http://<worker-pod-ip>:9113/metrics` from inside the
cluster.

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
| `t_destination_received` | Optional `/streams/destination-received` callback from an experimental receiver. | Valid only when an external receiver is explicitly instrumented and `CONTROLLER_DESTINATION_CALLBACK_ENABLED=true`; without that receiver, destination phases are expected to be absent and must not be interpreted as delivery latency. If the callback omits `t_destination_received`, the controller records receive time as an approximation. |

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
| `stream_lifecycle_phase_observations_total` | `phase`, `status`, `reason` plus controlled metadata | Count of derived phase observations after both endpoints are present, including pending phases with approximate endpoints and ignored negative durations caused by clock skew. |
| `stream_lifecycle_missing_timestamp_total` | `timestamp`, `phase`, `reason` plus controlled metadata | Count of phase endpoints still missing when in-memory lifecycle tracking is cleaned up. Destination-related misses are expected unless an experimental receiver is instrumented. |
| `stream_lifecycle_approximate_timestamp_total` | `timestamp`, `source` plus controlled metadata | Count of accepted lifecycle timestamps that used a controller-side approximation because the external timestamp was unavailable. |
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
counted once with `status="pending"` and `reason="approximate_endpoint"`. Missing
endpoints that remain absent until cleanup are counted by
`stream_lifecycle_missing_timestamp_total{reason="stream_cleanup"}`. Accepted
controller-side approximations are counted by
`stream_lifecycle_approximate_timestamp_total`. Because lifecycle state is
in-memory, `stream_lifecycle_missing_timestamp_total` only covers streams whose
tracking entry survives until normal cleanup; controller restarts or crashes can
still undercount missing lifecycle endpoints.

Destination-derived phases are emitted only when `t_destination_received` is
observed from an experimental receiver callback. Enable the callback explicitly
with `CONTROLLER_DESTINATION_CALLBACK_ENABLED=true` and post to
`/streams/destination-received` with `stream`, required `generation`, and optional
finite `t_destination_received` epoch seconds. The controller returns the active
`generation` from `/streams/started` and keeps a persisted per-stream generation
high-water mark so reused stream names receive non-reused generations after
cleanup; receivers must echo that generation so delayed callbacks from previous
runs are rejected instead of contaminating the active run. The high-water map is
bounded by `STREAM_GENERATION_HIGH_WATER_MAX_ENTRIES` (default `10000`); when the
limit is exceeded, the controller prunes the oldest inactive entries and keeps
active streams/lifecycle entries protected. Callback responses include whether
the accepted timestamp was approximate so receiver experiments can detect omitted
external timestamps immediately. The derived destination phases are
`ffmpeg_first_progress_to_destination`, `proxy_to_destination`, and
`controller_to_destination`. If no receiver is instrumented, do not use these
phases for delivery latency; use the existing first-progress phases instead and
expect destination timestamps to appear as missing at lifecycle cleanup.

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
- `t_destination_received` is meaningful only for experiments with an
  instrumented receiver that reports external destination arrival. It is not a
  default LiveEdgeCast datapath timestamp and should not be mixed with
  first-progress cold-start percentiles unless the experiment explicitly includes
  that receiver.

## Catálogo detalhado para dashboards e artigo

As tabelas abaixo consolidam tipo, labels, unidade, cardinalidade esperada,
interpretação operacional e uso recomendado no artigo. Todas as métricas do
controller recebem também os labels controlados `tenant`, `environment` e
`region`; eles são omitidos das colunas quando não forem específicos da métrica.
Métricas do worker são exportadas por Pod e ganham labels de target adicionados
pelo Prometheus, como `pod`, `namespace`, `job` e `instance`.

### Métricas de lifecycle e cold start

| Métrica | Tipo | Labels específicos | Unidade | Cardinalidade esperada | Interpretação | Uso no artigo |
| --- | --- | --- | --- | --- | --- | --- |
| `stream_lifecycle_timestamp_observed_total` | Counter | `timestamp`, `source` | eventos | Baixa: 10 timestamps × poucas fontes × metadados controlados. | Conta timestamps de lifecycle aceitos pelo controller. Crescimento desigual por timestamp indica perda de observabilidade em algum marco. | Relatar completude da instrumentação e volume de amostras por fase. |
| `stream_lifecycle_phase_seconds` | Histogram | `phase`, `start_timestamp`, `end_timestamp` | segundos | Baixa: fases derivadas fixas × buckets × metadados controlados. | Duração de fases de startup quando ambos endpoints são exatos. A fase `proxy_to_first_progress` é o cold start até primeiro progresso; `proxy_to_destination` é fim-a-fim externo apenas com receptor experimental. | Base principal para P50/P95/P99 de cold start, decomposição de latência e, quando instrumentado, latência até destino externo. |
| `stream_lifecycle_phase_observations_total` | Counter | `phase`, `status`, `reason` | eventos | Baixa: fases fixas × status/razões controladas. | Conta observações derivadas após ambos endpoints estarem presentes, inclusive fases pendentes por endpoint aproximado ou descartadas por duração negativa. | Denominador parcial de qualidade dos dados; combine com contadores de timestamps ausentes/aproximados para reportar perdas e limitações de medição. |
| `stream_lifecycle_missing_timestamp_total` | Counter | `timestamp`, `phase`, `reason` | eventos | Baixa: timestamps × fases fixas × razões controladas. | Conta endpoints de fase que continuam ausentes no cleanup do lifecycle. Ausência de `t_destination_received` é normal quando não há receptor experimental instrumentado. | Evidência direta de completude por fase e filtro para experimentos com destino externo. |
| `stream_lifecycle_approximate_timestamp_total` | Counter | `timestamp`, `source` | eventos | Baixa: timestamps × fontes controladas. | Conta timestamps aceitos como aproximações, como publish-start aproximado pelo recebimento no controller ou destino sem timestamp próprio. | Reportar qualidade temporal e excluir/qualificar percentis afetados por aproximações. |
| `worker_pod_lifecycle_watch_errors_total` | Counter | `status`, `reason` | eventos | Baixa: razões controladas. | Falhas do watch de eventos/status de Pods de worker. | Evidência de confiabilidade da coleta de timestamps Kubernetes. |
| `worker_pod_lifecycle_watch_up` | Gauge | nenhum específico | booleano `0/1` | Uma série por combinação de metadados controlados. | `1` quando o loop de watch está ativo; `0` após falha até reinício do watch. | Filtro de validade para experimentos de cold start. |

### Métricas de eventos, alocação e handover

| Métrica | Tipo | Labels específicos | Unidade | Cardinalidade esperada | Interpretação | Uso no artigo |
| --- | --- | --- | --- | --- | --- | --- |
| `stream_started_events_total` | Counter | `status`, `reason` | eventos | Baixa: status e razões controladas. | Eventos `/streams/started` aceitos, replays e erros. | Validar carga aplicada e idempotência durante repetições. |
| `stream_ended_events_total` | Counter | `status`, `reason` | eventos | Baixa. | Eventos `/streams/ended` aceitos, replays e erros. | Validar encerramento de sessões e limpeza de estado. |
| `stale_ended_events_ignored_total` | Counter | `status`, `reason` | eventos | Baixa. | Eventos de fim obsoletos ignorados para evitar cleanup indevido. | Quantificar efeitos de reordenação de eventos e handover. |
| `idempotent_replay_total` | Counter | `status`, `reason` | eventos | Baixa. | Replays idempotentes detectados em endpoints críticos. | Separar retries esperados de falhas reais nas taxas do artigo. |
| `stream_event_to_controller_seconds` | Histogram | `event` | segundos | Baixa: `started`/`ended` × buckets. | Tempo de processamento do controller por evento de stream. | Latência do plano de controle, não cold start completo. |
| `stream_event_to_controller_total` | Counter | `event`, `status`, `reason` | eventos | Baixa. | Contagem de eventos processados por tipo e resultado. | Denominador para taxas de erro por endpoint. |
| `stream_registration_duration_seconds` | Histogram | nenhum específico | segundos | Baixa. | Duração da etapa de registro/refresh de owner. | Decompor overhead do controller antes da alocação. |
| `stream_registration_total` | Counter | `status`, `reason` | eventos | Baixa. | Tentativas de registro de stream. | Taxa de sucesso de registro. |
| `stream_allocation_duration_seconds` | Histogram | nenhum específico | segundos | Baixa. | Duração do fluxo de alocação de worker. | P95 de alocação e contribuição para cold start. |
| `stream_allocation_total` | Counter | `status`, `reason` | eventos | Baixa. | Tentativas de alocação, incluindo sucesso, replay idempotente e erros. | Métrica principal de allocation success. |
| `worker_create_duration_seconds` | Histogram | nenhum específico | segundos | Baixa. | Duração da chamada de criação de Pod de worker. | Separar overhead de API Kubernetes do tempo de scheduling. |
| `worker_create_total` | Counter | `status`, `reason` | eventos | Baixa. | Tentativas de criação de worker e causas de falha. | Diagnóstico de falhas de alocação. |
| `worker_ready_duration_seconds` | Histogram | nenhum específico | segundos | Baixa. | Tempo da criação do worker até primeira observação Ready pelo controller. | Aproximação operacional de readiness para comparar cenários. |
| `worker_ready_total` | Counter | `status`, `reason` | eventos | Baixa. | Observações de Ready e respectivas razões. | Validar se workers chegaram a Ready em cada repetição. |
| `stream_release_duration_seconds` | Histogram | nenhum específico | segundos | Baixa. | Duração do release/cleanup de worker. | Medir custo de teardown e estabilização entre repetições. |
| `stream_release_total` | Counter | `status`, `reason` | eventos | Baixa. | Tentativas de release e resultado. | Confirmar limpeza após cada cenário. |
| `stream_proxy_handover_total` | Counter | nenhum específico | handovers | Uma série por metadados controlados. | Handovers efetivos aceitos entre proxies. | Numerador recomendado para handover rate efetivo. |
| `handover_attempts_total` | Counter | nenhum específico | avaliações | Uma série por metadados controlados. | Avaliações de ownership/handover, incluindo primeiro registro e refresh do mesmo owner. | Normalizar volume por carga de ownership; não usar como denominador de aceite de handover efetivo. |
| `handover_success_total` | Counter | nenhum específico | tentativas | Uma série por metadados controlados. | Avaliações aceitas, incluindo primeiro registro sem owner anterior. | Use com cautela; para handover entre proxies prefira `stream_proxy_handover_total`. |
| `handover_conflict_total` | Counter | nenhum específico | conflitos | Uma série por metadados controlados. | Tentativas de troca negadas porque o owner atual permanece elegível. | Denominador, junto com `stream_proxy_handover_total`, para taxas de aceite/conflito em trocas reais. |

### Métricas de recuperação, saúde e órfãos

| Métrica | Tipo | Labels específicos | Unidade | Cardinalidade esperada | Interpretação | Uso no artigo |
| --- | --- | --- | --- | --- | --- | --- |
| `worker_recovery_duration_seconds` | Histogram | nenhum específico | segundos | Baixa. | Duração de todas as tentativas de recuperar worker não saudável; o resultado fica no contador separado `worker_recovery_total`. | MTTR médio/P95 operacional de tentativas de recovery, sem segmentação direta por status. |
| `worker_recovery_total` | Counter | `status`, `reason` | eventos | Baixa. | Resultado de tentativas de recuperação: substituído, erro ou estado obsoleto. | Taxa de recuperação bem-sucedida e causas de falha. |
| `proxy_healthcheck_duration_seconds` | Histogram | nenhum específico | segundos | Baixa. | Tempo para avaliar healthcheck de proxies. | Contexto para cenários de failover/handover. |
| `proxy_healthcheck_total` | Counter | `status`, `reason` | eventos | Baixa. | Avaliações de saúde de proxy por resultado. | Evidenciar quando handover foi motivado por proxy não saudável. |
| `worker_healthcheck_duration_seconds` | Histogram | nenhum específico | segundos | Baixa. | Duração de probes `/health` nos workers. | Contexto para detecção de falha antes do MTTR. |
| `worker_healthcheck_total` | Counter | `status`, `reason` | eventos | Baixa. | Probes de saúde de worker por resultado. | Taxa de falha que dispara recuperação. |
| `worker_pods_available` | Gauge | `namespace` | pods | Muito baixa: um namespace por ambiente. | Número de Pods de worker com condição `Ready=True`; a implementação atual não subtrai workers já associados a streams. | Readiness/capacidade bruta observada; não usar como capacidade livre de alocação. |
| `pod_ready_status` | Gauge | `pod`, `namespace` | booleano `0/1` | Média: um por Pod proxy/worker. | Readiness agregada do Pod conforme status Kubernetes. | Smoke test; para séries do artigo, agregar por componente. |

O controller não expõe uma métrica dedicada de órfãos. Órfãos são observáveis por
logs estruturados de `worker_deleted` sem stream associado e por aproximações em
PromQL comparando workers vivos, readiness e atividade FFmpeg/RTMP. Essa lacuna
deve ser reportada como limitação de observabilidade quando o artigo discutir
limpeza de estado.

### Métricas de proxy RTMP e ativos

| Métrica | Tipo | Labels específicos | Unidade | Cardinalidade esperada | Interpretação | Uso no artigo |
| --- | --- | --- | --- | --- | --- | --- |
| `proxy_rtmp_active_streams` | Gauge | `proxy_pod` | streams | Média: uma série por proxy ativo. | Quantidade de streams ativos por proxy conforme `/stats`. | Carga efetiva e denominador para normalizar handover. |
| `proxy_rtmp_active_publishers` | Gauge | `proxy_pod` | publishers | Média: uma série por proxy ativo. | Clientes publicadores ativos por proxy. | Validar que a carga injetada chegou ao RTMP. |
| `proxy_rtmp_active_clients` | Gauge | `proxy_pod` | clientes | Média: uma série por proxy ativo. | Clientes RTMP totais por proxy. | Indicador de fan-in/fan-out RTMP. |
| `proxy_rtmp_stream_active` | Gauge | `proxy_pod` | booleano `0/1` | Média: uma série por proxy ativo. | Indica se o proxy tem ao menos um stream. | Presença de atividade por proxy. |
| `proxy_rtmp_stats_up` | Gauge | `proxy_pod` | booleano `0/1` | Média: uma série por proxy ativo. | Sucesso do último scrape `/stats` por proxy. | Filtro de validade para métricas RTMP. |
| `proxy_rtmp_stats_scrape_errors_total` | Counter | `proxy_pod` | erros | Média: uma série por proxy ativo. | Falhas de fetch/parse do `/stats` por proxy. | Qualidade da observabilidade RTMP. |
| `proxy_rtmp_stats_discovery_errors_total` | Counter | nenhum específico | erros | Uma série por metadados controlados. | Falhas ao listar Pods de proxy antes do scrape. | Limitação de coleta em janelas com falha Kubernetes/API. |
| `proxy_active_connections` | Gauge | `proxy_pod` | conexões | Média. | Conexões RTMP ativas quando coletadas pelo controller. | Indicador auxiliar; prefira `/stats` para contagem de clientes. |
| `proxy_bandwidth_mbps` | Gauge | `proxy_pod` | megabits/s | Média. | Banda atual por proxy quando coletada. | Contexto de carga; valide origem antes de usar como métrica primária. |

### Métricas do worker FFmpeg

| Métrica | Tipo | Labels específicos | Unidade | Cardinalidade esperada | Interpretação | Uso no artigo |
| --- | --- | --- | --- | --- | --- | --- |
| `worker_ffmpeg_running` | Gauge | nenhum no exporter; target adiciona `pod`/`namespace`. | booleano `0/1` | Média: uma série por worker vivo. | PID file aponta para processo FFmpeg em execução. | Confirmar ativação do worker após alocação. |
| `worker_ffmpeg_health_state` | Gauge | target labels | booleano `0/1` | Média. | FFmpeg está rodando e progresso recente não está stale. | Indicador de worker ativo/saudável e aproximação para órfãos. |
| `worker_ffmpeg_last_progress_timestamp_seconds` | Gauge | target labels | Unix timestamp em segundos | Média. | Momento, no relógio do exporter, da última linha completa de progresso. | Diagnóstico de staleness; não usar como timestamp de cold start. |
| `worker_ffmpeg_progress_age_seconds` | Gauge | target labels | segundos | Média. | Idade do último progresso. | Detectar congelamento de processamento. |
| `worker_ffmpeg_first_progress_timestamp_seconds` | Gauge | target labels | Unix timestamp em segundos | Média: uma série por worker vivo. | Primeiro momento, no relógio do exporter, em que uma linha completa de progresso foi observada para o arquivo/run atual; volta a `0` após truncamento ou rotação até novo progresso. | Aproximação local do primeiro progresso observável pelo exporter. |
| `worker_ffmpeg_out_time_seconds` | Gauge | target labels | segundos de mídia | Média. | Último `out_time` do FFmpeg convertido para segundos. | Evidenciar avanço de transcodificação durante o experimento. |
| `worker_ffmpeg_total_size_bytes` | Gauge | target labels | bytes | Média. | Último `total_size` reportado pelo FFmpeg. | Volume de saída processado. |
| `worker_ffmpeg_speed` | Gauge | target labels | multiplicador `x` | Média. | Velocidade de processamento relativa ao tempo real. | Métrica de desempenho do worker. |
| `worker_ffmpeg_bitrate_bits_per_second` | Gauge | target labels | bits/s | Média: uma série por worker vivo. | Último `bitrate` do arquivo `-progress` convertido de valores sem unidade (bits/s), `bits/s`, `kbits/s`, `Mbits/s` ou `Gbits/s`; valores negativos, ausentes, `N/A` ou inválidos viram `0`. | Taxa de bits reportada pelo FFmpeg para contextualizar carga e qualidade de saída. |
| `worker_ffmpeg_exit_total` | Counter | `exit_code` mais target labels | saídas | Média: exit codes controlados por worker. | Saídas únicas de FFmpeg observadas pelo exporter. | Falhas de processamento por repetição. |
| `worker_ffmpeg_exporter_errors_total` | Counter | `stage` mais target labels | erros | Baixa por worker: estágios controlados. | Erros de leitura/persistência do exporter. | Qualidade da telemetria do worker. |

### Métricas de recursos

A fonte primária de consumo de recursos deve ser o pipeline Kubernetes padrão:
cAdvisor/kubelet para CPU, memória e rede, e kube-state-metrics para estado,
criação e fase de Pods. Esses sinais são coletados diretamente do cluster e não
dependem de valores simulados pelo controller.

| Métrica | Tipo | Labels específicos | Unidade | Cardinalidade esperada | Interpretação | Uso no artigo |
| --- | --- | --- | --- | --- | --- | --- |
| `container_cpu_usage_seconds_total` | Counter | `namespace`, `pod`, `container` | core-segundos | Média: container por Pod. | CPU acumulada observada por cAdvisor/kubelet. | Fonte primária para CPU por componente e core-segundos. |
| `container_memory_working_set_bytes` | Gauge | `namespace`, `pod`, `container` | bytes | Média: container por Pod. | Working set de memória observado por cAdvisor/kubelet. | Fonte primária para memória por componente. |
| `container_network_receive_bytes_total` | Counter | `namespace`, `pod`, `interface` e labels do alvo | bytes | Média: interface/Pod conforme runtime. | Bytes recebidos observados por cAdvisor/kubelet; filtre `interface!="lo"` para evitar loopback. | Fonte primária para rede RX por componente. |
| `container_network_transmit_bytes_total` | Counter | `namespace`, `pod`, `interface` e labels do alvo | bytes | Média: interface/Pod conforme runtime. | Bytes transmitidos observados por cAdvisor/kubelet; filtre `interface!="lo"` para evitar loopback. | Fonte primária para rede TX por componente. |
| `liveedgecast:proxy:network_receive_bytes_per_second` | Recording rule | `pod` | bytes/s | Baixa: uma série por Pod RTMP proxy. | Taxa RX do Pod `proxy-*` calculada de cAdvisor, excluindo `proxy-lb-*` e loopback. | Proxy atual para tráfego RTMP recebido por proxy; não é bytes por stream. |
| `liveedgecast:proxy:network_transmit_bytes_per_second` | Recording rule | `pod` | bytes/s | Baixa: uma série por Pod RTMP proxy. | Taxa TX do Pod `proxy-*` calculada de cAdvisor, excluindo `proxy-lb-*` e loopback. | Proxy atual para tráfego RTMP transmitido por proxy; não é bytes por stream. |
| `liveedgecast:component:network_receive_bytes_per_second:rate1m` | Recording rule | `component` | bytes/s | Baixa: `proxy-lb`, `proxy`, `worker`, `controller`. | Taxa RX agregada por componente inferido pelo nome do Pod. | Comparação de tráfego recebido por componente. |
| `liveedgecast:component:network_transmit_bytes_per_second:rate1m` | Recording rule | `component` | bytes/s | Baixa: `proxy-lb`, `proxy`, `worker`, `controller`. | Taxa TX agregada por componente inferido pelo nome do Pod. | Comparação de tráfego transmitido por componente. |
| `kube_pod_status_phase` | Gauge | `namespace`, `pod`, `phase` | booleano `0/1` | Média: fase por Pod. | Fase Kubernetes do Pod exportada pelo kube-state-metrics. | Número de Pods ativos, status/phase e denominador de Pod-segundos. |
| `kube_pod_created` | Gauge | `namespace`, `pod` | Unix timestamp em segundos | Média: uma série por Pod. | Tempo de criação do Pod exportado pelo kube-state-metrics. | Idade/lifetime de Pods e análise de churn. |
| `pod_cpu_usage_percent` | Gauge | `pod`, `namespace` | porcentagem | Média: proxy/worker por Pod. | **Deprecated/auxiliar**: declarada pelo controller, mas não populada pelo coletor atual. | Não usar no artigo até haver coleta real; preferir cAdvisor `container_cpu_usage_seconds_total`. |
| `pod_memory_usage_bytes` | Gauge | `pod`, `namespace` | bytes | Média. | Auxiliar: uso de memória estimado/coletado por Pod. | Apenas contexto; a implementação pode aproximar valor por limite. |
| `pod_memory_usage_percent` | Gauge | `pod`, `namespace` | porcentagem | Média. | **Deprecated/auxiliar**: percentual de memória do limite; a implementação atual usa aproximação quando só o limite é conhecido. | Não usar como métrica primária de consumo; preferir cAdvisor `container_memory_working_set_bytes`. |
| `pod_network_io_bytes_total` | Counter | `pod`, `direction` | bytes | Média: Pod × direção. | **Deprecated/auxiliar**: declarada pelo controller, mas não incrementada pelo coletor atual. | Não usar no artigo até haver coleta real; preferir cAdvisor RX/TX por componente. |

As métricas de recurso emitidas pelo controller são auxiliares. Para resultados
quantitativos do artigo, use cAdvisor/kubelet e kube-state-metrics como fontes
primárias; `pod_cpu_usage_percent`, `pod_memory_usage_percent` e
`pod_network_io_bytes_total` permanecem deprecated como fonte de resultado
enquanto o controller não tiver coletor real equivalente. As recording rules em
`k8s/observability/liveedgecast-resource-rules.yaml` apenas agregam essas fontes
nativas por componente; elas não introduzem valores simulados nem preços fixos.

### Definição experimental de custo relativo

O catálogo de métricas não possui uma métrica nativa de preço ou fatura. Para o
artigo, defina custo relativo como uma família de variáveis derivadas em uma
janela de run fechada, usando somente fontes observadas no cluster:

| Variável derivada | Fonte primária | Unidade | Interpretação |
| --- | --- | --- | --- |
| `CPU_s(component)` | `increase(container_cpu_usage_seconds_total{container!="",container!="POD"}[window])` | CPU-segundos/core-segundos | CPU efetivamente consumida por componente durante o run. |
| `MEM_GiB_s(component)` | `sum_over_time(container_memory_working_set_bytes[window:sample_step]) * sample_step_seconds / 1024^3` | GiB-segundos | Integral aproximada do working set de memória por componente; evita multiplicar séries efêmeras pela janela inteira. |
| `NET_RX_B(component)` | `increase(container_network_receive_bytes_total{interface!="lo"}[window])` | bytes | Rede recebida observada por componente, sem loopback. |
| `NET_TX_B(component)` | `increase(container_network_transmit_bytes_total{interface!="lo"}[window])` | bytes | Rede transmitida observada por componente, sem loopback. |
| `POD_s_worker` | `sum_over_time((kube_pod_status_phase{phase=~"Pending|Running",pod=~"worker-.*"} == 1)[window:sample_step]) * sample_step_seconds` | Pod-segundos | Tempo total em que workers existiram como Pods ativos no run. |
| `POD_s_proxy` | `sum_over_time((kube_pod_status_phase{phase=~"Pending|Running",pod=~"proxy-.*"} == 1)[window:sample_step]) * sample_step_seconds` | Pod-segundos | Tempo total em que proxies RTMP existiram como Pods ativos no run; manter `proxy-lb-*` separado quando analisado. |

Uma pontuação agregada opcional pode combinar essas variáveis com pesos
pré-registrados:

```text
relative_cost_score =
  w_cpu * CPU_s
  + w_mem * MEM_GiB_s
  + w_rx * NET_RX_B
  + w_tx * NET_TX_B
  + w_pod_worker * POD_s_worker
  + w_pod_proxy * POD_s_proxy
```

Use a pontuação apenas para comparar cenários executados no mesmo cluster e com
os mesmos manifests, requests/limits, imagens, bitrate e janela de observação. A
comparação baseline always-on vs serverless seletivo deve contabilizar o mesmo
intervalo de run: no baseline, incluir Pods pré-provisionados mesmo quando
ociosos; no serverless, incluir criação, processamento, release e cleanup dos
workers seletivos. Normalize também por stream bem-sucedida, minuto de mídia ou
byte entregue para separar economia de capacidade ociosa de diferenças de carga.

Limitações importantes:

- O custo real de cloud não é medido diretamente; essas variáveis não consultam
  billing, lista de preços, descontos, reservas, taxas regionais nem custos de
  control plane.
- Os valores dependem do cluster, requests/limits, tipo de nó, CNI, runtime,
  autoscaling e provedor. Uma pontuação obtida em um ambiente não deve ser
  tratada como preço portátil para outro ambiente.
- cAdvisor mede uso de CPU, memória e rede, não preço. Kube-state-metrics mede
  fase/idade de Pod, não arredondamento de cobrança, minimum billing time,
  tráfego faturável por zona ou custos indiretos de observabilidade.
- Integrais de gauges são aproximações discretas: escolha `sample_step` próximo
  ao scrape interval e mantenha o mesmo valor entre cenários para não favorecer
  Pods curtos ou longos.
- Pesos `w_*` mudam a conclusão da soma agregada; publique sempre os termos
  brutos (`CPU_s`, `MEM_GiB_s`, `NET_RX_B`, `NET_TX_B`, `POD_s`) junto com a
  pontuação ponderada.

### Cardinalidade e retenção de labels

- **Baixa cardinalidade**: labels com enumeração fixa (`status`, `reason`,
  `phase`, `timestamp`, `event`) e metadados controlados. São seguros para
  alertas e agregações de longo prazo.
- **Cardinalidade média**: labels por Pod (`pod`, `proxy_pod`) variam com escala e
  rollouts. Use para troubleshooting e agregue por componente no artigo.
- **Labels proibidos em métricas**: `stream`, `streamKey`, `generation`,
  `worker_pod` em histogramas de lifecycle e identificadores de sessão. Eles
  aparecem em logs estruturados para correlação, mas não em Prometheus.
- **Unidades**: métricas terminadas em `_seconds` usam segundos; `_bytes` usa
  bytes; `_total` é contador acumulado; gauges booleanos usam `0` ou `1`.
