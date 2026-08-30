"""LiveStream status-subresource updates."""

from typing import Any


def _merge_patch(current: Any, desired: Any) -> Any:
    """Build an RFC 7386 merge patch that also removes obsolete fields."""
    if not isinstance(current, dict) or not isinstance(desired, dict):
        return desired

    patch = {key: None for key in current.keys() - desired.keys()}
    for key, desired_value in desired.items():
        current_value = current.get(key)
        if current_value != desired_value:
            patch[key] = _merge_patch(current_value, desired_value)
    return patch


def patch_if_changed(
    custom_api: Any, namespace: str, name: str, current: dict, calculated: dict
) -> bool:
    """Patch status only when the complete calculated value has changed."""
    current_status = current.get("status") or {}
    if current_status == calculated:
        return False
    status_patch = _merge_patch(current_status, calculated)
    custom_api.patch_namespaced_custom_object_status(
        group="liveedgecast.io",
        version="v1alpha1",
        namespace=namespace,
        plural="livestreams",
        name=name,
        body={"status": status_patch},
        _content_type="application/merge-patch+json",
    )
    return True
