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
        self.failed_job = SimpleNamespace(
            metadata=SimpleNamespace(name="failed-worker", uid="job-uid")
        )
        self.observed = reconciler.ReconcileObservations(
            source={"available": True},
            owned_jobs=(self.failed_job,),
            selected_job=self.failed_job,
            selected_job_observation=jobs.JobObservation(
                name="failed-worker",
                phase="Failed",
                bound_source_session_id="session-1",
                configuration_id="configuration-1",
            ),
            pod_phase="Failed",
            pod_ready=False,
            previous_phase="Streaming",
        )
        self.current = {
            "metadata": {
                "name": "stream-1",
                "namespace": "media",
                "uid": "stream-uid",
            }
        }

    def test_available_source_selects_only_failed_job_deletion(self) -> None:
        decision = reconciler.decide_lifecycle(self.observed)

        self.assertEqual("Recovering", decision.phase)
        self.assertIs(reconciler.LifecycleAction.DELETE_FAILED_JOB, decision.action)

    def test_repeated_execution_deletes_the_exact_observed_job_without_creation(self) -> None:
        decision = reconciler.decide_lifecycle(self.observed)
        batch_api = Mock()

        with (
            patch.object(jobs, "delete_for_livestream", return_value=True) as delete,
            patch.object(jobs, "create_for_livestream") as create,
        ):
            reconciler._execute(decision, self.current, batch_api, self.observed)
            reconciler._execute(decision, self.current, batch_api, self.observed)

        self.assertEqual(
            [
                unittest.mock.call(
                    batch_api, "media", self.current, self.failed_job
                ),
                unittest.mock.call(
                    batch_api, "media", self.current, self.failed_job
                ),
            ],
            delete.call_args_list,
        )
        create.assert_not_called()

    def test_unavailable_source_does_not_request_failed_job_deletion(self) -> None:
        observed = reconciler.ReconcileObservations(
            **{**self.observed.__dict__, "source": {"available": False}}
        )

        decision = reconciler.decide_lifecycle(observed)

        self.assertEqual("Interrupted", decision.phase)
        self.assertIs(reconciler.LifecycleAction.NONE, decision.action)


if __name__ == "__main__":
    unittest.main()
