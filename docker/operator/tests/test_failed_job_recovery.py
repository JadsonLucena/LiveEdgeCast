"""Regression tests for failed processing Job recovery."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import jobs  # noqa: E402
import reconciler  # noqa: E402


class FailedJobRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.failed_job_resource = SimpleNamespace(
            metadata=SimpleNamespace(name="failed-worker", uid="job-uid")
        )
        self.failed_job = jobs.JobObservation(
            name="failed-worker",
            phase="Failed",
            bound_source_session_id="session-1",
            configuration_id="configuration-1",
        )
        self.observed = reconciler.ReconcileObservations(
            source={"available": True},
            owned_jobs=(self.failed_job_resource,),
            selected_job=self.failed_job,
            selected_job_resource=self.failed_job_resource,
            pod_phase="Failed",
            pod_ready=False,
            persisted_phase="Streaming",
        )
        self.current = {
            "metadata": {
                "name": "stream-1",
                "namespace": "media",
                "uid": "stream-uid",
            }
        }

    def test_observe_preserves_selected_job_summary_and_resource(self) -> None:
        batch_api = Mock()
        core_api = Mock()

        with (
            patch.object(reconciler.source, "observe", return_value={"available": True}),
            patch.object(
                reconciler, "_list_owned_jobs", return_value=[self.failed_job_resource]
            ),
            patch.object(jobs, "configuration_id", return_value="configuration-1"),
            patch.object(jobs, "observe", return_value=self.failed_job),
            patch.object(reconciler, "_pod_phase", return_value=("Failed", False)),
        ):
            observed = reconciler._observe(
                {
                    **self.current,
                    "spec": {"source": {"sessionId": "session-1"}},
                    "status": {"phase": "Streaming"},
                },
                batch_api,
                core_api,
            )

        self.assertIs(self.failed_job, observed.selected_job)
        self.assertIs(self.failed_job_resource, observed.selected_job_resource)

    def test_available_source_selects_only_failed_job_deletion(self) -> None:
        decision = reconciler.decide_lifecycle(self.observed)

        self.assertEqual("Recovering", decision.phase)
        self.assertIs(reconciler.LifecycleAction.DELETE_FAILED_JOB, decision.action)

    def test_repeated_execution_deletes_exact_resource_without_creation(self) -> None:
        decision = reconciler.decide_lifecycle(self.observed)
        batch_api = Mock()

        with (
            patch.object(jobs, "delete_for_livestream", return_value=True) as delete,
            patch.object(jobs, "create_for_livestream") as create,
            patch.object(jobs, "list_for_livestream") as list_jobs,
        ):
            reconciler._execute(decision, self.current, batch_api, self.observed)
            reconciler._execute(decision, self.current, batch_api, self.observed)

        self.assertEqual(
            [
                unittest.mock.call(
                    batch_api, "media", self.current, self.failed_job_resource
                ),
                unittest.mock.call(
                    batch_api, "media", self.current, self.failed_job_resource
                ),
            ],
            delete.call_args_list,
        )
        create.assert_not_called()
        list_jobs.assert_not_called()

    def test_unavailable_or_unobserved_source_interrupts_without_action(self) -> None:
        for source_observation in ({"available": False}, {}):
            with self.subTest(source=source_observation):
                observed = reconciler.ReconcileObservations(
                    **{**self.observed.__dict__, "source": source_observation}
                )

                decision = reconciler.decide_lifecycle(observed)

                self.assertEqual("Interrupted", decision.phase)
                self.assertIs(reconciler.LifecycleAction.NONE, decision.action)


if __name__ == "__main__":
    unittest.main()
