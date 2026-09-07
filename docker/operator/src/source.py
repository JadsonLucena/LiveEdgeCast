"""Interpret source facts recorded on a LiveStream without performing ingest."""

from typing import Any


def observe(livestream: dict[str, Any]) -> dict[str, Any]:
    """Describe the desired source and retain only matching Proxy observations.

    Source availability is reported by the ingest/Proxy integration. The Operator
    deliberately does not probe, connect to, or otherwise own that responsibility.
    """
    desired = livestream.get("spec", {}).get("source", {})
    previous = livestream.get("status", {}).get("source", {})
    observation: dict[str, Any] = {
        "proxyName": desired.get("proxyName", ""),
        "sessionId": desired.get("sessionId", ""),
    }
    if isinstance(desired.get("available"), bool):
        observation["available"] = desired["available"]
    desired_proxy = desired.get("proxyName")
    desired_session = desired.get("sessionId")
    matches_desired_source = (
        previous.get("proxyName") == desired_proxy
        and previous.get("sessionId") == desired_session
    )
    if matches_desired_source:
        if "available" not in observation and isinstance(
            previous.get("available"), bool
        ):
            observation["available"] = previous["available"]
        if previous.get("lastSeenAt"):
            observation["lastSeenAt"] = previous["lastSeenAt"]
    return observation
