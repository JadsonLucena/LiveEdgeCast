"""Regression tests for tri-state source availability during recovery."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import jobs  # noqa: E402
import reconciler  # noqa: E402


class SourceAvailabilityRecoveryTest(unittest.TestCase):
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
            persisted_phase="Streaming",
        )

    def decision_for(self, source: dict) -> reconciler.LifecycleDecision:
        observed = reconciler.ReconcileObservations(
            **{**self.observed.__dict__, "source": source}
        )
        return reconciler.decide_lifecycle(observed)

    def test_confirmed_available_source_starts_destructive_recovery(self) -> None:
        decision = self.decision_for({"available": True})

        self.assertEqual("Recovering", decision.phase)
        self.assertIs(reconciler.LifecycleAction.DELETE_FAILED_JOB, decision.action)

    def test_confirmed_unavailable_source_interrupts_without_deletion(self) -> None:
        decision = self.decision_for({"available": False})

        self.assertEqual("Interrupted", decision.phase)
        self.assertIs(reconciler.LifecycleAction.NONE, decision.action)

    def test_unknown_source_preserves_terminal_job_and_previous_phase(self) -> None:
        for source in ({}, {"available": None}):
            with self.subTest(source=source):
                decision = self.decision_for(source)

                self.assertEqual("Streaming", decision.phase)
                self.assertIs(reconciler.LifecycleAction.NONE, decision.action)

    def test_unknown_source_does_not_keep_recovering_for_failed_job(self) -> None:
        self.observed = reconciler.ReconcileObservations(
            **{**self.observed.__dict__, "persisted_phase": "Recovering"}
        )

        decision = self.decision_for({})

        self.assertEqual("Provisioning", decision.phase)
        self.assertIs(reconciler.LifecycleAction.NONE, decision.action)

    def test_unknown_source_condition_awaits_proxy_observation(self) -> None:
        condition = reconciler._condition({}, generation=7, available=None)

        self.assertEqual("SourceAvailable", condition["type"])
        self.assertEqual("Unknown", condition["status"])
        self.assertEqual("AwaitingSourceObservation", condition["reason"])
        self.assertEqual(7, condition["observedGeneration"])

    def test_recovering_without_job_creates_replacement_only_when_available(self) -> None:
        without_job = reconciler.ReconcileObservations(
            **{
                **self.observed.__dict__,
                "selected_job": None,
                "selected_job_observation": None,
                "owned_jobs": (),
                "persisted_phase": "Recovering",
            }
        )

        available = reconciler.decide_lifecycle(without_job)
        unknown = reconciler.decide_lifecycle(
            reconciler.ReconcileObservations(
                **{**without_job.__dict__, "source": {}}
            )
        )

        self.assertIs(reconciler.LifecycleAction.CREATE_JOB, available.action)
        self.assertIs(reconciler.LifecycleAction.NONE, unknown.action)


if __name__ == "__main__":
    unittest.main()
