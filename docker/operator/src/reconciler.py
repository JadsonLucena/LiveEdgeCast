"""Stateless reconciliation of LiveStream observations."""

import logging
from datetime import datetime, timezone
from typing import Any

from kubernetes.client.exceptions import ApiException

import jobs
import source
import status

LOGGER = logging.getLogger(__name__)


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


def _pod_phase(
    core_api: Any, namespace: str, job: Any | None
) -> tuple[str | None, bool]:
    if job is None:
        return None, False
    pods = core_api.list_namespaced_pod(
        namespace=namespace,
        label_selector=f"job-name={job.metadata.name}",
    ).items
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


def reconcile(resource: dict, custom_api: Any, batch_api: Any, core_api: Any) -> None:
    """Rebuild status from current API objects; no in-memory state is authoritative."""
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
        if error.status == 404:
            return
        raise

    owned_jobs = jobs.list_for_livestream(batch_api, namespace, current)
    desired_session_id = current.get("spec", {}).get("source", {}).get("sessionId")
    observed_jobs = [(job, jobs.observe(job)) for job in owned_jobs]
    current_job = next(
        (
            (job, observation)
            for job, observation in observed_jobs
            if observation.bound_source_session_id == desired_session_id
        ),
        None,
    )
    job_observation = current_job[1] if current_job else None
    pod_phase, pod_ready = _pod_phase(
        core_api, namespace, current_job[0] if current_job else None
    )
    source_observation = source.observe(current)
    source_available = source_observation.get("available")
    generation = current.get("metadata", {}).get("generation", 0)

    if source_available is False and (job_observation or owned_jobs):
        phase = "Interrupted"
    elif not job_observation and owned_jobs:
        phase = "Handover"
    elif not job_observation:
        phase = "Registered"
    elif job_observation.phase == "Failed":
        phase = "Recovering"
    elif job_observation.phase == "Succeeded":
        phase = "Stopping"
    elif pod_ready:
        phase = "Streaming"
    elif pod_phase == "Running":
        phase = "Starting"
    else:
        phase = "Provisioning"

    calculated: dict[str, Any] = {
        "observedGeneration": generation,
        "phase": phase,
        "source": source_observation,
        "processing": {"healthy": pod_ready},
        "conditions": [_condition(current, generation, source_available)],
    }
    if job_observation:
        calculated["job"] = {
            "name": job_observation.name,
            "phase": job_observation.phase,
        }
        if job_observation.bound_source_session_id:
            calculated["job"]["boundSourceSessionId"] = (
                job_observation.bound_source_session_id
            )

    changed = status.patch_if_changed(custom_api, namespace, name, current, calculated)
    LOGGER.info(
        "reconciled LiveStream %s/%s (status_changed=%s)", namespace, name, changed
    )
