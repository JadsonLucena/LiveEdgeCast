"""LiveEdgeCast Operator watch loop."""

import logging
import time

from kubernetes import client, config, watch
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

from reconciler import reconcile

GROUP = "liveedgecast.io"
VERSION = "v1alpha1"
PLURAL = "livestreams"
LOGGER = logging.getLogger(__name__)


def _load_configuration() -> None:
    try:
        config.load_incluster_config()
        LOGGER.info("loaded in-cluster Kubernetes configuration")
    except ConfigException:
        config.load_kube_config()
        LOGGER.info("loaded local kubeconfig")


def _reconcile_all(custom_api, batch_api, core_api) -> str | None:
    response = custom_api.list_cluster_custom_object(GROUP, VERSION, PLURAL)
    for resource in response.get("items", []):
        reconcile(resource, custom_api, batch_api, core_api)
    return response.get("metadata", {}).get("resourceVersion")


def run() -> None:
    _load_configuration()
    custom_api = client.CustomObjectsApi()
    batch_api = client.BatchV1Api()
    core_api = client.CoreV1Api()

    while True:
        try:
            resource_version = _reconcile_all(custom_api, batch_api, core_api)
            stream = watch.Watch()
            for event in stream.stream(
                custom_api.list_cluster_custom_object,
                GROUP,
                VERSION,
                PLURAL,
                resource_version=resource_version,
                timeout_seconds=300,
            ):
                resource = event.get("object", {})
                if event.get("type") != "DELETED":
                    reconcile(resource, custom_api, batch_api, core_api)
        except (ApiException, OSError) as error:
            LOGGER.warning(
                "Kubernetes watch interrupted; relisting before retry: %s", error
            )
            time.sleep(2)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
