# Stream Notification Lifecycle

This document describes the current cleanup-phase architecture. Proxies notify the Controller about stream lifecycle events, but the Controller does **not** retain ownership state or provision and reconcile Workers. Worker Jobs or Operator behavior are intentionally outside this phase.

## Components

- **Proxy** receives the RTMP publish, exposes health and statistics endpoints, and notifies the Controller when publishing starts or ends.
- **Controller** accepts and logs stream start and end notifications.

There is no Worker Deployment or Worker Service. The Controller does not create, map, replace, inspect, or delete Worker Pods.

The notifications are transient events. The Controller does not use them to build an ownership registry.

## Publish Start

1. A broadcaster starts an RTMP publish through the public Proxy Service.
2. The selected Proxy runs `on_publish_start.sh`.
3. The Proxy calls `POST /streams/started?stream=<key>&proxy_pod=<pod>`.
4. The Controller logs the notification and returns `started_event_processed`.

The endpoint does not allocate or start a Worker.

## Publish End

1. The Proxy runs `on_publish_done.sh` when publishing ends.
2. The Proxy calls `POST /streams/ended?stream=<key>&proxy_pod=<pod>`.
3. The Controller logs the notification and returns `ended`.

No Worker is stopped or deleted by this endpoint.

## Responsibility Boundaries

- **Proxy**: receive RTMP, report publish start/end, and expose health/statistics.
- **Controller**: accept and log lifecycle notifications without retaining an ownership registry.
- **Worker lifecycle**: deliberately unimplemented in this cleanup phase. A future phase may introduce a Job or Operator, but no such behavior is implied by the current API.
