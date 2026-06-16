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


def test_missing_metrics_ignores_prometheus_metadata(tmp_path):
    cfg = config(tmp_path)
    missing = runner.missing_metrics(
        cfg,
        {
            "_metadata": {"started_at": 1.0, "ended_at": 2.0},
            "controller_active_streams": {"available": True, "response": {"status": "success"}},
        },
        activation_rows=[{
            "t_controller_received_event": 1.0,
            "t_worker_create_requested": 1.1,
            "t_worker_ready": 1.2,
            "t_ffmpeg_started": 1.3,
            "t_ffmpeg_first_progress": 1.4,
            "t_destination_received": 1.5,
        }],
        release_rows=[{"total_release_seconds": 2.0}],
    )

    assert "_metadata" not in missing
    assert "controller_active_streams" not in missing


def test_worker_ffmpeg_promql_is_namespace_scoped_by_default(tmp_path):
    cfg = config(tmp_path)
    rendered = runner.render_promql(cfg, "worker_ffmpeg_running$worker_metric_label_selector")

    assert rendered == 'worker_ffmpeg_running{namespace="media"}'


def test_worker_ffmpeg_promql_can_be_unscoped_explicitly(tmp_path):
    cfg = config(tmp_path)
    cfg.worker_metric_label_selector = ""
    rendered = runner.render_promql(cfg, "worker_ffmpeg_running$worker_metric_label_selector")

    assert rendered == "worker_ffmpeg_running"


def test_duplicate_publisher_nonzero_exit_keeps_process_status_separate(tmp_path):
    cfg = config(tmp_path, scenario="duplicate-streamkey")
    result = {"returncode": 1, "publisher_index": 2, "stop_reason": None}

    assert runner.publisher_process_status(result) == "nonzero_exit"
    assert runner.publisher_status(cfg, result) == "duplicate_publisher_exited"


def test_context_patch_incomplete_changes_exit_code_by_default(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg.patch_proxy_context = True
    cfg.kubectl_path = "/bin/true"
    cfg.ffmpeg_path = "/bin/true"
    cfg.output_dir = tmp_path / "out"

    monkeypatch.setattr(runner, "parse_args", lambda argv=None: cfg)
    monkeypatch.setattr(runner, "execute_experiment", lambda config_arg, dirs: {
        "status": "partial_context_patch",
        "context_scope_ok": False,
        "context_patch_status": "incomplete",
        "restore_ok": True,
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


def test_context_patch_incomplete_can_be_allowed_explicitly(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg.patch_proxy_context = True
    cfg.allow_unscoped_context = True
    cfg.kubectl_path = "/bin/true"
    cfg.ffmpeg_path = "/bin/true"
    cfg.output_dir = tmp_path / "out"

    monkeypatch.setattr(runner, "parse_args", lambda argv=None: cfg)
    monkeypatch.setattr(runner, "execute_experiment", lambda config_arg, dirs: {
        "status": "partial_context_patch",
        "context_scope_ok": False,
        "context_patch_status": "incomplete",
        "restore_ok": True,
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


def test_deployment_env_snapshot_refuses_value_from_target_keys(monkeypatch, tmp_path):
    cfg = config(tmp_path)

    def fake_kubectl_json(config_arg, args, timeout=60):
        return {
            "returncode": 0,
            "json": {
                "spec": {"template": {"spec": {"containers": [{
                    "name": "proxy",
                    "env": [{"name": "EXPERIMENT_ID", "valueFrom": {"secretKeyRef": {"name": "ctx", "key": "experiment"}}}],
                }]}}}
            },
        }

    monkeypatch.setattr(runner, "kubectl_json", fake_kubectl_json)

    snapshot = runner.deployment_env_snapshot(cfg, "proxy", ["EXPERIMENT_ID", "SCENARIO", "RUN_ID"])

    assert snapshot["snapshot_ok"] is True
    assert snapshot["safe_to_patch"] is False
    assert snapshot["unsafe_value_from_keys"] == ["EXPERIMENT_ID"]
    assert snapshot["reason"] == "target_keys_use_valueFrom"


def test_deployment_env_snapshot_requires_container_for_sidecars(monkeypatch, tmp_path):
    cfg = config(tmp_path)

    def fake_kubectl_json(config_arg, args, timeout=60):
        return {
            "returncode": 0,
            "json": {
                "spec": {"template": {"spec": {"containers": [
                    {"name": "proxy", "env": []},
                    {"name": "sidecar", "env": []},
                ]}}}
            },
        }

    monkeypatch.setattr(runner, "kubectl_json", fake_kubectl_json)

    snapshot = runner.deployment_env_snapshot(cfg, "proxy", ["EXPERIMENT_ID", "SCENARIO", "RUN_ID"])

    assert snapshot["safe_to_patch"] is False
    assert snapshot["reason"] == "multiple_containers_require_explicit_container"


def test_deployment_env_snapshot_allows_explicit_container_with_sidecars(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg.proxy_container = "proxy"

    def fake_kubectl_json(config_arg, args, timeout=60):
        return {
            "returncode": 0,
            "json": {
                "spec": {"template": {"spec": {"containers": [
                    {"name": "proxy", "env": [{"name": "SCENARIO", "value": "old"}]},
                    {"name": "sidecar", "env": [{"name": "SCENARIO", "value": "sidecar"}]},
                ]}}}
            },
        }

    monkeypatch.setattr(runner, "kubectl_json", fake_kubectl_json)

    snapshot = runner.deployment_env_snapshot(cfg, "proxy", ["EXPERIMENT_ID", "SCENARIO", "RUN_ID"])

    assert snapshot["safe_to_patch"] is True
    assert snapshot["target_container"] == "proxy"
    assert snapshot["values"]["SCENARIO"] == "old"


def test_restore_context_keys_targets_original_container(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    commands = []

    def fake_run_cmd(command, timeout=60):
        commands.append(command)
        return {"returncode": 0, "command": command, "stdout": "", "stderr": ""}

    monkeypatch.setattr(runner, "run_cmd", fake_run_cmd)
    patch_result = {
        "patched_deployments": ["proxy"],
        "previous_env": {
            "proxy": {
                "snapshot_ok": True,
                "safe_to_patch": True,
                "target_container": "proxy",
                "values": {"EXPERIMENT_ID": None, "SCENARIO": "old", "RUN_ID": None},
            }
        },
    }

    result = runner.restore_context_keys(cfg, dirs, patch_result)

    assert result["ok"] is True
    assert any("--containers=proxy" in part for command in commands for part in command)


def test_inconclusive_duplicate_streamkey_changes_exit_code_by_default(monkeypatch, tmp_path):
    cfg = config(tmp_path, scenario="duplicate-streamkey")
    cfg.ffmpeg_path = "/bin/true"
    cfg.output_dir = tmp_path / "out"

    monkeypatch.setattr(runner, "parse_args", lambda argv=None: cfg)
    monkeypatch.setattr(runner, "execute_experiment", lambda config_arg, dirs: {
        "status": "valid",
        "restore_ok": True,
        "context_scope_ok": True,
        "started_at": 1.0,
        "ended_at": 2.0,
        "preflight": {"proxy_context_patch": {"controller_scope_effective": False}},
        "runs": [],
        "prometheus": {},
    })
    monkeypatch.setattr(runner, "build_metrics", lambda config_arg, dirs: {
        "activation": {}, "resources": [], "cost": [], "missing": [],
        "duplicate_streamkey": [{"scenario_inconclusive": True}],
    })
    monkeypatch.setattr(runner, "generate_charts", lambda dirs: {})
    monkeypatch.setattr(runner, "generate_report", lambda config_arg, dirs, execution, metrics, charts: {})

    assert runner.main([]) == 1


def test_inconclusive_duplicate_streamkey_can_be_allowed(monkeypatch, tmp_path):
    cfg = config(tmp_path, scenario="duplicate-streamkey")
    cfg.allow_inconclusive = True
    cfg.ffmpeg_path = "/bin/true"
    cfg.output_dir = tmp_path / "out"

    monkeypatch.setattr(runner, "parse_args", lambda argv=None: cfg)
    monkeypatch.setattr(runner, "execute_experiment", lambda config_arg, dirs: {
        "status": "valid",
        "restore_ok": True,
        "context_scope_ok": True,
        "started_at": 1.0,
        "ended_at": 2.0,
        "preflight": {"proxy_context_patch": {"controller_scope_effective": False}},
        "runs": [],
        "prometheus": {},
    })
    monkeypatch.setattr(runner, "build_metrics", lambda config_arg, dirs: {
        "activation": {}, "resources": [], "cost": [], "missing": [],
        "duplicate_streamkey": [{"scenario_inconclusive": True}],
    })
    monkeypatch.setattr(runner, "generate_charts", lambda dirs: {})
    monkeypatch.setattr(runner, "generate_report", lambda config_arg, dirs, execution, metrics, charts: {})

    assert runner.main([]) == 0


def test_duplicate_publisher_nonzero_without_rejection_changes_exit_code(monkeypatch, tmp_path):
    cfg = config(tmp_path, scenario="duplicate-streamkey")
    cfg.ffmpeg_path = "/bin/true"
    cfg.output_dir = tmp_path / "out"

    monkeypatch.setattr(runner, "parse_args", lambda argv=None: cfg)
    monkeypatch.setattr(runner, "execute_experiment", lambda config_arg, dirs: {
        "status": "valid",
        "restore_ok": True,
        "context_scope_ok": True,
        "started_at": 1.0,
        "ended_at": 2.0,
        "preflight": {"proxy_context_patch": {"controller_scope_effective": False}},
        "runs": [],
        "prometheus": {},
    })
    monkeypatch.setattr(runner, "build_metrics", lambda config_arg, dirs: {
        "activation": {}, "resources": [], "cost": [], "missing": [],
        "duplicate_streamkey": [{"duplicate_publisher_nonzero_without_controller_rejection": True}],
    })
    monkeypatch.setattr(runner, "generate_charts", lambda dirs: {})
    monkeypatch.setattr(runner, "generate_report", lambda config_arg, dirs, execution, metrics, charts: {})

    assert runner.main([]) == 1


def test_collect_prometheus_writes_per_run_files_and_loads_resume_safe_evidence(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg.prometheus_url = "http://prometheus.example"
    dirs = runner.ensure_layout(cfg.report_root)

    def fake_prometheus_query(config_arg, query, start, end, step=5, controller_label_selector=None):
        value = "1" if config_arg.run_id == "run" else "2"
        return {
            "available": True,
            "query": query,
            "rendered_query": query,
            "response": {"status": "success", "data": {"result": [{"metric": {"pod": "worker-a"}, "values": [[start, value], [end, value]]}]}},
        }

    monkeypatch.setattr(runner, "prometheus_query", fake_prometheus_query)
    monkeypatch.setattr(runner, "prometheus_instant_query", lambda config, query, ts, controller_label_selector=None: {
        "available": True,
        "query": query,
        "rendered_query": query,
        "response": {"status": "success", "data": {"result": []}},
    })

    runner.collect_prometheus(cfg, dirs, start=10.0, end=20.0)
    cfg.run_id = "run2"
    runner.collect_prometheus(cfg, dirs, start=100.0, end=110.0)

    assert (dirs["raw"] / "prometheus_range_queries.run.run.json").exists()
    assert (dirs["raw"] / "prometheus_range_queries.run.run2.json").exists()
    assert (dirs["raw"] / "prometheus_range_queries.__index__.json").exists()
    assert not any(path.name in {"prometheus_range_queries.index.json", "prometheus_range_queries.__index__.json"} for path in runner.prometheus_run_files(dirs))

    merged = runner.load_prometheus_evidence(dirs)
    assert [run["run_id"] for run in merged["_metadata"]["runs"]] == ["run", "run2"]
    assert runner.prom_values(merged["workers_active"]) == [1.0, 1.0, 2.0, 2.0]


def test_always_on_worker_reference_is_window_and_stream_count_aware(tmp_path):
    windows = [
        {"started_at": 0.0, "ended_at": 10.0, "stream_keys": ["a"]},
        {"started_at": 20.0, "ended_at": 30.0, "stream_keys": ["a", "b", "c"]},
    ]

    value, source = runner.always_on_worker_pod_seconds_reference(windows, ["fallback"], fallback_duration=999.0)

    assert value == 40.0
    assert source == "sum_per_run_window_stream_count_times_duration"


def test_concurrency_chart_is_not_generated_when_activation_samples_are_missing(tmp_path):
    cfg = config(tmp_path, scenario="concurrency")
    dirs = runner.ensure_layout(cfg.report_root)
    runner.write_json(dirs["root"] / "metadata.json", {"scenario": "concurrency"})
    runner.write_csv(
        dirs["metrics"] / "activation_metrics.csv",
        [{"concurrency": "5", "total_activation_seconds": ""}, {"concurrency": "10", "total_activation_seconds": "None"}],
        ["concurrency", "total_activation_seconds"],
    )
    runner.write_csv(dirs["metrics"] / "resource_usage.csv", [], ["metric", "component", "samples"])
    runner.write_csv(dirs["metrics"] / "resilience_metrics.csv", [], ["recovery_seconds"])

    charts = runner.generate_charts(dirs)

    assert charts["activation_p95_by_concurrency"].endswith("activation_p95_by_concurrency.txt")
    assert (dirs["charts"] / "activation_p95_by_concurrency.txt").exists()
    assert "finite observed activation samples" in (dirs["charts"] / "activation_p95_by_concurrency.txt").read_text()


def test_report_json_exposes_evidence_validity_summary(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    runner.write_json(dirs["root"] / "metadata.json", {"scenario": "cold-start", "experiment_id": "exp"})
    runner.write_csv(dirs["metrics"] / "activation_metrics.csv", [{"total_activation_seconds": "1.5"}], ["total_activation_seconds"])
    runner.write_csv(dirs["metrics"] / "correctness_metrics.csv", [{"worker_observed_for_stream": "True"}], ["worker_observed_for_stream"])
    runner.write_csv(dirs["metrics"] / "duplicate_streamkey_metrics.csv", [], ["scenario_inconclusive"])
    runner.write_csv(dirs["metrics"] / "resilience_metrics.csv", [], ["run_id"])
    (dirs["raw"] / "publishers.jsonl").write_text("", encoding="utf-8")
    (dirs["raw"] / "controller_events.jsonl").write_text('{"event_type":"publish_received"}\n', encoding="utf-8")
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_started", "run_id": "run", "repetition": 1, "timestamp": 1.0, "stream_keys": ["key1"]})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_finished", "run_id": "run", "repetition": 1, "ended_at": 2.0, "stream_keys": ["key1"]})
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run.json",
        {"_metadata": {"run_id": "run", "started_at": 1.0, "ended_at": 2.0}, "workers_active": {"available": True, "response": {"status": "success", "data": {"result": [{"metric": {}, "values": [[1.0, "1"]]}]}}}},
    )

    report = runner.generate_report(
        cfg,
        dirs,
        execution={"restore_ok": True, "context_scope_ok": True, "context_patch_status": "not_requested", "preflight": {"proxy_context_patch": {}}},
        metrics={"activation": {}, "resources": [], "cost": [], "missing": []},
        charts={},
    )

    summary = report["summary"]
    assert summary["prometheus_evidence_files_complete"] is True
    assert summary["prometheus_resume_safe"] is True
    assert summary["prometheus_analysis_ready"] is False
    assert summary["prometheus_samples_observed"] is True
    assert summary["resource_baseline_window_aware"] is True
    assert summary["observable_activation_samples"] == 1
    assert summary["worker_observed_samples"] == 1
    assert summary["controller_events_observed"] is True


def test_prometheus_coverage_detects_missing_run_evidence(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_started", "run_id": "run-a", "repetition": 1, "timestamp": 1.0, "stream_keys": ["key1"]})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_finished", "run_id": "run-a", "repetition": 1, "ended_at": 2.0, "stream_keys": ["key1"]})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_started", "run_id": "run-b", "repetition": 1, "timestamp": 10.0, "stream_keys": ["key1"]})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_finished", "run_id": "run-b", "repetition": 1, "ended_at": 20.0, "stream_keys": ["key1"]})
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run-a.json",
        {"_metadata": {"run_id": "run-a", "started_at": 1.0, "ended_at": 2.0}, "workers_active": {"available": True, "response": {"status": "success", "data": {"result": [{"metric": {}, "values": [[1.0, "1"]]}]}}}},
    )

    coverage = runner.prometheus_run_coverage(cfg, dirs)

    assert coverage["complete"] is False
    assert coverage["expected_run_ids"] == ["run-a", "run-b"]
    assert coverage["observed_run_ids"] == ["run-a"]
    assert coverage["missing_run_ids"] == ["run-b"]


def test_prometheus_metric_coverage_records_per_run_gaps(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    for run_id in ["run-a", "run-b"]:
        runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_started", "run_id": run_id, "repetition": 1, "timestamp": 1.0, "stream_keys": ["key1"]})
        runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_finished", "run_id": run_id, "repetition": 1, "ended_at": 2.0, "stream_keys": ["key1"]})
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run-a.json",
        {"_metadata": {"run_id": "run-a"}, "workers_active": {"available": True, "response": {"status": "success", "data": {"result": [{"metric": {}, "values": [[1.0, "1"]]}]}}}},
    )
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run-b.json",
        {"_metadata": {"run_id": "run-b"}, "workers_active": {"available": False, "reason": "missing"}},
    )

    rows = runner.prometheus_metric_coverage_rows(cfg, dirs)
    worker_rows = [row for row in rows if row["metric"] == "workers_active"]

    assert len(worker_rows) == 2
    assert [row["run_id"] for row in worker_rows] == ["run-a", "run-b"]
    assert worker_rows[0]["available"] is True
    assert worker_rows[0]["sample_count"] == 1
    assert worker_rows[1]["available"] is False
    assert "workers_active" in runner.incomplete_prometheus_metric_names(rows)


def test_report_json_persists_final_automation_verdict(tmp_path):
    cfg = config(tmp_path, scenario="duplicate-streamkey")
    dirs = runner.ensure_layout(cfg.report_root)
    runner.write_json(dirs["root"] / "metadata.json", {"scenario": "duplicate-streamkey", "experiment_id": "exp"})
    runner.write_csv(dirs["metrics"] / "activation_metrics.csv", [], ["total_activation_seconds"])
    runner.write_csv(dirs["metrics"] / "correctness_metrics.csv", [], ["worker_observed_for_stream"])
    runner.write_csv(dirs["metrics"] / "duplicate_streamkey_metrics.csv", [{"scenario_inconclusive": "True"}], ["scenario_inconclusive"])
    runner.write_csv(dirs["metrics"] / "resilience_metrics.csv", [], ["run_id"])
    (dirs["raw"] / "publishers.jsonl").write_text("", encoding="utf-8")
    (dirs["raw"] / "controller_events.jsonl").write_text("", encoding="utf-8")
    metrics = {"activation": {}, "resources": [], "cost": [], "missing": [], "duplicate_streamkey": [{"scenario_inconclusive": True}]}
    execution = {"status": "valid", "restore_ok": True, "context_scope_ok": True, "preflight": {"proxy_context_patch": {}}}
    verdict = runner.automation_verdict(cfg, execution, metrics)

    report = runner.generate_report(cfg, dirs, execution=execution, metrics=metrics, charts={}, verdict=verdict)

    assert report["summary"]["automation_status"] == "failed"
    assert report["summary"]["automation_exit_code"] == 1
    assert "scenario_hypothesis_inconclusive" in report["summary"]["automation_failure_reasons"]


def test_safe_id_rejects_path_dot_components():
    import argparse

    for value in [".", ".."]:
        try:
            runner.safe_id(value, "experiment_id")
        except argparse.ArgumentTypeError:
            pass
        else:
            raise AssertionError(f"expected unsafe id to be rejected: {value}")


def test_safe_id_rejects_reserved_run_ids():
    import argparse

    for value in ["index", "latest", "__index__"]:
        try:
            runner.safe_id(value, "run_id")
        except argparse.ArgumentTypeError:
            pass
        else:
            raise AssertionError(f"expected reserved run id to be rejected: {value}")


def test_prometheus_success_without_samples_is_not_available_for_analysis(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_started", "run_id": "run-a", "repetition": 1, "timestamp": 1.0, "stream_keys": ["key1"]})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_finished", "run_id": "run-a", "repetition": 1, "ended_at": 2.0, "stream_keys": ["key1"]})
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run-a.json",
        {"_metadata": {"run_id": "run-a"}, "workers_active": {"available": True, "response": {"status": "success", "data": {"result": []}}}},
    )

    rows = runner.prometheus_metric_coverage_rows(cfg, dirs)
    worker = [row for row in rows if row["metric"] == "workers_active"][0]

    assert worker["query_success"] is True
    assert worker["samples_observed"] is False
    assert worker["available_for_analysis"] is False
    assert worker["available"] is False
    assert "workers_active" in runner.incomplete_prometheus_metric_names(rows)


def test_resource_activity_reduction_requires_worker_samples(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    runner.write_json(dirs["root"] / "metadata.json", {"started_at": 1.0, "ended_at": 2.0})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_started", "run_id": "run-a", "repetition": 1, "timestamp": 1.0, "stream_keys": ["key1"]})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_finished", "run_id": "run-a", "repetition": 1, "ended_at": 2.0, "stream_keys": ["key1"]})
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run-a.json",
        {"_metadata": {"run_id": "run-a", "started_at": 1.0, "ended_at": 2.0}, "workers_active": {"available": True, "response": {"status": "success", "data": {"result": []}}}},
    )

    metrics = runner.build_metrics(cfg, dirs)
    relative = next(row for row in metrics["cost"] if row["metric"] == "relative_worker_activity_reduction_vs_always_on")
    worker = next(row for row in metrics["cost"] if row["metric"] == "worker_pod_seconds")

    assert relative["value"] is None
    assert relative["source"] == "insufficient_prometheus_worker_samples"
    assert worker["source"] == "insufficient_prometheus_worker_samples"


def test_resource_activity_ignores_extra_prometheus_run_files(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    runner.write_json(dirs["root"] / "metadata.json", {"started_at": 0.0, "ended_at": 10.0})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_started", "run_id": "run-a", "repetition": 1, "timestamp": 0.0, "stream_keys": ["key1"]})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_finished", "run_id": "run-a", "repetition": 1, "ended_at": 10.0, "stream_keys": ["key1"]})
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run.run-a.json",
        {"_metadata": {"run_id": "run-a", "started_at": 0.0, "ended_at": 10.0}, "workers_active": {"available": True, "response": {"status": "success", "data": {"result": [{"metric": {}, "values": [[0.0, "1"], [10.0, "1"]]}]}}}, "proxies_active": {"available": True, "response": {"status": "success", "data": {"result": [{"metric": {}, "values": [[0.0, "1"], [10.0, "1"]]}]}}}, "pod_cpu_rate": {"available": True, "response": {"status": "success", "data": {"result": [{"metric": {}, "values": [[0.0, "1"], [10.0, "1"]]}]}}}},
    )
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run.stale.json",
        {"_metadata": {"run_id": "stale", "started_at": 0.0, "ended_at": 10.0}, "workers_active": {"available": True, "response": {"status": "success", "data": {"result": [{"metric": {}, "values": [[0.0, "100"], [10.0, "100"]]}]}}}},
    )

    metrics = runner.build_metrics(cfg, dirs)
    worker = next(row for row in metrics["cost"] if row["metric"] == "worker_pod_seconds")

    assert worker["value"] == 10.0
    assert metrics["prometheus_coverage"]["extra_run_ids"] == ["stale"]


def test_require_prometheus_analysis_affects_automation_verdict(tmp_path):
    cfg = config(tmp_path)
    cfg.prometheus_url = "http://prometheus.example"
    cfg.require_prometheus_analysis = True
    metrics = {
        "duplicate_streamkey": [],
        "prometheus_coverage": {"complete": True, "expected_run_ids": ["run"]},
        "prometheus_metric_coverage": [
            {"run_id": "run", "metric": "workers_active", "available_for_analysis": True},
            {"run_id": "run", "metric": "proxies_active", "available_for_analysis": False},
            {"run_id": "run", "metric": "pod_cpu_rate", "available_for_analysis": True},
        ],
    }
    verdict = runner.automation_verdict(cfg, {"status": "valid"}, metrics)

    assert verdict["automation_exit_code"] == 1
    assert "prometheus_analysis_not_ready" in verdict["automation_failure_reasons"]


def test_report_root_always_uses_experiment_child_directory(tmp_path):
    parent = tmp_path / "exp"
    cfg = config(parent)
    cfg.experiment_id = "exp"

    assert cfg.report_root == parent / "exp"


def test_incomplete_prometheus_metrics_ignore_extra_run_files(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_started", "run_id": "expected", "repetition": 1, "timestamp": 1.0, "stream_keys": ["key1"]})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_finished", "run_id": "expected", "repetition": 1, "ended_at": 2.0, "stream_keys": ["key1"]})
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run.expected.json",
        {"_metadata": {"run_id": "expected"}, "workers_active": {"available": True, "response": {"status": "success", "data": {"result": [{"metric": {}, "values": [[1.0, "1"]]}]}}}},
    )
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run.stale.json",
        {"_metadata": {"run_id": "stale"}, "workers_active": {"available": True, "response": {"status": "success", "data": {"result": []}}}},
    )

    rows = runner.prometheus_metric_coverage_rows(cfg, dirs)
    stale_rows = [row for row in rows if row["run_id"] == "stale"]

    assert stale_rows
    assert all(row["expected_by_run_windows"] is False for row in stale_rows)
    assert "workers_active" not in runner.incomplete_prometheus_metric_names(rows)


def test_prometheus_metric_coverage_csv_includes_expected_flag(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    runner.write_json(dirs["root"] / "metadata.json", {"started_at": 1.0, "ended_at": 2.0})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_started", "run_id": "expected", "repetition": 1, "timestamp": 1.0, "stream_keys": ["key1"]})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_finished", "run_id": "expected", "repetition": 1, "ended_at": 2.0, "stream_keys": ["key1"]})
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run.expected.json",
        {"_metadata": {"run_id": "expected"}, "workers_active": {"available": True, "response": {"status": "success", "data": {"result": [{"metric": {}, "values": [[1.0, "1"]]}]}}}},
    )
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run.stale.json",
        {"_metadata": {"run_id": "stale"}, "workers_active": {"available": True, "response": {"status": "success", "data": {"result": []}}}},
    )

    runner.build_metrics(cfg, dirs)
    coverage_rows = runner.csv_rows(dirs["metrics"] / "prometheus_metric_coverage.csv")

    assert "expected_by_run_windows" in coverage_rows[0]
    stale_rows = [row for row in coverage_rows if row["run_id"] == "stale"]
    assert stale_rows
    assert all(row["expected_by_run_windows"].lower() == "false" for row in stale_rows)


def test_report_markdown_exposes_prometheus_analysis_readiness(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    runner.write_json(dirs["root"] / "metadata.json", {"scenario": "cold-start", "experiment_id": "exp"})
    runner.write_csv(dirs["metrics"] / "activation_metrics.csv", [{"total_activation_seconds": "1.5"}], ["total_activation_seconds"])
    runner.write_csv(dirs["metrics"] / "correctness_metrics.csv", [{"worker_observed_for_stream": "True"}], ["worker_observed_for_stream"])
    runner.write_csv(dirs["metrics"] / "duplicate_streamkey_metrics.csv", [], ["scenario_inconclusive"])
    runner.write_csv(dirs["metrics"] / "resilience_metrics.csv", [], ["run_id"])
    (dirs["raw"] / "publishers.jsonl").write_text("", encoding="utf-8")
    (dirs["raw"] / "controller_events.jsonl").write_text("", encoding="utf-8")

    runner.generate_report(
        cfg,
        dirs,
        execution={"restore_ok": True, "context_scope_ok": True, "context_patch_status": "not_requested", "preflight": {"proxy_context_patch": {}}},
        metrics={"activation": {}, "resources": [], "cost": [], "missing": []},
        charts={},
    )

    report_md = (dirs["root"] / "report.md").read_text(encoding="utf-8")
    assert "prometheus_evidence_files_complete" in report_md
    assert "prometheus_analysis_ready" in report_md


def test_prometheus_samples_observed_ignores_extra_stale_runs(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    runner.write_json(dirs["root"] / "metadata.json", {"scenario": "cold-start", "experiment_id": "exp"})
    runner.write_csv(dirs["metrics"] / "activation_metrics.csv", [], ["total_activation_seconds"])
    runner.write_csv(dirs["metrics"] / "correctness_metrics.csv", [], ["worker_observed_for_stream"])
    runner.write_csv(dirs["metrics"] / "duplicate_streamkey_metrics.csv", [], ["scenario_inconclusive"])
    runner.write_csv(dirs["metrics"] / "resilience_metrics.csv", [], ["run_id"])
    (dirs["raw"] / "publishers.jsonl").write_text("", encoding="utf-8")
    (dirs["raw"] / "controller_events.jsonl").write_text("", encoding="utf-8")
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_started", "run_id": "expected", "repetition": 1, "timestamp": 1.0, "stream_keys": ["key1"]})
    runner.append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_finished", "run_id": "expected", "repetition": 1, "ended_at": 2.0, "stream_keys": ["key1"]})
    runner.write_json(
        dirs["raw"] / "prometheus_range_queries.run.stale.json",
        {"_metadata": {"run_id": "stale"}, "workers_active": {"available": True, "response": {"status": "success", "data": {"result": [{"metric": {}, "values": [[1.0, "1"]]}]}}}},
    )

    report = runner.generate_report(
        cfg,
        dirs,
        execution={"restore_ok": True, "context_scope_ok": True, "context_patch_status": "not_requested", "preflight": {"proxy_context_patch": {}}},
        metrics={"activation": {}, "resources": [], "cost": [], "missing": []},
        charts={},
    )

    assert report["summary"]["prometheus_samples_observed"] is False
    assert report["summary"]["prometheus_extra_run_ids"] == ["stale"]


def test_cost_estimation_legacy_alias_is_generated_for_prompt_compatibility(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)

    runner.build_metrics(cfg, dirs)

    assert (dirs["metrics"] / "resource_activity.csv").exists()
    assert (dirs["metrics"] / "cost_estimation.csv").exists()
    assert "deprecated_alias_notice" in (dirs["metrics"] / "cost_estimation.csv").read_text(encoding="utf-8")


def test_experiment_id_can_be_derived_from_output_dir(tmp_path):
    stream_file = tmp_path / "stream_keys.txt"
    stream_file.write_text("key1\n", encoding="utf-8")
    cfg = runner.parse_args([
        "--stream-keys-file", str(stream_file),
        "--scenario", "cold-start",
        "--duration-seconds", "120",
        "--repetitions", "30",
        "--prometheus-url", "http://localhost:9090",
        "--namespace", "media",
        "--output-dir", str(tmp_path / "reports" / "teste-final"),
    ])

    assert cfg.experiment_id == "teste-final"
    assert cfg.output_dir == tmp_path / "reports"
    assert cfg.report_root == tmp_path / "reports" / "teste-final"


def test_ffmpeg_command_generates_1080p30_testsrc_by_default(tmp_path):
    cfg = config(tmp_path)

    command = runner.ffmpeg_command(cfg, "key1")

    assert "testsrc=size=1920x1080:rate=30" in command
    assert "10000k" in command
    assert "-minrate" in command
    assert "20000k" in command
    assert "44100" in command
    assert "-pix_fmt" in command and "yuv420p" in command
    assert "-g" in command and "60" in command
    assert command[-1] == "rtmp://example/live/key1"


def test_ffmpeg_vbv_bufsize_preserves_bitrate_units():
    assert runner.ffmpeg_vbv_bufsize("6500k") == "13000k"
    assert runner.ffmpeg_vbv_bufsize("4M") == "8M"
    assert runner.ffmpeg_vbv_bufsize("1200000") == "2400000"


def test_ffmpeg_command_enforces_constant_bitrate_when_requested(tmp_path):
    cfg = config(tmp_path)
    cfg.bitrate = "6500k"
    cfg.constant_bitrate = True

    command = runner.ffmpeg_command(cfg, "key1")

    assert command[command.index("-b:v") + 1] == "6500k"
    assert command[command.index("-minrate") + 1] == "6500k"
    assert command[command.index("-maxrate") + 1] == "6500k"
    assert command[command.index("-bufsize") + 1] == "13000k"
    assert command[command.index("-x264-params") + 1] == "nal-hrd=cbr:force-cfr=1"


def test_ffmpeg_command_enforces_constant_bitrate_for_source_file(tmp_path):
    cfg = config(tmp_path)
    cfg.source_file = "sample.mp4"
    cfg.bitrate = "4M"
    cfg.constant_bitrate = True

    command = runner.ffmpeg_command(cfg, "key1")

    assert "-c:v" in command and "libx264" in command
    assert command[command.index("-minrate") + 1] == "4M"
    assert command[command.index("-maxrate") + 1] == "4M"
    assert command[command.index("-bufsize") + 1] == "8M"
    assert command[command.index("-x264-params") + 1] == "nal-hrd=cbr:force-cfr=1"


def test_ffmpeg_command_uses_tee_for_extra_rtmp_destinations(tmp_path):
    cfg = config(tmp_path)
    cfg.tee_rtmp_urls = ["rtmp://mirror-a/live", "rtmp://mirror-b/live"]

    command = runner.ffmpeg_command(cfg, "key1")

    assert command[-2] == "tee"
    assert command[-1] == (
        "[f=flv:onfail=ignore]rtmp://example/live/key1|"
        "[f=flv:onfail=ignore]rtmp://mirror-a/live/key1|"
        "[f=flv:onfail=ignore]rtmp://mirror-b/live/key1"
    )
    assert command.count("-c:v") == 1


def test_parse_args_accepts_generated_source_controls(tmp_path):
    cfg = runner.parse_args([
        "--stream-keys", "key1,key2",
        "--scenario", "cold-start",
        "--duration-seconds", "30",
        "--output-dir", str(tmp_path / "reports" / "smoke"),
        "--bitrate", "6500k",
        "--testsrc-size", "1920x1080",
        "--testsrc-rate", "30",
        "--audio-bitrate", "128k",
        "--constant-bitrate",
        "--tee-rtmp-urls", "rtmp://mirror-a/live,rtmps://mirror-b/live",
    ])

    assert cfg.stream_keys == ["key1", "key2"]
    assert cfg.bitrate == "6500k"
    assert cfg.testsrc_size == "1920x1080"
    assert cfg.testsrc_rate == "30"
    assert cfg.audio_bitrate == "128k"
    assert cfg.constant_bitrate is True
    assert cfg.tee_rtmp_urls == ["rtmp://mirror-a/live", "rtmps://mirror-b/live"]


def test_correctness_uses_unknown_run_id_controller_events_as_worker_evidence(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    (dirs["raw"] / "streams.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "run", "repetition": 1, "timestamp": 10.0, "stream_keys": ["key1"]}) + "\n" +
        json.dumps({"event": "run_finished", "run_id": "run", "repetition": 1, "ended_at": 20.0, "stream_keys": ["key1"]}) + "\n"
    )
    (dirs["raw"] / "publishers.jsonl").write_text(
        json.dumps({"event": "publisher_finished", "run_id": "run", "repetition": 1, "publisher_index": 1, "stream_key": "key1", "started_at": 11.0, "ended_at": 19.0, "returncode": 0}) + "\n"
    )
    events = [
        {"timestamp_epoch": 12.0, "event_type": "worker_created", "stream": "key1", "worker_pod": "worker-key1-abc", "run_id": "unknown", "scenario": "unknown"},
        {"timestamp_epoch": 13.0, "event_type": "ffmpeg_first_progress", "stream": "key1", "worker_pod": "worker-key1-abc", "run_id": "unknown", "scenario": "unknown"},
    ]
    (dirs["raw"] / "controller_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    runner.build_metrics(cfg, dirs)

    rows = list(__import__("csv").DictReader((dirs["metrics"] / "correctness_metrics.csv").open()))
    stream_row = next(row for row in rows if row["stream_key"] == "key1")
    assert stream_row["worker_observed_for_stream"] == "True"
    assert stream_row["one_worker_per_stream"] == "True"


def test_missing_metrics_does_not_require_destination_by_default(tmp_path):
    cfg = config(tmp_path)
    missing = runner.missing_metrics(
        cfg,
        {},
        activation_rows=[{
            "t_controller_received_event": 1.0,
            "t_worker_create_requested": 1.1,
            "t_worker_ready": 1.2,
            "t_ffmpeg_started": 1.3,
            "t_ffmpeg_first_progress": 1.4,
        }],
        release_rows=[{"total_release_seconds": 2.0}],
    )

    assert "t_destination_received" not in missing


def test_missing_metrics_can_require_destination_when_explicit(tmp_path):
    cfg = config(tmp_path)
    cfg.require_destination_received = True
    missing = runner.missing_metrics(
        cfg,
        {},
        activation_rows=[{
            "t_controller_received_event": 1.0,
            "t_worker_create_requested": 1.1,
            "t_worker_ready": 1.2,
            "t_ffmpeg_started": 1.3,
            "t_ffmpeg_first_progress": 1.4,
        }],
        release_rows=[{"total_release_seconds": 2.0}],
    )

    assert "t_destination_received" in missing


def test_require_network_metrics_promotes_proxy_network_to_required(tmp_path):
    cfg = config(tmp_path)
    assert "proxy_network_receive_bps" not in runner.required_prometheus_metrics_for_analysis(cfg)
    assert "proxy_network_transmit_bps" not in runner.required_prometheus_metrics_for_analysis(cfg)

    cfg.require_network_metrics = True

    required = runner.required_prometheus_metrics_for_analysis(cfg)
    assert "proxy_network_receive_bps" in required
    assert "proxy_network_transmit_bps" in required


def test_build_stream_result_rows_uses_unknown_run_id_controller_events_for_worker_columns(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    (dirs["raw"] / "streams.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "run", "repetition": 1, "timestamp": 10.0, "stream_keys": ["key1"]}) + "\n" +
        json.dumps({"event": "run_finished", "run_id": "run", "repetition": 1, "ended_at": 30.0, "stream_keys": ["key1"]}) + "\n"
    )
    (dirs["raw"] / "publishers.jsonl").write_text(
        json.dumps({"event": "publisher_finished", "run_id": "run", "repetition": 1, "publisher_index": 1, "stream_key": "key1", "started_at": 11.0, "ended_at": 29.0, "returncode": 0, "publisher_status": "success"}) + "\n"
    )
    activation_header = ["run_id", "repetition", "stream_key", "total_activation_seconds", "status"]
    with (dirs["metrics"] / "activation_metrics.csv").open("w", newline="") as f:
        import csv
        writer = csv.DictWriter(f, fieldnames=activation_header)
        writer.writeheader()
        writer.writerow({"run_id": "run", "repetition": "1", "stream_key": "key1", "total_activation_seconds": "5.0", "status": "derived_from_controller_structured_logs"})
    (dirs["metrics"] / "release_metrics.csv").write_text("run_id,repetition,stream_key,total_release_seconds\nrun,1,key1,1.0\n")
    (dirs["metrics"] / "correctness_metrics.csv").write_text(
        "run_id,repetition,stream_key,worker_observed_for_stream,duplicate_worker_detected,primary_proxy_pod\n"
        "run,1,key1,True,False,proxy-a\n"
    )
    events = [
        {"timestamp_epoch": 12.0, "event_type": "worker_created", "stream": "key1", "worker_pod": "worker-key1-abc", "proxy_pod": "proxy-a", "run_id": "unknown"},
        {"timestamp_epoch": 13.0, "event_type": "worker_ready_observed", "stream": "key1", "worker_pod": "worker-key1-abc", "proxy_pod": "proxy-a", "run_id": "unknown"},
    ]
    (dirs["raw"] / "controller_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    rows = runner.build_stream_result_rows(cfg, dirs)

    row = rows[0]
    assert row["initial_worker"] == "worker-key1-abc"
    assert row["final_worker"] == "worker-key1-abc"
    assert row["proxy_owner"] == "proxy-a"


def test_event_detection_small_negative_delta_is_clamped_and_classified(tmp_path):
    cfg = config(tmp_path)
    dirs = runner.ensure_layout(cfg.report_root)
    (dirs["raw"] / "streams.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "run", "repetition": 1, "timestamp": 90.0, "stream_keys": ["key1"]}) + "\n" +
        json.dumps({"event": "run_finished", "run_id": "run", "repetition": 1, "ended_at": 200.0, "stream_keys": ["key1"]}) + "\n"
    )
    (dirs["raw"] / "publishers.jsonl").write_text(
        json.dumps({"event":"publisher_finished","run_id":"run","repetition":1,"stream_key":"key1","started_at":100.0,"ended_at":200.0,"returncode":0}) + "\n"
    )
    events = [
        {"timestamp_epoch":101.010,"event_type":"stream_lifecycle_timestamp_observed","stream":"key1","message":"t_publish_start_proxy observed from proxy_hook"},
        {"timestamp_epoch":101.000,"event_type":"stream_lifecycle_timestamp_observed","stream":"key1","message":"t_controller_received_event observed from controller"},
        {"timestamp_epoch":102.0,"event_type":"stream_lifecycle_timestamp_observed","stream":"key1","message":"t_worker_create_requested observed from controller"},
        {"timestamp_epoch":103.0,"event_type":"stream_lifecycle_timestamp_observed","stream":"key1","message":"t_ffmpeg_first_progress observed from worker"},
    ]
    (dirs["raw"] / "controller_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    runner.build_metrics(cfg, dirs)

    import csv
    row = list(csv.DictReader((dirs["metrics"] / "activation_metrics.csv").open()))[0]
    assert row["event_detection_seconds"] == "0.0"
    assert row["event_detection_status"] == "clamped_to_zero_clock_skew_or_ordering_noise"
