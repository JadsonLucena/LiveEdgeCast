"""LiveStream status-subresource updates."""

from typing import Any


def patch_if_changed(
    custom_api: Any, namespace: str, name: str, current: dict, calculated: dict
) -> bool:
    """Patch status only when the complete calculated value has changed."""
    current_status = current.get("status") or {}
    if current_status == calculated:
        return False
    custom_api.patch_namespaced_custom_object_status(
        group="liveedgecast.io",
        version="v1alpha1",
        namespace=namespace,
        plural="livestreams",
        name=name,
        body={"status": calculated},
        _content_type="application/merge-patch+json",
    )
    return True
