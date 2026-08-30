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


class PodListingTests(unittest.TestCase):
    def test_lists_pods_for_all_validated_jobs_by_exact_owner_identity(self):
        current = stream()
        first = job([owner()])
        second = job([owner()])
        second.metadata.name = "processor-two"
        second.metadata.uid = "job-uid-two"
        matching_first = SimpleNamespace(
            metadata=SimpleNamespace(
                owner_references=[owner("batch/v1", "Job", "processor", "job-uid")]
            )
        )
        matching_second = SimpleNamespace(
            metadata=SimpleNamespace(
                owner_references=[
                    owner("batch/v1", "Job", "processor-two", "job-uid-two")
                ]
            )
        )
        same_name_wrong_uid = SimpleNamespace(
            metadata=SimpleNamespace(
                owner_references=[owner("batch/v1", "Job", "processor", "other-uid")]
            )
        )
        labelled_but_unowned = SimpleNamespace(
            metadata=SimpleNamespace(owner_references=[])
        )
        api = Mock()
        api.list_namespaced_pod.return_value.items = [
            matching_first,
            matching_second,
            same_name_wrong_uid,
            labelled_but_unowned,
        ]

        result = reconciler._list_processing_pods(
            api, "media", current, [first, second]
        )

        self.assertEqual([matching_first, matching_second], result)
        api.list_namespaced_pod.assert_called_once_with(
            namespace="media",
            label_selector="liveedgecast.io/livestream=one",
        )

    def test_does_not_list_pods_without_validated_jobs(self):
        api = Mock()

        self.assertEqual(
            [], reconciler._list_processing_pods(api, "media", stream(), [])
        )

        api.list_namespaced_pod.assert_not_called()


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

    def test_label_matches_without_ownership_do_not_block_finalization(self):
        current = stream()
        calculated, _ = reconciler._stopping_status(current)
        current["status"] = calculated
        impostor_job = job([owner(uid="another-stream-uid")])
        custom_api, batch_api, core_api = Mock(), Mock(), Mock()
        batch_api.list_namespaced_job.return_value.items = [impostor_job]

        reconciler._finalize(current, custom_api, batch_api, core_api)

        batch_api.list_namespaced_job.assert_called_once_with(
            namespace="media",
            label_selector="liveedgecast.io/livestream=one",
        )
        core_api.list_namespaced_pod.assert_not_called()
        batch_api.delete_namespaced_job.assert_not_called()
        body = custom_api.patch_namespaced_custom_object.call_args.kwargs["body"]
        self.assertEqual(["other.io/finalizer"], body["metadata"]["finalizers"])

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
