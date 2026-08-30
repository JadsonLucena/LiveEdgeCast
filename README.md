# LiveEdgeCast

LiveEdgeCast contains the minimal foundation completed in **Phase 1**, the
declarative `LiveStream` API established in **Phase 2**, and the namespaced
Operator, watch, RBAC, Deployment, and stateless reconstruction delivered in
**Phase 3**. The repository includes an RTMP Proxy, an FFmpeg-based Worker
container image, the `LiveStream` CRD, and the Operator. Phase 3 provides the
reconciliation foundation, but complete Job creation, finalization, ingest
integration, and end-to-end processing are not implemented yet.

## Current repository state

The deployable Kubernetes resources are limited to:

- the `media` namespace;
- an NGINX-RTMP Proxy Deployment;
- the singular `proxy` Service, of type `LoadBalancer`, exposed on TCP port 1935;
- the `LiveStream` CRD;
- the Operator ServiceAccount, Role, and RoleBinding;
- the single-replica `liveedgecast-operator` Deployment.

The proxy accepts and serves RTMP streams. Nothing in the current application
creates per-stream workloads, persists stream ownership, reacts to stream
lifecycle callbacks, or performs autoscaling. The worker image is retained as a
building block, but no Kubernetes resource launches it.

The following are deliberately absent:

- the former imperative Controller lifecycle API;
- HAProxy-based routing;
- KEDA scaling and Prometheus metrics;
- a shared Worker Deployment or Service; and
- RTMP ingest integration and complete processing through per-stream Jobs.

The Operator continuously watches `LiveStream` resources and reconstructs its
observations by listing LiveStreams, Jobs, and Pods from the Kubernetes API. It
does not use a ConfigMap or in-memory state as a source of truth, so it can
resume reconciliation after a restart.

The manifests and scripts combine the completed Phase 1 foundation, the Phase 2
API definition, and the Phase 3 namespaced Operator foundation; they are not a
production implementation of the target design.

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

**Ingest integration and complete per-stream Job reconciliation are not
implemented in this repository yet.** The current Operator observes API state
and updates status, but does not yet provide the full processing lifecycle.

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

The script builds both the Proxy and Operator images, loads them into kind when
needed, applies the manifests, and waits for both Deployments. For a kind
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
both images into kind.

Follow the Operator logs with:

```sh
kubectl logs deployment/liveedgecast-operator -n media -f
```

Remove the current deployment:

```sh
./tools/down.sh
```

Applying `k8s/` installs the `LiveStream` CRD, Operator RBAC, and Operator
Deployment, but does not create any `LiveStream` instances, provide ingest
integration, or complete the creation and processing of per-stream Jobs.
Consequently, it does not yet launch Worker containers as a full stream
processing flow.
