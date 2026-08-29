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

The CRD schema validates the possible `status.phase` values. A future Operator
will be responsible for applying the transitions below. This API definition
does not implement the Operator, per-stream Jobs, or Proxy callbacks.

## Fixed transitions

- Creation after `publication_started` begins in `Registered`.
- `Registered` → `Provisioning` → `Starting` → `Streaming`.
- A terminal Job failure while the source is available follows
  `Provisioning` | `Starting` | `Streaming` → `Recovering` → `Provisioning`.
- Loss of the source follows `Registered` | `Provisioning` | `Starting` |
  `Streaming` | `Recovering` → `Interrupted`.
- Reconnection within the TTL follows `Interrupted` → `Handover` →
  `Provisioning`.
- An expired TTL or exhausted retry limit follows `Interrupted` → `Stopping`.
- A source change follows `Starting` | `Streaming` | `Recovering` →
  `Handover` → `Provisioning`.
- The end of the current publication follows `Registered` | `Provisioning` |
  `Starting` | `Streaming` | `Recovering` | `Handover` → `Stopping`.
- Finalization removes the `LiveStream` resource.
