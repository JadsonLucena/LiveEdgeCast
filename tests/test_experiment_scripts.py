import importlib
import json
import logging
import subprocess
import time
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS_DIR = ROOT / "tools" / "experiments"
sys.path.insert(0, str(EXPERIMENTS_DIR))

import common  # noqa: E402
from common import (  # noqa: E402
    add_duplicate_reconnect_options,
    add_kill_options,
    add_saturation_options,
    build_incremental_levels,
    build_parser,
    collect_controller_artifacts,
    delete_pod,
    config_from_args,
    redact_url,
    process_wait_timeout_seconds,
    run_command,
    select_pod,
    start_process,
    start_ffmpeg_streams,
    stop_processes,
    sanitize_command,
    validate_duplicate_timing,
    validate_kill_timing,
    wait_for_processes,
)


def parsed_config(parser, *extra_args):
    args = parser.parse_args(
        [
            "--experiment-id",
            "exp-001",
            "--scenario",
            parser.scenario,
            "--run-id",
            "run-001",
            "--concurrency",
            "2",
            "--duration-seconds",
            "10",
            "--bitrate",
            "800k",
            *extra_args,
        ]
    )
    return config_from_args(args)


def scenario_parser(scenario):
    parser = build_parser("test parser", [scenario])
    parser.scenario = scenario
    return parser


def test_kill_timing_requires_failure_injection_during_active_run():
    parser = scenario_parser("kill_proxy")
    add_kill_options(parser)
    config = parsed_config(parser, "--kill-after-seconds", "10")

    with pytest.raises(SystemExit) as exc:
        validate_kill_timing(parser, config)

    assert exc.value.code == 2


def test_duplicate_attempt_must_overlap_primary_publisher():
    parser = scenario_parser("duplicate_stream_key_reconnect")
    add_duplicate_reconnect_options(parser)
    config = parsed_config(parser, "--duplicate-attempt-delay-seconds", "10")

    with pytest.raises(SystemExit) as exc:
        validate_duplicate_timing(parser, config)

    assert exc.value.code == 2


def test_incremental_step_size_above_concurrency_runs_target_once():
    parser = scenario_parser("incremental_pilot")
    add_saturation_options(parser)
    config = parsed_config(parser, "--step-size", "3")

    assert build_incremental_levels(config) == [2]


def test_incremental_pilot_entrypoint_handles_oversized_step_size(
    monkeypatch, tmp_path
):
    incremental_pilot = importlib.import_module("incremental_pilot")

    monkeypatch.setattr(
        incremental_pilot, "validate_runtime_tools", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        incremental_pilot, "collect_controller_artifacts", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        incremental_pilot, "start_ffmpeg_streams", lambda *args, **kwargs: [object()]
    )
    monkeypatch.setattr(
        incremental_pilot,
        "wait_for_processes",
        lambda *args, **kwargs: [{"name": "fake", "returncode": 0}],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "incremental_pilot.py",
            "--experiment-id",
            "exp",
            "--scenario",
            "incremental_pilot",
            "--run-id",
            "run",
            "--concurrency",
            "2",
            "--duration-seconds",
            "10",
            "--bitrate",
            "800k",
            "--step-size",
            "3",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert incremental_pilot.main() == 0

    summary = json.loads(
        (tmp_path / "exp" / "incremental_pilot" / "run" / "summary.json").read_text()
    )
    assert [level["concurrency"] for level in summary["levels"]] == [2]


def test_process_wait_timeout_uses_actual_process_count():
    parser = scenario_parser("incremental_pilot")
    add_saturation_options(parser)
    config = parsed_config(
        parser,
        "--concurrency",
        "100",
        "--startup-interval-seconds",
        "2",
    )

    assert process_wait_timeout_seconds(config, process_count=1) == (
        config.duration_seconds + config.stop_grace_seconds + 10
    )


def test_urls_are_redacted_in_logged_commands():
    assert (
        redact_url("rtmp://user:secret@example.com:1935/live/token")
        == "rtmp://***:***@example.com:1935/..."
    )
    assert (
        sanitize_command(["ffmpeg", "rtmp://example.com/live/key"])[1]
        == "rtmp://example.com/..."
    )


def test_run_command_timeout_is_best_effort():
    logger = logging.getLogger("test_run_command_timeout")
    result = run_command(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        logger,
        timeout=1,
    )

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 124
    assert "Timeout after 1s" in result.stderr


def test_wait_for_processes_stops_timed_out_process(tmp_path):
    logger = logging.getLogger("test_wait_for_processes_timeout")
    proc = start_process(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        "sleeping-process",
        tmp_path,
        logger,
    )

    started = time.time()
    results = wait_for_processes(
        [proc], logger, timeout_seconds=0.1, stop_grace_seconds=0.1
    )

    assert time.time() - started < 5
    assert results[0]["timed_out"] is True
    assert results[0]["returncode"] is not None
    assert results[0]["ended_at"] is not None


def test_wait_for_processes_records_stop_error_on_timeout():
    class FailingStopProcess:
        name = "failing-stop"
        command = []
        started_at = time.time() - 1
        ended_at = None
        stdout_path = Path("stdout.log")
        stderr_path = Path("stderr.log")

        class Process:
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("fake", timeout)

            def poll(self):
                return None

        process = Process()

        def stop(self, grace_seconds):
            raise RuntimeError("cannot stop")

        def result(self):
            return {
                "name": self.name,
                "ended_at": self.ended_at,
                "returncode": self.process.poll(),
            }

    results = wait_for_processes(
        [FailingStopProcess()],
        logging.getLogger("test_wait_stop_error"),
        timeout_seconds=0.1,
        stop_grace_seconds=0.1,
    )

    assert results[0]["timed_out"] is True
    assert results[0]["stop_error"]["type"] == "RuntimeError"


def test_start_ffmpeg_streams_stops_already_started_processes_on_failure(
    monkeypatch, tmp_path
):
    parser = scenario_parser("incremental_pilot")
    add_saturation_options(parser)
    config = parsed_config(parser, "--output-dir", str(tmp_path))
    stopped = []

    class FakeManagedProcess:
        name = "fake"
        command = []
        started_at = time.time()
        stdout_path = tmp_path / "stdout.log"
        stderr_path = tmp_path / "stderr.log"

        def stop(self, grace_seconds):
            stopped.append(grace_seconds)
            return 0

        def result(self):
            return {"name": self.name, "returncode": 0}

    calls = {"count": 0}

    def fake_start_process(command, name, artifact_dir, logger):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("boom")
        return FakeManagedProcess()

    monkeypatch.setattr(common, "start_process", fake_start_process)

    with pytest.raises(RuntimeError):
        start_ffmpeg_streams(config, ["one", "two"], logging.getLogger("test"))

    assert stopped == [config.stop_grace_seconds]


def test_delete_pod_sets_status_from_returncode(monkeypatch):
    parser = scenario_parser("kill_proxy")
    add_kill_options(parser)
    config = parsed_config(parser)

    def fake_kubectl(config, args, logger, timeout=30):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="failed")

    monkeypatch.setattr(common, "kubectl", fake_kubectl)

    result = delete_pod(config, "proxy-1", logging.getLogger("test_delete_pod"))

    assert result["status"] == "delete_failed"
    assert result["returncode"] == 1


def test_select_pod_requires_single_candidate(monkeypatch):
    parser = scenario_parser("kill_proxy")
    add_kill_options(parser)
    config = parsed_config(parser)

    payload = {
        "items": [
            {"metadata": {"name": "proxy-1"}, "status": {"phase": "Running"}},
            {"metadata": {"name": "proxy-2"}, "status": {"phase": "Running"}},
        ]
    }

    def fake_kubectl(config, args, logger, timeout=30):
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(common, "kubectl", fake_kubectl)

    assert select_pod(config, "app=proxy", logging.getLogger("test")) == "proxy-1"
    assert (
        select_pod(
            config,
            "app=proxy",
            logging.getLogger("test"),
            require_single=True,
        )
        is None
    )


def test_target_proxy_pod_is_parsed_from_cli():
    parser = scenario_parser("kill_proxy")
    add_kill_options(parser)
    config = parsed_config(parser, "--target-proxy-pod", "proxy-1")

    assert config.target_proxy_pod == "proxy-1"


def test_stop_processes_records_cleanup_stop_error():
    parser = scenario_parser("kill_proxy")
    add_kill_options(parser)
    config = parsed_config(parser)

    class FailingCleanupProcess:
        name = "failing-cleanup"
        command = []
        started_at = time.time()
        ended_at = None
        stdout_path = Path("stdout.log")
        stderr_path = Path("stderr.log")

        class Process:
            def poll(self):
                return None

        process = Process()

        def stop(self, grace_seconds):
            raise RuntimeError("cleanup failed")

        def result(self):
            return {"name": self.name, "returncode": self.process.poll()}

    results = stop_processes(
        [FailingCleanupProcess()], config, logging.getLogger("test_cleanup_stop")
    )

    assert results[0]["stopped_by"] == "cleanup"
    assert results[0]["stop_error"]["type"] == "RuntimeError"


def test_collect_controller_artifacts_records_failure_metadata(monkeypatch, tmp_path):
    parser = scenario_parser("incremental_pilot")
    add_saturation_options(parser)
    config = parsed_config(
        parser,
        "--controller-url",
        "http://controller.example.test",
        "--output-dir",
        str(tmp_path),
    )
    config.artifact_dir.mkdir(parents=True)

    def failing_urlopen(request, timeout=10):
        raise OSError("unreachable")

    monkeypatch.setattr(common, "urlopen", failing_urlopen)

    artifacts = collect_controller_artifacts(
        config, logging.getLogger("test_controller")
    )

    assert artifacts["controller_health.json"]["status"] == "collection_failed"
    assert artifacts["controller_health.json"]["error"]["type"] == "OSError"
    assert Path(artifacts["controller_health.json"]["path"]).exists()
