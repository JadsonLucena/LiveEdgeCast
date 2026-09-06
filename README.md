# LiveEdgeCast

LiveEdgeCast contains the minimal foundation completed in **Phase 1**, the
declarative `LiveStream` API established in **Phase 2**, the namespaced
Operator, watch, RBAC, Deployment, and stateless reconstruction delivered in
**Phase 3**, lifecycle finalization from **Phase 4**, per-stream processing Jobs
from **Phase 5**, and the Worker's media health watchdog and Job-based failure
flow from **Phase 6**. **Phase 7** completes processing recovery after a Job
reaches terminal failure: according to the observed source availability, the
Operator either replaces the failed Job or records that processing was
interrupted.

The repository includes an RTMP Proxy, an FFmpeg-based Worker container image,
the `LiveStream` CRD, and the Operator. Ingest integration, handover, and
recovery from an interrupted source are not implemented yet.

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
- RTMP ingest integration, handover, and recovery after an unavailable source
  becomes available again.

The Operator continuously watches `LiveStream` resources and reconstructs its
observations by listing LiveStreams, Jobs, and Pods from the Kubernetes API. It
does not use a ConfigMap or in-memory state as a source of truth, so it can
resume reconciliation after a restart.

The manifests and scripts combine the completed foundation and API work from
Phases 1–2 with the Operator, finalization, Job reconciliation, and media health
watchdog delivered in Phases 3–7. They are not yet a production implementation
of the target design.

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

**Ingest integration, handover, and recovery from the `Interrupted` phase are
not implemented in this repository yet.**

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

### Worker media health watchdog

Each Worker receives `MEDIA_HEALTH_INTERVAL_SECONDS=10` from the Operator. The
value is a positive integer in seconds and controls how long FFmpeg may run
without reporting an increasing processed-media timestamp. Ten seconds is a
small default intended to detect a stalled source promptly; change the
`MEDIA_HEALTH_INTERVAL_SECONDS` constant in `docker/operator/src/jobs.py` when
a deployment needs a different tolerance. The interval is part of the Job
configuration identity, so after an Operator rollout it replaces Jobs created
with the previous interval instead of retaining their immutable Pod templates.

The Worker reads FFmpeg's dedicated `-progress` stream and refreshes its health
state only when `out_time_us` or `out_time_ms` increases. These processed-media
timestamps, rather than generic FFmpeg output or connection activity, are the
indicators of significant progress. If neither advances within the interval,
the watchdog logs the stall, sends FFmpeg `SIGTERM`, waits for a **5-second
grace period**, and uses `SIGKILL` only if FFmpeg is still running. The Worker
then waits for FFmpeg and exits non-zero, which makes the Pod `Failed`. It does
not restart FFmpeg or keep the container alive.

### Phase 7: terminal Job recovery

Phase 7 is complete. Its recovery responsibilities are deliberately divided
between the Worker, Kubernetes, and the Operator:

1. An FFmpeg failure makes the Worker container exit non-zero, causing its Pod
   to fail.
2. The Kubernetes Job Controller performs the short attempts for that same Job,
   up to its `backoffLimit`. An individual failed Pod therefore does **not**
   cause the Operator to replace the Job.
3. The Operator starts processing recovery only after observing the Job
   condition `type: Failed` with `status: "True"`; Pod failure alone is not a
   recovery signal.
4. Recovery is allowed only when the observation for the current source is
   explicitly `source.available: true`. The Operator then sets the stream to
   `Recovering` and requests deletion of the terminally failed Job.
5. The replacement is never created alongside the failed Job. Only after a
   reconciliation confirms that the failed Job has been deleted does the
   Operator create its replacement, during the transition from `Recovering`
   to `Provisioning`.
6. An explicitly unavailable source (`source.available: false`) moves the
   stream to `Interrupted`; the Operator retains the failed Job and performs no
   processing recovery. Missing or `null` availability is unknown and also
   cannot authorize recovery.

The complete implemented sequence is therefore **FFmpeg failure → failed Pod →
Job Controller retries up to `backoffLimit` → terminal Job `Failed=True` →
Operator chooses recovery from the observed source availability**. The Operator
does not poll the Worker, does not replace a Job because an individual Pod
failed, and does not replace the Job while the Job Controller is still
retrying.

To validate a stalled source manually, publish a stream and then leave its
connection open without producing more media. In another terminal, watch:

```sh
kubectl get pods -n media -w
```

After the configured interval, confirm that the Worker Pod becomes `Failed`
and that a new Pod is created for the **same Job** (the Pod's `job-name` label
remains unchanged). The Operator must not replace the Job before its terminal
`Failed` condition.
