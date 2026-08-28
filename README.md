# LiveEdgeCast

LiveEdgeCast is in a **cleanup phase**. This repository currently contains only a
minimal RTMP proxy, a health-only placeholder service, a worker container image,
and Kubernetes manifests for the proxy and placeholder. It does not currently
implement stream orchestration or serverless retransmission.

## Current repository state

The deployable Kubernetes resources are limited to:

- the `media` namespace;
- an NGINX-RTMP proxy Deployment and its public and internal Services; and
- a health-only placeholder Deployment and ServiceAccount.

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

The manifests and scripts are cleanup-phase scaffolding, not a production
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

## Cleanup-phase deployment

### Requirements

- Docker
- a Kubernetes cluster
- `kubectl`
- `kind` when the active cluster is a kind cluster

Deploy the resources that currently exist:

```sh
./tools/up.sh
```

For a kind cluster, the script also starts a local port forward. Publish to:

```text
rtmp://127.0.0.1:1935/live/{stream-key}
```

On another cluster, inspect the `proxy` LoadBalancer Service for its external
address:

```sh
kubectl get service proxy -n media
```

Remove the cleanup-phase deployment:

```sh
./tools/down.sh
```

These commands do not install a CRD or Operator and do not launch workers.
