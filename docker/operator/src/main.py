"""LiveEdgeCast Operator watch loop."""

import logging
import signal
import threading

from kubernetes import client, config, watch
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

from reconciler import reconcile

GROUP = "liveedgecast.io"
VERSION = "v1alpha1"
PLURAL = "livestreams"
NAMESPACE = "media"
RESYNC_SECONDS = 5
LOGGER = logging.getLogger(__name__)
STOP_EVENT = threading.Event()
ACTIVE_WATCH: watch.Watch | None = None


def _request_shutdown(signum, _frame) -> None:
    """Ask the active watch loop to finish after Kubernetes sends SIGTERM."""
    LOGGER.info("received signal %s; stopping the Operator watch", signum)
    STOP_EVENT.set()
    active_watch = ACTIVE_WATCH
    if active_watch is not None:
        active_watch.stop()


def _load_configuration() -> None:
    try:
        config.load_incluster_config()
        LOGGER.info("loaded in-cluster Kubernetes configuration")
    except ConfigException:
        config.load_kube_config()
        LOGGER.info("loaded local kubeconfig")


def _reconcile_all(custom_api, batch_api, core_api) -> str | None:
    response = custom_api.list_namespaced_custom_object(
        GROUP, VERSION, NAMESPACE, PLURAL
    )
    for resource in response.get("items", []):
        reconcile(resource, custom_api, batch_api, core_api)
    return response.get("metadata", {}).get("resourceVersion")


def run() -> None:
    global ACTIVE_WATCH

    _load_configuration()
    custom_api = client.CustomObjectsApi()
    batch_api = client.BatchV1Api()
    core_api = client.CoreV1Api()

    while not STOP_EVENT.is_set():
        try:
            resource_version = _reconcile_all(custom_api, batch_api, core_api)
            stream = watch.Watch()
            ACTIVE_WATCH = stream
            try:
                if STOP_EVENT.is_set():
                    stream.stop()
                    break
                for event in stream.stream(
                    custom_api.list_namespaced_custom_object,
                    GROUP,
                    VERSION,
                    NAMESPACE,
                    PLURAL,
                    resource_version=resource_version,
                    timeout_seconds=RESYNC_SECONDS,
                ):
                    if STOP_EVENT.is_set():
                        stream.stop()
                        break
                    if event.get("type") == "ERROR":
                        error = event.get("object", {})
                        LOGGER.warning(
                            "Kubernetes watch returned an error (code=%s, reason=%s); relisting",
                            error.get("code"),
                            error.get("reason"),
                        )
                        break
                    resource = event.get("object", {})
                    if event.get("type") in {"ADDED", "MODIFIED"}:
                        reconcile(resource, custom_api, batch_api, core_api)
            finally:
                stream.stop()
                if ACTIVE_WATCH is stream:
                    ACTIVE_WATCH = None
        except (ApiException, OSError) as error:
            LOGGER.warning(
                "Kubernetes watch interrupted; relisting before retry: %s", error
            )
            STOP_EVENT.wait(2)

    LOGGER.info("Operator watch stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)
    run()
