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


@dataclass(frozen=True)
class ReconcileObservations:
    """All API facts used by the lifecycle decision."""

    source: dict[str, Any]
    owned_jobs: tuple[Any, ...]
    selected_job: jobs.JobObservation | None
    pod_phase: str | None
    pod_ready: bool


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


def _observe(current: dict, batch_api: Any, core_api: Any) -> ReconcileObservations:
    """Read a complete, immutable snapshot from the current LiveStream."""
    namespace = current["metadata"]["namespace"]
    source_observation = source.observe(current)
    owned_jobs = _list_owned_jobs(batch_api, namespace, current)
    desired_session_id = current.get("spec", {}).get("source", {}).get("sessionId")
    observed_jobs = tuple((job, jobs.observe(job)) for job in owned_jobs)
    selected = next(
        (
            (job, observation)
            for job, observation in observed_jobs
            if observation.bound_source_session_id == desired_session_id
        ),
        None,
    )
    pod_phase, pod_ready = _pod_phase(
        core_api, namespace, selected[0] if selected else None
    )
    return ReconcileObservations(
        source=source_observation,
        owned_jobs=tuple(owned_jobs),
        selected_job=selected[1] if selected else None,
        pod_phase=pod_phase,
        pod_ready=pod_ready,
    )


def decide_lifecycle(observed: ReconcileObservations) -> LifecycleDecision:
    """Derive lifecycle solely from the supplied current observations."""
    source_available = observed.source.get("available")
    if source_available is False and (observed.selected_job or observed.owned_jobs):
        phase = "Interrupted"
    elif not observed.selected_job and observed.owned_jobs:
        phase = "Handover"
    elif not observed.selected_job:
        phase = "Registered"
    elif observed.selected_job.phase == "Failed":
        phase = "Recovering"
    elif observed.selected_job.phase == "Succeeded":
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


def _finalize(current: dict, custom_api: Any, batch_api: Any) -> None:
    """Request dependant deletion and release only after it has converged."""
    metadata = current["metadata"]
    finalizers = metadata.get("finalizers") or []
    if FINALIZER not in finalizers:
        return
    owned_jobs = _list_owned_jobs(batch_api, metadata["namespace"], current)
    for job in owned_jobs:
        try:
            batch_api.delete_namespaced_job(
                name=job.metadata.name,
                namespace=metadata["namespace"],
                propagation_policy="Foreground",
            )
        except ApiException as error:
            if not _is_not_found(error):
                raise
    # Foreground deletion is asynchronous. Keep our finalizer until a later
    # reconciliation verifies that every owned Job and its Pods have gone.
    if owned_jobs:
        return
    try:
        _patch_finalizers(
            custom_api, current, [item for item in finalizers if item != FINALIZER]
        )
    except ApiException as error:
        if not _is_not_found(error):
            raise


def _execute(decision: LifecycleDecision) -> None:
    """Execute the one action selected by the pure lifecycle decision."""
    if decision.action is not LifecycleAction.NONE:
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
        _finalize(current, custom_api, batch_api)
        return

    finalizers = current_metadata.get("finalizers") or []
    if FINALIZER not in finalizers:
        _patch_finalizers(custom_api, current, [*finalizers, FINALIZER])
        return

    observed = _observe(current, batch_api, core_api)
    decision = decide_lifecycle(observed)
    _execute(decision)

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
    if observed.selected_job:
        calculated["job"] = {
            "name": observed.selected_job.name,
            "phase": observed.selected_job.phase,
        }
        if observed.selected_job.bound_source_session_id:
            calculated["job"]["boundSourceSessionId"] = (
                observed.selected_job.bound_source_session_id
            )

    changed = status.patch_if_changed(custom_api, namespace, name, current, calculated)
    LOGGER.info(
        "reconciled LiveStream %s/%s (phase=%s, action=%s, status_changed=%s)",
        namespace,
        name,
        decision.phase,
        decision.action.value,
        changed,
    )
