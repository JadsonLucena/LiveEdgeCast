"""Unit tests for safe LiveStream finalization."""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from kubernetes.client.exceptions import ApiException

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import jobs  # noqa: E402
import reconciler  # noqa: E402


def owner(
    api_version="liveedgecast.io/v1alpha1",
    kind="LiveStream",
    name="one",
    uid="stream-uid",
):
    return SimpleNamespace(api_version=api_version, kind=kind, name=name, uid=uid)


def job(owners=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="processor",
            uid="job-uid",
            owner_references=owners or [],
            creation_timestamp=datetime.now(timezone.utc),
            labels={},
            annotations={},
        )
    )


def stream(finalizers=None, status=None):
    return {
        "metadata": {
            "name": "one",
            "namespace": "media",
            "uid": "stream-uid",
            "generation": 7,
            "resourceVersion": "11",
            "deletionTimestamp": "2026-08-30T00:00:00Z",
            "finalizers": (
                finalizers
                if finalizers is not None
                else [reconciler.FINALIZER, "other.io/finalizer"]
            ),
        },
        "status": status or {"observedGeneration": 5, "source": {"available": True}},
    }


class JobDeletionTests(unittest.TestCase):
    def test_deletes_only_after_complete_owner_check(self):
        api = Mock()
        current = stream()
        self.assertFalse(
            jobs.delete_for_livestream(api, "media", current, job([owner(uid="wrong")]))
        )
        api.delete_namespaced_job.assert_not_called()

        self.assertTrue(
            jobs.delete_for_livestream(api, "media", current, job([owner()]))
        )
        api.delete_namespaced_job.assert_called_once_with(
            name="processor", namespace="media", propagation_policy="Foreground"
        )

    def test_not_found_is_idempotent_success(self):
        api = Mock()
        api.delete_namespaced_job.side_effect = ApiException(status=404)
        self.assertTrue(
            jobs.delete_for_livestream(api, "media", stream(), job([owner()]))
        )


class FinalizationTests(unittest.TestCase):
    def test_first_pass_sets_stopping_and_keeps_finalizer_even_when_empty(self):
        current = stream()
        custom_api, batch_api, core_api = Mock(), Mock(), Mock()
        batch_api.list_namespaced_job.return_value.items = []

        reconciler._finalize(current, custom_api, batch_api, core_api)

        patch = custom_api.patch_namespaced_custom_object_status.call_args.kwargs[
            "body"
        ]["status"]
        self.assertEqual("Stopping", patch["phase"])
        self.assertNotIn("observedGeneration", patch)
        self.assertEqual("CleanupPending", patch["conditions"][0]["type"])
        custom_api.patch_namespaced_custom_object.assert_not_called()
        core_api.list_namespaced_pod.assert_not_called()

    def test_later_empty_observation_removes_only_our_finalizer(self):
        current = stream()
        calculated, _ = reconciler._stopping_status(current)
        current["status"] = calculated
        custom_api, batch_api, core_api = Mock(), Mock(), Mock()
        batch_api.list_namespaced_job.return_value.items = []

        reconciler._finalize(current, custom_api, batch_api, core_api)

        body = custom_api.patch_namespaced_custom_object.call_args.kwargs["body"]
        self.assertEqual(["other.io/finalizer"], body["metadata"]["finalizers"])

    def test_owned_job_and_pod_are_observed_before_cascading_delete(self):
        current = stream()
        owned_job = job([owner()])
        pod = SimpleNamespace(
            metadata=SimpleNamespace(
                owner_references=[owner("batch/v1", "Job", "processor", "job-uid")]
            )
        )
        custom_api, batch_api, core_api = Mock(), Mock(), Mock()
        batch_api.list_namespaced_job.return_value.items = [owned_job]
        core_api.list_namespaced_pod.return_value.items = [pod]

        reconciler._finalize(current, custom_api, batch_api, core_api)

        batch_api.delete_namespaced_job.assert_called_once_with(
            name="processor", namespace="media", propagation_policy="Foreground"
        )
        custom_api.patch_namespaced_custom_object.assert_not_called()

    def test_deleting_without_liveedgecast_finalizer_does_nothing(self):
        custom_api, batch_api, core_api = Mock(), Mock(), Mock()
        reconciler._finalize(
            stream(["other.io/finalizer"]), custom_api, batch_api, core_api
        )
        custom_api.assert_not_called()
        batch_api.assert_not_called()
        core_api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
