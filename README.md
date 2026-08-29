# LiveEdgeCast

LiveEdgeCast is building its **Phase 1 foundation**. This foundation contains
only a minimal RTMP proxy, its Kubernetes exposure, and an FFmpeg-based Worker
container image. It does not currently implement stream orchestration or
serverless retransmission.

## Current repository state

The deployable Kubernetes resources are limited to:

- the `media` namespace;
- an NGINX-RTMP Proxy Deployment and a single `LoadBalancer` Service for RTMP
  over TCP port 1935.

The proxy accepts and serves RTMP streams. Nothing in the current application
creates per-stream workloads, persists stream ownership, reacts to stream
lifecycle callbacks, or performs autoscaling. The worker image is retained as a
building block, but no Kubernetes resource launches it.

The following are deliberately absent:

- the former imperative Controller lifecycle API;
- HAProxy-based routing;
- KEDA scaling and Prometheus metrics;
- a shared Worker Deployment or Service; and
- `LiveStream` custom resources, an Operator, and per-stream Jobs.

Phase 1 does not retain a health-check application as a placeholder for the
future Operator. The Operator will be introduced directly in its corresponding
implementation phase.

The manifests and scripts are Phase 1 foundation scaffolding, not a production
implementation of the target design.

## Fixed target architecture

Future implementation work must use one declarative ownership model:

1. An RTMP ingest component records the desired stream as a `LiveStream` custom
   resource.
2. A Kubernetes Operator reconciles each `LiveStream` into one per-stream Job.
3. The Job runs the worker image and forwards that stream to its configured
   destination.
4. Stream termination updates or removes the custom resource; the Operator then
   reconciles the associated Job to the stopped state.

Kubernetes API state is the source of truth in this target. There is no separate
imperative Controller, HAProxy tier, Prometheus/KEDA scaling loop, or shared
Worker Deployment in the design.

**The CRD, Operator, ingest integration, and per-stream Job reconciliation are
not implemented in this repository yet.** Their names above describe the fixed
target architecture only.

## Phase 1 foundation deployment

### Requirements

- Docker
- a local kind or Docker Desktop Kubernetes cluster that can use locally built
  Docker images
- `kubectl`
- `kind` when using a kind cluster

Deploy the resources that currently exist:

```sh
./tools/up.sh
```

For a kind cluster, the script also starts a local port forward. Publish to:

```text
rtmp://127.0.0.1:1935/live/{stream-key}
```

On Docker Desktop, inspect the local `proxy` LoadBalancer Service:

```sh
kubectl get service proxy -n media
```

Remote clusters are not supported by this Phase 1 script. The Proxy Deployment
uses a locally built image name with `imagePullPolicy: Never`. Docker Desktop
shares its local image store, while the script explicitly loads the Proxy image
into kind.

Remove the Phase 1 foundation deployment:

```sh
./tools/down.sh
```

These commands do not install a CRD or Operator and do not launch workers.
