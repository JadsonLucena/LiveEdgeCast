# LiveStream API lifecycle

`LiveStream` is the declarative record for one live publication. Its presence
means that the publication is being managed; its absence represents an offline
stream. There is deliberately no `Offline` phase.

## API fields

- `spec` is the desired configuration supplied by the ingest integration. It
  identifies the stream, its current source, its target, and its recovery
  policy.
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

## Implemented processing sequence

- A new resource is first recorded as `Registered`; a subsequent reconciliation
  creates its Job and records `Provisioning`.
- A pending Job remains `Provisioning`. A running Pod moves the stream to
  `Starting`, and a Ready Pod moves it to `Streaming`.
- An FFmpeg failure exits the Worker container non-zero and fails the Pod. The
  Job Controller, not the Operator, creates the short Pod attempts for the same
  Job until its `backoffLimit` is reached. Individual Pod failures do not
  replace the Job.
- The Operator recognizes processing failure only from a Job condition with
  `type: Failed` and `status: "True"`. It does not infer terminal failure from a
  failed Pod.
- When that terminal Job failure is observed and the current source is
  available, the Operator records `Recovering` and deletes the failed Job. A
  following reconciliation observes the Job-less `Recovering` state, creates a
  replacement Job, and records `Provisioning`.
- When the terminal Job failure is observed and the current source is
  unavailable, the Operator records `Interrupted`, does not delete the failed
  Job, and does not run processing recovery.
- A source reported unavailable while a processing Job exists also records
  `Interrupted`; source availability therefore takes precedence over
  terminal-Job replacement.
- A completed Job records `Stopping`. Deletion finalization removes Jobs and
  processing Pods owned by the `LiveStream` before removing its finalizer.

The Operator reconstructs these decisions from the persisted `status.phase`
together with current Source, Job, and Pod observations. In particular, a
Job-less `Recovering` stream provisions a replacement only when its source is
explicitly available, while a Job-less stream in `Interrupted` or `Stopping`
remains in that phase. No process-local lifecycle counter or sequence is used.

## Not implemented yet

The phase enum also reserves states needed by the target lifecycle, but their
presence in the API is not a claim that their workflows are complete. In
particular, the current implementation does **not** yet provide:

- ingest callbacks that create, update, or remove `LiveStream` resources;
- handover for a changed source or session;
- reconnection and recovery from `Interrupted` through `Handover`;
- interruption TTL expiry; or
- a policy that moves an interrupted stream to `Stopping` after recovery
  attempts are exhausted.

These remain future transitions and must not be relied upon as current Operator
behavior.
