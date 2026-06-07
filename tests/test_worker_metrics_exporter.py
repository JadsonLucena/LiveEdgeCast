import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "worker_metrics_exporter", Path("docker/worker/metrics_exporter.py")
)
exporter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = exporter
spec.loader.exec_module(exporter)


def metric_value(payload, metric_name, labels=None):
    labels = labels or ""
    prefix = f"{metric_name}{labels} "
    for line in payload.splitlines():
        if line.startswith(prefix):
            return float(line.split()[-1])
    raise AssertionError(f"missing metric {prefix!r} in:\n{payload}")


def test_parse_complete_progress_fixture_uses_latest_record():
    fixture = Path("tests/fixtures/ffmpeg_progress/complete.progress").read_text()

    records = list(exporter.parse_progress_records(fixture))

    assert len(records) == 2
    assert exporter.parse_out_time_seconds(records[-1]) == 2.5
    assert records[-1]["total_size"] == "8192"
    assert records[-1]["speed"] == "0.98x"


def test_parse_partial_progress_fixture_ignores_unfinished_line_but_keeps_complete_values():
    fixture = Path("tests/fixtures/ffmpeg_progress/partial.progress").read_text()

    records = list(exporter.parse_progress_records(fixture))

    assert records[-1]["frame"] == "4"
    assert records[-1]["out_time"] == "00:00:04.000000"
    assert "speed" not in records[-1]


def test_progress_follower_handles_partial_write_completion_truncation_and_rotation(tmp_path):
    progress = tmp_path / "ffmpeg.progress"
    progress.write_text("frame=1\nout_time=00:00:01.000000\nspeed=1.")
    follower = exporter.ProgressFollower(str(progress))

    first = follower.poll(now=10.0)
    assert first["frame"] == "1"
    assert "speed" not in first
    progress.write_text(progress.read_text() + "25x\nprogress=continue\n")
    assert follower.poll(now=11.0)["speed"] == "1.25x"
    assert follower.latest_timestamp == 11.0

    progress.write_text("frame=2\ntotal_size=222\nout_time=00:00:02.000000\nspeed=0.75x\nprogress=continue\n")
    assert follower.poll(now=12.0)["total_size"] == "222"

    rotated = tmp_path / "ffmpeg.progress.rotated"
    progress.rename(rotated)
    progress.write_text("frame=3\ntotal_size=333\nout_time=00:00:03.000000\nspeed=1.00x\nprogress=continue\n")
    assert follower.poll(now=13.0)["total_size"] == "333"


def test_progress_follower_reads_records_beyond_chunk_boundaries(tmp_path):
    progress = tmp_path / "ffmpeg.progress"
    chunks = [
        f"frame={index}\ntotal_size={index * 100}\nout_time=00:00:{index:02d}.000000\nspeed=1.00x\nprogress=continue\n"
        for index in range(1, 8)
    ]
    progress.write_text("".join(chunks))
    follower = exporter.ProgressFollower(str(progress), read_chunk_size=37)

    latest = follower.poll(now=20.0)

    assert latest["frame"] == "7"
    assert latest["total_size"] == "700"
    assert exporter.parse_out_time_seconds(latest) == 7.0
    assert follower.latest_timestamp == 20.0


def test_progress_follower_refreshes_timestamp_for_identical_new_records(tmp_path):
    progress = tmp_path / "ffmpeg.progress"
    record = "frame=1\ntotal_size=100\nout_time=00:00:01.000000\nspeed=1.00x\nprogress=continue\n"
    progress.write_text(record)
    follower = exporter.ProgressFollower(str(progress))

    assert follower.poll(now=30.0)["frame"] == "1"
    assert follower.latest_timestamp == 30.0

    progress.write_text(record + record)

    assert follower.poll(now=31.0)["frame"] == "1"
    assert follower.latest_timestamp == 31.0


def test_metrics_collector_preserves_previous_gauges_during_partial_record(tmp_path, monkeypatch):
    progress = tmp_path / "ffmpeg.progress"
    pid_file = tmp_path / "ffmpeg.pid"
    exit_file = tmp_path / "ffmpeg.exit"
    state_file = tmp_path / "ffmpeg.exit.metrics_state"
    progress.write_text(
        "frame=1\ntotal_size=4096\nout_time=00:00:01.500000\nspeed=1.25x\nprogress=continue\n"
    )
    pid_file.write_text("1234")
    monkeypatch.setattr(exporter, "process_is_running", lambda pid: pid == 1234)
    times = iter([100.0, 101.0])
    collector = exporter.MetricsCollector(
        follower=exporter.ProgressFollower(str(progress)),
        pid_file=str(pid_file),
        exit_store=exporter.ExitCounterStore(str(exit_file), str(state_file)),
        stale_seconds=15.0,
        clock=lambda: next(times),
    )

    first_payload = collector.collect()
    assert metric_value(first_payload, "worker_ffmpeg_out_time_seconds") == 1.5
    assert metric_value(first_payload, "worker_ffmpeg_total_size_bytes") == 4096
    assert metric_value(first_payload, "worker_ffmpeg_speed") == 1.25

    progress.write_text(progress.read_text() + "frame=2\n")
    partial_payload = collector.collect()

    assert metric_value(partial_payload, "worker_ffmpeg_out_time_seconds") == 1.5
    assert metric_value(partial_payload, "worker_ffmpeg_total_size_bytes") == 4096
    assert metric_value(partial_payload, "worker_ffmpeg_speed") == 1.25
    assert metric_value(partial_payload, "worker_ffmpeg_last_progress_timestamp_seconds") == 101.0


def test_progress_follower_keeps_last_record_when_progress_file_io_fails(tmp_path, monkeypatch):
    progress = tmp_path / "ffmpeg.progress"
    progress.write_text(
        "frame=1\ntotal_size=4096\nout_time=00:00:01.500000\nspeed=1.25x\nprogress=continue\n"
    )
    follower = exporter.ProgressFollower(str(progress))

    assert follower.poll(now=40.0)["total_size"] == "4096"

    def raise_permission_error(_path):
        raise PermissionError("progress file temporarily unreadable")

    monkeypatch.setattr(exporter.os, "stat", raise_permission_error)

    assert follower.poll(now=41.0)["total_size"] == "4096"
    assert follower.latest_timestamp == 40.0


def test_progress_follower_keeps_last_record_when_progress_file_open_fails(tmp_path, monkeypatch):
    progress = tmp_path / "ffmpeg.progress"
    progress.write_text(
        "frame=1\ntotal_size=4096\nout_time=00:00:01.500000\nspeed=1.25x\nprogress=continue\n"
    )
    follower = exporter.ProgressFollower(str(progress))

    assert follower.poll(now=50.0)["total_size"] == "4096"

    def raise_permission_error(*args, **kwargs):
        raise PermissionError("progress file temporarily unreadable")

    monkeypatch.setattr(exporter, "open", raise_permission_error, raising=False)

    assert follower.poll(now=51.0)["total_size"] == "4096"
    assert follower.latest_timestamp == 50.0


def test_progress_follower_does_not_refresh_timestamp_after_transient_open_failure(tmp_path, monkeypatch):
    progress = tmp_path / "ffmpeg.progress"
    progress.write_text(
        "frame=1\ntotal_size=4096\nout_time=00:00:01.500000\nspeed=1.25x\nprogress=continue\n"
    )
    follower = exporter.ProgressFollower(str(progress))

    assert follower.poll(now=60.0)["total_size"] == "4096"
    assert follower.latest_timestamp == 60.0

    def raise_permission_error(*args, **kwargs):
        raise PermissionError("progress file temporarily unreadable")

    monkeypatch.setattr(exporter, "open", raise_permission_error, raising=False)
    assert follower.poll(now=70.0)["total_size"] == "4096"
    assert follower.latest_timestamp == 60.0

    monkeypatch.delattr(exporter, "open", raising=False)
    assert follower.poll(now=80.0)["total_size"] == "4096"
    assert follower.latest_timestamp == 60.0


def test_exit_counter_store_retries_dirty_state_after_save_failure(tmp_path, monkeypatch):
    exit_file = tmp_path / "ffmpeg.exit"
    state_file = tmp_path / "ffmpeg.exit.metrics_state"
    exit_file.write_text("run-a 1\n")
    store = exporter.ExitCounterStore(str(exit_file), str(state_file))
    original_save_state = store._save_state
    attempts = {"count": 0}

    def flaky_save_state():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("transient state write failure")
        original_save_state()

    monkeypatch.setattr(store, "_save_state", flaky_save_state)

    assert store.poll() == {"1": 1}
    assert attempts["count"] == 1
    assert not state_file.exists()

    assert store.poll() == {"1": 1}
    assert attempts["count"] == 2
    assert "seen run-a" in state_file.read_text()


def test_metrics_collector_exports_expected_ffmpeg_gauges_and_dedupes_exit_events(tmp_path, monkeypatch):
    progress = tmp_path / "ffmpeg.progress"
    pid_file = tmp_path / "ffmpeg.pid"
    exit_file = tmp_path / "ffmpeg.exit"
    state_file = tmp_path / "ffmpeg.exit.metrics_state"
    progress.write_text(
        "frame=1\ntotal_size=4096\nout_time=00:00:01.500000\nspeed=1.25x\nprogress=continue\n"
    )
    pid_file.write_text("1234")
    exit_file.write_text("run-a 1\nrun-a 1\nrun-b 0\n")
    monkeypatch.setattr(exporter, "process_is_running", lambda pid: pid == 1234)

    clock = iter([100.0, 101.0])
    collector = exporter.MetricsCollector(
        follower=exporter.ProgressFollower(str(progress)),
        pid_file=str(pid_file),
        exit_store=exporter.ExitCounterStore(str(exit_file), str(state_file)),
        stale_seconds=15.0,
        clock=lambda: next(clock),
    )

    payload = collector.collect()
    assert metric_value(payload, "worker_ffmpeg_running") == 1
    assert metric_value(payload, "worker_ffmpeg_health_state") == 1
    assert metric_value(payload, "worker_ffmpeg_last_progress_timestamp_seconds") == 100.0
    assert metric_value(payload, "worker_ffmpeg_progress_age_seconds") == 0.0
    assert metric_value(payload, "worker_ffmpeg_out_time_seconds") == 1.5
    assert metric_value(payload, "worker_ffmpeg_total_size_bytes") == 4096
    assert metric_value(payload, "worker_ffmpeg_speed") == 1.25
    assert metric_value(payload, "worker_ffmpeg_exit_total", '{exit_code="1"}') == 1
    assert metric_value(payload, "worker_ffmpeg_exit_total", '{exit_code="0"}') == 1

    restarted = exporter.MetricsCollector(
        follower=exporter.ProgressFollower(str(progress)),
        pid_file=str(pid_file),
        exit_store=exporter.ExitCounterStore(str(exit_file), str(state_file)),
        stale_seconds=15.0,
        clock=lambda: 101.0,
    )
    restarted_payload = restarted.collect()
    assert metric_value(restarted_payload, "worker_ffmpeg_exit_total", '{exit_code="1"}') == 1
    assert metric_value(restarted_payload, "worker_ffmpeg_exit_total", '{exit_code="0"}') == 1
