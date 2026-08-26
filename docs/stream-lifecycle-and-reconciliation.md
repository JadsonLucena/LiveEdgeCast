# Stream Lifecycle and Reconciliation Flow

This document defines the full end-to-end lifecycle for a live stream in the current pull-only architecture.

## Components

- **Proxy**: receives RTMP publish from broadcaster and only notifies the Controller.
- **Controller**: single source of truth for ownership, allocation, start/stop orchestration, health reconciliation, and failover decisions.
- **Worker**: dedicated pod per stream key. Pulls from the assigned Proxy pod and pushes to YouTube.

Proxy capacity is fixed by the `replicas` field of the Proxy Deployment. Runtime metrics are used for observability, not to change the proxy replica count.

## Core State Model (Controller)

Per stream key, the Controller maintains:

```json
{
  "streamKey": {
    "proxyPod": "string",
    "workerPod": "string",
    "generation": 1,
    "expiresAt": 0
  }
}
```

Important state structures:

- `stream_registry`: stream -> current proxy owner + TTL expiration.
- `stream_to_worker`: stream -> allocated worker pod.
- `worker_to_stream`: worker pod -> stream.
- `stream_generation`: stream -> generation token.

## Start Lifecycle (Publish Start)

1. Broadcaster starts RTMP publish into a Proxy pod.
2. Proxy executes `on_publish_start.sh`.
3. Proxy calls:
   - `POST /streams/started?stream=<key>&proxy_pod=<proxyPod>`
4. Controller handles the full flow:
   - registers/refreshes stream ownership in `stream_registry`;
   - allocates a worker for the stream;
   - if worker is already available, starts it immediately.
5. Controller starts worker via `kubectl exec` passing:
   - `STREAM_KEY`
   - `STREAM_GENERATION`
   - `PROXY_DNS`
6. Worker runs `worker_stream_runner.sh` (single-shot):
   - pulls from `rtmp://<PROXY_DNS>:1935/live/<STREAM_KEY>`;
   - pushes to `${RTMP_PUSH_BASE_URL}/${STREAM_KEY}`;
   - exits non-zero on failure (crash-fast).

## End Lifecycle (Publish End)

1. Broadcaster stops publish in Proxy.
2. Proxy executes `on_publish_done.sh`.
3. Proxy calls:
   - `POST /streams/ended?stream=<key>&proxy_pod=<proxyPod>`
4. Controller performs all cleanup:
   - removes registry ownership for the stream if applicable;
   - releases worker allocation mapping;
   - removes workers that are no longer assigned to active streams.

## Reconciliation Flow

### Proxy Health Reconciliation

- Controller checks proxy health every 3s.
- If a proxy becomes unhealthy and exceeds failure threshold:
  - all impacted streams in `stream_registry` are expired;
  - mapped workers consuming from that proxy are deleted;
  - reallocation/restart is driven by subsequent stream events and reconciler loop.

### Worker Health Reconciliation

- Controller checks worker health every 3s.
- For each allocated stream:
  - validates worker pod readiness;
  - validates stream processing signal.
- If unhealthy:
  - removes mapping;
  - deletes defective worker pod;
  - allows replacement via normal allocation flow.

## Handover and Generation Rules

When the same stream key is seen on another proxy pod:

1. Controller evaluates ownership handover eligibility.
2. If handover is accepted:
   - increments `stream_generation`;
   - updates stream owner to new proxy.
3. Worker start requests may include generation check:
   - if generation mismatches, Controller ignores stale start.

This prevents split-brain and stale worker restarts from old ownership context.

## Failure Strategy (Crash-Fast)

- Worker does not run long local recovery loops.
- Worker failures are intentional signals for replacement.
- Controller is responsible for replacement and convergence.

## TTL and Inactivity Rules

- `STREAM_TTL_SECONDS = 180`.
- If stream activity is not refreshed within TTL, registry entry expires.
- Controller remains responsible for cleanup/reconciliation actions.

## Observability

Key metrics include:

- stream delivery status/error counters;
- proxy and worker health-related metrics;
- `stream_assignment_info{stream, proxy_pod, worker_pod, generation}`.

These metrics support per-stream tracking during normal operation, handover, and failover.

## Responsibility Boundaries

- **Proxy**:
  - notify start (`/streams/started`)
  - notify end (`/streams/ended`)
  - no allocation/reconciliation decisions.

- **Worker**:
  - execute pull/push for assigned stream.
  - fail fast on any critical execution problem.

- **Controller**:
  - owns lifecycle orchestration;
  - owns healthchecks and reconciliation;
  - owns state persistence and generation safety.
