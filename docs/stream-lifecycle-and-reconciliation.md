# Stream Lifecycle

This document describes the current cleanup-phase architecture. The Proxy accepts RTMP publishes, but does not invoke imperative Controller callbacks. The Controller does **not** retain ownership state or provision and reconcile Workers.

## Components

- **Proxy** receives the RTMP publish and exposes health and statistics endpoints.
- **Controller** exposes only its health endpoint.

There is no Worker Deployment or Worker Service. The Controller does not create, map, replace, inspect, or delete Worker Pods.

There are no stream start/end notification endpoints or internal allocation/release endpoints.

## Publish Lifecycle

1. A broadcaster starts an RTMP publish through the public Proxy Service.
2. The selected Proxy receives and serves the stream.
3. Publishing ends without invoking a Controller callback.

No Worker is allocated, started, stopped, or deleted by this flow.

## Responsibility Boundaries

- **Proxy**: receive RTMP and expose health/statistics.
- **Controller**: expose health without stream lifecycle mutation APIs.
- **Worker lifecycle**: deliberately unimplemented in this cleanup phase. A later phase may introduce declarative `LiveStream` resources; no compatibility callbacks are retained in the current API.
