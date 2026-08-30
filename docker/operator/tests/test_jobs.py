"""Tests for persisted LiveStream ownership checks."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest


MODULE_PATH = Path(__file__).parents[1] / "src" / "jobs.py"
SPEC = importlib.util.spec_from_file_location("jobs", MODULE_PATH)
jobs = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(jobs)


class BatchApi:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def list_namespaced_job(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(items=self.items)


def owner(**overrides):
    values = {
        "api_version": "liveedgecast.io/v1alpha1",
        "kind": "LiveStream",
        "name": "stream-one",
        "uid": "stream-uid",
        "controller": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def job(name, owners, labels=None, created_at=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            owner_references=owners,
            labels=labels or {},
            creation_timestamp=created_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )


class ListForLivestreamTests(unittest.TestCase):
    livestream = {"metadata": {"name": "stream-one", "uid": "stream-uid"}}

    def test_keeps_label_selector_as_api_prefilter(self):
        batch_api = BatchApi([])

        jobs.list_for_livestream(batch_api, "media", self.livestream)

        self.assertEqual(
            batch_api.calls,
            [{
                "namespace": "media",
                "label_selector": "liveedgecast.io/livestream=stream-one",
            }],
        )

    def test_accepts_only_complete_matching_owner_reference(self):
        matching_without_controller = owner(controller=False)
        candidates = [
            job("matching-controller", [owner()]),
            job("matching-without-controller", [matching_without_controller]),
            job("wrong-api-version", [owner(api_version="v1")]),
            job("wrong-kind", [owner(kind="Other")]),
            job("wrong-name", [owner(name="stream-two")]),
            job("wrong-uid", [owner(uid="other-uid")]),
        ]

        result = jobs.list_for_livestream(BatchApi(candidates), "media", self.livestream)

        self.assertEqual(
            [item.metadata.name for item in result],
            ["matching-controller", "matching-without-controller"],
        )

    def test_rejects_label_only_job_without_owner_references(self):
        label_only = job(
            "label-only",
            [],
            labels={"liveedgecast.io/livestream": "stream-one"},
        )

        result = jobs.list_for_livestream(BatchApi([label_only]), "media", self.livestream)

        self.assertEqual(result, [])

    def test_rejects_match_when_resource_identity_is_incomplete(self):
        candidate = job("matching", [owner()])

        for metadata in ({"name": "stream-one"}, {"uid": "stream-uid"}):
            with self.subTest(metadata=metadata):
                result = jobs.list_for_livestream(
                    BatchApi([candidate]), "media", {"metadata": metadata}
                )
                self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
