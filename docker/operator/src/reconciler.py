"""Stateless reconciliation of LiveStream observations."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from kubernetes.client.exceptions import ApiException

import jobs
import source
import status

LOGGER = logging.getLogger(__name__)
FINALIZER = "liveedgecast.io/stream-cleanup"


class LifecycleAction(Enum):
    """One operation selected from the current API observations."""

    NONE = "none"
    CREATE_JOB = "create_job"
    DELETE_JOBS = "delete_jobs"
    DELETE_FAILED_JOB = "delete_failed_job"


@dataclass(frozen=True)
class ReconcileObservations:
    """All API facts used by the lifecycle decision."""

    source: dict[str, Any]
    owned_jobs: tuple[Any, ...]
    selected_job: Any | None
    selected_job_observation: jobs.JobObservation | None
    pod_phase: str | None
    pod_ready: bool
    previous_phase: str | None


@dataclass(frozen=True)
class LifecycleDecision:
    """Domain result and, at most, its single required operation."""

    phase: str
    action: LifecycleAction = LifecycleAction.NONE


def _is_not_found(error: ApiException) -> bool:
    return error.status == 404


def _condition(current: dict, generation: int, available: bool | None) -> dict:
    if available is True:
        condition_status = "True"
        reason = "SourceObserved"
        message = "The current source publication was observed as available."
    elif available is False:
        condition_status = "False"
        reason = "SourceUnavailable"
        message = "The Proxy reported that the current source is unavailable."
    else:
        condition_status = "Unknown"
        reason = "AwaitingSourceObservation"
        message = "Waiting for the Proxy to report source availability."
    previous = next(
        (
            item
            for item in current.get("status", {}).get("conditions", [])
            if item.get("type") == "SourceAvailable"
        ),
        None,
    )
    unchanged = previous and all(
        previous.get(key) == value
        for key, value in {
            "status": condition_status,
            "reason": reason,
            "message": message,
        }.items()
    )
    transitioned_at = (
        previous.get("lastTransitionTime")
        if unchanged and previous.get("lastTransitionTime")
        else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    return {
        "type": "SourceAvailable",
        "status": condition_status,
        "reason": reason,
        "message": message,
        "lastTransitionTime": transitioned_at,
        "observedGeneration": generation,
    }


def _list_owned_jobs(batch_api: Any, namespace: str, current: dict) -> list[Any]:
    try:
        return jobs.list_for_livestream(batch_api, namespace, current)
    except ApiException as error:
        if _is_not_found(error):
            return []
        raise


def _pod_phase(
    core_api: Any, namespace: str, job: Any | None
) -> tuple[str | None, bool]:
    if job is None:
        return None, False
    try:
        pods = core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"job-name={job.metadata.name}",
        ).items
    except ApiException as error:
        if _is_not_found(error):
            return None, False
        raise
    selected = [
        pod
        for pod in pods
        if any(
            owner.kind == "Job"
            and owner.name == job.metadata.name
            and owner.uid == job.metadata.uid
            for owner in (pod.metadata.owner_references or [])
        )
    ]
    if not selected:
        return None, False
    pod = max(selected, key=lambda item: item.metadata.creation_timestamp)
    ready = any(
        condition.type == "Ready" and condition.status == "True"
        for condition in (pod.status.conditions or [])
    )
    return pod.status.phase, ready


def _list_processing_pods(
    core_api: Any,
    namespace: str,
    current: dict,
    owned_job_ids: set[tuple[str, str]],
) -> list[Any]:
    """Return labelled Pods controlled by current or previously validated Jobs."""
    stream_name = current.get("metadata", {}).get("name")
    if not stream_name or not owned_job_ids:
        return []
    try:
        candidates = core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"{jobs.LIVESTREAM_LABEL}={stream_name}",
        ).items
    except ApiException as error:
        if _is_not_found(error):
            return []
        raise
    return [
        pod
        for pod in candidates
        if any(
            owner.api_version == "batch/v1"
            and owner.kind == "Job"
            and (owner.name, owner.uid) in owned_job_ids
            for owner in (pod.metadata.owner_references or [])
        )
    ]


def _observe(current: dict, batch_api: Any, core_api: Any) -> ReconcileObservations:
    """Read a complete, immutable snapshot from the current LiveStream."""
    namespace = current["metadata"]["namespace"]
    source_observation = source.observe(current)
    owned_jobs = _list_owned_jobs(batch_api, namespace, current)
    desired_session_id = current.get("spec", {}).get("source", {}).get("sessionId")
    desired_configuration_id = jobs.configuration_id(current)
    observed_jobs = tuple((job, jobs.observe(job)) for job in owned_jobs)
    selected = next(
        (
            (job, observation)
            for job, observation in observed_jobs
            if observation.bound_source_session_id == desired_session_id
            and observation.configuration_id == desired_configuration_id
        ),
        None,
    )
    pod_phase, pod_ready = _pod_phase(
        core_api, namespace, selected[0] if selected else None
    )
    return ReconcileObservations(
        source=source_observation,
        owned_jobs=tuple(owned_jobs),
        selected_job=selected[0] if selected else None,
        selected_job_observation=selected[1] if selected else None,
        pod_phase=pod_phase,
        pod_ready=pod_ready,
        previous_phase=current.get("status", {}).get("phase"),
    )


def decide_lifecycle(observed: ReconcileObservations) -> LifecycleDecision:
    """Derive lifecycle solely from the supplied current observations."""
    source_available = observed.source.get("available")
    selected_job = observed.selected_job_observation
    if source_available is False and (selected_job or observed.owned_jobs):
        phase = "Interrupted"
    elif not selected_job and observed.owned_jobs:
        return LifecycleDecision(phase="Handover", action=LifecycleAction.DELETE_JOBS)
    elif not selected_job:
        # With no workload to disambiguate the context, the phase persisted in
        # the status subresource is itself part of the Kubernetes observation.
        # In particular, an Interrupted stream must not look like a new stream
        # merely because its Job has already disappeared.
        if observed.previous_phase == "Interrupted":
            return LifecycleDecision(phase="Interrupted")
        if observed.previous_phase == "Recovering":
            if source_available is True:
                return LifecycleDecision(
                    phase="Provisioning", action=LifecycleAction.CREATE_JOB
                )
            return LifecycleDecision(
                phase=("Interrupted" if source_available is False else "Recovering")
            )
        if observed.previous_phase in {"Registered", "Provisioning", "Handover"}:
            if source_available is False:
                return LifecycleDecision(phase="Interrupted")
            return LifecycleDecision(
                phase="Provisioning", action=LifecycleAction.CREATE_JOB
            )
        if observed.previous_phase in {"Starting", "Streaming"}:
            return LifecycleDecision(
                phase=("Interrupted" if source_available is False else "Recovering")
            )
        # Persist the initial Registered state before provisioning. This makes
        # a subsequent reconcile reconstruct the CREATE_JOB decision without
        # relying on process-local counters or sequencing.
        return LifecycleDecision(
            phase="Registered",
        )
    elif selected_job.phase == "Failed":
        return LifecycleDecision(
            phase="Recovering",
            action=(
                LifecycleAction.DELETE_FAILED_JOB
                if source_available is True
                else LifecycleAction.NONE
            ),
        )
    elif selected_job.phase == "Succeeded":
        phase = "Stopping"
    elif observed.pod_ready:
        phase = "Streaming"
    elif observed.pod_phase == "Running":
        phase = "Starting"
    else:
        phase = "Provisioning"
    return LifecycleDecision(phase=phase)


def _patch_finalizers(custom_api: Any, current: dict, finalizers: list[str]) -> None:
    """Replace finalizers only if the observed resource version is current."""
    metadata = current["metadata"]
    custom_api.patch_namespaced_custom_object(
        group="liveedgecast.io",
        version="v1alpha1",
        namespace=metadata["namespace"],
        plural="livestreams",
        name=metadata["name"],
        body={
            "metadata": {
                "resourceVersion": metadata["resourceVersion"],
                "finalizers": finalizers,
            }
        },
        _content_type="application/merge-patch+json",
    )


def _validated_cleanup_job_ids(
    current: dict, owned_jobs: list[Any]
) -> set[tuple[str, str]]:
    """Combine current Job identities with those persisted during cleanup."""
    persisted = current.get("status", {}).get("cleanup", {}).get("jobs", [])
    job_ids = {
        (item.get("name"), item.get("uid"))
        for item in persisted
        if item.get("name") and item.get("uid")
    }
    job_ids.update(
        (job.metadata.name, job.metadata.uid)
        for job in owned_jobs
        if job.metadata.name and job.metadata.uid
    )
    return job_ids


def _stopping_status(
    current: dict, owned_job_ids: set[tuple[str, str]]
) -> tuple[dict, bool]:
    """Preserve observations and validated Job identities during cleanup."""
    calculated = dict(current.get("status") or {})
    calculated["phase"] = "Stopping"
    calculated["cleanup"] = {
        "jobs": [{"name": name, "uid": uid} for name, uid in sorted(owned_job_ids)]
    }
    conditions = [dict(item) for item in calculated.get("conditions", [])]
    already_observed = any(
        item.get("type") == "CleanupPending"
        and item.get("message") == current["metadata"].get("deletionTimestamp")
        for item in conditions
    )
    if not already_observed:
        generation = calculated.get(
            "observedGeneration", current["metadata"].get("generation", 0)
        )
        conditions = [
            item for item in conditions if item.get("type") != "CleanupPending"
        ]
        conditions.append(
            {
                "type": "CleanupPending",
                "status": "True",
                "reason": "ProcessingDeletionRequested",
                "message": current["metadata"].get("deletionTimestamp"),
                "lastTransitionTime": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "observedGeneration": generation,
            }
        )
        calculated["conditions"] = conditions
    return calculated, already_observed


def _finalize(current: dict, custom_api: Any, batch_api: Any, core_api: Any) -> None:
    """Request dependant deletion and release only after it has converged."""
    metadata = current["metadata"]
    finalizers = metadata.get("finalizers") or []
    if FINALIZER not in finalizers:
        return
    namespace = metadata["namespace"]
    owned_jobs = _list_owned_jobs(batch_api, metadata["namespace"], current)
    owned_job_ids = _validated_cleanup_job_ids(current, owned_jobs)
    processing_pods = _list_processing_pods(core_api, namespace, current, owned_job_ids)

    calculated, cleanup_was_previously_observed = _stopping_status(
        current, owned_job_ids
    )
    status.patch_if_changed(
        custom_api, namespace, metadata["name"], current, calculated
    )

    for job in owned_jobs:
        jobs.delete_for_livestream(batch_api, namespace, current, job)
    # Job deletion is asynchronous and Pods can outlive their Job observation.
    # Keep our finalizer until both the validated Jobs and their Pods are gone.
    if owned_jobs or processing_pods or not cleanup_was_previously_observed:
        return
    try:
        _patch_finalizers(
            custom_api, current, [item for item in finalizers if item != FINALIZER]
        )
    except ApiException as error:
        if not _is_not_found(error):
            raise


def _execute(
    decision: LifecycleDecision,
    current: dict,
    batch_api: Any,
    observed: ReconcileObservations | None = None,
) -> None:
    """Execute the one action selected by the pure lifecycle decision."""
    if decision.action is LifecycleAction.CREATE_JOB:
        metadata = current["metadata"]
        jobs.create_for_livestream(batch_api, metadata["namespace"], current)
    elif decision.action is LifecycleAction.DELETE_FAILED_JOB:
        if observed is None or observed.selected_job is None:
            raise ValueError("failed Job deletion requires its observed resource")
        metadata = current["metadata"]
        jobs.delete_for_livestream(
            batch_api, metadata["namespace"], current, observed.selected_job
        )
    elif decision.action is LifecycleAction.DELETE_JOBS:
        metadata = current["metadata"]
        for job in jobs.list_for_livestream(batch_api, metadata["namespace"], current):
            jobs.delete_for_livestream(batch_api, metadata["namespace"], current, job)
    elif decision.action is not LifecycleAction.NONE:
        raise ValueError(f"unsupported lifecycle action: {decision.action}")


def reconcile(resource: dict, custom_api: Any, batch_api: Any, core_api: Any) -> None:
    """Rebuild lifecycle from current API state without cross-run memory."""
    metadata = resource.get("metadata", {})
    namespace, name = metadata["namespace"], metadata["name"]
    try:
        current = custom_api.get_namespaced_custom_object(
            group="liveedgecast.io",
            version="v1alpha1",
            namespace=namespace,
            plural="livestreams",
            name=name,
        )
    except ApiException as error:
        if _is_not_found(error):
            return
        raise

    current_metadata = current.get("metadata", {})
    if current_metadata.get("deletionTimestamp"):
        _finalize(current, custom_api, batch_api, core_api)
        return

    finalizers = current_metadata.get("finalizers") or []
    if FINALIZER not in finalizers:
        _patch_finalizers(custom_api, current, [*finalizers, FINALIZER])
        return

    observed = _observe(current, batch_api, core_api)
    decision = decide_lifecycle(observed)
    _execute(decision, current, batch_api, observed)

    generation = current_metadata.get("generation", 0)
    calculated: dict[str, Any] = {
        "observedGeneration": generation,
        "phase": decision.phase,
        "source": observed.source,
        "processing": {"healthy": observed.pod_ready},
        "conditions": [
            _condition(current, generation, observed.source.get("available"))
        ],
    }
    if observed.selected_job_observation:
        selected_job = observed.selected_job_observation
        calculated["job"] = {
            "name": selected_job.name,
            "phase": selected_job.phase,
        }
        if selected_job.bound_source_session_id:
            calculated["job"][
                "boundSourceSessionId"
            ] = selected_job.bound_source_session_id

    changed = status.patch_if_changed(custom_api, namespace, name, current, calculated)
    LOGGER.info(
        "reconciled LiveStream %s/%s (phase=%s, action=%s, status_changed=%s)",
        namespace,
        name,
        decision.phase,
        decision.action.value,
        changed,
    )
