# LiveEdgeCast

LiveEdgeCast contains the minimal foundation completed in **Phase 1** and the
declarative `LiveStream` API established in **Phase 2**. The repository includes
an RTMP Proxy, an FFmpeg-based Worker container image, and the `LiveStream` CRD.
It does not yet include ingest integration, an Operator, or per-stream Jobs, so
stream orchestration and serverless retransmission are not implemented.

## Current repository state

The deployable Kubernetes resources are limited to:

- the `media` namespace;
- an NGINX-RTMP Proxy Deployment;
- the singular `proxy` Service, of type `LoadBalancer`, exposed on TCP port 1935;
- the `LiveStream` CRD.

The proxy accepts and serves RTMP streams. Nothing in the current application
creates per-stream workloads, persists stream ownership, reacts to stream
lifecycle callbacks, or performs autoscaling. The worker image is retained as a
building block, but no Kubernetes resource launches it.

The following are deliberately absent:

- the former imperative Controller lifecycle API;
- HAProxy-based routing;
- KEDA scaling and Prometheus metrics;
- a shared Worker Deployment or Service; and
- an Operator, RTMP ingest integration, and per-stream Jobs.

The repository does not retain a health-check application as a placeholder for
the future Operator. The Operator will be introduced directly in its
corresponding implementation phase.

The manifests and scripts combine the completed Phase 1 foundation with the
Phase 2 API definition; they are not a production implementation of the target
design.

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

**The Operator, ingest integration, and per-stream Job reconciliation are not
implemented in this repository yet.** Their names above describe the fixed
target architecture only.

## Current foundation and API deployment

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

Remote clusters are not supported by this local deployment script. The Proxy
Deployment uses a locally built image name with `imagePullPolicy: Never`.
Docker Desktop shares its local image store, while the script explicitly loads
the Proxy image into kind.

Remove the current deployment:

```sh
./tools/down.sh
```

Applying `k8s/` installs the `LiveStream` CRD, but does not create any
`LiveStream` instances, install an Operator, provide ingest integration, or
create per-stream Jobs. Consequently, it does not launch Worker containers.
