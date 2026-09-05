"""Regression tests for terminal Job recovery decisions and execution."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))

import jobs  # noqa: E402
import reconciler  # noqa: E402


def job_observation(phase: str = "Failed") -> jobs.JobObservation:
    return jobs.JobObservation("worker-job", phase, "session-1", "config-1")


def observations(
    *,
    job=None,
    observation=None,
    source_available=True,
    previous_phase=None,
    owned_jobs=(),
    pod_phase=None,
):
    return reconciler.ReconcileObservations(
        source={"available": source_available},
        owned_jobs=owned_jobs,
        selected_job=job,
        selected_job_observation=observation,
        pod_phase=pod_phase,
        pod_ready=False,
        previous_phase=previous_phase,
    )


class TerminalRecoveryDecisionTests(TestCase):
    def test_failed_job_with_available_source_is_deleted_while_recovering(self):
        selected = object()
        observed = observations(
            job=selected,
            observation=job_observation(),
            owned_jobs=(selected,),
        )

        decision = reconciler.decide_lifecycle(observed)

        self.assertEqual("Recovering", decision.phase)
        self.assertIs(reconciler.LifecycleAction.DELETE_FAILED_JOB, decision.action)

    def test_failed_job_is_not_deleted_without_confirmed_source_availability(self):
        observed = observations(
            job=object(), observation=job_observation(), source_available=None
        )

        decision = reconciler.decide_lifecycle(observed)

        self.assertEqual("Recovering", decision.phase)
        self.assertIs(reconciler.LifecycleAction.NONE, decision.action)

    def test_recovery_provisions_replacement_only_after_job_is_absent(self):
        observed = observations(previous_phase="Recovering")

        decision = reconciler.decide_lifecycle(observed)

        self.assertEqual("Provisioning", decision.phase)
        self.assertIs(reconciler.LifecycleAction.CREATE_JOB, decision.action)

    def test_failed_pod_does_not_trigger_terminal_job_recovery(self):
        selected = object()
        observed = observations(
            job=selected,
            observation=job_observation("Running"),
            owned_jobs=(selected,),
            pod_phase="Failed",
        )

        decision = reconciler.decide_lifecycle(observed)

        self.assertEqual("Provisioning", decision.phase)
        self.assertIs(reconciler.LifecycleAction.NONE, decision.action)


class TerminalRecoveryExecutionTests(TestCase):
    def setUp(self):
        self.current = {
            "metadata": {
                "name": "stream",
                "namespace": "media",
                "uid": "stream-uid",
            }
        }

    @mock.patch.object(reconciler.jobs, "delete_for_livestream")
    @mock.patch.object(reconciler.jobs, "list_for_livestream")
    def test_delete_failed_job_uses_exact_observed_resource(
        self, list_jobs, delete_job
    ):
        selected = object()
        observed = observations(
            job=selected,
            observation=job_observation(),
            owned_jobs=(selected, object()),
        )
        decision = reconciler.LifecycleDecision(
            "Recovering", reconciler.LifecycleAction.DELETE_FAILED_JOB
        )
        batch_api = object()

        reconciler._execute(decision, self.current, batch_api, observed)

        list_jobs.assert_not_called()
        delete_job.assert_called_once_with(
            batch_api, "media", self.current, selected
        )

    def test_delete_failed_job_keeps_full_owner_reference_validation(self):
        foreign_job = SimpleNamespace(
            metadata=SimpleNamespace(
                name="worker-job",
                owner_references=[
                    SimpleNamespace(
                        api_version=jobs.LIVESTREAM_API_VERSION,
                        kind=jobs.LIVESTREAM_KIND,
                        name="stream",
                        uid="someone-elses-uid",
                    )
                ],
            )
        )
        observed = observations(
            job=foreign_job,
            observation=job_observation(),
            owned_jobs=(foreign_job,),
        )
        decision = reconciler.LifecycleDecision(
            "Recovering", reconciler.LifecycleAction.DELETE_FAILED_JOB
        )
        batch_api = mock.Mock()

        reconciler._execute(decision, self.current, batch_api, observed)

        batch_api.delete_namespaced_job.assert_not_called()

    @mock.patch.object(reconciler.jobs, "create_for_livestream")
    def test_replacement_creation_does_not_delete_pods(self, create_job):
        observed = observations(previous_phase="Recovering")
        decision = reconciler.decide_lifecycle(observed)
        batch_api = object()

        reconciler._execute(decision, self.current, batch_api, observed)

        create_job.assert_called_once_with(batch_api, "media", self.current)
