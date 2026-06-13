import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS_DIR = ROOT / "tools" / "experiments"
sys.path.insert(0, str(EXPERIMENTS_DIR))

import run_experiment as runner  # noqa: E402


def config(tmp_path, scenario="cold-start"):
    return runner.RunnerConfig(
        stream_keys=["key1"],
        scenario=scenario,
        experiment_id="exp",
        run_id="run",
        repetitions=1,
        duration_seconds=1,
        warmup_seconds=0,
        cooldown_seconds=0,
        rtmp_url="rtmp://example/live",
        secondary_rtmp_url=None,
        source_file=None,
        bitrate=None,
        namespace="media",
        prometheus_url=None,
        controller_url=None,
        output_dir=tmp_path,
        kill_worker=False,
        kill_proxy=False,
        dry_run=False,
        ffmpeg_path="ffmpeg",
        kubectl_path="kubectl",
        startup_interval_seconds=0,
        kill_after_seconds=1,
        duplicate_attempt_delay_seconds=1,
        reconnect_delay_seconds=1,
        pilot_step_size=5,
        saturation_p95_seconds=5.0,
        saturation_error_rate=0.2,
        baseline=None,
        release_after_seconds=1,
        patch_proxy_context=False,
    )


def test_parse_json_event_line_extracts_structured_controller_event():
    line = '{"timestamp":"2026-01-01T00:00:01Z","event_type":"stream_lifecycle_timestamp_observed","stream":"s1","message":"t_worker_ready observed from pod_watch"}'

    event = runner.parse_json_event_line(line)

    assert event["event_type"] == "stream_lifecycle_timestamp_observed"
    assert event["timestamp_epoch"] == 1767225601.0
    assert runner.lifecycle_field_from_event(event) == "t_worker_ready"


def test_extract_structured_events_writes_jsonl(tmp_path):
    log = tmp_path / "controller.log"
    log.write_text('plain line\n{"timestamp":"2026-01-01T00:00:00Z","event_type":"publish_received","stream":"key1"}\n')
    out = tmp_path / "controller_events.jsonl"

    count = runner.extract_structured_events(log, out, "controller")

    assert count == 1
    rows = runner.read_jsonl(out)
    assert rows[0]["component"] == "controller"
    assert rows[0]["event_type"] == "publish_received"


def test_build_metrics_derives_activation_from_controller_events(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    (dirs["raw"] / "publishers.jsonl").write_text(
        json.dumps({"event":"publisher_finished","stream_key":"key1","started_at":100.0,"ended_at":200.0,"returncode":0}) + "\n"
    )
    events = [
        {"timestamp_epoch":101.0,"event_type":"stream_lifecycle_timestamp_observed","stream":"key1","message":"t_publish_start_proxy observed from proxy_hook"},
        {"timestamp_epoch":102.0,"event_type":"stream_lifecycle_timestamp_observed","stream":"key1","message":"t_controller_received_event observed from controller"},
        {"timestamp_epoch":103.0,"event_type":"stream_lifecycle_timestamp_observed","stream":"key1","message":"t_worker_create_requested observed from controller"},
        {"timestamp_epoch":104.0,"event_type":"stream_lifecycle_timestamp_observed","stream":"key1","message":"t_worker_pod_created observed from pod_watch"},
        {"timestamp_epoch":105.0,"event_type":"stream_lifecycle_timestamp_observed","stream":"key1","message":"t_worker_ready observed from pod_watch"},
        {"timestamp_epoch":106.0,"event_type":"stream_lifecycle_timestamp_observed","stream":"key1","message":"t_ffmpeg_started observed from worker"},
        {"timestamp_epoch":107.0,"event_type":"stream_lifecycle_timestamp_observed","stream":"key1","message":"t_ffmpeg_first_progress observed from worker"},
        {"timestamp_epoch":201.0,"event_type":"stream_ended_received","stream":"key1"},
        {"timestamp_epoch":203.0,"event_type":"worker_deleted","stream":"key1"},
    ]
    (dirs["raw"] / "controller_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    metrics = runner.build_metrics(cfg, dirs)

    activation = list(__import__("csv").DictReader((dirs["metrics"] / "activation_metrics.csv").open()))[0]
    assert float(activation["event_detection_seconds"]) == 1.0
    assert float(activation["total_activation_seconds"]) == 7.0
    assert activation["status"] == "derived_from_controller_structured_logs"
    release = list(__import__("csv").DictReader((dirs["metrics"] / "release_metrics.csv").open()))[0]
    assert float(release["total_release_seconds"]) == 3.0
    assert metrics["activation"]["total_activation_seconds_per_stream"]["samples"] == 1


def test_build_pilot_levels_includes_expected_steps_and_max():
    assert runner.build_pilot_levels(20, 5) == [1, 5, 10, 15, 20]
    assert runner.build_pilot_levels(18, 5) == [1, 5, 10, 15, 18]


def test_build_metrics_keeps_repetitions_as_independent_samples(tmp_path):
    cfg = config(tmp_path)
    cfg.repetitions = 2
    dirs = runner.ensure_layout(cfg.report_root)
    stream_records = [
        {"event": "run_started", "repetition": 1, "timestamp": 90.0, "stream_keys": ["key1"]},
        {"event": "run_finished", "repetition": 1, "ended_at": 150.0, "stream_keys": ["key1"]},
        {"event": "run_started", "repetition": 2, "timestamp": 190.0, "stream_keys": ["key1"]},
        {"event": "run_finished", "repetition": 2, "ended_at": 250.0, "stream_keys": ["key1"]},
    ]
    (dirs["raw"] / "streams.jsonl").write_text("".join(json.dumps(e) + "\n" for e in stream_records))
    publisher_records = [
        {"event": "publisher_finished", "repetition": 1, "publisher_index": 1, "stream_key": "key1", "started_at": 100.0, "ended_at": 140.0, "returncode": 0},
        {"event": "publisher_finished", "repetition": 2, "publisher_index": 1, "stream_key": "key1", "started_at": 200.0, "ended_at": 240.0, "returncode": 0},
    ]
    (dirs["raw"] / "publishers.jsonl").write_text("".join(json.dumps(e) + "\n" for e in publisher_records))
    events = []
    for base in (100.0, 200.0):
        events.extend([
            {"timestamp_epoch": base + 1, "event_type": "stream_lifecycle_timestamp_observed", "stream": "key1", "message": "t_publish_start_proxy observed from proxy_hook"},
            {"timestamp_epoch": base + 2, "event_type": "stream_lifecycle_timestamp_observed", "stream": "key1", "message": "t_controller_received_event observed from controller"},
            {"timestamp_epoch": base + 3, "event_type": "stream_lifecycle_timestamp_observed", "stream": "key1", "message": "t_worker_create_requested observed from controller"},
            {"timestamp_epoch": base + 4, "event_type": "stream_lifecycle_timestamp_observed", "stream": "key1", "message": "t_worker_pod_created observed from pod_watch"},
            {"timestamp_epoch": base + 5, "event_type": "stream_lifecycle_timestamp_observed", "stream": "key1", "message": "t_worker_ready observed from pod_watch"},
            {"timestamp_epoch": base + 6, "event_type": "stream_lifecycle_timestamp_observed", "stream": "key1", "message": "t_ffmpeg_started observed from worker"},
            {"timestamp_epoch": base + 7, "event_type": "stream_lifecycle_timestamp_observed", "stream": "key1", "message": "t_ffmpeg_first_progress observed from worker"},
        ])
    (dirs["raw"] / "controller_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    metrics = runner.build_metrics(cfg, dirs)

    rows = list(__import__("csv").DictReader((dirs["metrics"] / "activation_metrics.csv").open()))
    assert [row["repetition"] for row in rows] == ["1", "2"]
    assert [float(row["total_activation_seconds"]) for row in rows] == [7.0, 7.0]
    assert metrics["activation"]["total_activation_seconds_per_stream"]["samples"] == 2


def test_cold_start_precondition_deletes_existing_workers(monkeypatch, tmp_path):
    cfg = config(tmp_path, scenario="cold-start")
    cfg.kubectl_path = "/bin/true"
    cfg.allow_worker_cleanup = True
    dirs = runner.ensure_layout(cfg.report_root)
    calls = iter([
        (["worker-old"], {"returncode": 0}),
        ([], {"returncode": 0}),
    ])
    deleted = []
    monkeypatch.setattr(runner, "list_worker_pods", lambda cfg_arg: next(calls))
    monkeypatch.setattr(runner, "delete_pod", lambda cfg_arg, pod: deleted.append(pod) or {"returncode": 0})

    result = runner.ensure_zero_workers_for_cold_start(cfg, dirs, repetition=1, timeout_seconds=1)

    assert result["status"] == "ok"
    assert deleted == ["worker-old"]
    events = runner.read_jsonl(dirs["raw"] / "streams.jsonl")
    assert events[-1]["event"] == "cold_start_precondition"


def test_cold_start_precondition_fails_when_workers_remain(monkeypatch, tmp_path):
    cfg = config(tmp_path, scenario="cold-start")
    cfg.kubectl_path = "/bin/true"
    cfg.allow_worker_cleanup = True
    dirs = runner.ensure_layout(cfg.report_root)
    monkeypatch.setattr(runner, "list_worker_pods", lambda cfg_arg: (["worker-stuck"], {"returncode": 0}))
    monkeypatch.setattr(runner, "delete_pod", lambda cfg_arg, pod: {"returncode": 0})
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)

    try:
        runner.ensure_zero_workers_for_cold_start(cfg, dirs, repetition=1, timeout_seconds=0)
    except RuntimeError as exc:
        assert "worker pods still active" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_cold_start_precondition_requires_cleanup_flag(monkeypatch, tmp_path):
    cfg = config(tmp_path, scenario="cold-start")
    cfg.kubectl_path = "/bin/true"
    cfg.allow_worker_cleanup = False
    dirs = runner.ensure_layout(cfg.report_root)
    monkeypatch.setattr(runner, "list_worker_pods", lambda cfg_arg: (["worker-old"], {"returncode": 0}))

    try:
        runner.ensure_zero_workers_for_cold_start(cfg, dirs, repetition=1, timeout_seconds=0)
    except RuntimeError as exc:
        assert "allow-worker-cleanup" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_restore_context_keys_restores_partially_patched_deployment(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    commands = []

    def fake_run_cmd(command, timeout=60):
        commands.append(command)
        return {"returncode": 0, "command": command, "stdout": "", "stderr": ""}

    monkeypatch.setattr(runner, "run_cmd", fake_run_cmd)
    patch_result = {
        "patched": True,
        "patched_deployments": ["proxy"],
        "previous_env": {
            "proxy": {"values": {"EXPERIMENT_ID": None, "SCENARIO": "old", "RUN_ID": None}},
            "controller": {"values": {"LIVEEDGECAST_EXPERIMENT_ID": "old"}},
        },
    }

    result = runner.restore_context_keys(cfg, dirs, patch_result)

    assert result["restored_deployments"] == ["proxy"]
    assert any("deployment/proxy" in cmd for command in commands for cmd in command)
    assert not any("deployment/controller" in cmd for command in commands for cmd in command)


def test_correctness_marks_zero_workers_as_not_valid(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    (dirs["raw"] / "streams.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "run", "repetition": 1, "timestamp": 10.0, "stream_keys": ["key1"]}) + "\n" +
        json.dumps({"event": "run_finished", "run_id": "run", "repetition": 1, "ended_at": 20.0, "stream_keys": ["key1"]}) + "\n"
    )
    (dirs["raw"] / "publishers.jsonl").write_text(
        json.dumps({"event": "publisher_finished", "run_id": "run", "repetition": 1, "publisher_index": 1, "stream_key": "key1", "started_at": 11.0, "ended_at": 19.0, "returncode": 0}) + "\n"
    )
    (dirs["raw"] / "controller_events.jsonl").write_text("")

    runner.build_metrics(cfg, dirs)

    rows = list(__import__("csv").DictReader((dirs["metrics"] / "correctness_metrics.csv").open()))
    stream_row = next(row for row in rows if row["stream_key"] == "key1")
    assert stream_row["worker_observed_for_stream"] == "False"
    assert stream_row["at_most_one_worker_per_stream"] == "True"
    assert stream_row["one_worker_per_stream"] == "False"


def test_resume_rejects_existing_run_id_repetition(tmp_path):
    cfg = config(tmp_path)
    cfg.resume = True
    root = cfg.report_root
    (root / "raw").mkdir(parents=True)
    (root / "raw" / "streams.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "run", "repetition": 1, "timestamp": 10.0}) + "\n"
    )

    try:
        runner.prepare_report_root(cfg)
    except RuntimeError as exc:
        assert "run_id/repetition" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_resume_allows_new_run_id(tmp_path):
    cfg = config(tmp_path)
    root = cfg.report_root
    (root / "raw").mkdir(parents=True)
    (root / "raw" / "streams.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "old-run", "repetition": 1, "timestamp": 10.0}) + "\n"
    )
    cfg.resume = True
    cfg.run_id = "new-run"

    assert runner.prepare_report_root(cfg) == root


def test_patch_proxy_context_skips_deployment_when_snapshot_fails(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg.kubectl_path = "/bin/true"
    cfg.patch_proxy_context = True
    dirs = runner.ensure_layout(cfg.report_root)
    commands = []

    def fake_snapshot(config_arg, deployment, keys):
        return {"deployment": deployment, "values": {key: None for key in keys}, "snapshot_ok": deployment != "proxy", "kubectl": {"returncode": 1 if deployment == "proxy" else 0}}

    def fake_run_cmd(command, timeout=60):
        commands.append(command)
        return {"returncode": 0, "command": command, "stdout": "", "stderr": ""}

    monkeypatch.setattr(runner, "deployment_env_snapshot", fake_snapshot)
    monkeypatch.setattr(runner, "run_cmd", fake_run_cmd)

    result = runner.patch_proxy_context(cfg, dirs)

    assert "proxy" not in result["patched_deployments"]
    assert "controller" in result["patched_deployments"]
    assert any(item["deployment"] == "proxy" for item in result["skipped_deployments"])
    assert not any("deployment/proxy" in part for command in commands for part in command)


def test_duplicate_streamkey_metrics_report_controller_rejection(tmp_path):
    cfg = config(tmp_path, scenario="duplicate-streamkey")
    dirs = runner.ensure_layout(cfg.report_root)
    (dirs["raw"] / "streams.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "run", "repetition": 1, "timestamp": 10.0, "stream_keys": ["key1"]}) + "\n" +
        json.dumps({"event": "run_finished", "run_id": "run", "repetition": 1, "ended_at": 30.0, "stream_keys": ["key1"]}) + "\n"
    )
    publishers = [
        {"event": "publisher_finished", "experiment_id": "exp", "scenario": "duplicate-streamkey", "run_id": "run", "repetition": 1, "publisher_index": 1, "stream_key": "key1", "started_at": 11.0, "ended_at": 29.0, "returncode": 0, "publisher_status": "success"},
        {"event": "publisher_finished", "experiment_id": "exp", "scenario": "duplicate-streamkey", "run_id": "run", "repetition": 1, "publisher_index": 2, "stream_key": "key1", "started_at": 12.0, "ended_at": 13.0, "returncode": 1, "publisher_status": "expected_conflict_or_stopped"},
    ]
    (dirs["raw"] / "publishers.jsonl").write_text("".join(json.dumps(e) + "\n" for e in publishers))
    events = [
        {"timestamp_epoch": 12.5, "event_type": "handover_denied", "stream": "key1", "run_id": "run", "repetition": 1},
    ]
    (dirs["raw"] / "controller_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    runner.build_metrics(cfg, dirs)

    duplicate_rows = list(__import__("csv").DictReader((dirs["metrics"] / "duplicate_streamkey_metrics.csv").open()))
    assert duplicate_rows[0]["duplicate_streamkey_attempted"] == "True"
    assert duplicate_rows[0]["duplicate_streamkey_rejected"] == "True"
    assert duplicate_rows[0]["duplicate_streamkey_unexpectedly_accepted"] == "False"
    correctness_rows = list(__import__("csv").DictReader((dirs["metrics"] / "correctness_metrics.csv").open()))
    stream_row = next(row for row in correctness_rows if row["stream_key"] == "key1")
    assert stream_row["duplicate_streamkey_rejected"] == "True"


def test_worker_overlap_detected_from_controller_events_without_snapshots(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    (dirs["raw"] / "streams.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "run", "repetition": 1, "timestamp": 10.0, "stream_keys": ["key1"]}) + "\n" +
        json.dumps({"event": "run_finished", "run_id": "run", "repetition": 1, "ended_at": 30.0, "stream_keys": ["key1"]}) + "\n"
    )
    (dirs["raw"] / "publishers.jsonl").write_text(
        json.dumps({"event": "publisher_finished", "run_id": "run", "repetition": 1, "publisher_index": 1, "stream_key": "key1", "started_at": 11.0, "ended_at": 29.0, "returncode": 0}) + "\n"
    )
    events = [
        {"timestamp_epoch": 12.0, "event_type": "worker_created", "stream": "key1", "worker_pod": "worker-a", "run_id": "run", "repetition": 1},
        {"timestamp_epoch": 13.0, "event_type": "worker_created", "stream": "key1", "worker_pod": "worker-b", "run_id": "run", "repetition": 1},
        {"timestamp_epoch": 14.0, "event_type": "worker_deleted", "stream": "key1", "worker_pod": "worker-a", "run_id": "run", "repetition": 1},
    ]
    (dirs["raw"] / "controller_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    runner.build_metrics(cfg, dirs)

    rows = list(__import__("csv").DictReader((dirs["metrics"] / "correctness_metrics.csv").open()))
    stream_row = next(row for row in rows if row["stream_key"] == "key1")
    assert stream_row["max_worker_count_observed"] == "2"
    assert stream_row["duplicate_worker_detected"] == "True"


def test_handover_counts_are_scoped_per_stream(tmp_path):
    cfg = config(tmp_path, scenario="concurrency")
    cfg.stream_keys = ["key1", "key2"]
    dirs = runner.ensure_layout(cfg.report_root)
    (dirs["raw"] / "streams.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "run", "repetition": 1, "timestamp": 10.0, "stream_keys": ["key1", "key2"]}) + "\n" +
        json.dumps({"event": "run_finished", "run_id": "run", "repetition": 1, "ended_at": 30.0, "stream_keys": ["key1", "key2"]}) + "\n"
    )
    (dirs["raw"] / "publishers.jsonl").write_text(
        json.dumps({"event": "publisher_finished", "run_id": "run", "repetition": 1, "publisher_index": 1, "stream_key": "key1", "started_at": 11.0, "ended_at": 29.0, "returncode": 0}) + "\n" +
        json.dumps({"event": "publisher_finished", "run_id": "run", "repetition": 1, "publisher_index": 1, "stream_key": "key2", "started_at": 11.0, "ended_at": 29.0, "returncode": 0}) + "\n"
    )
    events = [
        {"timestamp_epoch": 12.0, "event_type": "handover_accepted", "stream": "key1", "proxy_pod": "proxy-b", "run_id": "run", "repetition": 1},
    ]
    (dirs["raw"] / "controller_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    runner.build_metrics(cfg, dirs)

    rows = list(__import__("csv").DictReader((dirs["metrics"] / "correctness_metrics.csv").open()))
    by_stream = {row["stream_key"]: row for row in rows if row["stream_key"] != "__orphans__"}
    assert by_stream["key1"]["handover_accepted"] == "1"
    assert by_stream["key2"]["handover_accepted"] == "0"


def test_duplicate_streamkey_inconclusive_without_second_proxy(tmp_path):
    cfg = config(tmp_path, scenario="duplicate-streamkey")
    dirs = runner.ensure_layout(cfg.report_root)
    (dirs["raw"] / "streams.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "run", "repetition": 1, "timestamp": 10.0, "stream_keys": ["key1"]}) + "\n" +
        json.dumps({"event": "run_finished", "run_id": "run", "repetition": 1, "ended_at": 30.0, "stream_keys": ["key1"]}) + "\n"
    )
    publishers = [
        {"event": "publisher_finished", "experiment_id": "exp", "scenario": "duplicate-streamkey", "run_id": "run", "repetition": 1, "publisher_index": 1, "stream_key": "key1", "started_at": 11.0, "ended_at": 29.0, "returncode": 0, "publisher_status": "success"},
        {"event": "publisher_finished", "experiment_id": "exp", "scenario": "duplicate-streamkey", "run_id": "run", "repetition": 1, "publisher_index": 2, "stream_key": "key1", "started_at": 12.0, "ended_at": 13.0, "returncode": 1, "publisher_status": "duplicate_publisher_exited"},
    ]
    (dirs["raw"] / "publishers.jsonl").write_text("".join(json.dumps(e) + "\n" for e in publishers))
    # Only one proxy is observed, so the controller-level between-proxy conflict claim is inconclusive.
    events = [
        {"timestamp_epoch": 11.0, "event_type": "publish_received", "stream": "key1", "proxy_pod": "proxy-a", "run_id": "run", "repetition": 1},
        {"timestamp_epoch": 12.5, "event_type": "handover_denied", "stream": "key1", "proxy_pod": "proxy-a", "run_id": "run", "repetition": 1},
    ]
    (dirs["raw"] / "controller_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    runner.build_metrics(cfg, dirs)

    duplicate_rows = list(__import__("csv").DictReader((dirs["metrics"] / "duplicate_streamkey_metrics.csv").open()))
    assert duplicate_rows[0]["scenario_inconclusive"] == "True"
    assert duplicate_rows[0]["controller_rejection_status"] == "rejected"
    assert duplicate_rows[0]["between_proxy_validity_status"] == "inconclusive"


def test_duplicate_streamkey_uses_generic_controller_conflict_evidence(tmp_path):
    cfg = config(tmp_path, scenario="duplicate-streamkey")
    dirs = runner.ensure_layout(cfg.report_root)
    (dirs["raw"] / "streams.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "run", "repetition": 1, "timestamp": 10.0, "stream_keys": ["key1"]}) + "\n" +
        json.dumps({"event": "run_finished", "run_id": "run", "repetition": 1, "ended_at": 30.0, "stream_keys": ["key1"]}) + "\n"
    )
    publishers = [
        {"event": "publisher_finished", "run_id": "run", "repetition": 1, "publisher_index": 1, "stream_key": "key1", "started_at": 11.0, "ended_at": 29.0, "returncode": 0, "publisher_status": "success"},
        {"event": "publisher_finished", "run_id": "run", "repetition": 1, "publisher_index": 2, "stream_key": "key1", "started_at": 12.0, "ended_at": 13.0, "returncode": 1, "publisher_status": "duplicate_publisher_exited"},
    ]
    (dirs["raw"] / "publishers.jsonl").write_text("".join(json.dumps(e) + "\n" for e in publishers))
    events = [
        {"timestamp_epoch": 11.0, "event_type": "publish_received", "stream": "key1", "proxy_pod": "proxy-a", "run_id": "run", "repetition": 1},
        {"timestamp_epoch": 12.0, "event_type": "publish_received", "stream": "key1", "proxy_pod": "proxy-b", "run_id": "run", "repetition": 1},
        {"timestamp_epoch": 12.5, "event_type": "stream_started_conflict", "stream": "key1", "proxy_pod": "proxy-b", "status": "conflict", "run_id": "run", "repetition": 1},
    ]
    (dirs["raw"] / "controller_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    runner.build_metrics(cfg, dirs)

    duplicate_rows = list(__import__("csv").DictReader((dirs["metrics"] / "duplicate_streamkey_metrics.csv").open()))
    assert duplicate_rows[0]["duplicate_streamkey_rejected"] == "True"
    assert duplicate_rows[0]["secondary_proxy_observed"] == "True"
    assert duplicate_rows[0]["status"] == "rejected"


def test_prometheus_scope_uses_effective_patch_result(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg.patch_proxy_context = True
    cfg.prometheus_url = "http://prometheus.example"
    dirs = runner.ensure_layout(cfg.report_root)
    captured = []

    def fake_urlopen(request, timeout=30):
        captured.append(request.full_url)
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"status":"success","data":{"result":[]}}'
        return Response()

    monkeypatch.setattr(runner, "urlopen", fake_urlopen)

    # Empty selector simulates --patch-proxy-context being requested while the
    # controller deployment was not effectively patched/rolled out.
    runner.collect_prometheus(cfg, dirs, start=1.0, end=2.0, controller_label_selector="")

    joined = "\n".join(captured)
    assert 'tenant%3D%22exp%22' not in joined
    assert json.loads((dirs["raw"] / "prometheus_range_queries.json").read_text())["_metadata"]["controller_scope_effective"] is False


def test_restore_failure_changes_exit_code_by_default(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg.patch_proxy_context = True
    cfg.kubectl_path = "/bin/true"
    cfg.ffmpeg_path = "/bin/true"
    cfg.output_dir = tmp_path / "out"

    monkeypatch.setattr(runner, "parse_args", lambda argv=None: cfg)
    monkeypatch.setattr(runner, "execute_experiment", lambda config_arg, dirs: {
        "status": "failed_restore",
        "restore_ok": False,
        "started_at": 1.0,
        "ended_at": 2.0,
        "preflight": {"proxy_context_patch": {"controller_scope_effective": False}},
        "runs": [],
        "prometheus": {},
    })
    monkeypatch.setattr(runner, "build_metrics", lambda config_arg, dirs: {"activation": {}, "resources": [], "cost": [], "missing": []})
    monkeypatch.setattr(runner, "generate_charts", lambda dirs: {})
    monkeypatch.setattr(runner, "generate_report", lambda config_arg, dirs, execution, metrics, charts: {})

    assert runner.main([]) == 1


def test_restore_failure_can_be_allowed_explicitly(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg.patch_proxy_context = True
    cfg.allow_restore_failure = True
    cfg.kubectl_path = "/bin/true"
    cfg.ffmpeg_path = "/bin/true"
    cfg.output_dir = tmp_path / "out"

    monkeypatch.setattr(runner, "parse_args", lambda argv=None: cfg)
    monkeypatch.setattr(runner, "execute_experiment", lambda config_arg, dirs: {
        "status": "failed_restore",
        "restore_ok": False,
        "started_at": 1.0,
        "ended_at": 2.0,
        "preflight": {"proxy_context_patch": {"controller_scope_effective": False}},
        "runs": [],
        "prometheus": {},
    })
    monkeypatch.setattr(runner, "build_metrics", lambda config_arg, dirs: {"activation": {}, "resources": [], "cost": [], "missing": []})
    monkeypatch.setattr(runner, "generate_charts", lambda dirs: {})
    monkeypatch.setattr(runner, "generate_report", lambda config_arg, dirs, execution, metrics, charts: {})

    assert runner.main([]) == 0


def test_second_proxy_must_be_correlated_after_second_attempt(tmp_path):
    cfg = config(tmp_path, scenario="duplicate-streamkey")
    dirs = runner.ensure_layout(cfg.report_root)
    (dirs["raw"] / "streams.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "run", "repetition": 1, "timestamp": 10.0, "stream_keys": ["key1"]}) + "\n" +
        json.dumps({"event": "run_finished", "run_id": "run", "repetition": 1, "ended_at": 30.0, "stream_keys": ["key1"]}) + "\n"
    )
    publishers = [
        {"event": "publisher_finished", "run_id": "run", "repetition": 1, "publisher_index": 1, "stream_key": "key1", "started_at": 11.0, "ended_at": 29.0, "returncode": 0, "publisher_status": "success"},
        {"event": "publisher_finished", "run_id": "run", "repetition": 1, "publisher_index": 2, "stream_key": "key1", "started_at": 20.0, "ended_at": 22.0, "returncode": 1, "publisher_status": "duplicate_publisher_exited"},
    ]
    (dirs["raw"] / "publishers.jsonl").write_text("".join(json.dumps(e) + "\n" for e in publishers))
    events = [
        {"timestamp_epoch": 11.0, "event_type": "publish_received", "stream": "key1", "proxy_pod": "proxy-a", "run_id": "run", "repetition": 1},
        {"timestamp_epoch": 12.0, "event_type": "publish_received", "stream": "key1", "proxy_pod": "proxy-b", "run_id": "run", "repetition": 1},
        {"timestamp_epoch": 21.0, "event_type": "stream_started_conflict", "stream": "key1", "proxy_pod": "proxy-a", "status": "conflict", "run_id": "run", "repetition": 1},
    ]
    (dirs["raw"] / "controller_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    runner.build_metrics(cfg, dirs)

    duplicate_rows = list(__import__("csv").DictReader((dirs["metrics"] / "duplicate_streamkey_metrics.csv").open()))
    assert duplicate_rows[0]["second_attempt_proxy_pod"] == "proxy-a"
    assert duplicate_rows[0]["secondary_proxy_observed"] == "False"
    assert duplicate_rows[0]["scenario_inconclusive"] == "True"
    assert duplicate_rows[0]["controller_rejection_status"] == "rejected"
    assert duplicate_rows[0]["between_proxy_validity_status"] == "inconclusive"
