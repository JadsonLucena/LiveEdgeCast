# LiveEdgeCast

LiveEdgeCast contains the minimal foundation completed in **Phase 1**, the
declarative `LiveStream` API established in **Phase 2**, and the namespaced
Operator, watch, RBAC, Deployment, and stateless reconstruction delivered in
**Phase 3**, lifecycle finalization from **Phase 4**, and per-stream processing
Jobs from **Phase 5**. The repository includes an RTMP Proxy, an FFmpeg-based
Worker container image, the `LiveStream` CRD, and the Operator. Ingest
integration and the remaining recovery/handover lifecycle are not implemented
yet.

## Current repository state

The deployable Kubernetes resources are limited to:

- the `media` namespace;
- an NGINX-RTMP Proxy Deployment;
- the singular `proxy` Service, of type `LoadBalancer`, exposed on TCP port 1935;
- the `LiveStream` CRD;
- the Operator ServiceAccount, Role, and RoleBinding;
- the single-replica `liveedgecast-operator` Deployment.

The proxy accepts and serves RTMP streams. For each `LiveStream` requiring
processing, the Operator creates one owned Kubernetes Job that runs the Worker.
The Job Controller retries failed Pods within its small retry budget; the
Operator reacts only after the Job becomes terminally failed. Changes to the
stream key or source/target URLs are detected as desired-configuration drift;
the Operator then replaces the immutable Job rather than leaving it bound to
stale Pod environment values.

The following are deliberately absent:

- the former imperative Controller lifecycle API;
- HAProxy-based routing;
- KEDA scaling and Prometheus metrics;
- a shared Worker Deployment or Service; and
- RTMP ingest integration and the remaining recovery/handover behavior.

The Operator continuously watches `LiveStream` resources and reconstructs its
observations by listing LiveStreams, Jobs, and Pods from the Kubernetes API. It
does not use a ConfigMap or in-memory state as a source of truth, so it can
resume reconciliation after a restart.

The manifests and scripts combine the completed foundation and API work from
Phases 1–2 with the Operator, finalization, and Job reconciliation delivered in
Phases 3–5. They are not yet a production implementation of the target design.

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

**Ingest integration and the complete recovery/handover lifecycle are not
implemented in this repository yet.**

## Current foundation, API, and Operator deployment

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

The script builds the Proxy, Operator, and Worker images, loads them into kind
when needed, applies the manifests, and waits for both Deployments. For a kind
cluster, it also starts a local port forward. Publish to:

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
all three images into kind.

Follow the Operator logs with:

```sh
kubectl logs deployment/liveedgecast-operator -n media -f
```

Remove the current deployment:

```sh
./tools/down.sh
```

Applying `k8s/` installs the `LiveStream` CRD, Operator RBAC, and Operator
Deployment, but does not create any `LiveStream` instances or provide ingest
integration. A manually created `LiveStream` is reconciled into an owned
per-stream Job.
