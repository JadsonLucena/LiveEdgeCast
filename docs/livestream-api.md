# LiveStream API lifecycle

`LiveStream` is the declarative record for one live publication. Its presence
means that the publication is being managed; its absence represents an offline
stream. There is deliberately no `Offline` phase.

## API fields

- `spec` is the desired configuration supplied by the ingest integration. It
  identifies the stream, its current source, its target, and its recovery
  policy.
- `spec.source.available` is set by the Proxy for the registered publication;
  the Operator reflects this desired-source fact into its own status without
  requiring the Proxy to write the status subresource.
- `spec.target.url` is the complete destination for this publication. For new
  resources, the Proxy constructs it exclusively from its explicit
  `RTMP_TARGET_BASE_URL` configuration and the URL-encoded stream key. The
  Proxy validates the resulting `rtmp://` or `rtmps://` URL before its first
  Kubernetes API request and rejects the publication with a clear hook log if
  no valid destination can be determined. It does not query a legacy
  Controller or an external database.
- `spec.target.baseUrlSecretRef` remains accepted for existing declarative
  resources and is resolved by the Worker, but it is not used by the Proxy
  when creating a `LiveStream`.
- On reconnection, the Proxy merge-patches only `spec.source`; the existing
  `spec.target` and `spec.recoveryPolicy` remain untouched.
- `status` is the state observed while reconciling that desired configuration.
  It contains the lifecycle phase and observations about the source, Job,
  processing health, interruption, and conditions.
- `sessionId` identifies one specific source publication. A new publication or
  reconnection has a new session ID, allowing events for an older publication
  to be recognized as stale. `spec.source.sessionId` is the desired source;
  session IDs under `status` record the source observed or bound to a Job.

The CRD schema validates the possible `status.phase` values. The current
Operator implements creation, processing, terminal-Job recovery, interruption
when the source is unavailable, stopping, and finalization as described below.

## Implemented finite-state machine

The lifecycle decision applies the following transitions in order. The order is
significant because source unavailability takes precedence over the state of an
existing Job.

| Current observation | Required source observation | Result | Operator action |
| --- | --- | --- | --- |
| Any selected or owned Job exists | `available: false` | `Interrupted` | None; retain the Job |
| Owned Jobs exist, but none matches the desired session and configuration | Not `false` | `Handover` | Delete all owned Jobs |
| No Job, persisted `Interrupted` or `Stopping` | Any | Preserve the persisted phase | None |
| No Job, persisted `Recovering` | `available: true` | `Provisioning` | Create the replacement Job |
| No Job, persisted `Recovering` | `available: false` | `Interrupted` | None |
| No Job, persisted `Recovering` | Missing or `null` | Preserve `Recovering` | None; await a definitive source observation |
| No Job, persisted `Registered`, `Provisioning`, or `Handover` | Not `false` | `Provisioning` | Create the Job |
| No Job, persisted `Registered`, `Provisioning`, or `Handover` | `available: false` | `Interrupted` | None |
| No Job, persisted `Starting` or `Streaming` | `available: true` | `Recovering` | None |
| No Job, persisted `Starting` or `Streaming` | `available: false` | `Interrupted` | None |
| No Job, persisted `Starting` or `Streaming` | Missing or `null` | Preserve the persisted phase | None |
| No Job and no recognized persisted phase | Any | `Registered` | None |
| Selected Job has terminal condition `Failed=True` | `available: true` | `Recovering` | Delete that failed Job |
| Selected Job has terminal condition `Failed=True` | `available: false` | `Interrupted` | None; retain the failed Job |
| Selected Job has terminal condition `Failed=True`, not already `Recovering` | Missing or `null` | `Interrupted` | None; retain the failed Job |
| Selected Job has terminal condition `Failed=True`, persisted `Recovering` | Missing or `null` | Preserve `Recovering` | None; foreground deletion was already requested |
| Selected Job has terminal condition `Complete=True` | Not `false` | `Stopping` | None |
| Selected Job and newest owned Pod is Ready | Not `false` | `Streaming` | None |
| Selected Job and newest owned Pod is Running but not Ready | Not `false` | `Starting` | None |
| Selected Job is otherwise pending | Not `false` | `Provisioning` | None |

An FFmpeg failure exits the Worker container non-zero and fails its Pod. Short
retries belong exclusively to the Kubernetes Job Controller, which creates new
Pod attempts for the **same Job** until `backoffLimit` is reached. An individual
Pod failure never causes the Operator to replace the Job. The only processing
failure signal that starts Operator recovery is a Job condition whose `type` is
`Failed` and whose `status` is `"True"`.

Recovery additionally requires an explicit observation of
`source.available: true` for the desired proxy and session. In that case, the
Operator records `Recovering` and requests deletion of the failed Job. Job
deletion is foreground and asynchronous, so reconciliation continues to retain
`Recovering` and request deletion while that Job remains observable. The
Operator creates no replacement until a later observation contains no failed
Job; it then creates the replacement as part of the transition to
`Provisioning`.

An explicit `source.available: false` leads a failed-Job recovery to
`Interrupted`. An unconfirmed availability (absent or `null`) also leads to
`Interrupted` when recovery has not started, retaining the failed Job and
creating no replacement. If foreground deletion was already requested from an
explicitly available observation, however, persisted `Recovering` is retained
across a temporary unknown observation—even after the failed Job disappears—so
a later `available: true` can safely provision the replacement. For an
unconfirmed source, the Operator publishes a `SourceAvailable` condition with
status `Unknown` and reason `AwaitingSourceObservation`.

A completed Job records `Stopping`. Deletion finalization independently records
`Stopping`, removes Jobs and processing Pods owned by the `LiveStream`, and
only then removes its finalizer.

The Operator reconstructs these decisions from the persisted `status.phase`
together with current Source, Job, and Pod observations. In particular, a
Job-less `Recovering` stream provisions a replacement only when its source is
explicitly available and otherwise retains that persisted checkpoint unless
the source is explicitly unavailable. A Job-less stream in `Interrupted` or
`Stopping` remains in that phase. No process-local lifecycle counter or
sequence is used.

## Not implemented yet

The phase enum also reserves states needed by the target lifecycle, but their
presence in the API is not a claim that their workflows are complete. In
particular, the current implementation does **not** yet provide:

- handover for a changed source or session;
- reconnection and recovery from `Interrupted` through `Handover`;
- interruption TTL expiry; or
- a policy that moves an interrupted stream to `Stopping` after recovery
  attempts are exhausted.

These remain future transitions and must not be relied upon as current Operator
behavior.
