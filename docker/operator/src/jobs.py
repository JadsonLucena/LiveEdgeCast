"""Observation and safe deletion of Jobs owned by a LiveStream."""

from dataclasses import dataclass
import hashlib
import re
from typing import Any

from kubernetes.client.exceptions import ApiException

LIVESTREAM_API_VERSION = "liveedgecast.io/v1alpha1"
LIVESTREAM_KIND = "LiveStream"
LIVESTREAM_LABEL = "liveedgecast.io/livestream"
SOURCE_SESSION_ANNOTATION = "liveedgecast.io/source-session-id"
CONFIGURATION_ID_ANNOTATION = "liveedgecast.io/configuration-id"
WORKER_IMAGE = "liveedgecast-worker:latest"
JOB_BACKOFF_LIMIT = 2
MEDIA_HEALTH_INTERVAL_SECONDS = 10


@dataclass(frozen=True)
class JobObservation:
    name: str
    phase: str
    bound_source_session_id: str | None
    configuration_id: str | None


def _is_owned_by_livestream(job: Any, livestream: dict) -> bool:
    """Confirm the complete controller identity rather than trusting labels."""
    metadata = livestream.get("metadata", {})
    uid = metadata.get("uid")
    name = metadata.get("name")
    owners = job.metadata.owner_references or []
    return bool(name and uid) and any(
        owner.api_version == LIVESTREAM_API_VERSION
        and owner.kind == LIVESTREAM_KIND
        and owner.name == name
        and owner.uid == uid
        for owner in owners
    )


def list_for_livestream(batch_api: Any, namespace: str, livestream: dict) -> list[Any]:
    """Return Jobs owned by this LiveStream, newest first."""
    name = livestream.get("metadata", {}).get("name")
    if not name:
        return []
    jobs = batch_api.list_namespaced_job(
        namespace=namespace,
        label_selector=f"{LIVESTREAM_LABEL}={name}",
    ).items

    owned = [job for job in jobs if _is_owned_by_livestream(job, livestream)]
    return sorted(
        owned,
        key=lambda job: (
            job.metadata.creation_timestamp.isoformat()
            if job.metadata.creation_timestamp
            else ""
        ),
        reverse=True,
    )


def delete_for_livestream(
    batch_api: Any, namespace: str, livestream: dict, job: Any
) -> bool:
    """Delete an owned Job idempotently, cascading deletion to its Pods.

    ``False`` means the supplied Job failed the full owner-reference check and
    was deliberately left untouched. A missing, previously owned Job counts as
    a successful deletion.
    """
    if not _is_owned_by_livestream(job, livestream):
        return False
    try:
        batch_api.delete_namespaced_job(
            name=job.metadata.name,
            namespace=namespace,
            propagation_policy="Foreground",
        )
    except ApiException as error:
        if error.status != 404:
            raise
    return True


def configuration_id(livestream: dict) -> str:
    """Fingerprint every mutable desired field embedded in a Job Pod template."""
    spec = livestream["spec"]
    values = (
        spec["streamKey"],
        spec["source"]["sessionId"],
        spec["source"]["url"],
        spec["target"]["url"],
    )
    framed = b"".join(
        len(value.encode()).to_bytes(8, byteorder="big") + value.encode()
        for value in values
    )
    return hashlib.sha256(framed).hexdigest()


def _job_name(livestream: dict) -> str:
    """Return a stable DNS name for the current desired processing configuration."""
    metadata = livestream["metadata"]
    identity = f"{metadata['uid']}:{configuration_id(livestream)}".encode()
    suffix = hashlib.sha256(identity).hexdigest()[:10]
    stream_name = re.sub(r"[^a-z0-9-]", "-", metadata["name"].lower()).strip("-")
    return f"lec-{stream_name[:47].rstrip('-')}-{suffix}"


def create_for_livestream(batch_api: Any, namespace: str, livestream: dict) -> str:
    """Create the deterministic processing Job, treating an existing Job as success."""
    metadata = livestream["metadata"]
    spec = livestream["spec"]
    session_id = spec["source"]["sessionId"]
    desired_configuration_id = configuration_id(livestream)
    name = _job_name(livestream)
    labels = {LIVESTREAM_LABEL: metadata["name"]}
    body = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
            "annotations": {
                SOURCE_SESSION_ANNOTATION: session_id,
                CONFIGURATION_ID_ANNOTATION: desired_configuration_id,
            },
            "ownerReferences": [
                {
                    "apiVersion": LIVESTREAM_API_VERSION,
                    "kind": LIVESTREAM_KIND,
                    "name": metadata["name"],
                    "uid": metadata["uid"],
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
            ],
        },
        "spec": {
            "backoffLimit": JOB_BACKOFF_LIMIT,
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": {
                        SOURCE_SESSION_ANNOTATION: session_id,
                        CONFIGURATION_ID_ANNOTATION: desired_configuration_id,
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "ffmpeg",
                            "image": WORKER_IMAGE,
                            "imagePullPolicy": "Never",
                            "env": [
                                {"name": "STREAM_KEY", "value": spec["streamKey"]},
                                {
                                    "name": "SOURCE_RTMP_URL",
                                    "value": spec["source"]["url"],
                                },
                                {
                                    "name": "TARGET_RTMP_URL",
                                    "value": spec["target"]["url"],
                                },
                                {
                                    "name": "MEDIA_HEALTH_INTERVAL_SECONDS",
                                    "value": str(MEDIA_HEALTH_INTERVAL_SECONDS),
                                },
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                        }
                    ],
                },
            },
        },
    }
    try:
        batch_api.create_namespaced_job(namespace=namespace, body=body)
    except ApiException as error:
        if error.status != 409:
            raise
    return name


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
    session_id = labels.get(SOURCE_SESSION_ANNOTATION) or annotations.get(
        SOURCE_SESSION_ANNOTATION
    )
    return JobObservation(
        job.metadata.name,
        phase,
        session_id,
        annotations.get(CONFIGURATION_ID_ANNOTATION),
    )
