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
