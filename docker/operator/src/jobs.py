"""Read-only observations of Jobs owned by a LiveStream."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobObservation:
    name: str
    phase: str
    bound_source_session_id: str | None


def list_for_livestream(batch_api: Any, namespace: str, livestream: dict) -> list[Any]:
    """Return Jobs controlled by this LiveStream, newest first."""
    metadata = livestream.get("metadata", {})
    uid = metadata.get("uid")
    name = metadata.get("name")
    jobs = batch_api.list_namespaced_job(
        namespace=namespace,
        label_selector=f"liveedgecast.io/livestream={name}",
    ).items

    def belongs_to_stream(job: Any) -> bool:
        stream_owners = [
            owner
            for owner in (job.metadata.owner_references or [])
            if owner.kind == "LiveStream"
        ]
        if stream_owners:
            return bool(uid) and any(owner.uid == uid for owner in stream_owners)
        labels = job.metadata.labels or {}
        return labels.get("liveedgecast.io/livestream") == name

    owned = [job for job in jobs if belongs_to_stream(job)]
    return sorted(
        owned,
        key=lambda job: (
            job.metadata.creation_timestamp.isoformat()
            if job.metadata.creation_timestamp
            else ""
        ),
        reverse=True,
    )


def observe(job: Any) -> JobObservation:
    """Summarize the state maintained by Kubernetes' Job controller."""
    status = job.status
    conditions = status.conditions or []
    if any(
        condition.type == "Complete" and condition.status == "True"
        for condition in conditions
    ):
        phase = "Succeeded"
    elif any(
        condition.type == "Failed" and condition.status == "True"
        for condition in conditions
    ):
        phase = "Failed"
    elif status.active:
        phase = "Running"
    else:
        phase = "Pending"

    labels = job.metadata.labels or {}
    annotations = job.metadata.annotations or {}
    session_id = labels.get("liveedgecast.io/source-session-id") or annotations.get(
        "liveedgecast.io/source-session-id"
    )
    return JobObservation(job.metadata.name, phase, session_id)
