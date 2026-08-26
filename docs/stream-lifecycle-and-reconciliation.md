# Stream Ownership Lifecycle and Proxy Reconciliation

This document describes the current cleanup-phase architecture. The Controller tracks which Proxy owns each live stream, but it does **not** provision or reconcile Workers. Worker Jobs or Operator behavior are intentionally outside this phase.

## Components

- **Proxy** receives the RTMP publish, exposes health and statistics endpoints, and notifies the Controller when publishing starts or ends.
- **Controller** owns stream-to-Proxy registration, handover decisions, generation tracking, state persistence, and Proxy health reconciliation.
- **Controller state ConfigMap** preserves ownership across Controller restarts.

There is no Worker Deployment or Worker Service. The Controller does not create, map, replace, inspect, or delete Worker Pods.

## Controller State

For each stream key, the Controller persists the current Proxy owner and generation:

```json
{
  "stream_to_proxy": {"stream-key": "proxy-pod"},
  "stream_registry": {"stream-key": {"proxy_pod": "proxy-pod"}},
  "stream_generation": {"stream-key": 1}
}
```

Worker allocation state is not part of the schema.

## Publish Start

1. A broadcaster starts an RTMP publish through the public Proxy Service.
2. The selected Proxy runs `on_publish_start.sh`.
3. The Proxy calls `POST /streams/started?stream=<key>&proxy_pod=<pod>`.
4. The Controller registers or refreshes that Proxy as the stream owner and persists the ownership state.
5. A repeated event from the same owner returns `idempotent_replay`.

The endpoint does not allocate or start a Worker.

## Publish End

1. The Proxy runs `on_publish_done.sh` when publishing ends.
2. The Proxy calls `POST /streams/ended?stream=<key>&proxy_pod=<pod>`.
3. The Controller compares the optional owner and generation with current state.
4. A stale event returns `stale_event_ignored` without changing ownership.
5. A matching event removes the stream's owner, registry entry, and generation, then persists the state. Repeated end events return `idempotent_replay`.

No Worker is stopped or deleted by this endpoint.

## Proxy Health Reconciliation

The Controller checks every Proxy that owns an active stream:

- A Ready delay prevents application probes during startup.
- Health checks run every three seconds with jitter.
- After three consecutive failures, the Controller expires every stream owned by that Proxy and persists the updated registry.

This loop reconciles ownership state only. It does not perform Worker recovery or replacement.

## Handover and Generation

When the same stream key appears on another Proxy, the Controller checks whether the current owner is unhealthy or no longer reports the stream in `/stats`.

- If the current owner remains eligible, the handover is rejected with HTTP 409.
- If handover is allowed, the Controller increments the generation, records the new Proxy owner, and persists the change.

Generation protects ownership cleanup from stale end events; it is not a Worker lease in the current phase.

## Responsibility Boundaries

- **Proxy**: receive RTMP, report publish start/end, and expose health/statistics.
- **Controller**: persist ownership, validate handovers, reject stale lifecycle events, and expire ownership after Proxy health failures.
- **Worker lifecycle**: deliberately unimplemented in this cleanup phase. A future phase may introduce a Job or Operator, but no such behavior is implied by the current API.
