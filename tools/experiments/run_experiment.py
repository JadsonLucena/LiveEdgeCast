#!/usr/bin/env python3
"""Unified LiveEdgeCast experiment runner.

This runner intentionally keeps raw evidence even when some observability points are
not available in the current deployment. Missing values are written as null and are
reported as limitations instead of being guessed.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlencode, quote
from urllib.request import urlopen, Request

SCENARIOS = (
    "cold-start",
    "concurrency",
    "release",
    "worker-failure",
    "proxy-failure",
    "handover",
    "duplicate-streamkey",
    "pilot-capacity",
)
SIMPLIFIED_MODE_UNSUPPORTED_LIFECYCLE_SCENARIOS = {
    "worker-failure",
    "proxy-failure",
    "handover",
}

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
RESERVED_RUN_IDS = {"index", "latest", "__index__", "legacy"}
PROMETHEUS_INDEX_FILENAME = "prometheus_range_queries.__index__.json"
PROMETHEUS_LEGACY_INDEX_FILENAME = "prometheus_range_queries.index.json"
PROMETHEUS_LEGACY_LATEST_FILENAME = "prometheus_range_queries.json"
PROMETHEUS_INSTANT_LATEST_FILENAME = "prometheus_instant_queries.json"
PROMETHEUS_INSTANT_RUN_PREFIX = "prometheus_instant_queries.run."
PROMETHEUS_RUN_PREFIX = "prometheus_range_queries.run."
# In simplified mode, Prometheus strictness is intentionally small. The runner
# still collects all DEFAULT_PROMQL rows, but only controller allocation/stream
# gauges are required for --require-prometheus-analysis by default. cAdvisor
# CPU/memory, detailed lifecycle histograms, handover and recovery metrics are
# best-effort because this project version disables those controller behaviors.
CORE_PROMETHEUS_METRICS_FOR_ANALYSIS = {
    "controller_active_streams",
    "controller_active_allocations",
}

SCENARIO_PROMETHEUS_METRICS_FOR_ANALYSIS = {
    "cold-start": set(),
    "concurrency": set(),
    "release": set(),
    "worker-failure": set(),
    "proxy-failure": set(),
    "handover": set(),
    "duplicate-streamkey": set(),
    "pilot-capacity": set(),
}

# Proxy/resource verification in simplified mode is intentionally limited to
# CPU and memory. Network traffic is not used for proxy scaling or required
# experiment evidence.

# Small tolerance for distributed timestamp ordering noise between proxy/controller hooks.
TIMESTAMP_ORDERING_TOLERANCE_SECONDS = 0.050

DEFAULT_PROMQL = {
    # Controller metrics can be scoped by tenant/environment/region when --patch-proxy-context is used.
    "controller_active_streams": "controller_active_streams$controller_label_selector",
    "controller_active_allocations": "controller_active_allocations$controller_label_selector",
    "worker_pods_available": "worker_pods_available$controller_label_selector",
    # Kubernetes/cAdvisor metrics are scoped by namespace and pod name; use a dedicated namespace to avoid contamination.
    "workers_active": 'count(kube_pod_info{namespace="$namespace", pod=~"worker-.*"})',
    "proxies_active": 'count(kube_pod_info{namespace="$namespace", pod=~"proxy-.*"})',
    "controllers_active": 'count(kube_pod_info{namespace="$namespace", pod=~"controller-.*"})',
    "pod_cpu_rate": 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="$namespace", container!="POD", pod=~"(proxy-lb|proxy|worker|controller)-.*"}[1m]))',
    "pod_memory_working_set": 'sum by (pod) (container_memory_working_set_bytes{namespace="$namespace", container!="POD", pod=~"(proxy-lb|proxy|worker|controller)-.*"})',
    "stream_lifecycle_phase_seconds_p50": 'histogram_quantile(0.50, sum by (le, phase) (increase(stream_lifecycle_phase_seconds_bucket$controller_label_selector[5m])))',
    "stream_lifecycle_phase_seconds_p95": 'histogram_quantile(0.95, sum by (le, phase) (increase(stream_lifecycle_phase_seconds_bucket$controller_label_selector[5m])))',
    "stream_lifecycle_phase_seconds_p99": 'histogram_quantile(0.99, sum by (le, phase) (increase(stream_lifecycle_phase_seconds_bucket$controller_label_selector[5m])))',
    "handover_attempts_total": "handover_attempts_total$controller_label_selector",
    "handover_success_total": "handover_success_total$controller_label_selector",
    "handover_conflict_total": "handover_conflict_total$controller_label_selector",
    "orphan_workers_deleted_total": "orphan_workers_deleted_total$controller_label_selector",
    "worker_recovery_total": "worker_recovery_total$controller_label_selector",
    "worker_recovery_duration_seconds_p95": 'histogram_quantile(0.95, sum by (le) (rate(worker_recovery_duration_seconds_bucket$controller_label_selector[5m])))',
    "ffmpeg_running": "worker_ffmpeg_running$worker_metric_label_selector",
    "ffmpeg_progress_age": "worker_ffmpeg_progress_age_seconds$worker_metric_label_selector",
    "ffmpeg_out_time_seconds": "worker_ffmpeg_out_time_seconds$worker_metric_label_selector",
    "proxy_rtmp_active_streams": "proxy_rtmp_active_streams$controller_label_selector",
    "proxy_rtmp_active_publishers": "proxy_rtmp_active_publishers$controller_label_selector",
    "proxy_rtmp_active_clients": "proxy_rtmp_active_clients$controller_label_selector",
}

@dataclass
class RunnerConfig:
    stream_keys: list[str]
    scenario: str
    experiment_id: str
    run_id: str
    repetitions: int
    duration_seconds: int
    warmup_seconds: int
    cooldown_seconds: int
    rtmp_url: str
    secondary_rtmp_url: str | None
    source_file: str | None
    bitrate: str | None
    namespace: str
    prometheus_url: str | None
    controller_url: str | None
    output_dir: Path
    kill_worker: bool
    kill_proxy: bool
    dry_run: bool
    ffmpeg_path: str
    kubectl_path: str
    startup_interval_seconds: float
    kill_after_seconds: int
    duplicate_attempt_delay_seconds: int
    reconnect_delay_seconds: int
    pilot_step_size: int
    saturation_p95_seconds: float
    saturation_error_rate: float
    baseline: str | None
    release_after_seconds: int
    patch_proxy_context: bool
    overwrite: bool = False
    resume: bool = False
    allow_partial: bool = False
    allow_worker_cleanup: bool = False
    allow_restore_failure: bool = False
    allow_unscoped_context: bool = False
    allow_inconclusive: bool = False
    require_prometheus_analysis: bool = False
    require_destination_received: bool = False
    legacy_output: bool = False
    testsrc_size: str = "1920x1080"
    testsrc_rate: str = "30"
    audio_bitrate: str = "128k"
    proxy_container: str | None = None
    controller_container: str | None = None
    worker_metric_label_selector: str | None = 'namespace="$namespace"'
    constant_bitrate: bool = False
    tee_rtmp_urls: list[str] | None = None
    tee_stream_keys: bool = False

    @property
    def report_root(self) -> Path:
        # Always place each experiment in its own child directory. Earlier
        # versions allowed output_dir itself to be the report root when its
        # basename matched experiment_id; that made --overwrite capable of
        # deleting a shared parent reports directory.
        return self.output_dir / self.experiment_id


def now_epoch() -> float:
    return time.time()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_id(value: str, field: str) -> str:
    if not value or not SAFE_ID_RE.match(value):
        raise argparse.ArgumentTypeError(f"{field} must use only letters, numbers, '_', '.', '-' ")
    # The id is later used as a single path component. Dots are allowed inside
    # normal identifiers, but the special path components below would escape or
    # alias the intended output directory. Keep this guard here so callers cannot
    # accidentally make --overwrite delete an unintended directory.
    if value in {".", ".."} or Path(value).name != value:
        raise argparse.ArgumentTypeError(f"{field} must be a single safe path component, not {value!r}")
    if field == "run_id" and value.lower() in RESERVED_RUN_IDS:
        raise argparse.ArgumentTypeError(f"{field} value {value!r} is reserved for internal artifact names")
    return value


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be numeric") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> RunnerConfig:
    parser = argparse.ArgumentParser(description="Run LiveEdgeCast scientific experiments and generate a report.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stream-keys", help="Comma-separated streamKeys.")
    group.add_argument("--stream-keys-file", type=Path, help="File with one streamKey per line.")
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    parser.add_argument("--experiment-id", default=None, type=lambda v: safe_id(v, "experiment_id"), help="Experiment identifier. When omitted, it is derived from the final component of --output-dir and reports are written to that exact path.")
    parser.add_argument("--run-id", default=None, type=lambda v: safe_id(v, "run_id"))
    parser.add_argument("--repetitions", type=positive_int, default=1)
    parser.add_argument("--duration-seconds", type=positive_int, default=120)
    parser.add_argument("--warmup-seconds", type=non_negative_int, default=0)
    parser.add_argument("--cooldown-seconds", type=non_negative_int, default=10)
    parser.add_argument("--rtmp-url", default=os.getenv("LIVEEDGECAST_RTMP_URL", "rtmp://127.0.0.1:1935/live"))
    parser.add_argument("--secondary-rtmp-url", default=os.getenv("LIVEEDGECAST_SECONDARY_RTMP_URL"), help="Optional RTMP URL used for the second publisher in handover/duplicate-streamkey scenarios; use it to target a different proxy directly.")
    parser.add_argument("--source-file", default=None)
    parser.add_argument("--bitrate", default=None, help="Video bitrate for generated publishers or transcoded source files. Defaults to 10000k for generated YouTube-aligned 1080p30 H.264 test streams.")
    parser.add_argument("--testsrc-size", default=os.getenv("LIVEEDGECAST_TESTSRC_SIZE", "1920x1080"), help="Synthetic lavfi testsrc size used when --source-file is omitted. Default: 1920x1080.")
    parser.add_argument("--testsrc-rate", default=os.getenv("LIVEEDGECAST_TESTSRC_RATE", "30"), help="Synthetic lavfi testsrc frame rate used when --source-file is omitted. Default: 30.")
    parser.add_argument("--audio-bitrate", default=os.getenv("LIVEEDGECAST_AUDIO_BITRATE", "128k"), help="AAC audio bitrate used by generated publishers. Default: 128k.")
    parser.add_argument("--constant-bitrate", action="store_true", help="Enforce constant video bitrate for FFmpeg publishers with minrate=maxrate=bitrate, VBV buffer sizing, and x264 HRD CBR signaling.")
    parser.add_argument("--tee-rtmp-urls", default=os.getenv("LIVEEDGECAST_TEE_RTMP_URLS"), help="Comma-separated extra RTMP base URLs mirrored by each publisher with ffmpeg -f tee after a single local encode. Provide base URLs only; the generated streamKey is appended automatically. Example: rtmp://host-a/live,rtmp://host-b/live")
    parser.add_argument("--tee-stream-keys", action="store_true", default=bool_true_env("LIVEEDGECAST_TEE_STREAM_KEYS"), help="For concurrency runs, publish all generated streamKeys from one local FFmpeg encode using tee outputs to the configured RTMP base URL.")
    parser.add_argument("--namespace", default=os.getenv("NAMESPACE", "media"))
    parser.add_argument("--prometheus-url", default=os.getenv("PROMETHEUS_URL"))
    parser.add_argument("--controller-url", default=os.getenv("LIVEEDGECAST_CONTROLLER_URL"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--kill-worker", action="store_true", help="Inject worker failure when supported by the scenario.")
    parser.add_argument("--kill-proxy", action="store_true", help="Inject proxy failure when supported by the scenario.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ffmpeg-path", default=os.getenv("FFMPEG", "ffmpeg"))
    parser.add_argument("--kubectl-path", default=os.getenv("KUBECTL", "kubectl"))
    parser.add_argument("--startup-interval-seconds", type=non_negative_float, default=0.0)
    parser.add_argument("--kill-after-seconds", type=non_negative_int, default=20)
    parser.add_argument("--duplicate-attempt-delay-seconds", type=non_negative_int, default=10)
    parser.add_argument("--reconnect-delay-seconds", type=non_negative_int, default=5)
    parser.add_argument("--pilot-step-size", type=positive_int, default=5)
    parser.add_argument("--saturation-p95-seconds", type=non_negative_float, default=5.0)
    parser.add_argument("--saturation-error-rate", type=non_negative_float, default=0.20)
    parser.add_argument("--baseline", choices=("always-on", "polling", "event-driven"), default=None)
    parser.add_argument("--release-after-seconds", type=non_negative_int, default=20, help="How long release scenario waits before stopping publishers.")
    parser.add_argument("--patch-proxy-context", action="store_true", help="Opt-in: temporarily patch proxy/controller deployments with experiment context and restore afterwards.")
    parser.add_argument("--overwrite", action="store_true", help="Delete an existing report directory before running. Mutually exclusive with --resume.")
    parser.add_argument("--resume", action="store_true", help="Allow appending raw evidence into an existing report directory. Mutually exclusive with --overwrite.")
    parser.add_argument("--allow-partial", action="store_true", help="Return exit code 0 for partial experiments. By default partial runs fail automation.")
    parser.add_argument("--allow-worker-cleanup", action="store_true", help="Allow cold-start precondition to delete existing worker pods. Without this flag, existing workers make the cold-start run invalid.")
    parser.add_argument("--allow-restore-failure", action="store_true", help="Return exit code 0 even if opt-in deployment context restoration fails. Use only after manual cluster cleanup.")
    parser.add_argument("--allow-unscoped-context", action="store_true", help="Return exit code 0 when --patch-proxy-context was requested but proxy/controller context patching was not fully effective.")
    parser.add_argument("--allow-inconclusive", action="store_true", help="Return exit code 0 for handover/duplicate-streamkey runs whose between-proxy hypothesis remains inconclusive. By default inconclusive hypothesis tests fail automation.")
    parser.add_argument("--require-prometheus-analysis", action="store_true", help="Return exit code 1 when --prometheus-url is configured but required Prometheus samples for resource/activity analysis are incomplete.")
    parser.add_argument("--require-destination-received", action="store_true", help="Require t_destination_received in per-stream activation metrics. Keep disabled unless an instrumented destination receiver is part of the experiment.")
    parser.add_argument("--legacy-output", action="store_true", help="Deprecated compatibility flag. metrics/cost_estimation.csv is generated by default as a legacy alias with a deprecation notice; resource_activity.csv remains the primary artifact.")
    parser.add_argument("--proxy-container", default=os.getenv("LIVEEDGECAST_PROXY_CONTAINER"), help="Container name to patch in deployment/proxy. Required when deployment/proxy has multiple containers.")
    parser.add_argument("--controller-container", default=os.getenv("LIVEEDGECAST_CONTROLLER_CONTAINER"), help="Container name to patch in deployment/controller. Required when deployment/controller has multiple containers.")
    parser.add_argument("--worker-metric-label-selector", default=os.getenv("LIVEEDGECAST_WORKER_METRIC_LABEL_SELECTOR", 'namespace="$namespace"'), help="Prometheus labels used to scope worker FFmpeg exporter metrics. Use an empty string only if those metrics do not carry scrape labels.")
    args = parser.parse_args(argv)

    keys = load_stream_keys(args.stream_keys, args.stream_keys_file)
    if not keys:
        parser.error("at least one streamKey is required")
    if args.scenario != "duplicate-streamkey" and len(keys) != len(set(keys)):
        parser.error("duplicated streamKeys are not allowed outside duplicate-streamkey scenario")
    if args.source_file and not Path(args.source_file).exists():
        parser.error(f"--source-file not found: {args.source_file}")
    if not re.match(r"^[1-9][0-9]*x[1-9][0-9]*$", args.testsrc_size):
        parser.error("--testsrc-size must use WIDTHxHEIGHT format, for example 1920x1080")
    if not re.match(r"^[1-9][0-9]*(?:\.[0-9]+)?$", args.testsrc_rate):
        parser.error("--testsrc-rate must be a positive frame rate, for example 30")
    if args.saturation_error_rate > 1:
        parser.error("--saturation-error-rate must be between 0 and 1")
    if args.overwrite and args.resume:
        parser.error("--overwrite and --resume are mutually exclusive")
    try:
        tee_rtmp_urls = parse_csv_urls(args.tee_rtmp_urls)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    output_dir = args.output_dir
    if args.experiment_id:
        experiment_id = args.experiment_id
    else:
        # Acceptance command compatibility: when --experiment-id is omitted,
        # treat the final component of --output-dir as the experiment id and
        # the parent as the reports directory. Example: --output-dir
        # ./reports/teste-final writes to ./reports/teste-final.
        if not output_dir.name:
            parser.error("--experiment-id is required when --output-dir has no final path component")
        try:
            experiment_id = safe_id(output_dir.name, "experiment_id")
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
        output_dir = output_dir.parent if str(output_dir.parent) else Path(".")

    return RunnerConfig(
        stream_keys=keys,
        scenario=args.scenario,
        experiment_id=experiment_id,
        run_id=args.run_id or f"run-{int(time.time())}",
        repetitions=args.repetitions,
        duration_seconds=args.duration_seconds,
        warmup_seconds=args.warmup_seconds,
        cooldown_seconds=args.cooldown_seconds,
        rtmp_url=args.rtmp_url.rstrip("/"),
        secondary_rtmp_url=args.secondary_rtmp_url.rstrip("/") if args.secondary_rtmp_url else None,
        source_file=args.source_file,
        bitrate=args.bitrate,
        namespace=args.namespace,
        prometheus_url=args.prometheus_url.rstrip("/") if args.prometheus_url else None,
        controller_url=args.controller_url.rstrip("/") if args.controller_url else None,
        output_dir=output_dir,
        kill_worker=args.kill_worker,
        kill_proxy=args.kill_proxy,
        dry_run=args.dry_run,
        ffmpeg_path=args.ffmpeg_path,
        kubectl_path=args.kubectl_path,
        startup_interval_seconds=args.startup_interval_seconds,
        kill_after_seconds=args.kill_after_seconds,
        duplicate_attempt_delay_seconds=args.duplicate_attempt_delay_seconds,
        reconnect_delay_seconds=args.reconnect_delay_seconds,
        pilot_step_size=args.pilot_step_size,
        saturation_p95_seconds=args.saturation_p95_seconds,
        saturation_error_rate=args.saturation_error_rate,
        baseline=args.baseline,
        release_after_seconds=args.release_after_seconds,
        patch_proxy_context=args.patch_proxy_context,
        overwrite=args.overwrite,
        resume=args.resume,
        allow_partial=args.allow_partial,
        allow_worker_cleanup=args.allow_worker_cleanup,
        allow_restore_failure=args.allow_restore_failure,
        allow_unscoped_context=args.allow_unscoped_context,
        allow_inconclusive=args.allow_inconclusive,
        require_prometheus_analysis=args.require_prometheus_analysis,
        require_destination_received=args.require_destination_received,
        legacy_output=args.legacy_output,
        testsrc_size=args.testsrc_size,
        testsrc_rate=args.testsrc_rate,
        audio_bitrate=args.audio_bitrate,
        proxy_container=args.proxy_container,
        controller_container=args.controller_container,
        worker_metric_label_selector=args.worker_metric_label_selector,
        constant_bitrate=args.constant_bitrate,
        tee_rtmp_urls=tee_rtmp_urls,
        tee_stream_keys=args.tee_stream_keys,
    )


def bool_true_env(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "y"}


def parse_csv_urls(value: str | None) -> list[str] | None:
    if not value:
        return None
    urls = [item.strip().rstrip("/") for item in value.split(",") if item.strip()]
    invalid = [url for url in urls if not url.startswith(("rtmp://", "rtmps://"))]
    if invalid:
        raise argparse.ArgumentTypeError(f"tee RTMP URL(s) must start with rtmp:// or rtmps://: {', '.join(invalid[:5])}")
    return urls or None


def load_stream_keys(inline: str | None, file_path: Path | None) -> list[str]:
    raw: list[str] = []
    if inline:
        raw.extend(part.strip() for part in inline.split(","))
    if file_path:
        raw.extend(line.strip() for line in file_path.read_text(encoding="utf-8").splitlines())
    keys = [value for value in raw if value and not value.startswith("#")]
    invalid = [key for key in keys if not re.match(r"^[A-Za-z0-9_.:-]+$", key)]
    if invalid:
        raise argparse.ArgumentTypeError(f"invalid streamKey(s): {', '.join(invalid[:5])}")
    return keys


def existing_run_keys(root: Path) -> set[tuple[str, int]]:
    """Return run_id/repetition keys already present in a report directory."""
    stream_log = root / "raw" / "streams.jsonl"
    keys: set[tuple[str, int]] = set()
    if not stream_log.exists():
        return keys
    for record in read_jsonl(stream_log):
        if record.get("event") not in {"run_started", "run_finished", "run_failed", "run_interrupted"}:
            continue
        run_id = record.get("run_id")
        repetition = record.get("repetition")
        if run_id is None or not isinstance(repetition, int):
            continue
        keys.add((str(run_id), repetition))
    return keys


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def prepare_report_root(config: RunnerConfig) -> Path:
    """Prepare report directory safely.

    By default experiments refuse to reuse a non-empty report directory, because
    JSONL raw evidence is append-only and mixing two executions would invalidate
    the resulting metrics. Use --overwrite to delete the existing directory or
    --resume when intentional continuation is desired. Resume is accepted only
    when the requested run_id/repetition keys are not already present.
    """
    root = config.report_root
    output_root = config.output_dir / config.experiment_id
    if root.resolve() != output_root.resolve() or not path_is_relative_to(root, config.output_dir):
        raise RuntimeError(f"unsafe report directory resolved outside output directory: {root}")
    if root.exists() and any(root.iterdir()):
        if config.overwrite:
            shutil.rmtree(root)
        elif not config.resume:
            raise RuntimeError(
                f"report directory already exists and is not empty: {root}. "
                "Use --overwrite to replace it or --resume to append intentionally."
            )
        else:
            existing = existing_run_keys(root)
            requested = {(str(config.run_id), rep) for rep in range(1, config.repetitions + 1)}
            collisions = sorted(existing & requested, key=lambda item: item[1])
            if collisions:
                formatted = ", ".join(f"{run_id}/r{rep}" for run_id, rep in collisions[:10])
                raise RuntimeError(
                    "--resume would reuse existing run_id/repetition evidence: "
                    f"{formatted}. Use a new --run-id or --overwrite to replace the report directory."
                )
    return root


def ensure_layout(root: Path) -> dict[str, Path]:
    dirs = {
        "root": root,
        "raw": root / "raw",
        "metrics": root / "metrics",
        "logs": root / "logs",
        "charts": root / "charts",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: Any) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class ManagedPublisher:
    def __init__(
        self,
        config: RunnerConfig,
        stream_key: str,
        command: list[str],
        process: subprocess.Popen,
        stdout_path: Path,
        stderr_path: Path,
        started_at: float,
        repetition: int,
        publisher_index: int,
        stream_keys: list[str] | None = None,
        publisher_mode: str = "single",
        stream_output_urls: dict[str, list[str]] | None = None,
    ):
        self.experiment_id = config.experiment_id
        self.scenario = config.scenario
        self.run_id = config.run_id
        self.stream_key = stream_key
        self.stream_keys = stream_keys or [stream_key]
        self.publisher_mode = publisher_mode
        self.stream_output_urls = stream_output_urls or {}
        self.command = command
        self.process = process
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.started_at = started_at
        self.ended_at: float | None = None
        self.repetition = repetition
        self.publisher_index = publisher_index
        self.stop_reason: str | None = None

    def stop(self, grace_seconds: float = 5, reason: str = "stopped_by_runner") -> None:
        self.stop_reason = self.stop_reason or reason
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except Exception:
                self.process.terminate()
            try:
                self.process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                self.stop_reason = f"{reason}_sigkill"
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except Exception:
                    self.process.kill()
                self.process.wait(timeout=10)
        self.ended_at = now_epoch()

    def result(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "scenario": self.scenario,
            "run_id": self.run_id,
            "stream_key": self.stream_key,
            "repetition": self.repetition,
            "publisher_index": self.publisher_index,
            "pid": self.process.pid,
            "returncode": self.process.poll(),
            "stop_reason": self.stop_reason,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "stdout": str(self.stdout_path),
            "stderr": str(self.stderr_path),
            "command": redact_command(self.command),
            "publisher_mode": self.publisher_mode,
            "publisher_group_size": len(self.stream_keys),
        }



RTMP_URL_PATTERN = re.compile(r"(rtmps?://)[^\]\[|\s]+")


def redact_text(value: str) -> str:
    return RTMP_URL_PATTERN.sub(r"\1...", value)


def redact_command(command: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    for part in command:
        redacted.append(redact_text(part))
    return redacted



def ffmpeg_vbv_bufsize(video_bitrate: str) -> str:
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)([kKmMgG]?)$", video_bitrate.strip())
    if not match:
        return "20000k"
    value = float(match.group(1)) * 2
    unit = match.group(2)
    rendered = str(int(value)) if value.is_integer() else str(value).rstrip("0").rstrip(".")
    return f"{rendered}{unit}"


def add_constant_bitrate_options(command: list[str], video_bitrate: str) -> None:
    command.extend([
        "-b:v", video_bitrate,
        "-minrate", video_bitrate,
        "-maxrate", video_bitrate,
        "-bufsize", ffmpeg_vbv_bufsize(video_bitrate),
        "-x264-params", "nal-hrd=cbr:force-cfr=1",
    ])


def ffmpeg_tee_escape(url: str) -> str:
    return url.replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def rtmp_target(base_url: str, stream_key: str) -> str:
    return f"{base_url.rstrip('/')}/{quote(stream_key, safe='')}"


def ffmpeg_output_args(config: RunnerConfig, stream_keys: Sequence[str], primary_url: str) -> list[str]:
    targets = [rtmp_target(primary_url, key) for key in stream_keys]
    for url in config.tee_rtmp_urls or []:
        targets.extend(rtmp_target(url, key) for key in stream_keys)
    if len(targets) == 1:
        return ["-f", "flv", targets[0]]
    tee_targets = "|".join(f"[f=flv:onfail=ignore]{ffmpeg_tee_escape(target)}" for target in targets)
    return ["-f", "tee", tee_targets]


def ffmpeg_command_for_stream_keys(config: RunnerConfig, stream_keys: Sequence[str], rtmp_url: str | None = None) -> list[str]:
    if not stream_keys:
        raise ValueError("at least one stream key is required")
    base_url = (rtmp_url or config.rtmp_url).rstrip("/")
    command = [config.ffmpeg_path, "-hide_banner", "-nostdin", "-re"]
    if config.source_file:
        command.extend(["-stream_loop", "-1", "-i", config.source_file, "-t", str(config.duration_seconds)])
        if config.bitrate:
            command.extend(["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"])
            if config.constant_bitrate:
                add_constant_bitrate_options(command, config.bitrate)
            else:
                command.extend(["-b:v", config.bitrate])
            command.extend(["-c:a", "aac"])
        else:
            command.extend(["-c", "copy"])
    else:
        video_bitrate = config.bitrate or "10000k"
        command.extend([
            "-f", "lavfi", "-i", f"testsrc=size={config.testsrc_size}:rate={config.testsrc_rate}",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", str(config.duration_seconds),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-r", config.testsrc_rate, "-g", "60", "-keyint_min", "60",
            "-sc_threshold", "0", "-bf", "2", "-refs", "1", "-coder", "1",
        ])
        if config.constant_bitrate:
            add_constant_bitrate_options(command, video_bitrate)
        else:
            command.extend(["-b:v", video_bitrate, "-minrate", video_bitrate, "-maxrate", video_bitrate, "-bufsize", "20000k"])
        command.extend([
            "-c:a", "aac", "-b:a", config.audio_bitrate, "-ar", "44100",
        ])
    command.extend(ffmpeg_output_args(config, stream_keys, base_url))
    return command


def ffmpeg_command(config: RunnerConfig, stream_key: str, rtmp_url: str | None = None) -> list[str]:
    return ffmpeg_command_for_stream_keys(config, [stream_key], rtmp_url=rtmp_url)


def start_publisher(
    config: RunnerConfig,
    dirs: dict[str, Path],
    stream_key: str,
    repetition: int,
    publisher_index: int,
    suffix: str = "",
    rtmp_url_override: str | None = None,
) -> ManagedPublisher:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{stream_key}{suffix}")
    stdout_path = dirs["logs"] / f"publisher-{config.experiment_id}-r{repetition:03d}-i{publisher_index:02d}-{safe_name}.stdout.log"
    stderr_path = dirs["logs"] / f"publisher-{config.experiment_id}-r{repetition:03d}-i{publisher_index:02d}-{safe_name}.stderr.log"
    command = ffmpeg_command(config, stream_key, rtmp_url=rtmp_url_override)
    started_at = now_epoch()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True)
    append_jsonl(dirs["raw"] / "publishers.jsonl", {
        "event": "publisher_started",
        "experiment_id": config.experiment_id,
        "scenario": config.scenario,
        "run_id": config.run_id,
        "stream_key": stream_key,
        "repetition": repetition,
        "publisher_index": publisher_index,
        "pid": process.pid,
        "timestamp": started_at,
        "command": redact_command(command),
        "rtmp_url_role": "secondary" if rtmp_url_override else "primary",
        "publisher_mode": "single",
        "publisher_group_size": 1,
    })
    return ManagedPublisher(config, stream_key, command, process, stdout_path, stderr_path, started_at, repetition, publisher_index)


def start_tee_stream_key_publisher(
    config: RunnerConfig,
    dirs: dict[str, Path],
    stream_keys: list[str],
    repetition: int,
) -> ManagedPublisher:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"tee-{len(stream_keys)}streams")
    stdout_path = dirs["logs"] / f"publisher-{config.experiment_id}-r{repetition:03d}-i001-{safe_name}.stdout.log"
    stderr_path = dirs["logs"] / f"publisher-{config.experiment_id}-r{repetition:03d}-i001-{safe_name}.stderr.log"
    command = ffmpeg_command_for_stream_keys(config, stream_keys)
    output_urls_by_stream = {
        stream_key: [
            redact_text(rtmp_target(base_url, stream_key))
            for base_url in [config.rtmp_url, *(config.tee_rtmp_urls or [])]
        ]
        for stream_key in stream_keys
    }
    started_at = now_epoch()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True)
    for index, stream_key in enumerate(stream_keys, start=1):
        append_jsonl(dirs["raw"] / "publishers.jsonl", {
            "event": "publisher_started",
            "experiment_id": config.experiment_id,
            "scenario": config.scenario,
            "run_id": config.run_id,
            "stream_key": stream_key,
            "repetition": repetition,
            "publisher_index": index,
            "pid": process.pid,
            "timestamp": started_at,
            "command": redact_command(command),
            "rtmp_url_role": "primary",
            "publisher_mode": "tee_stream_keys",
            "publisher_group_size": len(stream_keys),
            "publisher_group_id": f"{config.run_id}-r{repetition}-tee-{process.pid}",
            "publisher_shared_log": True,
            "tee_output_urls": output_urls_by_stream[stream_key],
            "tee_output_count": len(output_urls_by_stream[stream_key]),
        })
    return ManagedPublisher(
        config,
        stream_keys[0],
        command,
        process,
        stdout_path,
        stderr_path,
        started_at,
        repetition,
        1,
        stream_keys=stream_keys,
        publisher_mode="tee_stream_keys",
        stream_output_urls=output_urls_by_stream,
    )


def publisher_process_status(result: dict[str, Any]) -> str:
    """Classify the FFmpeg process outcome without architectural interpretation."""
    rc = result.get("returncode")
    stop_reason = str(result.get("stop_reason") or "")
    if rc == 0:
        return "success"
    if stop_reason.startswith("expected_"):
        return "expected_stopped"
    if stop_reason.endswith("_sigkill"):
        return "killed_by_runner"
    if rc is None:
        return "running_or_unknown"
    return "nonzero_exit"


def publisher_status(config: RunnerConfig, result: dict[str, Any]) -> str:
    process_status = publisher_process_status(result)
    if process_status in {"success", "expected_stopped", "running_or_unknown"}:
        return process_status
    if config.scenario == "duplicate-streamkey" and int(result.get("publisher_index") or 0) > 1:
        return "duplicate_publisher_exited"
    return "unexpected_failed"


def wait_or_stop_publishers(
    config: RunnerConfig,
    dirs: dict[str, Path],
    publishers: list[ManagedPublisher],
    wait: bool = True,
    stop_reason: str = "expected_stop",
) -> list[dict[str, Any]]:
    deadline = now_epoch() + config.duration_seconds + config.cooldown_seconds + 30
    if wait:
        for publisher in publishers:
            remaining = max(0.1, deadline - now_epoch())
            try:
                publisher.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                publisher.stop(reason="timeout_after_nominal_duration")
            publisher.ended_at = publisher.ended_at or now_epoch()
    else:
        for publisher in publishers:
            if publisher.process.poll() is None:
                publisher.stop(reason=stop_reason)
            else:
                publisher.ended_at = publisher.ended_at or now_epoch()
    return record_publisher_results(config, dirs, publishers)


def record_publisher_results(config: RunnerConfig, dirs: dict[str, Path], publishers: list[ManagedPublisher]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for publisher in publishers:
        base_result = publisher.result()
        for offset, stream_key in enumerate(publisher.stream_keys):
            result = {**base_result, "stream_key": stream_key}
            if len(publisher.stream_keys) > 1:
                result["publisher_index"] = offset + 1
                result["publisher_group_id"] = f"{publisher.run_id}-r{publisher.repetition}-tee-{publisher.process.pid}"
                result["publisher_shared_log"] = True
            if stream_key in publisher.stream_output_urls:
                result["tee_output_urls"] = publisher.stream_output_urls[stream_key]
                result["tee_output_count"] = len(publisher.stream_output_urls[stream_key])
            result["publisher_process_status"] = publisher_process_status(result)
            result["publisher_status"] = publisher_status(config, result)
            append_jsonl(dirs["raw"] / "publishers.jsonl", {"event": "publisher_finished", **result})
            results.append(result)
    return results


def run_cmd(command: Sequence[str], timeout: int = 60) -> dict[str, Any]:
    started = now_epoch()
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return {"command": list(command), "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr, "started_at": started, "ended_at": now_epoch()}
    except Exception as exc:
        return {"command": list(command), "returncode": 127, "stdout": "", "stderr": str(exc), "started_at": started, "ended_at": now_epoch(), "error": type(exc).__name__}


def kubectl_json(config: RunnerConfig, args: Sequence[str], timeout: int = 60) -> dict[str, Any]:
    completed = run_cmd([config.kubectl_path, *args], timeout=timeout)
    try:
        completed["json"] = json.loads(completed.get("stdout") or "{}") if completed["returncode"] == 0 else None
    except json.JSONDecodeError as exc:
        completed["json"] = None
        completed["parse_error"] = str(exc)
    return completed


def collect_kubernetes(config: RunnerConfig, dirs: dict[str, Path], phase: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {"phase": phase, "timestamp": now_epoch(), "available": False}
    if not shutil.which(config.kubectl_path) and not Path(config.kubectl_path).exists():
        evidence["error"] = f"kubectl not found: {config.kubectl_path}"
        return evidence
    pods = kubectl_json(config, ["get", "pods", "-n", config.namespace, "-o", "json"])
    events = kubectl_json(config, ["get", "events", "-n", config.namespace, "--sort-by=.lastTimestamp", "-o", "json"])
    evidence.update({"available": pods["returncode"] == 0, "pods_returncode": pods["returncode"], "events_returncode": events["returncode"]})
    for item in (pods.get("json") or {}).get("items", []):
        append_jsonl(dirs["raw"] / "pod_snapshots.jsonl", {"phase": phase, "snapshot_at": evidence["timestamp"], "pod": item})
    for item in (events.get("json") or {}).get("items", []):
        append_jsonl(dirs["raw"] / "kubernetes_events.jsonl", {"phase": phase, "snapshot_at": evidence["timestamp"], "event": item})
    write_json(dirs["raw"] / f"kubernetes_{phase}.json", {"pods": pods.get("json"), "events": events.get("json"), "errors": {"pods": pods.get("stderr"), "events": events.get("stderr")}})
    return evidence



def collect_logs(config: RunnerConfig, dirs: dict[str, Path], phase: str | None = None) -> dict[str, Any]:
    results: dict[str, Any] = {}
    if not shutil.which(config.kubectl_path) and not Path(config.kubectl_path).exists():
        return {"available": False, "error": f"kubectl not found: {config.kubectl_path}"}
    log_dir = dirs["logs"] / phase if phase else dirs["logs"]
    log_dir.mkdir(parents=True, exist_ok=True)
    selectors = {"controller": "app=controller", "proxy": "app=proxy", "worker": "app=worker"}
    for name, selector in selectors.items():
        out = run_cmd([config.kubectl_path, "logs", "-n", config.namespace, "-l", selector, "--all-containers=true", "--tail=-1"], timeout=120)
        path = log_dir / f"{name}.log"
        path.write_text((out.get("stdout") or "") + ("\n# STDERR\n" + out.get("stderr", "") if out.get("stderr") else ""), encoding="utf-8")
        event_count = extract_structured_events(path, dirs["raw"] / f"{name}_events.jsonl", component=name, collection_phase=phase or "final")
        previous_out = run_cmd([config.kubectl_path, "logs", "-n", config.namespace, "-l", selector, "--all-containers=true", "--tail=-1", "--previous"], timeout=120)
        previous_path = log_dir / f"{name}.previous.log"
        previous_path.write_text((previous_out.get("stdout") or "") + ("\n# STDERR\n" + previous_out.get("stderr", "") if previous_out.get("stderr") else ""), encoding="utf-8")
        previous_count = extract_structured_events(previous_path, dirs["raw"] / f"{name}_events.jsonl", component=name, collection_phase=f"{phase or 'final'}-previous")
        results[name] = {"returncode": out["returncode"], "path": str(path), "structured_events": event_count, "previous_returncode": previous_out["returncode"], "previous_path": str(previous_path), "previous_structured_events": previous_count}
    # Merge publisher logs for convenience.
    publisher_log = log_dir / "publishers.log"
    with publisher_log.open("w", encoding="utf-8") as merged:
        for path in sorted(dirs["logs"].glob("publisher-*.stderr.log")):
            merged.write(f"\n===== {path.name} =====\n")
            merged.write(path.read_text(encoding="utf-8", errors="replace"))
    results["publishers"] = {"path": str(publisher_log)}
    if phase:
        write_root_log_aliases(dirs, log_dir)
    return results


def write_root_log_aliases(dirs: dict[str, Path], source_log_dir: Path) -> None:
    """Create prompt-compatible root log aliases for the latest collected phase."""
    if source_log_dir == dirs["logs"]:
        return
    for name in ("controller", "proxy", "worker", "publishers"):
        src = source_log_dir / f"{name}.log"
        dst = dirs["logs"] / f"{name}.log"
        if src.exists():
            shutil.copyfile(src, dst)


def parse_event_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_json_event_line(line: str) -> dict[str, Any] | None:
    start = line.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(line[start:].strip())
    except json.JSONDecodeError:
        return None
    message = payload.get("message")
    if isinstance(message, str) and message.strip().startswith("{"):
        try:
            nested = json.loads(message)
        except json.JSONDecodeError:
            nested = None
        if isinstance(nested, dict) and nested.get("event_type"):
            merged = dict(payload)
            for key, value in nested.items():
                if key not in merged or merged.get(key) in (None, "log", "unknown", "default"):
                    merged[key] = value
            payload = merged
    payload.setdefault("timestamp_epoch", parse_event_timestamp(payload.get("timestamp")))
    return payload


def event_dedupe_key(event: dict[str, Any]) -> str:
    payload = {
        key: event.get(key)
        for key in ("component", "event_type", "timestamp", "timestamp_epoch", "stream", "generation", "message", "worker_pod", "proxy_pod")
    }
    return json.dumps(payload, sort_keys=True, default=str)


def extract_structured_events(log_path: Path, output_path: Path, component: str, collection_phase: str = "final") -> int:
    count = 0
    if not log_path.exists():
        return 0
    seen = set()
    if output_path.exists():
        for existing in read_jsonl(output_path):
            seen.add(event_dedupe_key(existing))
    with output_path.open("a", encoding="utf-8") as out:
        for line_number, line in enumerate(log_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            event = parse_json_event_line(line)
            if not event:
                continue
            event["component"] = component
            event["source_log"] = str(log_path)
            event["source_line"] = line_number
            event["collection_phase"] = collection_phase
            key = event_dedupe_key(event)
            if key in seen:
                continue
            seen.add(key)
            out.write(json.dumps(event, sort_keys=True, default=str) + "\n")
            count += 1
    return count


def controller_get(config: RunnerConfig, path: str) -> dict[str, Any]:
    if not config.controller_url:
        return {"available": False, "reason": "controller_url_not_configured", "path": path}
    url = f"{config.controller_url}{path}"
    try:
        with urlopen(Request(url, headers={"User-Agent": "liveedgecast-experiment/1"}), timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", None)
        return {"available": True, "path": path, "status": status, "body": body}
    except Exception as exc:
        return {"available": False, "path": path, "error": {"type": type(exc).__name__, "message": str(exc)}}


def wait_for_controller_health(config: RunnerConfig, timeout_seconds: int = 180, interval_seconds: float = 2.0) -> dict[str, Any]:
    """Wait until the controller /health endpoint is reachable.

    This is intentionally based on config.controller_url so it also validates that
    a local kubectl port-forward has recovered after a rollout.
    """
    if not config.controller_url:
        return {"ok": True, "skipped": True, "reason": "controller_url_not_configured"}
    started_at = now_epoch()
    deadline = started_at + timeout_seconds
    attempts: list[dict[str, Any]] = []
    while now_epoch() <= deadline:
        result = controller_get(config, "/health")
        attempts.append({"timestamp": now_epoch(), "result": result})
        if result.get("available") and int(result.get("status") or 0) == 200:
            return {
                "ok": True,
                "skipped": False,
                "started_at": started_at,
                "ended_at": now_epoch(),
                "timeout_seconds": timeout_seconds,
                "attempts": attempts[-10:],
            }
        time.sleep(interval_seconds)
    return {
        "ok": False,
        "skipped": False,
        "started_at": started_at,
        "ended_at": now_epoch(),
        "timeout_seconds": timeout_seconds,
        "attempts": attempts[-10:],
        "reason": "controller_health_timeout",
    }


def prometheus_targets(config: RunnerConfig) -> dict[str, Any]:
    if not config.prometheus_url:
        return {"available": False, "reason": "prometheus_url_not_configured"}
    url = f"{config.prometheus_url}/api/v1/targets"
    try:
        with urlopen(Request(url, headers={"User-Agent": "liveedgecast-experiment/1"}), timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {"available": True, "response": payload}
    except Exception as exc:
        return {"available": False, "error": {"type": type(exc).__name__, "message": str(exc)}}


def count_healthy_prometheus_targets(payload: dict[str, Any], namespace: str, job: str) -> int:
    active_targets = ((payload.get("data") or {}).get("activeTargets") or []) if isinstance(payload, dict) else []
    count = 0
    for target in active_targets:
        labels = target.get("labels") or {}
        if labels.get("namespace") == namespace and labels.get("job") == job and target.get("health") == "up":
            count += 1
    return count


def wait_for_prometheus_targets(config: RunnerConfig, jobs: Sequence[str] = ("controller", "proxy"), timeout_seconds: int = 180, interval_seconds: float = 5.0) -> dict[str, Any]:
    """Wait until Prometheus has healthy active targets for the LiveEdgeCast services."""
    if not config.prometheus_url:
        return {"ok": True, "skipped": True, "reason": "prometheus_url_not_configured"}
    started_at = now_epoch()
    deadline = started_at + timeout_seconds
    attempts: list[dict[str, Any]] = []
    while now_epoch() <= deadline:
        snapshot = prometheus_targets(config)
        counts: dict[str, int] = {}
        if snapshot.get("available"):
            payload = snapshot.get("response") or {}
            counts = {job: count_healthy_prometheus_targets(payload, config.namespace, job) for job in jobs}
            if all(counts.get(job, 0) >= 1 for job in jobs):
                return {
                    "ok": True,
                    "skipped": False,
                    "started_at": started_at,
                    "ended_at": now_epoch(),
                    "timeout_seconds": timeout_seconds,
                    "jobs": list(jobs),
                    "counts": counts,
                    "attempts": attempts[-10:],
                }
        attempts.append({"timestamp": now_epoch(), "counts": counts, "snapshot": snapshot if not snapshot.get("available") else {"available": True}})
        time.sleep(interval_seconds)
    return {
        "ok": False,
        "skipped": False,
        "started_at": started_at,
        "ended_at": now_epoch(),
        "timeout_seconds": timeout_seconds,
        "jobs": list(jobs),
        "attempts": attempts[-10:],
        "reason": "prometheus_targets_timeout",
    }


def prometheus_instant_result_count(result: dict[str, Any]) -> int:
    response = result.get("response") or {}
    data = response.get("data") or {}
    return len(data.get("result") or [])


def wait_for_scoped_controller_prometheus_samples(config: RunnerConfig, controller_label_selector: str, timeout_seconds: int = 180, interval_seconds: float = 5.0) -> dict[str, Any]:
    """Wait until the patched controller context has been scraped by Prometheus.

    rollout status and /health only prove that the Pod is up. The scientific run
    also needs scoped Prometheus samples, otherwise range queries using
    tenant/environment/region can miss the beginning of the experiment.
    """
    if not config.prometheus_url:
        return {"ok": True, "skipped": True, "reason": "prometheus_url_not_configured"}
    if not controller_label_selector:
        return {"ok": True, "skipped": True, "reason": "controller_label_selector_not_configured"}
    started_at = now_epoch()
    deadline = started_at + timeout_seconds
    attempts: list[dict[str, Any]] = []
    queries = {
        "controller_active_streams": "controller_active_streams$controller_label_selector",
        "proxy_rtmp_stats_up": "proxy_rtmp_stats_up$controller_label_selector",
    }
    while now_epoch() <= deadline:
        ts = now_epoch()
        counts: dict[str, int] = {}
        query_results: dict[str, Any] = {}
        for name, query in queries.items():
            result = prometheus_instant_query(config, query, ts, controller_label_selector=controller_label_selector)
            counts[name] = prometheus_instant_result_count(result) if result.get("available") else 0
            query_results[name] = {
                "available": result.get("available"),
                "rendered_query": result.get("rendered_query") or result.get("query"),
                "error": result.get("error"),
                "count": counts[name],
            }
        attempts.append({"timestamp": ts, "counts": counts, "queries": query_results})
        if all(count >= 1 for count in counts.values()):
            return {
                "ok": True,
                "skipped": False,
                "started_at": started_at,
                "ended_at": now_epoch(),
                "timeout_seconds": timeout_seconds,
                "controller_label_selector": controller_label_selector,
                "counts": counts,
                "attempts": attempts[-10:],
            }
        time.sleep(interval_seconds)
    return {
        "ok": False,
        "skipped": False,
        "started_at": started_at,
        "ended_at": now_epoch(),
        "timeout_seconds": timeout_seconds,
        "controller_label_selector": controller_label_selector,
        "attempts": attempts[-10:],
        "reason": "scoped_prometheus_samples_timeout",
    }


def wait_for_deployment_context_stabilization(
    config: RunnerConfig,
    dirs: dict[str, Path],
    phase: str,
    deployments: Sequence[str],
    controller_label_selector: str = "",
) -> dict[str, Any]:
    """Wait for rollout, controller health, and Prometheus visibility after context changes."""
    commands: list[dict[str, Any]] = []
    unique_deployments = list(dict.fromkeys(deployments))
    for deployment in unique_deployments:
        commands.append(run_cmd([
            config.kubectl_path,
            "rollout",
            "status",
            f"deployment/{deployment}",
            "-n",
            config.namespace,
            "--timeout=180s",
        ], timeout=210))

    controller_health = wait_for_controller_health(config, timeout_seconds=180, interval_seconds=2.0)
    prometheus_target_health = wait_for_prometheus_targets(config, timeout_seconds=180, interval_seconds=5.0)
    scoped_prometheus_samples = wait_for_scoped_controller_prometheus_samples(
        config,
        controller_label_selector,
        timeout_seconds=180,
        interval_seconds=5.0,
    ) if controller_label_selector else {"ok": True, "skipped": True, "reason": "no_scoped_selector_for_phase"}

    result = {
        "phase": phase,
        "timestamp": now_epoch(),
        "deployments": unique_deployments,
        "commands": commands,
        "controller_health": controller_health,
        "prometheus_targets": prometheus_target_health,
        "scoped_prometheus_samples": scoped_prometheus_samples,
    }
    result["ok"] = (
        all(command.get("returncode") == 0 for command in commands)
        and bool(controller_health.get("ok"))
        and bool(prometheus_target_health.get("ok"))
        and bool(scoped_prometheus_samples.get("ok"))
    )
    write_json(dirs["raw"] / f"deployment_context_stabilization_{phase}.json", result)
    return result


def collect_controller_http(config: RunnerConfig, dirs: dict[str, Path], phase: str) -> dict[str, Any]:
    result = {
        "phase": phase,
        "timestamp": now_epoch(),
        "health": controller_get(config, "/health"),
        "metrics": controller_get(config, "/metrics"),
    }
    write_json(dirs["raw"] / f"controller_http_{phase}.json", result)
    return result


def target_container_name(config: RunnerConfig, deployment: str) -> str | None:
    if deployment == "proxy":
        return config.proxy_container
    if deployment == "controller":
        return config.controller_container
    return None


def context_env_keys_for_deployment(deployment: str) -> list[str]:
    if deployment == "proxy":
        return ["EXPERIMENT_ID", "SCENARIO", "RUN_ID"]
    if deployment == "controller":
        return [
            "LIVEEDGECAST_EXPERIMENT_ID", "LIVEEDGECAST_SCENARIO", "LIVEEDGECAST_RUN_ID",
            "LIVEEDGECAST_TENANT", "LIVEEDGECAST_ENVIRONMENT", "LIVEEDGECAST_REGION",
        ]
    return []


def context_set_env_command(config: RunnerConfig, deployment: str, assignments: Sequence[str], target_container: str | None = None) -> list[str]:
    args = [config.kubectl_path, "set", "env", f"deployment/{deployment}"]
    if target_container:
        args.append(f"--containers={target_container}")
    args.extend(assignments)
    args.extend(["-n", config.namespace])
    return args


def deployment_env_snapshot(config: RunnerConfig, deployment: str, keys: Sequence[str]) -> dict[str, Any]:
    """Capture env state for the deployment container that would be patched.

    The snapshot deliberately refuses unsafe cases instead of trying to reconstruct
    them later: multi-container Deployments require an explicit container name and
    target keys backed by valueFrom are not patched because kubectl set env cannot
    safely restore Secret/ConfigMap references from a scalar snapshot.
    """
    result = kubectl_json(config, ["get", f"deployment/{deployment}", "-n", config.namespace, "-o", "json"], timeout=60)
    values = {key: None for key in keys}
    snapshot = {
        "deployment": deployment,
        "values": values,
        "kubectl": result,
        "snapshot_ok": result.get("returncode") == 0,
        "safe_to_patch": False,
        "target_container": target_container_name(config, deployment),
        "container_count": 0,
        "container_env": [],
        "unsafe_value_from_keys": [],
        "reason": None,
    }
    if not snapshot["snapshot_ok"]:
        snapshot["reason"] = "kubectl_get_deployment_failed"
        return snapshot
    try:
        containers = (((result.get("json") or {}).get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
        snapshot["container_count"] = len(containers)
        requested_container = snapshot["target_container"]
        if not containers:
            snapshot["snapshot_ok"] = False
            snapshot["reason"] = "deployment_has_no_containers"
            return snapshot
        if requested_container:
            selected = next((container for container in containers if container.get("name") == requested_container), None)
            if selected is None:
                snapshot["snapshot_ok"] = False
                snapshot["reason"] = f"container_not_found:{requested_container}"
                return snapshot
        elif len(containers) == 1:
            selected = containers[0]
            snapshot["target_container"] = selected.get("name")
        else:
            snapshot["reason"] = "multiple_containers_require_explicit_container"
            return snapshot

        env_entries = selected.get("env") or []
        snapshot["container_env"] = env_entries
        unsafe_value_from_keys: list[str] = []
        for item in env_entries:
            name = item.get("name")
            if name not in values:
                continue
            if "valueFrom" in item:
                unsafe_value_from_keys.append(str(name))
                continue
            values[name] = item.get("value")
        snapshot["unsafe_value_from_keys"] = unsafe_value_from_keys
        if unsafe_value_from_keys:
            snapshot["reason"] = "target_keys_use_valueFrom"
            snapshot["safe_to_patch"] = False
            return snapshot
        snapshot["safe_to_patch"] = True
        snapshot["reason"] = "safe"
        return snapshot
    except Exception as exc:
        snapshot["snapshot_error"] = str(exc)
        snapshot["snapshot_ok"] = False
        snapshot["safe_to_patch"] = False
        snapshot["reason"] = "snapshot_parse_error"
        return snapshot


def restore_context_keys(config: RunnerConfig, dirs: dict[str, Path], patch_result: dict[str, Any]) -> dict[str, Any]:
    """Restore context env keys changed by patch_proxy_context().

    Restoration is tracked per deployment. A partial patch can still mutate one
    Deployment even when a later Deployment or rollout fails, so restoration must
    not depend on a single all-or-nothing boolean.
    """
    patched_deployments = list(patch_result.get("patched_deployments") or [])
    if not patched_deployments:
        result = {"skipped": True, "reason": "context_was_not_patched", "ok": True}
        write_json(dirs["raw"] / "proxy_context_restore.json", result)
        return result
    commands = []
    restored_deployments = []
    previous_env = patch_result.get("previous_env") or {}
    for deployment in patched_deployments:
        snapshot = previous_env.get(deployment) or {}
        if not snapshot.get("snapshot_ok", True) or not snapshot.get("safe_to_patch", True):
            commands.append({"deployment": deployment, "returncode": 1, "skipped": True, "reason": snapshot.get("reason") or "previous_env_snapshot_not_safe"})
            continue
        values = snapshot.get("values") or {}
        target_container = snapshot.get("target_container")
        assignments = [f"{key}-" if old_value is None else f"{key}={old_value}" for key, old_value in values.items()]
        args = context_set_env_command(config, deployment, assignments, target_container=target_container)
        commands.append(run_cmd(args, timeout=60))
        if commands[-1].get("returncode") == 0:
            restored_deployments.append(deployment)
            commands.append(run_cmd([config.kubectl_path, "rollout", "status", f"deployment/{deployment}", "-n", config.namespace, "--timeout=120s"], timeout=150))
    post_restore_stabilization = wait_for_deployment_context_stabilization(
        config,
        dirs,
        phase="after_restore",
        deployments=restored_deployments,
        controller_label_selector="",
    ) if restored_deployments else {"ok": not patched_deployments, "skipped": True, "reason": "no_restored_deployments"}
    result = {
        "commands": commands,
        "patched_deployments": patched_deployments,
        "restored_deployments": restored_deployments,
        "post_restore_stabilization": post_restore_stabilization,
        "ok": all(c.get("returncode") == 0 for c in commands) and bool(post_restore_stabilization.get("ok")),
    }
    write_json(dirs["raw"] / "proxy_context_restore.json", result)
    return result


def patch_proxy_context(config: RunnerConfig, dirs: dict[str, Path]) -> dict[str, Any]:
    """Opt-in propagation of experiment context to proxy hooks and controller metrics/logs."""
    if not config.patch_proxy_context:
        result = {"available": True, "skipped": True, "patched": False, "reason": "--patch-proxy-context not enabled"}
        write_json(dirs["raw"] / "proxy_context_patch.json", result)
        return result
    if not shutil.which(config.kubectl_path) and not Path(config.kubectl_path).exists():
        result = {"available": False, "skipped": True, "patched": False, "reason": f"kubectl not found: {config.kubectl_path}"}
        write_json(dirs["raw"] / "proxy_context_patch.json", result)
        return result

    proxy_keys = context_env_keys_for_deployment("proxy")
    controller_keys = context_env_keys_for_deployment("controller")
    previous_env = {
        "proxy": deployment_env_snapshot(config, "proxy", proxy_keys),
        "controller": deployment_env_snapshot(config, "controller", controller_keys),
    }
    proxy_assignments = [
        f"EXPERIMENT_ID={config.experiment_id}",
        f"SCENARIO={config.scenario}",
        f"RUN_ID={config.run_id}",
    ]
    controller_assignments = [
        f"LIVEEDGECAST_EXPERIMENT_ID={config.experiment_id}",
        f"LIVEEDGECAST_SCENARIO={config.scenario}",
        f"LIVEEDGECAST_RUN_ID={config.run_id}",
        f"LIVEEDGECAST_TENANT={config.experiment_id}",
        f"LIVEEDGECAST_ENVIRONMENT={config.scenario}",
        f"LIVEEDGECAST_REGION={config.run_id}",
    ]
    commands = []
    patched_deployments: list[str] = []
    effective_deployments: list[str] = []
    skipped_deployments: list[dict[str, Any]] = []
    for deployment, assignments in (("proxy", proxy_assignments), ("controller", controller_assignments)):
        snapshot = previous_env.get(deployment) or {}
        if not snapshot.get("snapshot_ok") or not snapshot.get("safe_to_patch", True):
            skipped_deployments.append({
                "deployment": deployment,
                "reason": snapshot.get("reason") or "env_snapshot_failed_or_unsafe",
                "snapshot": snapshot,
            })
            continue
        command = context_set_env_command(config, deployment, assignments, target_container=snapshot.get("target_container"))
        set_env_result = run_cmd(command, timeout=60)
        commands.append(set_env_result)
        if set_env_result.get("returncode") == 0:
            patched_deployments.append(deployment)
            rollout_result = run_cmd([config.kubectl_path, "rollout", "status", f"deployment/{deployment}", "-n", config.namespace, "--timeout=120s"], timeout=150)
            commands.append(rollout_result)
            if rollout_result.get("returncode") == 0:
                effective_deployments.append(deployment)
    effective_metric_scope = prometheus_controller_label_selector(config) if "controller" in effective_deployments else ""
    post_patch_stabilization = wait_for_deployment_context_stabilization(
        config,
        dirs,
        phase="after_patch",
        deployments=effective_deployments,
        controller_label_selector=effective_metric_scope,
    ) if effective_deployments else {"ok": not patched_deployments, "skipped": True, "reason": "no_effective_deployments"}
    if effective_metric_scope and not post_patch_stabilization.get("ok"):
        # Do not scope later Prometheus analysis to labels that have not been observed yet.
        effective_metric_scope = ""
    result = {
        "available": True,
        "skipped": False,
        "patched": bool(patched_deployments),
        "all_patched": bool(effective_deployments) and all(c.get("returncode") == 0 for c in commands) and not skipped_deployments and bool(post_patch_stabilization.get("ok")),
        "patched_deployments": patched_deployments,
        "effective_deployments": effective_deployments,
        "skipped_deployments": skipped_deployments,
        "commands": commands,
        "previous_env": previous_env,
        "post_patch_stabilization": post_patch_stabilization,
        "metric_scope": prometheus_controller_label_selector(config),
        "effective_metric_scope": effective_metric_scope,
        "controller_scope_effective": bool(effective_metric_scope),
    }
    write_json(dirs["raw"] / "proxy_context_patch.json", result)
    return result

def prometheus_controller_label_selector(config: RunnerConfig) -> str:
    if not config.patch_proxy_context:
        return ""
    return '{tenant="%s",environment="%s",region="%s"}' % (
        prom_label_value(config.experiment_id),
        prom_label_value(config.scenario),
        prom_label_value(config.run_id),
    )


def prometheus_worker_metric_label_selector(config: RunnerConfig) -> str:
    """Return the selector used to scope worker-exporter FFmpeg metrics.

    The worker exporter does not emit Kubernetes namespace labels itself; in
    production these usually arrive from Prometheus scrape metadata. The selector
    is configurable because label names differ across Prometheus installations.
    """
    raw = (config.worker_metric_label_selector or "").strip()
    if not raw:
        return ""
    rendered = (
        raw
        .replace("$namespace", prom_label_value(config.namespace))
        .replace("$experiment_id", prom_label_value(config.experiment_id))
        .replace("$scenario", prom_label_value(config.scenario))
        .replace("$run_id", prom_label_value(config.run_id))
    )
    if rendered.startswith("{") and rendered.endswith("}"):
        return rendered
    return "{" + rendered + "}"


def prom_label_value(value: str) -> str:
    return str(value).replace('\\', '\\\\').replace('"', '\\"')


def render_promql(config: RunnerConfig, query: str, controller_label_selector: str | None = None) -> str:
    selector = prometheus_controller_label_selector(config) if controller_label_selector is None else controller_label_selector
    return (
        query
        .replace("$namespace", prom_label_value(config.namespace))
        .replace("$experiment_id", prom_label_value(config.experiment_id))
        .replace("$scenario", prom_label_value(config.scenario))
        .replace("$run_id", prom_label_value(config.run_id))
        .replace("$controller_label_selector", selector)
        .replace("$worker_metric_label_selector", prometheus_worker_metric_label_selector(config))
    )


def prometheus_query(config: RunnerConfig, query: str, start: float, end: float, step: int = 5, controller_label_selector: str | None = None) -> dict[str, Any]:
    if not config.prometheus_url:
        return {"available": False, "reason": "prometheus_url_not_configured", "query": query, "template_query": query}
    rendered_query = render_promql(config, query, controller_label_selector=controller_label_selector)
    params = urlencode({"query": rendered_query, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": str(step)})
    url = f"{config.prometheus_url}/api/v1/query_range?{params}"
    try:
        with urlopen(Request(url, headers={"User-Agent": "liveedgecast-experiment/1"}), timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {"available": True, "query": rendered_query, "template_query": query, "rendered_query": rendered_query, "response": payload}
    except Exception as exc:
        return {"available": False, "query": locals().get("rendered_query", query), "template_query": query, "rendered_query": locals().get("rendered_query", query), "error": {"type": type(exc).__name__, "message": str(exc)}}


def prometheus_instant_query(config: RunnerConfig, query: str, ts: float, controller_label_selector: str | None = None) -> dict[str, Any]:
    if not config.prometheus_url:
        return {"available": False, "reason": "prometheus_url_not_configured", "query": query, "template_query": query}
    rendered_query = render_promql(config, query, controller_label_selector=controller_label_selector)
    params = urlencode({"query": rendered_query, "time": f"{ts:.3f}"})
    url = f"{config.prometheus_url}/api/v1/query?{params}"
    try:
        with urlopen(Request(url, headers={"User-Agent": "liveedgecast-experiment/1"}), timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {"available": True, "query": rendered_query, "template_query": query, "rendered_query": rendered_query, "response": payload}
    except Exception as exc:
        return {"available": False, "query": locals().get("rendered_query", query), "template_query": query, "rendered_query": locals().get("rendered_query", query), "error": {"type": type(exc).__name__, "message": str(exc)}}


def prometheus_result_path(config: RunnerConfig, dirs: dict[str, Path]) -> Path:
    return dirs["raw"] / f"{PROMETHEUS_RUN_PREFIX}{config.run_id}.json"


def prometheus_instant_result_path(config: RunnerConfig, dirs: dict[str, Path]) -> Path:
    return dirs["raw"] / f"{PROMETHEUS_INSTANT_RUN_PREFIX}{config.run_id}.json"


def prometheus_result_run_id_from_path(path: Path) -> str | None:
    name = path.name
    if name.startswith(PROMETHEUS_RUN_PREFIX) and name.endswith(".json"):
        run_id = name[len(PROMETHEUS_RUN_PREFIX):-len(".json")]
        if run_id:
            return run_id
    # Backward-compatible support for older per-run files named
    # prometheus_range_queries.<run-id>.json. Reserved artifact names are not
    # interpreted as run ids.
    if name.startswith("prometheus_range_queries.") and name.endswith(".json"):
        stem = name[len("prometheus_range_queries."):-len(".json")]
        if stem and stem not in {"index", "__index__"}:
            return stem
    return None


def prometheus_run_files(dirs: dict[str, Path]) -> list[Path]:
    excluded = {PROMETHEUS_LEGACY_LATEST_FILENAME, PROMETHEUS_INDEX_FILENAME, PROMETHEUS_LEGACY_INDEX_FILENAME}
    candidates = [path for path in dirs["raw"].glob("prometheus_range_queries.*.json") if path.name not in excluded]
    return sorted(path for path in candidates if prometheus_result_run_id_from_path(path))


def update_prometheus_index(config: RunnerConfig, dirs: dict[str, Path], path: Path, start: float, end: float) -> None:
    index_path = dirs["raw"] / PROMETHEUS_INDEX_FILENAME
    payload: dict[str, Any] = {"schema": "prometheus-range-query-index/v1", "runs": []}
    if index_path.exists():
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {"schema": "prometheus-range-query-index/v1", "runs": []}
    runs = [run for run in payload.get("runs") or [] if run.get("run_id") != config.run_id]
    runs.append({
        "run_id": config.run_id,
        "path": path.name,
        "started_at": start,
        "ended_at": end,
        "scenario": config.scenario,
        "experiment_id": config.experiment_id,
    })
    payload["runs"] = sorted(runs, key=lambda run: str(run.get("run_id") or ""))
    write_json(index_path, payload)


def collect_prometheus(config: RunnerConfig, dirs: dict[str, Path], start: float, end: float, controller_label_selector: str | None = None) -> dict[str, Any]:
    results = {
        "_metadata": {
            "schema": "prometheus-range-query-result/v1",
            "run_id": config.run_id,
            "started_at": start,
            "ended_at": end,
            "controller_label_selector": controller_label_selector or "",
            "controller_scope_effective": bool(controller_label_selector),
            "resume_safe": True,
        }
    }
    instant_results = {
        "_metadata": {
            "schema": "prometheus-instant-query-result/v1",
            "run_id": config.run_id,
            "queried_at": end,
            "controller_label_selector": controller_label_selector or "",
            "controller_scope_effective": bool(controller_label_selector),
        }
    }
    for name, query in DEFAULT_PROMQL.items():
        results[name] = prometheus_query(config, query, start, end, controller_label_selector=controller_label_selector)
        instant_results[name] = prometheus_instant_query(config, query, end, controller_label_selector=controller_label_selector)
    per_run_path = prometheus_result_path(config, dirs)
    instant_path = prometheus_instant_result_path(config, dirs)
    results["_metadata"]["instant_query_path"] = instant_path.name
    write_json(per_run_path, results)
    write_json(instant_path, instant_results)
    update_prometheus_index(config, dirs, per_run_path, start, end)
    # Compatibility artifacts for tools that still expect the latest run at the legacy paths.
    # Aggregation in this runner reads per-run files first so resume does not lose prior evidence.
    write_json(dirs["raw"] / PROMETHEUS_LEGACY_LATEST_FILENAME, results)
    write_json(dirs["raw"] / PROMETHEUS_INSTANT_LATEST_FILENAME, instant_results)
    return results


def merge_prometheus_results(results_by_run: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {"_metadata": {"schema": "prometheus-range-query-merged/v1", "runs": []}}
    for result in results_by_run:
        metadata = result.get("_metadata") or {}
        merged["_metadata"]["runs"].append(metadata)
        for name, value in result.items():
            if str(name).startswith("_") or not isinstance(value, dict):
                continue
            target = merged.setdefault(name, {"available": False, "response": {"status": "success", "data": {"result": []}}, "sources": []})
            target["available"] = bool(target.get("available")) or bool(value.get("available"))
            if value.get("query"):
                target.setdefault("query", value.get("query"))
            if value.get("rendered_query"):
                target.setdefault("rendered_query", value.get("rendered_query"))
            if value.get("error") and not target.get("error"):
                target["error"] = value.get("error")
            if value.get("reason") and not target.get("reason"):
                target["reason"] = value.get("reason")
            target.setdefault("sources", []).append(metadata.get("run_id"))
            response = value.get("response") or {}
            data = response.get("data") or {}
            series = data.get("result") or []
            target_response = target.setdefault("response", {"status": response.get("status") or "success", "data": {"result": []}})
            target_data = target_response.setdefault("data", {"result": []})
            target_data.setdefault("result", []).extend(series)
            if response.get("status") and target_response.get("status") != "success":
                target_response["status"] = response.get("status")
    return merged


def load_prometheus_evidence(dirs: dict[str, Path]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for path in prometheus_run_files(dirs):
        try:
            results.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    if results:
        return merge_prometheus_results(results)
    legacy = dirs["raw"] / PROMETHEUS_LEGACY_LATEST_FILENAME
    if legacy.exists():
        try:
            return json.loads(legacy.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def prometheus_result_run_id(result: dict[str, Any], fallback: str | None = None) -> str | None:
    metadata = result.get("_metadata") or {}
    run_id = metadata.get("run_id") or fallback
    return str(run_id) if run_id else None


def load_prometheus_results_by_run(dirs: dict[str, Path]) -> dict[str, dict[str, Any]]:
    """Load per-run Prometheus evidence keyed by run_id.

    The legacy raw/prometheus_range_queries.json is intentionally ignored when
    per-run files exist because it represents only the latest collection and is
    not resume-safe. If only the legacy file exists, it is exposed under the
    run_id stored in its metadata, when available.
    """
    results: dict[str, dict[str, Any]] = {}
    for path in prometheus_run_files(dirs):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        run_id = prometheus_result_run_id(payload, fallback=prometheus_result_run_id_from_path(path))
        if run_id:
            results[run_id] = payload
    if results:
        return results
    legacy = dirs["raw"] / PROMETHEUS_LEGACY_LATEST_FILENAME
    if legacy.exists():
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        run_id = prometheus_result_run_id(payload)
        if run_id:
            results[run_id] = payload
    return results


def prometheus_metric_sample_count(result: dict[str, Any]) -> int:
    response = result.get("response") or {}
    data = response.get("data") or {}
    count = 0
    for series in data.get("result") or []:
        count += len(series.get("values") or [])
    return count


def prometheus_run_coverage(config: RunnerConfig, dirs: dict[str, Path], windows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    windows = windows if windows is not None else load_run_windows(config, dirs)
    expected_run_ids = sorted({str(window.get("run_id") or config.run_id) for window in windows if window.get("run_id") or config.run_id})
    results_by_run = load_prometheus_results_by_run(dirs)
    observed_run_ids = sorted(results_by_run)
    missing_run_ids = sorted(set(expected_run_ids) - set(observed_run_ids))
    extra_run_ids = sorted(set(observed_run_ids) - set(expected_run_ids))
    coverage_by_run = [
        {
            "run_id": run_id,
            "has_prometheus_evidence": run_id in results_by_run,
            "expected_by_run_windows": run_id in expected_run_ids,
        }
        for run_id in sorted(set(expected_run_ids) | set(observed_run_ids))
    ]
    return {
        "expected_run_ids": expected_run_ids,
        "observed_run_ids": observed_run_ids,
        "missing_run_ids": missing_run_ids,
        "extra_run_ids": extra_run_ids,
        "coverage_by_run": coverage_by_run,
        "complete": bool(expected_run_ids) and not missing_run_ids,
    }


def prometheus_metric_coverage_rows(config: RunnerConfig, dirs: dict[str, Path], windows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    windows = windows if windows is not None else load_run_windows(config, dirs)
    expected_run_ids = sorted({str(window.get("run_id") or config.run_id) for window in windows if window.get("run_id") or config.run_id})
    results_by_run = load_prometheus_results_by_run(dirs)
    rows: list[dict[str, Any]] = []
    for run_id in sorted(set(expected_run_ids) | set(results_by_run)):
        payload = results_by_run.get(run_id) or {}
        expected_by_run_windows = run_id in set(expected_run_ids)
        for metric in DEFAULT_PROMQL:
            value = payload.get(metric) if isinstance(payload, dict) else None
            sample_count = prometheus_metric_sample_count(value) if isinstance(value, dict) else 0
            query_success = bool(isinstance(value, dict) and value.get("available") and (value.get("response") or {}).get("status") == "success")
            samples_observed = sample_count > 0
            available_for_analysis = query_success and samples_observed
            required_for_analysis = metric_expected_for_analysis(config, metric)
            rows.append({
                "run_id": run_id,
                "metric": metric,
                "expected_by_run_windows": expected_by_run_windows,
                "metric_expected_for_scenario": required_for_analysis,
                "required_for_analysis": required_for_analysis,
                # Backward-compatible field: true only when the metric produced usable samples.
                "available": available_for_analysis,
                "query_success": query_success,
                "samples_observed": samples_observed,
                "available_for_analysis": available_for_analysis,
                "sample_count": sample_count,
                "query": (value.get("query") if isinstance(value, dict) else None),
                "rendered_query": (value.get("rendered_query") if isinstance(value, dict) else None),
                "status": ((value.get("response") or {}).get("status") if isinstance(value, dict) else None),
                "error": ((value.get("error") or value.get("reason")) if isinstance(value, dict) else "prometheus_evidence_missing_for_run"),
            })
    return rows


def coverage_value_true(row: dict[str, Any], field: str) -> bool:
    value = row.get(field)
    return value is True or str(value).lower() == "true"


def incomplete_prometheus_metric_names(rows: list[dict[str, Any]], *, required_only: bool = False) -> list[str]:
    by_metric: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not coverage_value_true(row, "expected_by_run_windows"):
            continue
        if required_only and not coverage_value_true(row, "required_for_analysis"):
            continue
        by_metric.setdefault(str(row.get("metric")), []).append(row)
    incomplete = []
    for metric, metric_rows in by_metric.items():
        if metric_rows and any(not coverage_value_true(row, "available_for_analysis") for row in metric_rows):
            incomplete.append(metric)
    return sorted(incomplete)


def prometheus_metric_runs_with_samples(rows: list[dict[str, Any]], metric: str) -> set[str]:
    return {str(row.get("run_id")) for row in rows if row.get("metric") == metric and coverage_value_true(row, "available_for_analysis")}


def prometheus_required_metrics_ready(rows: list[dict[str, Any]], required_metrics: set[str], expected_run_ids: set[str]) -> bool:
    if not expected_run_ids:
        return False
    for metric in required_metrics:
        if prometheus_metric_runs_with_samples(rows, metric) & expected_run_ids != expected_run_ids:
            return False
    return True


def finite_csv_number(raw: Any) -> bool:
    if raw in (None, "", "None", "null"):
        return False
    try:
        return math.isfinite(float(raw))
    except (TypeError, ValueError):
        return False


def prometheus_observed_duration(prom: dict[str, Any]) -> float | None:
    metadata = prom.get("_metadata") or {}
    runs = metadata.get("runs") or []
    total = 0.0
    for run in runs:
        duration = delta(run.get("started_at"), run.get("ended_at"))
        if duration is not None and duration > 0:
            total += duration
    if total > 0:
        return total
    duration = delta(metadata.get("started_at"), metadata.get("ended_at"))
    return duration if duration is not None and duration > 0 else None


def select_pod_by_selector(config: RunnerConfig, selector: str, stream_key: str | None = None) -> str | None:
    result = kubectl_json(config, ["get", "pods", "-n", config.namespace, "-l", selector, "-o", "json"])
    for item in (result.get("json") or {}).get("items", []):
        meta = item.get("metadata", {})
        status = item.get("status", {})
        if status.get("phase") not in {"Running", "Pending"}:
            continue
        if stream_key and (meta.get("annotations") or {}).get("liveedgecast.io/stream") != stream_key:
            continue
        return meta.get("name")
    return None


def delete_pod(config: RunnerConfig, pod: str) -> dict[str, Any]:
    return run_cmd([config.kubectl_path, "delete", "pod", pod, "-n", config.namespace, "--grace-period=0", "--force"], timeout=60)


def list_worker_pods(config: RunnerConfig) -> tuple[list[str], dict[str, Any]]:
    result = kubectl_json(config, ["get", "pods", "-n", config.namespace, "-l", "app=worker", "-o", "json"], timeout=60)
    pods: list[str] = []
    for item in (result.get("json") or {}).get("items", []):
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        name = metadata.get("name")
        phase = status.get("phase")
        deletion_timestamp = metadata.get("deletionTimestamp")
        if name and not deletion_timestamp and phase in {"Pending", "Running", "Unknown"}:
            pods.append(name)
    return pods, result


def ensure_zero_workers_for_cold_start(config: RunnerConfig, dirs: dict[str, Path], repetition: int, timeout_seconds: int = 90) -> dict[str, Any]:
    """Ensure a true worker scale-to-zero precondition before a cold-start run.

    This function deliberately fails the run if Kubernetes cannot be queried, because
    otherwise the experiment could report warm-start data as cold-start data. Existing
    worker pods in the experiment namespace are only deleted when
    --allow-worker-cleanup is enabled; otherwise active workers make the run invalid.
    """
    if not shutil.which(config.kubectl_path) and not Path(config.kubectl_path).exists():
        result = {
            "event": "cold_start_precondition_failed",
            "repetition": repetition,
            "timestamp": now_epoch(),
            "reason": f"kubectl not found: {config.kubectl_path}",
        }
        append_jsonl(dirs["raw"] / "streams.jsonl", result)
        raise RuntimeError(result["reason"])

    initial_pods, list_result = list_worker_pods(config)
    if list_result.get("returncode") != 0:
        result = {
            "event": "cold_start_precondition_failed",
            "repetition": repetition,
            "timestamp": now_epoch(),
            "reason": "unable_to_list_worker_pods",
            "kubectl": list_result,
        }
        append_jsonl(dirs["raw"] / "streams.jsonl", result)
        raise RuntimeError(f"Unable to verify cold-start precondition: {list_result.get('stderr') or list_result.get('error')}")

    if initial_pods and not config.allow_worker_cleanup:
        result = {
            "event": "cold_start_precondition_failed",
            "repetition": repetition,
            "timestamp": now_epoch(),
            "reason": "active_worker_pods_present_and_cleanup_not_allowed",
            "active_worker_pods": initial_pods,
        }
        append_jsonl(dirs["raw"] / "streams.jsonl", result)
        write_json(dirs["raw"] / f"cold_start_precondition_r{repetition}.json", result)
        raise RuntimeError(
            "Cold-start precondition failed: active worker pods present. "
            "Rerun in an isolated namespace or pass --allow-worker-cleanup."
        )

    actions: list[dict[str, Any]] = []
    for pod in initial_pods:
        actions.append({"pod": pod, "delete": delete_pod(config, pod)})

    deadline = now_epoch() + timeout_seconds
    remaining = initial_pods
    while now_epoch() < deadline:
        remaining, _ = list_worker_pods(config)
        if not remaining:
            break
        time.sleep(2)

    result = {
        "event": "cold_start_precondition",
        "repetition": repetition,
        "timestamp": now_epoch(),
        "initial_worker_pods": initial_pods,
        "delete_actions": actions,
        "remaining_worker_pods": remaining,
        "status": "ok" if not remaining else "failed",
    }
    append_jsonl(dirs["raw"] / "streams.jsonl", result)
    write_json(dirs["raw"] / f"cold_start_precondition_r{repetition}.json", result)
    if remaining:
        raise RuntimeError(f"Cold-start precondition failed: worker pods still active: {', '.join(remaining)}")
    return result


def execute_single_run(config: RunnerConfig, dirs: dict[str, Path], repetition: int, stream_keys: list[str]) -> dict[str, Any]:
    run_started = now_epoch()
    append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_started", "experiment_id": config.experiment_id, "scenario": config.scenario, "run_id": config.run_id, "repetition": repetition, "timestamp": run_started, "stream_keys": stream_keys})
    publishers: list[ManagedPublisher] = []
    failures: list[dict[str, Any]] = []
    injected: list[dict[str, Any]] = []
    try:
        if config.warmup_seconds:
            time.sleep(config.warmup_seconds)
        collect_kubernetes(config, dirs, f"before-r{repetition}")
        if config.scenario == "cold-start":
            ensure_zero_workers_for_cold_start(config, dirs, repetition)
            collect_kubernetes(config, dirs, f"after-zero-workers-r{repetition}")
        if config.scenario == "duplicate-streamkey":
            key = stream_keys[0]
            publishers.append(start_publisher(config, dirs, key, repetition, 1, suffix="-primary"))
            time.sleep(config.duplicate_attempt_delay_seconds)
            publishers.append(start_publisher(config, dirs, key, repetition, 2, suffix="-duplicate", rtmp_url_override=config.secondary_rtmp_url))
        elif config.scenario == "handover":
            key = stream_keys[0]
            first = start_publisher(config, dirs, key, repetition, 1, suffix="-handover-a")
            publishers.append(first)
            time.sleep(min(config.reconnect_delay_seconds, config.duration_seconds))
            first.stop(reason="expected_handover_primary_stop")
            append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "handover_primary_stopped", "experiment_id": config.experiment_id, "scenario": config.scenario, "run_id": config.run_id, "repetition": repetition, "stream_key": key, "timestamp": now_epoch()})
            time.sleep(config.reconnect_delay_seconds)
            publishers.append(start_publisher(config, dirs, key, repetition, 2, suffix="-handover-b", rtmp_url_override=config.secondary_rtmp_url))
        else:
            if config.scenario == "concurrency" and config.tee_stream_keys and len(stream_keys) > 1:
                publishers.append(start_tee_stream_key_publisher(config, dirs, stream_keys, repetition))
            else:
                for index, key in enumerate(stream_keys):
                    publishers.append(start_publisher(config, dirs, key, repetition, index + 1))
                    if index < len(stream_keys) - 1 and config.startup_interval_seconds:
                        time.sleep(config.startup_interval_seconds)
        if config.scenario == "release":
            time.sleep(min(config.release_after_seconds, config.duration_seconds))
            # Capture worker/controller evidence while the stream is still expected to be active.
            collect_logs(config, dirs, phase=f"r{repetition}-before-release")
        if config.scenario == "worker-failure" or config.kill_worker:
            time.sleep(min(config.kill_after_seconds, max(1, config.duration_seconds - 1)))
            target_stream = stream_keys[0]
            pod = select_pod_by_selector(config, "app=worker", stream_key=target_stream) or select_pod_by_selector(config, "app=worker")
            if pod:
                collect_logs(config, dirs, phase=f"r{repetition}-before-worker-delete")
                deleted = delete_pod(config, pod)
                injected.append({"type": "worker-failure", "stream_key": target_stream, "pod": pod, "timestamp": now_epoch(), "result": deleted})
            else:
                injected.append({"type": "worker-failure", "stream_key": target_stream, "status": "pod_not_found", "timestamp": now_epoch()})
        if config.scenario == "proxy-failure" or config.kill_proxy:
            time.sleep(min(config.kill_after_seconds, max(1, config.duration_seconds - 1)))
            pod = select_pod_by_selector(config, "app=proxy")
            if pod:
                collect_logs(config, dirs, phase=f"r{repetition}-before-proxy-delete")
                deleted = delete_pod(config, pod)
                injected.append({"type": "proxy-failure", "stream_key": stream_keys[0] if stream_keys else None, "pod": pod, "timestamp": now_epoch(), "result": deleted})
            else:
                injected.append({"type": "proxy-failure", "stream_key": stream_keys[0] if stream_keys else None, "status": "pod_not_found", "timestamp": now_epoch()})
        # Release scenario intentionally stops publishers and waits for cleanup observation.
        results = wait_or_stop_publishers(
            config,
            dirs,
            publishers,
            wait=(config.scenario != "release"),
            stop_reason="expected_release_stop" if config.scenario == "release" else "expected_stop",
        )
        if config.cooldown_seconds:
            time.sleep(config.cooldown_seconds)
        collect_kubernetes(config, dirs, f"after-r{repetition}")
        failures = [r for r in results if r.get("publisher_status") == "unexpected_failed"]
        run_ended = now_epoch()
        run_summary = {
            "repetition": repetition,
            "started_at": run_started,
            "ended_at": run_ended,
            "duration_seconds": run_ended - run_started,
            "stream_keys": stream_keys,
            "publishers": results,
            "failure_count": len(failures),
            "error_rate": len(failures) / len(results) if results else 1.0,
            "injected_failures": injected,
        }
        append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_finished", "experiment_id": config.experiment_id, "scenario": config.scenario, "run_id": config.run_id, **run_summary})
        collect_logs(config, dirs, phase=f"r{repetition}-after-run")
        return run_summary
    except KeyboardInterrupt:
        for publisher in publishers:
            publisher.stop(reason="interrupted_by_user")
        if publishers:
            record_publisher_results(config, dirs, publishers)
        append_jsonl(dirs["raw"] / "streams.jsonl", {
            "event": "run_interrupted",
            "experiment_id": config.experiment_id,
            "scenario": config.scenario,
            "run_id": config.run_id,
            "repetition": repetition,
            "started_at": run_started,
            "ended_at": now_epoch(),
            "stream_keys": stream_keys,
        })
        raise
    except Exception as exc:
        for publisher in publishers:
            publisher.stop(reason="unexpected_run_exception")
        publisher_results = record_publisher_results(config, dirs, publishers) if publishers else []
        run_ended = now_epoch()
        failure_summary = {
            "event": "run_failed",
            "experiment_id": config.experiment_id,
            "scenario": config.scenario,
            "run_id": config.run_id,
            "repetition": repetition,
            "started_at": run_started,
            "ended_at": run_ended,
            "stream_keys": stream_keys,
            "publishers": publisher_results,
            "failure_count": len([r for r in publisher_results if r.get("publisher_status") == "unexpected_failed"]),
            "error_rate": (len([r for r in publisher_results if r.get("publisher_status") == "unexpected_failed"]) / len(publisher_results)) if publisher_results else 1.0,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "injected_failures": injected,
        }
        append_jsonl(dirs["raw"] / "streams.jsonl", failure_summary)
        return {k: v for k, v in failure_summary.items() if k != "event"}




def build_pilot_levels(max_n: int, step_size: int) -> list[int]:
    if max_n <= 0:
        return []
    if max_n == 1:
        return [1]
    levels = [1]
    current = step_size
    while current < max_n:
        if current not in levels:
            levels.append(current)
        current += step_size
    if max_n not in levels:
        levels.append(max_n)
    return levels


def activation_p95_for_repetition(config: RunnerConfig, dirs: dict[str, Path], repetition: int) -> float | None:
    publisher_rows = [r for r in read_jsonl(dirs["raw"] / "publishers.jsonl") if r.get("event") == "publisher_finished"]
    activation_rows, _, _ = build_lifecycle_rows(config, dirs, publisher_rows)
    values = sorted(
        float(r["total_activation_seconds"])
        for r in activation_rows
        if int(r.get("repetition") or -1) == repetition and r.get("total_activation_seconds") is not None
    )
    return percentile(values, 95) if values else None


def experiment_query_window(dirs: dict[str, Path], fallback_start: float, fallback_end: float, run_id: str | None = None) -> tuple[float, float]:
    records = read_jsonl(dirs["raw"] / "streams.jsonl")
    if run_id is not None:
        records = [r for r in records if str(r.get("run_id") or "") == run_id]
    starts = [r.get("timestamp") for r in records if r.get("event") == "run_started" and isinstance(r.get("timestamp"), (int, float))]
    ends = [r.get("ended_at") for r in records if r.get("event") in {"run_finished", "run_failed", "run_interrupted"} and isinstance(r.get("ended_at"), (int, float))]
    return (min(starts) if starts else fallback_start, max(ends) if ends else fallback_end)


def sum_run_window_durations(windows: list[dict[str, Any]]) -> float:
    total = 0.0
    for window in windows:
        duration = delta(window.get("started_at"), window.get("ended_at"))
        if duration is not None:
            total += duration
    return total


def always_on_worker_pod_seconds_reference(windows: list[dict[str, Any]], fallback_stream_keys: list[str], fallback_duration: float) -> tuple[float, str]:
    total = 0.0
    saw_window = False
    for window in windows:
        duration = delta(window.get("started_at"), window.get("ended_at"))
        if duration is None or duration <= 0:
            continue
        stream_keys = window.get("stream_keys") or fallback_stream_keys
        stream_count = len(stream_keys) if isinstance(stream_keys, list) else len(fallback_stream_keys)
        total += max(1, stream_count) * duration
        saw_window = True
    if saw_window:
        return total, "sum_per_run_window_stream_count_times_duration"
    return max(1, len(fallback_stream_keys)) * fallback_duration, "fallback_len_stream_keys_times_query_window"


def simplified_lifecycle_warning(config: RunnerConfig) -> dict[str, Any] | None:
    if config.scenario not in SIMPLIFIED_MODE_UNSUPPORTED_LIFECYCLE_SCENARIOS and not config.kill_worker and not config.kill_proxy:
        return None
    return {
        "event": "simplified_lifecycle_scenario_warning",
        "experiment_id": config.experiment_id,
        "scenario": config.scenario,
        "run_id": config.run_id,
        "timestamp": now_epoch(),
        "status": "unsupported_recovery_handover_semantics",
        "message": (
            "Simplified mode disables controller handover, auto-recovery and orphan cleanup; "
            "this run observes terminal/no-recovery behavior rather than validating automatic resilience."
        ),
    }


def execute_experiment(config: RunnerConfig, dirs: dict[str, Path], simplified_warning: dict[str, Any] | None = None) -> dict[str, Any]:
    if not shutil.which(config.ffmpeg_path) and not Path(config.ffmpeg_path).exists():
        raise RuntimeError(f"ffmpeg not found: {config.ffmpeg_path}")
    if simplified_warning:
        append_jsonl(dirs["raw"] / "streams.jsonl", simplified_warning)

    setup_started_at = now_epoch()
    patch_result = patch_proxy_context(config, dirs)
    preflight = {
        "proxy_context_patch": patch_result,
        "controller_before": collect_controller_http(config, dirs, "before"),
    }
    experiment_started_at = now_epoch()
    run_summaries: list[dict[str, Any]] = []
    status = "valid"
    context_scope_ok = True
    context_patch_status = "not_requested"
    if config.patch_proxy_context:
        context_scope_ok = bool(patch_result.get("all_patched"))
        context_patch_status = "effective" if context_scope_ok else "incomplete"
        if not context_scope_ok:
            status = "partial_context_patch"
    logs: dict[str, Any] = {}
    controller_after: dict[str, Any] = {}
    prometheus: dict[str, Any] = {}
    restore_result: dict[str, Any] = {}

    try:
        if config.scenario == "pilot-capacity":
            for level in build_pilot_levels(len(config.stream_keys), config.pilot_step_size):
                keys = config.stream_keys[:level]
                summary = execute_single_run(config, dirs, level, keys)
                summary["pilot_concurrency"] = level
                run_summaries.append(summary)
                saturation_reasons = []
                if summary.get("error_rate", 0) >= config.saturation_error_rate:
                    saturation_reasons.append("publisher_error_rate")
                # Best-effort latency criterion: collect logs after each level and evaluate observed P95.
                collect_logs(config, dirs, phase=f"pilot-level-{level}")
                observed_p95 = activation_p95_for_repetition(config, dirs, repetition=level)
                summary["activation_p95_seconds"] = observed_p95
                if observed_p95 is not None and observed_p95 >= config.saturation_p95_seconds:
                    saturation_reasons.append("activation_p95_threshold")
                if summary.get("error"):
                    saturation_reasons.append("run_error")
                if saturation_reasons:
                    summary["saturation_reason"] = ",".join(saturation_reasons)
                    break
        else:
            for repetition in range(1, config.repetitions + 1):
                run_summaries.append(execute_single_run(config, dirs, repetition, config.stream_keys))
        if any(run.get("error") for run in run_summaries):
            status = "failed" if config.scenario == "cold-start" else "partial"

        experiment_ended_at = now_epoch()
        query_start, query_end = experiment_query_window(dirs, experiment_started_at, experiment_ended_at, run_id=config.run_id)
        prometheus = collect_prometheus(config, dirs, query_start, query_end, controller_label_selector=patch_result.get("effective_metric_scope") or "")
        # Collect logs and controller state before restoring patched deployments; restore may roll pods.
        logs = collect_logs(config, dirs, phase="final-before-restore")
        controller_after = collect_controller_http(config, dirs, "after")
    finally:
        restore_result = restore_context_keys(config, dirs, patch_result)

    ended_at = now_epoch()
    restore_ok = bool(restore_result.get("ok", True))
    if config.patch_proxy_context and not restore_ok:
        status = "failed_restore" if status == "valid" else f"{status}_restore_failed"
    return {
        "status": status,
        "context_scope_ok": context_scope_ok,
        "context_patch_status": context_patch_status,
        "restore_ok": restore_ok,
        "setup_started_at": setup_started_at,
        "started_at": experiment_started_at,
        "ended_at": ended_at,
        "prometheus_window": {"started_at": experiment_query_window(dirs, experiment_started_at, ended_at, run_id=config.run_id)[0], "ended_at": experiment_query_window(dirs, experiment_started_at, ended_at, run_id=config.run_id)[1], "run_id": config.run_id},
        "preflight": preflight,
        "runs": run_summaries,
        "prometheus": summarize_prometheus_availability(prometheus),
        "logs": logs,
        "controller_after": controller_after,
        "context_restore": restore_result,
    }



def summarize_prometheus_availability(results: dict[str, Any]) -> dict[str, Any]:
    return {
        name: {"available": bool(value.get("available")), "status": (value.get("response") or {}).get("status"), "error": value.get("error") or value.get("reason")}
        for name, value in results.items()
        if not str(name).startswith("_") and isinstance(value, dict)
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def extract_pod_rows(dirs: dict[str, Path], stream_keys: list[str]) -> list[dict[str, Any]]:
    rows = []
    for record in read_jsonl(dirs["raw"] / "pod_snapshots.jsonl"):
        pod = record.get("pod") or {}
        metadata = pod.get("metadata") or {}
        status = pod.get("status") or {}
        annotations = metadata.get("annotations") or {}
        labels = metadata.get("labels") or {}
        name = metadata.get("name")
        if not name:
            continue
        stream = annotations.get("liveedgecast.io/stream")
        if stream_keys and stream and stream not in stream_keys:
            continue
        rows.append({
            "snapshot_phase": record.get("phase"),
            "snapshot_at": record.get("snapshot_at"),
            "pod": name,
            "component": labels.get("app") or infer_component(name),
            "stream_key": stream,
            "generation": annotations.get("liveedgecast.io/generation"),
            "proxy_pod": annotations.get("liveedgecast.io/proxy-pod"),
            "phase": status.get("phase"),
            "pod_ip": status.get("podIP"),
            "start_time": status.get("startTime"),
            "restart_count": sum((cs.get("restartCount") or 0) for cs in status.get("containerStatuses", []) or []),
            "ready": any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []) or []),
        })
    return rows


def infer_component(pod_name: str) -> str:
    for prefix in ("worker", "proxy", "controller"):
        if pod_name.startswith(prefix):
            return prefix
    return "unknown"


def prom_values(result: dict[str, Any]) -> list[float]:
    response = result.get("response") or {}
    data = response.get("data") or {}
    values: list[float] = []
    for series in data.get("result") or []:
        for _, value in series.get("values") or []:
            try:
                values.append(float(value))
            except Exception:
                continue
    return values



def prom_series_values_by_component(result: dict[str, Any]) -> dict[str, list[float]]:
    response = result.get("response") or {}
    data = response.get("data") or {}
    grouped: dict[str, list[float]] = {}
    for series in data.get("result") or []:
        metric = series.get("metric") or {}
        # Prefer explicit workload labels when they exist, then fall back to
        # pod-name inference. Some Prometheus/cAdvisor setups expose resource
        # series without a ``pod`` label after recording rules, but retain
        # labels such as ``component`` or ``app``. If we only inferred from the
        # pod label, valid worker CPU samples could be grouped as ``unknown``
        # (or disappear from the expected worker row in resource_usage.csv).
        component = (
            str(metric.get("component") or metric.get("app") or metric.get("app_kubernetes_io_name") or "").strip()
            or infer_component(metric.get("pod") or metric.get("pod_name") or metric.get("container") or "")
        )
        component = infer_component(component) if component else "unknown"
        if component == "unknown" and metric.get("job"):
            component = infer_component(str(metric.get("job")))
        bucket = grouped.setdefault(component, [])
        for _, value in series.get("values") or []:
            try:
                bucket.append(float(value))
            except Exception:
                continue
    return grouped


def prom_time_integral(result: dict[str, Any], fallback_duration: float = 0.0) -> tuple[float | None, str]:
    """Integrate a Prometheus count-like query over time using step-wise samples."""
    response = result.get("response") or {}
    data = response.get("data") or {}
    total = 0.0
    saw_values = False
    for series in data.get("result") or []:
        samples = []
        for ts, value in series.get("values") or []:
            try:
                samples.append((float(ts), float(value)))
            except Exception:
                continue
        if not samples:
            continue
        saw_values = True
        samples.sort()
        if len(samples) == 1:
            total += samples[0][1] * fallback_duration
            continue
        for (t1, v1), (t2, _v2) in zip(samples, samples[1:]):
            if t2 > t1:
                total += v1 * (t2 - t1)
    if not saw_values:
        return None, "no_prometheus_samples"
    return total, "prometheus_time_integral"


def first_observable_recovery_event(events: list[dict[str, Any]], injected_ts: Any, stream_key: str | None) -> tuple[float | None, str | None]:
    if not isinstance(injected_ts, (int, float)):
        return None, None
    preferred_fields = ("t_ffmpeg_first_progress", "t_worker_ready", "t_worker_pod_created")
    candidates: list[tuple[int, float, str]] = []
    for event in events:
        ts = event.get("timestamp_epoch")
        if not isinstance(ts, (int, float)) or ts < injected_ts:
            continue
        if stream_key and event.get("stream") not in (stream_key, None):
            continue
        field = lifecycle_field_from_event(event)
        if field in preferred_fields:
            candidates.append((preferred_fields.index(field), ts, field))
        elif event.get("event_type") in {"worker_recovered", "worker_replacement_completed"}:
            candidates.append((0, ts, str(event.get("event_type"))))
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][1], candidates[0][2]



def stats(values: Sequence[float]) -> dict[str, Any]:
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not clean:
        return {"samples": 0, "mean": None, "median": None, "stddev": None, "p50": None, "p95": None, "p99": None, "min": None, "max": None, "ci95_low": None, "ci95_high": None}
    clean.sort()
    mean = statistics.mean(clean)
    stddev = statistics.stdev(clean) if len(clean) > 1 else 0.0
    ci_delta = 1.96 * stddev / math.sqrt(len(clean)) if len(clean) > 1 else 0.0
    return {
        "samples": len(clean),
        "mean": mean,
        "median": statistics.median(clean),
        "stddev": stddev,
        "p50": percentile(clean, 50),
        "p95": percentile(clean, 95),
        "p99": percentile(clean, 99),
        "min": min(clean),
        "max": max(clean),
        "ci95_low": mean - ci_delta,
        "ci95_high": mean + ci_delta,
    }


def percentile(sorted_values: Sequence[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct / 100
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)



def lifecycle_field_from_event(event: dict[str, Any]) -> str | None:
    if event.get("event_type") == "stream_lifecycle_timestamp_observed":
        message = str(event.get("message") or "")
        match = re.search(r"\b(t_[A-Za-z0-9_]+) observed\b", message)
        if match:
            return match.group(1)
    direct = {
        "publish_received": "t_controller_received_event",
        "stream_ended_received": "t_controller_received_end",
        "worker_deleted": "t_worker_terminated",
        "destination_received": "t_destination_received",
    }
    return direct.get(str(event.get("event_type")))


def latest_event_time(events: list[dict[str, Any]], event_type: str, stream: str) -> float | None:
    values = [e.get("timestamp_epoch") for e in events if e.get("event_type") == event_type and e.get("stream") == stream and isinstance(e.get("timestamp_epoch"), (int, float))]
    return max(values) if values else None


def load_run_windows(config: RunnerConfig, dirs: dict[str, Path]) -> list[dict[str, Any]]:
    windows: dict[tuple[str, int], dict[str, Any]] = {}
    for record in read_jsonl(dirs["raw"] / "streams.jsonl"):
        repetition = record.get("repetition")
        if not isinstance(repetition, int):
            continue
        run_id = str(record.get("run_id") or config.run_id)
        current = windows.setdefault((run_id, repetition), {"run_id": run_id, "repetition": repetition, "stream_keys": list(config.stream_keys)})
        if record.get("event") == "run_started":
            current["started_at"] = record.get("timestamp")
            current["stream_keys"] = record.get("stream_keys") or current["stream_keys"]
        elif record.get("event") in {"run_finished", "run_failed", "run_interrupted"}:
            current["ended_at"] = record.get("ended_at") or record.get("timestamp")
            current["status"] = "failed" if record.get("event") == "run_failed" else ("interrupted" if record.get("event") == "run_interrupted" else "finished")
            current["stream_keys"] = record.get("stream_keys") or current["stream_keys"]
    if windows:
        return [windows[key] for key in sorted(windows, key=lambda item: (item[0], item[1]))]
    return [
        {"run_id": config.run_id, "repetition": rep, "started_at": None, "ended_at": None, "stream_keys": list(config.stream_keys)}
        for rep in range(1, config.repetitions + 1)
    ]


def scoped_identity_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"unknown", "none", "null"}:
        return None
    return text


def event_matches_window_identity(event: dict[str, Any], window: dict[str, Any]) -> bool:
    # Structured logs produced before --patch-proxy-context is enabled can carry
    # run_id/scenario/experiment_id as "unknown". Treat those values as
    # unspecified and fall back to the run window timestamp instead of dropping
    # valid worker lifecycle evidence.
    event_run_id = scoped_identity_value(event.get("run_id"))
    window_run_id = scoped_identity_value(window.get("run_id"))
    if event_run_id and window_run_id and event_run_id != window_run_id:
        return False
    event_rep = event.get("repetition")
    if isinstance(event_rep, int) and isinstance(window.get("repetition"), int) and event_rep != window.get("repetition"):
        return False
    return True

def event_in_window(event: dict[str, Any], window: dict[str, Any], total_windows: int) -> bool:
    ts = event.get("timestamp_epoch")
    start = window.get("started_at")
    end = window.get("ended_at")
    if not isinstance(ts, (int, float)):
        # If there is only one run and no event timestamp, keep the event visible.
        return total_windows == 1
    if isinstance(start, (int, float)) and ts < start:
        return False
    if isinstance(end, (int, float)) and ts > end:
        return False
    return True


def build_lifecycle_rows(config: RunnerConfig, dirs: dict[str, Path], publisher_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, int, str], dict[str, Any]]]:
    events = read_jsonl(dirs["raw"] / "controller_events.jsonl")
    run_windows = load_run_windows(config, dirs)
    grouped: dict[tuple[str, int, str], dict[str, Any]] = {}

    for window in run_windows:
        repetition = int(window.get("repetition") or 1)
        for key in (window.get("stream_keys") or config.stream_keys):
            run_id = str(window.get("run_id") or config.run_id)
            grouped[(run_id, repetition, key)] = {"run_id": run_id, "repetition": repetition, "stream_key": key, "concurrency": len(window.get("stream_keys") or config.stream_keys)}

    for event in events:
        stream = event.get("stream")
        if not stream or stream not in config.stream_keys:
            continue
        field = lifecycle_field_from_event(event)
        if not field:
            continue
        ts = event.get("timestamp_epoch")
        if not isinstance(ts, (int, float)):
            continue
        matching_windows = [w for w in run_windows if stream in (w.get("stream_keys") or config.stream_keys) and event_matches_window_identity(event, w) and event_in_window(event, w, len(run_windows))]
        # Without usable run windows, only a single repetition can be safely inferred.
        if not matching_windows and len(run_windows) == 1:
            matching_windows = run_windows
        for window in matching_windows:
            repetition = int(window.get("repetition") or 1)
            run_id = str(window.get("run_id") or config.run_id)
            entry = grouped.setdefault((run_id, repetition, stream), {"run_id": run_id, "repetition": repetition, "stream_key": stream, "concurrency": len(window.get("stream_keys") or config.stream_keys)})
            if field in entry:
                if field in {"t_worker_terminated", "t_controller_received_end", "t_destination_received"}:
                    entry[field] = max(entry[field], ts)
                else:
                    entry[field] = min(entry[field], ts)
            else:
                entry[field] = ts

    for row in publisher_rows:
        key = row.get("stream_key")
        repetition = int(row.get("repetition") or 1)
        run_id = str(row.get("run_id") or config.run_id)
        if key in config.stream_keys:
            entry = grouped.setdefault((run_id, repetition, key), {"run_id": run_id, "repetition": repetition, "stream_key": key, "concurrency": None})
            current_start = entry.get("t_publish_start_client")
            candidate_start = row.get("started_at")
            if isinstance(candidate_start, (int, float)) and (current_start is None or candidate_start < current_start):
                entry["t_publish_start_client"] = candidate_start
            current_end = entry.get("t_publish_end_client")
            candidate_end = row.get("ended_at")
            if isinstance(candidate_end, (int, float)) and (current_end is None or candidate_end > current_end):
                entry["t_publish_end_client"] = candidate_end

    activation_rows: list[dict[str, Any]] = []
    release_rows: list[dict[str, Any]] = []
    for run_id, repetition, key in sorted(grouped):
        entry = grouped[(run_id, repetition, key)]
        activation = {
            "run_id": run_id,
            "repetition": repetition,
            "stream_key": key,
            "concurrency": entry.get("concurrency"),
            "t_publish_start_client": entry.get("t_publish_start_client"),
            "t_publish_start_proxy": entry.get("t_publish_start_proxy"),
            "t_controller_received_event": entry.get("t_controller_received_event"),
            "t_worker_create_requested": entry.get("t_worker_create_requested"),
            "t_worker_pod_created": entry.get("t_worker_pod_created"),
            "t_worker_scheduled": entry.get("t_worker_scheduled"),
            "t_worker_container_started": entry.get("t_worker_container_started"),
            "t_worker_ready": entry.get("t_worker_ready"),
            "t_ffmpeg_started": entry.get("t_ffmpeg_started"),
            "t_ffmpeg_first_progress": entry.get("t_ffmpeg_first_progress"),
            "t_destination_received": entry.get("t_destination_received"),
        }
        activation["event_detection_seconds"] = delta(activation.get("t_publish_start_proxy"), activation.get("t_controller_received_event"), TIMESTAMP_ORDERING_TOLERANCE_SECONDS)
        activation["event_detection_status"] = delta_status(activation.get("t_publish_start_proxy"), activation.get("t_controller_received_event"), TIMESTAMP_ORDERING_TOLERANCE_SECONDS)
        activation["worker_create_seconds"] = delta(activation.get("t_worker_create_requested"), activation.get("t_worker_pod_created"))
        activation["worker_scheduling_seconds"] = delta(activation.get("t_worker_pod_created"), activation.get("t_worker_scheduled"))
        activation["worker_ready_seconds"] = delta(activation.get("t_worker_create_requested"), activation.get("t_worker_ready"))
        activation["ffmpeg_start_seconds"] = delta(activation.get("t_worker_ready"), activation.get("t_ffmpeg_started"))
        activation["ffmpeg_first_progress_seconds"] = delta(activation.get("t_ffmpeg_started"), activation.get("t_ffmpeg_first_progress"))
        total_start = activation.get("t_publish_start_client") or activation.get("t_publish_start_proxy") or activation.get("t_controller_received_event")
        activation["total_activation_seconds"] = delta(total_start, activation.get("t_ffmpeg_first_progress"))
        observed = sum(1 for field in ("t_controller_received_event", "t_worker_create_requested", "t_worker_ready", "t_ffmpeg_started", "t_ffmpeg_first_progress") if activation.get(field) is not None)
        activation["status"] = "derived_from_controller_structured_logs" if observed else "not_observable"
        activation_rows.append(activation)
        release = {
            "run_id": run_id,
            "repetition": repetition,
            "stream_key": key,
            "release_detection_seconds": delta(entry.get("t_publish_end_client"), entry.get("t_controller_received_end")),
            "worker_delete_seconds": delta(entry.get("t_controller_received_end"), entry.get("t_worker_terminated")),
            "total_release_seconds": delta(entry.get("t_publish_end_client"), entry.get("t_worker_terminated")),
            "status": "derived_from_controller_structured_logs" if entry.get("t_controller_received_end") or entry.get("t_worker_terminated") else "not_observable",
        }
        release_rows.append(release)
    return activation_rows, release_rows, grouped

def delta(start: Any, end: Any, tolerance_seconds: float = 0.0) -> float | None:
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    value = float(end) - float(start)
    if value < 0:
        if abs(value) <= tolerance_seconds:
            return 0.0
        return None
    return value


def delta_status(start: Any, end: Any, tolerance_seconds: float = 0.0) -> str:
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return "not_observable"
    value = float(end) - float(start)
    if value < 0:
        if abs(value) <= tolerance_seconds:
            return "clamped_to_zero_clock_skew_or_ordering_noise"
        return "invalid_negative_delta"
    return "observed"


def window_for_timestamp(windows: list[dict[str, Any]], timestamp: Any, run_id: str | None = None) -> dict[str, Any] | None:
    candidates = windows
    if run_id:
        candidates = [w for w in windows if str(w.get("run_id") or "") == str(run_id)]
    if not isinstance(timestamp, (int, float)):
        return candidates[0] if len(candidates) == 1 else None
    for window in candidates:
        if event_in_window({"timestamp_epoch": timestamp}, window, len(windows)):
            return window
    return None


def repetition_for_timestamp(windows: list[dict[str, Any]], timestamp: Any) -> int | None:
    window = window_for_timestamp(windows, timestamp)
    return int(window.get("repetition") or 1) if window else None


def worker_event_observations_from_controller_events(
    config: RunnerConfig,
    windows: list[dict[str, Any]],
    controller_events: list[dict[str, Any]],
) -> tuple[dict[tuple[str, int, str], set[str]], dict[tuple[str, int, str], int]]:
    """Return worker pods observed per stream and max observed overlap.

    Kubernetes snapshots can miss short-lived workers when they are created and
    deleted between snapshot phases. Controller structured events are denser and
    carry the streamKey/worker_pod relation. Use them to prove that a worker was
    observed for a stream, while still estimating duplicate simultaneous workers
    from create/delete lifecycle events when those are present.
    """
    observed: dict[tuple[str, int, str], set[str]] = {}
    max_counts: dict[tuple[str, int, str], int] = {}
    worker_observation_events = {
        "worker_create_requested",
        "worker_created",
        "worker_ready_observed",
        "ffmpeg_started",
        "ffmpeg_first_progress",
        "stream_lifecycle_timestamp_observed",
    }
    for window in windows:
        run_id = str(window.get("run_id") or config.run_id)
        rep = int(window.get("repetition") or 1)
        for stream in (window.get("stream_keys") or config.stream_keys):
            key = (run_id, rep, stream)
            timeline: list[tuple[float, int, str]] = []
            for event in controller_events:
                if not event_stream_matches(event, stream):
                    continue
                if not event_matches_window_identity(event, window) or not event_in_window(event, window, len(windows)):
                    continue
                worker_pod = event.get("worker_pod")
                ts = event.get("timestamp_epoch")
                event_type = str(event.get("event_type") or "")
                if worker_pod:
                    observed.setdefault(key, set()).add(str(worker_pod))
                if not worker_pod or not isinstance(ts, (int, float)):
                    continue
                if event_type == "worker_deleted":
                    timeline.append((float(ts), -1, str(worker_pod)))
                elif event_type == "worker_created":
                    timeline.append((float(ts), 1, str(worker_pod)))
                elif event_type in worker_observation_events:
                    # A worker observation without a worker_created event is
                    # enough to prove existence, but not enough to infer overlap.
                    max_counts[key] = max(max_counts.get(key, 0), 1)
            active: set[str] = set()
            # Deletions first at the same timestamp avoid false positives when
            # replacement events are emitted with coarse timestamp resolution.
            for _ts, action, worker_pod in sorted(timeline, key=lambda item: (item[0], item[1])):
                if action < 0:
                    active.discard(worker_pod)
                else:
                    active.add(worker_pod)
                    max_counts[key] = max(max_counts.get(key, 0), len(active))
    return observed, max_counts



def event_stream_matches(event: dict[str, Any], stream: str) -> bool:
    return event.get("stream") == stream or event.get("stream_key") == stream


def is_controller_denial_event(event: dict[str, Any]) -> bool:
    event_type = str(event.get("event_type") or "").lower()
    status = str(event.get("status") or "").lower()
    message = str(event.get("message") or event.get("detail") or "").lower()
    if event_type in {"handover_denied", "stream_conflict_denied", "stream_started_conflict", "stream_conflict", "ownership_conflict", "stream_allocation_conflict"}:
        return True
    if status in {"denied", "conflict", "rejected"}:
        return True
    return "already owned" in message or "conflict" in message or "409" in message


def is_controller_acceptance_event(event: dict[str, Any]) -> bool:
    event_type = str(event.get("event_type") or "").lower()
    status = str(event.get("status") or "").lower()
    if event_type in {"handover_accepted", "stream_registered", "worker_created", "publish_received"} and status not in {"denied", "conflict", "rejected"}:
        return True
    return event_type == "handover_accepted" or status in {"accepted", "registered", "created", "success"}


def proxy_observations_for_stream(
    controller_events: list[dict[str, Any]],
    window: dict[str, Any],
    stream: str,
    total_windows: int,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for event in controller_events:
        proxy = event.get("proxy_pod")
        ts = event.get("timestamp_epoch")
        if not proxy or not isinstance(ts, (int, float)):
            continue
        if not event_stream_matches(event, stream):
            continue
        if not event_matches_window_identity(event, window) or not event_in_window(event, window, total_windows):
            continue
        observations.append({
            "timestamp": float(ts),
            "proxy_pod": str(proxy),
            "event_type": event.get("event_type"),
            "status": event.get("status"),
        })
    observations.sort(key=lambda item: item["timestamp"])
    return observations


def scenario_proxy_validity_for_total(
    config: RunnerConfig,
    controller_events: list[dict[str, Any]],
    window: dict[str, Any],
    stream: str,
    total_windows: int,
    second_attempt_started_at: float | None = None,
) -> dict[str, Any]:
    raw_observations = proxy_observations_for_stream(controller_events, window, stream, total_windows)
    proxies: list[str] = []
    for item in raw_observations:
        proxy = str(item.get("proxy_pod") or "")
        if proxy and proxy not in proxies:
            proxies.append(proxy)

    first_observation = raw_observations[0] if raw_observations else None
    primary = str(first_observation.get("proxy_pod")) if first_observation else None
    second_attempt_observations = [
        item for item in raw_observations
        if second_attempt_started_at is not None and item["timestamp"] >= second_attempt_started_at
    ]
    second_attempt_proxy = str(second_attempt_observations[0].get("proxy_pod")) if second_attempt_observations else None
    secondary = None
    if primary and second_attempt_proxy and second_attempt_proxy != primary:
        secondary = second_attempt_proxy
    elif not second_attempt_started_at and len(proxies) > 1:
        # Backward-compatible fallback for historical evidence without publisher timestamps.
        secondary = proxies[1]

    secondary_observed = secondary is not None
    second_attempt_proxy_correlated = bool(second_attempt_started_at is not None and second_attempt_proxy is not None)
    same_proxy_detected = bool(primary and second_attempt_proxy and second_attempt_proxy == primary)
    requires_second_proxy = config.scenario in {"handover", "duplicate-streamkey"}
    inconclusive = requires_second_proxy and not secondary_observed
    reason = ""
    if inconclusive:
        if second_attempt_started_at is None:
            reason = "second_publisher_attempt_timestamp_not_observed"
        elif not second_attempt_proxy_correlated:
            reason = "second_attempt_proxy_not_observed"
        elif same_proxy_detected:
            reason = "second_attempt_reached_same_proxy"
        else:
            reason = "second_proxy_not_observed"
    return {
        "primary_proxy_pod": primary,
        "secondary_proxy_pod": secondary,
        "second_attempt_proxy_pod": second_attempt_proxy,
        "observed_proxy_sequence": ";".join(proxies),
        "secondary_proxy_observed": secondary_observed,
        "second_attempt_proxy_correlated": second_attempt_proxy_correlated,
        "same_proxy_detected": same_proxy_detected,
        "scenario_inconclusive": inconclusive,
        "scenario_inconclusive_reason": reason,
        "between_proxy_validity_status": "valid_between_proxy_observation" if secondary_observed else ("inconclusive" if requires_second_proxy else "not_applicable"),
        "secondary_rtmp_url_configured": bool(config.secondary_rtmp_url),
    }


def build_duplicate_streamkey_rows(
    config: RunnerConfig,
    dirs: dict[str, Path],
    controller_events: list[dict[str, Any]],
    publisher_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    windows = load_run_windows(config, dirs)
    rows: list[dict[str, Any]] = []
    for window in windows:
        run_id = str(window.get("run_id") or config.run_id)
        rep = int(window.get("repetition") or 1)
        for stream in (window.get("stream_keys") or config.stream_keys):
            pubs = [
                row for row in publisher_rows
                if str(row.get("run_id") or config.run_id) == run_id
                and int(row.get("repetition") or 0) == rep
                and row.get("stream_key") == stream
            ]
            second_publishers = [row for row in pubs if int(row.get("publisher_index") or 0) > 1]
            duplicate_pubs = second_publishers if config.scenario == "duplicate-streamkey" else []
            attempted = bool(duplicate_pubs)
            second_attempt_started_values = [
                float(row.get("started_at")) for row in second_publishers
                if isinstance(row.get("started_at"), (int, float))
            ]
            second_attempt_started_at = min(second_attempt_started_values) if second_attempt_started_values else None
            scoped_events = [
                e for e in controller_events
                if event_stream_matches(e, stream)
                and event_matches_window_identity(e, window)
                and event_in_window(e, window, len(windows))
            ]
            if second_attempt_started_at is not None:
                scoped_events_after_second_attempt = [
                    e for e in scoped_events
                    if not isinstance(e.get("timestamp_epoch"), (int, float)) or float(e.get("timestamp_epoch")) >= second_attempt_started_at
                ]
            else:
                scoped_events_after_second_attempt = scoped_events
            denial_events = [e for e in scoped_events_after_second_attempt if is_controller_denial_event(e)]
            accepted_events = [e for e in scoped_events_after_second_attempt if is_controller_acceptance_event(e)]
            proxy_validity = scenario_proxy_validity_for_total(config, controller_events, window, stream, len(windows), second_attempt_started_at=second_attempt_started_at)
            duplicate_statuses = [row.get("publisher_status") for row in duplicate_pubs]
            duplicate_process_statuses = [row.get("publisher_process_status") or publisher_process_status(row) for row in duplicate_pubs]
            rejected = attempted and bool(denial_events)
            unexpectedly_accepted = attempted and not rejected and any(status == "success" for status in duplicate_statuses)
            nonzero_without_rejection = attempted and not rejected and any(status == "nonzero_exit" for status in duplicate_process_statuses)
            if not attempted:
                controller_rejection_status = "not_attempted"
            elif rejected:
                controller_rejection_status = "rejected"
            elif unexpectedly_accepted:
                controller_rejection_status = "unexpectedly_accepted"
            else:
                controller_rejection_status = "not_observed"
            between_proxy_status = str(proxy_validity.get("between_proxy_validity_status") or "not_applicable")
            if attempted and rejected:
                status = "rejected"
            elif attempted and unexpectedly_accepted:
                status = "unexpectedly_accepted"
            elif attempted and nonzero_without_rejection:
                status = "duplicate_publisher_process_failed_without_controller_rejection"
            elif attempted and proxy_validity.get("scenario_inconclusive"):
                status = "attempted_controller_rejection_not_observed_between_proxy_inconclusive"
            elif attempted:
                status = "attempted_without_controller_rejection_observed"
            else:
                status = "not_attempted"
            rows.append({
                "run_id": run_id,
                "repetition": rep,
                "stream_key": stream,
                "second_publication_attempted": bool(second_publishers),
                "second_attempt_started_at": second_attempt_started_at,
                "duplicate_streamkey_attempted": attempted,
                "duplicate_streamkey_rejected": rejected,
                "duplicate_streamkey_unexpectedly_accepted": unexpectedly_accepted,
                "duplicate_publisher_count": len(duplicate_pubs),
                "duplicate_publisher_statuses": ";".join(str(status) for status in duplicate_statuses if status is not None),
                "duplicate_publisher_process_statuses": ";".join(str(status) for status in duplicate_process_statuses if status is not None),
                "duplicate_publisher_nonzero_without_controller_rejection": nonzero_without_rejection,
                "controller_denial_events": len(denial_events),
                "controller_acceptance_events": len(accepted_events),
                "controller_rejection_status": controller_rejection_status,
                "between_proxy_validity_status": between_proxy_status,
                **proxy_validity,
                "status": status,
            })
    return rows


def build_correctness_rows(
    config: RunnerConfig,
    dirs: dict[str, Path],
    pod_rows: list[dict[str, Any]],
    controller_events: list[dict[str, Any]],
    publisher_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    windows = load_run_windows(config, dirs)
    # Track worker pods by snapshot to detect simultaneous duplication, not historical replacement across repetitions or resumed run ids.
    snapshot_workers: dict[tuple[str, int, Any, str], set[str]] = {}
    max_workers_by_stream: dict[tuple[str, int, str], int] = {}
    event_worker_pods_by_stream, event_max_workers_by_stream = worker_event_observations_from_controller_events(config, windows, controller_events)
    duplicate_by_stream: dict[tuple[str, int, str], bool] = {}
    duplicate_rows = build_duplicate_streamkey_rows(config, dirs, controller_events, publisher_rows)
    duplicate_by_key = {(str(row.get("run_id") or config.run_id), int(row.get("repetition") or 0), row.get("stream_key")): row for row in duplicate_rows}
    orphan_by_window: dict[tuple[str, int], set[str]] = {
        (str(w.get("run_id") or config.run_id), int(w.get("repetition") or 1)): set() for w in windows
    }

    for row in pod_rows:
        window = window_for_timestamp(windows, row.get("snapshot_at"), row.get("run_id"))
        if window is None or row.get("component") != "worker":
            continue
        run_id = str(window.get("run_id") or config.run_id)
        rep = int(window.get("repetition") or 1)
        stream = row.get("stream_key")
        pod = row.get("pod")
        snapshot_at = row.get("snapshot_at")
        phase = row.get("phase")
        if phase not in {"Running", "Pending"}:
            continue
        if stream:
            key = (run_id, rep, snapshot_at, stream)
            bucket = snapshot_workers.setdefault(key, set())
            if pod:
                bucket.add(str(pod))
            count = len(bucket)
            max_key = (run_id, rep, stream)
            max_workers_by_stream[max_key] = max(max_workers_by_stream.get(max_key, 0), count)
            if count > 1:
                duplicate_by_stream[max_key] = True
        elif pod:
            orphan_by_window.setdefault((run_id, rep), set()).add(str(pod))

    rows: list[dict[str, Any]] = []
    for window in windows:
        run_id = str(window.get("run_id") or config.run_id)
        rep = int(window.get("repetition") or 1)
        stream_keys = window.get("stream_keys") or config.stream_keys
        for key in stream_keys:
            observed_event_workers = event_worker_pods_by_stream.get((run_id, rep, key), set())
            max_count = max(max_workers_by_stream.get((run_id, rep, key), 0), event_max_workers_by_stream.get((run_id, rep, key), 0), 1 if observed_event_workers else 0)
            duplicate_info = duplicate_by_key.get((run_id, rep, key), {})
            rows.append({
                "run_id": run_id,
                "repetition": rep,
                "stream_key": key,
                "max_worker_count_observed": max_count,
                "worker_observed_for_stream": max_count >= 1,
                "at_most_one_worker_per_stream": max_count <= 1,
                "one_worker_per_stream": max_count == 1,
                "duplicate_worker_detected": bool(duplicate_by_stream.get((run_id, rep, key), False)) or event_max_workers_by_stream.get((run_id, rep, key), 0) > 1,
                "duplicate_streamkey_attempted": duplicate_info.get("duplicate_streamkey_attempted", False),
                "duplicate_streamkey_rejected": duplicate_info.get("duplicate_streamkey_rejected", False),
                "duplicate_streamkey_unexpectedly_accepted": duplicate_info.get("duplicate_streamkey_unexpectedly_accepted", False),
                "primary_proxy_pod": duplicate_info.get("primary_proxy_pod"),
                "secondary_proxy_pod": duplicate_info.get("secondary_proxy_pod"),
                "secondary_proxy_observed": duplicate_info.get("secondary_proxy_observed"),
                "second_attempt_proxy_correlated": duplicate_info.get("second_attempt_proxy_correlated"),
                "second_attempt_proxy_pod": duplicate_info.get("second_attempt_proxy_pod"),
                "controller_rejection_status": duplicate_info.get("controller_rejection_status"),
                "between_proxy_validity_status": duplicate_info.get("between_proxy_validity_status"),
                "same_proxy_detected": duplicate_info.get("same_proxy_detected"),
                "scenario_inconclusive": duplicate_info.get("scenario_inconclusive"),
                "scenario_inconclusive_reason": duplicate_info.get("scenario_inconclusive_reason"),
                "handover_accepted": sum(1 for e in controller_events if e.get("event_type") == "handover_accepted" and event_stream_matches(e, key) and event_matches_window_identity(e, window) and event_in_window(e, window, len(windows))),
                "handover_denied": sum(1 for e in controller_events if e.get("event_type") == "handover_denied" and event_stream_matches(e, key) and event_matches_window_identity(e, window) and event_in_window(e, window, len(windows))),
                "stale_events_ignored": sum(1 for e in controller_events if e.get("event_type") == "stale_event_ignored" and event_stream_matches(e, key) and event_matches_window_identity(e, window) and event_in_window(e, window, len(windows))),
            })
        rows.append({
            "run_id": run_id,
            "repetition": rep,
            "stream_key": "__orphans__",
            "max_worker_count_observed": len(orphan_by_window.get((run_id, rep), set())),
            "worker_observed_for_stream": None,
            "at_most_one_worker_per_stream": None,
            "one_worker_per_stream": None,
            "duplicate_worker_detected": None,
            "duplicate_streamkey_attempted": None,
            "duplicate_streamkey_rejected": None,
            "duplicate_streamkey_unexpectedly_accepted": None,
            "primary_proxy_pod": None,
            "secondary_proxy_pod": None,
            "secondary_proxy_observed": None,
            "second_attempt_proxy_correlated": None,
            "second_attempt_proxy_pod": None,
            "controller_rejection_status": None,
            "between_proxy_validity_status": None,
            "same_proxy_detected": None,
            "scenario_inconclusive": None,
            "scenario_inconclusive_reason": None,
            "handover_accepted": sum(1 for e in controller_events if e.get("event_type") == "handover_accepted" and event_matches_window_identity(e, window) and event_in_window(e, window, len(windows))),
            "handover_denied": sum(1 for e in controller_events if e.get("event_type") == "handover_denied" and event_matches_window_identity(e, window) and event_in_window(e, window, len(windows))),
            "stale_events_ignored": sum(1 for e in controller_events if e.get("event_type") == "stale_event_ignored" and event_matches_window_identity(e, window) and event_in_window(e, window, len(windows))),
        })
    return rows


def build_metrics(config: RunnerConfig, dirs: dict[str, Path]) -> dict[str, Any]:
    run_windows = load_run_windows(config, dirs)
    prom_coverage = prometheus_run_coverage(config, dirs, run_windows)
    prom_results_by_run = load_prometheus_results_by_run(dirs)
    expected_prom_run_ids = set(prom_coverage.get("expected_run_ids") or [])
    # Use only Prometheus evidence tied to expected run windows for analysis.
    # Extra/stale per-run files may exist after manual file copies or failed
    # resume attempts; they should be reported as extras, not merged into
    # resource/activity numerators.
    prom_run_ids_for_analysis = expected_prom_run_ids if expected_prom_run_ids else set(prom_results_by_run)
    prom_results_for_analysis = [prom_results_by_run[run_id] for run_id in sorted(prom_run_ids_for_analysis) if run_id in prom_results_by_run]
    prom = merge_prometheus_results(prom_results_for_analysis) if prom_results_for_analysis else load_prometheus_evidence(dirs)
    prom_metric_rows = prometheus_metric_coverage_rows(config, dirs, run_windows)
    write_csv(
        dirs["metrics"] / "prometheus_metric_coverage.csv",
        prom_metric_rows,
        ["run_id", "metric", "expected_by_run_windows", "metric_expected_for_scenario", "required_for_analysis", "available", "query_success", "samples_observed", "available_for_analysis", "sample_count", "query", "rendered_query", "status", "error"],
    )
    pod_rows = extract_pod_rows(dirs, config.stream_keys)
    publisher_rows = [r for r in read_jsonl(dirs["raw"] / "publishers.jsonl") if r.get("event") == "publisher_finished"]
    controller_events = read_jsonl(dirs["raw"] / "controller_events.jsonl")

    activation_rows, release_rows, lifecycle_by_stream = build_lifecycle_rows(config, dirs, publisher_rows)
    activation_fields = [
        "run_id", "repetition", "stream_key", "concurrency", "t_publish_start_client", "t_publish_start_proxy", "t_controller_received_event", "t_worker_create_requested", "t_worker_pod_created", "t_worker_scheduled", "t_worker_container_started", "t_worker_ready", "t_ffmpeg_started", "t_ffmpeg_first_progress", "t_destination_received", "event_detection_seconds", "event_detection_status", "worker_create_seconds", "worker_scheduling_seconds", "worker_ready_seconds", "ffmpeg_start_seconds", "ffmpeg_first_progress_seconds", "total_activation_seconds", "status"
    ]
    write_csv(dirs["metrics"] / "activation_metrics.csv", activation_rows, activation_fields)
    write_csv(dirs["metrics"] / "release_metrics.csv", release_rows, ["run_id", "repetition", "stream_key", "release_detection_seconds", "worker_delete_seconds", "total_release_seconds", "status"])

    resilience_rows = []
    for run in read_jsonl(dirs["raw"] / "streams.jsonl"):
        if run.get("event") in {"run_finished", "run_failed"}:
            for injected in run.get("injected_failures") or []:
                injected_ts = injected.get("timestamp")
                stream_key = injected.get("stream_key")
                recovery_end, recovery_event = first_observable_recovery_event(controller_events, injected_ts, stream_key)
                resilience_rows.append({
                    "run_id": run.get("run_id"),
                    "repetition": run.get("repetition"),
                    "type": injected.get("type"),
                    "stream_key": stream_key,
                    "pod": injected.get("pod"),
                    "timestamp": injected_ts,
                    "recovery_completed_at": recovery_end,
                    "recovery_event": recovery_event,
                    "recovery_seconds": delta(injected_ts, recovery_end),
                    "status": injected.get("status") or ("injected" if injected.get("pod") else "not_injected"),
                })
    write_csv(dirs["metrics"] / "resilience_metrics.csv", resilience_rows, ["run_id", "repetition", "type", "stream_key", "pod", "timestamp", "recovery_completed_at", "recovery_event", "recovery_seconds", "status"])

    resource_rows = []
    for metric_name in ("pod_cpu_rate", "pod_memory_working_set"):
        grouped_components = prom_series_values_by_component(prom.get(metric_name, {}))
        if not grouped_components:
            resource_rows.append({"metric": metric_name, "component": "not_observable", **stats([])})
            continue
        for component, values in sorted(grouped_components.items()):
            resource_rows.append({"metric": metric_name, "component": component, **stats(values)})
    write_csv(dirs["metrics"] / "resource_usage.csv", resource_rows, ["metric", "component", "samples", "mean", "median", "stddev", "p50", "p95", "p99", "min", "max", "ci95_low", "ci95_high"])

    duplicate_streamkey_rows = build_duplicate_streamkey_rows(config, dirs, controller_events, publisher_rows)
    write_csv(
        dirs["metrics"] / "duplicate_streamkey_metrics.csv",
        duplicate_streamkey_rows,
        ["run_id", "repetition", "stream_key", "second_publication_attempted", "second_attempt_started_at", "duplicate_streamkey_attempted", "duplicate_streamkey_rejected", "duplicate_streamkey_unexpectedly_accepted", "duplicate_publisher_count", "duplicate_publisher_statuses", "duplicate_publisher_process_statuses", "duplicate_publisher_nonzero_without_controller_rejection", "controller_denial_events", "controller_acceptance_events", "controller_rejection_status", "between_proxy_validity_status", "primary_proxy_pod", "secondary_proxy_pod", "second_attempt_proxy_pod", "observed_proxy_sequence", "secondary_proxy_observed", "second_attempt_proxy_correlated", "same_proxy_detected", "scenario_inconclusive", "scenario_inconclusive_reason", "secondary_rtmp_url_configured", "status"],
    )

    correctness_rows = build_correctness_rows(config, dirs, pod_rows, controller_events, publisher_rows)
    write_csv(
        dirs["metrics"] / "correctness_metrics.csv",
        correctness_rows,
        ["run_id", "repetition", "stream_key", "max_worker_count_observed", "worker_observed_for_stream", "at_most_one_worker_per_stream", "one_worker_per_stream", "duplicate_worker_detected", "duplicate_streamkey_attempted", "duplicate_streamkey_rejected", "duplicate_streamkey_unexpectedly_accepted", "primary_proxy_pod", "secondary_proxy_pod", "secondary_proxy_observed", "second_attempt_proxy_correlated", "second_attempt_proxy_pod", "controller_rejection_status", "between_proxy_validity_status", "same_proxy_detected", "scenario_inconclusive", "scenario_inconclusive_reason", "handover_accepted", "handover_denied", "stale_events_ignored"],
    )


    metadata = json.loads((config.report_root / "metadata.json").read_text(encoding="utf-8")) if (config.report_root / "metadata.json").exists() else {}
    execution = json.loads((config.report_root / "execution.json").read_text(encoding="utf-8")) if (config.report_root / "execution.json").exists() else {}
    prom_window = execution.get("prometheus_window") or {}
    query_duration = prometheus_observed_duration(prom)
    if query_duration is None:
        query_duration = delta(prom_window.get("started_at"), prom_window.get("ended_at"))
    if query_duration is None:
        query_duration = max(0.0, (metadata.get("ended_at") or 0) - (metadata.get("started_at") or 0))
    expected_run_ids = set(prom_coverage.get("expected_run_ids") or [])
    evidence_file_run_ids = set(prom_coverage.get("observed_run_ids") or [])
    required_resource_metrics = {"workers_active"}
    worker_sample_run_ids = prometheus_metric_runs_with_samples(prom_metric_rows, "workers_active")
    worker_samples_complete = bool(expected_run_ids) and (worker_sample_run_ids & expected_run_ids == expected_run_ids)

    # Resource activity is valid only for run windows whose required worker pod-count
    # series actually produced samples. A Prometheus file with an empty successful
    # query is not enough evidence for the worker activity reduction claim.
    resource_run_ids = expected_run_ids & worker_sample_run_ids
    if not resource_run_ids and not expected_run_ids:
        resource_run_ids = evidence_file_run_ids & worker_sample_run_ids
    resource_windows = [window for window in run_windows if str(window.get("run_id") or config.run_id) in resource_run_ids]
    run_duration_sum = sum_run_window_durations(resource_windows)
    reference_duration = run_duration_sum if run_duration_sum > 0 else query_duration

    worker_pod_seconds, worker_cost_source = prom_time_integral(prom.get("workers_active", {}), fallback_duration=query_duration)
    proxy_pod_seconds, proxy_cost_source = prom_time_integral(prom.get("proxies_active", {}), fallback_duration=query_duration)
    controller_pod_seconds, controller_cost_source = prom_time_integral(prom.get("controllers_active", {}), fallback_duration=query_duration)
    if controller_pod_seconds is None:
        controller_pod_seconds = query_duration
        controller_cost_source = "prometheus_query_window_assumes_single_controller"
    if not worker_samples_complete:
        worker_cost_source = "insufficient_prometheus_worker_samples"

    # Reference assumes one always-on worker capacity unit per active streamKey in each
    # covered run window. When Prometheus worker samples are incomplete, the relative
    # reduction is intentionally not computed instead of comparing a partial numerator
    # with a broader denominator.
    always_on_worker_pod_seconds, always_on_source = always_on_worker_pod_seconds_reference(resource_windows, config.stream_keys, reference_duration)
    if not worker_samples_complete:
        economy_relative = None
        economy_source = "insufficient_prometheus_worker_samples"
    else:
        economy_relative = None if (worker_pod_seconds is None or always_on_worker_pod_seconds <= 0) else 1 - (worker_pod_seconds / always_on_worker_pod_seconds)
        economy_source = "resource_activity_time_integral_estimate" if worker_pod_seconds is not None else "not_supported_without_prometheus"
    cost_rows = [
        {"metric": "worker_pod_seconds", "value": worker_pod_seconds, "source": worker_cost_source},
        {"metric": "proxy_pod_seconds", "value": proxy_pod_seconds, "source": proxy_cost_source},
        {"metric": "controller_pod_seconds", "value": controller_pod_seconds, "source": controller_cost_source},
        {"metric": "always_on_worker_pod_seconds_reference", "value": always_on_worker_pod_seconds, "source": always_on_source},
        {"metric": "relative_worker_activity_reduction_vs_always_on", "value": economy_relative, "source": economy_source},
    ]
    write_csv(dirs["metrics"] / "resource_activity.csv", cost_rows, ["metric", "value", "source"])
    # Prompt compatibility: keep the historical artifact name available by
    # default, but make the first row explicit that this is not financial cost.
    legacy_cost_rows = cost_rows + [{"metric": "deprecated_alias_notice", "value": "", "source": "cost_estimation.csv is a legacy compatibility alias; use resource_activity.csv for resource pod-second activity, not financial cost"}]
    write_csv(dirs["metrics"] / "cost_estimation.csv", legacy_cost_rows, ["metric", "value", "source"])

    lifecycle_values = {name: prom_values(prom.get(name, {})) for name in ("stream_lifecycle_phase_seconds_p50", "stream_lifecycle_phase_seconds_p95", "stream_lifecycle_phase_seconds_p99")}
    activation_stats = {k: stats(v) for k, v in lifecycle_values.items()}
    activation_stats["total_activation_seconds_per_stream"] = stats([r["total_activation_seconds"] for r in activation_rows if r.get("total_activation_seconds") is not None])
    activation_stats["event_detection_seconds_per_stream"] = stats([r["event_detection_seconds"] for r in activation_rows if r.get("event_detection_seconds") is not None])
    activation_stats["worker_ready_seconds_per_stream"] = stats([r["worker_ready_seconds"] for r in activation_rows if r.get("worker_ready_seconds") is not None])
    return {
        "activation": activation_stats,
        "resources": resource_rows,
        "correctness": correctness_rows,
        "duplicate_streamkey": duplicate_streamkey_rows,
        "cost": cost_rows,
        "prometheus_coverage": prom_coverage,
        "prometheus_metric_coverage": prom_metric_rows,
        "missing": missing_metrics(config, prom, activation_rows, release_rows),
    }


def missing_metrics(config: RunnerConfig, prom: dict[str, Any], activation_rows: list[dict[str, Any]] | None = None, release_rows: list[dict[str, Any]] | None = None) -> list[str]:
    missing = []
    for name, result in prom.items():
        if str(name).startswith("_") or not isinstance(result, dict):
            continue
        response = result.get("response") or {}
        data = response.get("data")
        explicit_result = isinstance(data, dict) and "result" in data
        if (not result.get("available") or response.get("status") != "success" or (explicit_result and prometheus_metric_sample_count(result) == 0)):
            missing.append(name)
    activation_rows = activation_rows or []
    release_rows = release_rows or []
    required_activation = ["t_controller_received_event", "t_worker_create_requested", "t_worker_ready", "t_ffmpeg_started", "t_ffmpeg_first_progress"]
    for field in required_activation:
        if not any(row.get(field) is not None for row in activation_rows):
            missing.append(f"per_stream_{field}")
    if not any(row.get("total_release_seconds") is not None for row in release_rows):
        missing.append("per_stream_total_release_seconds")
    if config.require_destination_received and not any(row.get("t_destination_received") is not None for row in activation_rows):
        missing.append("t_destination_received")
    return sorted(set(missing))




def csv_float_column(path: Path, column: str) -> list[float]:
    if not path.exists():
        return []
    values: list[float] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = row.get(column)
            try:
                if raw not in (None, "", "None"):
                    values.append(float(raw))
            except ValueError:
                continue
    return values


def csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_chart_limitation(path: Path, reason: str) -> str:
    txt = path.with_suffix(".txt")
    txt.write_text(reason + "\n", encoding="utf-8")
    return str(txt)


def generate_charts(dirs: dict[str, Path]) -> dict[str, str]:
    paths: dict[str, str] = {}
    metadata = json.loads((dirs["root"] / "metadata.json").read_text(encoding="utf-8")) if (dirs["root"] / "metadata.json").exists() else {}
    scenario = metadata.get("scenario")
    try:
        import matplotlib.pyplot as plt  # type: ignore
    except Exception:
        for name in ("activation_boxplot", "activation_p95_by_concurrency", "activation_p95_observed_dataset", "resource_usage_cpu", "resource_usage_memory", "workers_over_time", "recovery_time"):
            paths[name] = write_chart_limitation(dirs["charts"] / f"{name}.png", "Matplotlib unavailable; chart not generated.")
        return paths

    activation_values = csv_float_column(dirs["metrics"] / "activation_metrics.csv", "total_activation_seconds")
    path = dirs["charts"] / "activation_boxplot.png"
    if activation_values:
        plt.figure()
        plt.boxplot(activation_values)
        plt.ylabel("seconds")
        plt.title("Total activation time")
        plt.savefig(path, bbox_inches="tight")
        plt.close()
        paths["activation_boxplot"] = str(path)
    else:
        paths["activation_boxplot"] = write_chart_limitation(path, "No total_activation_seconds samples available.")

    path = dirs["charts"] / "activation_p95_by_concurrency.png"
    activation_rows = csv_rows(dirs["metrics"] / "activation_metrics.csv")
    grouped_by_concurrency: dict[str, list[float]] = {}
    for row in activation_rows:
        # The runner records pilot capacity levels as repetition ids. For ordinary runs,
        # concurrency is not known per row, so the by-concurrency chart is intentionally not generated.
        conc = row.get("concurrency")
        if not conc:
            continue
        try:
            value = float(row.get("total_activation_seconds") or "nan")
        except ValueError:
            continue
        if math.isfinite(value):
            grouped_by_concurrency.setdefault(conc, []).append(value)
    grouped_by_concurrency = {label: values for label, values in grouped_by_concurrency.items() if values}
    if scenario in {"concurrency", "pilot-capacity"} and grouped_by_concurrency:
        labels = sorted(grouped_by_concurrency, key=lambda value: int(value) if str(value).isdigit() else str(value))
        values = [percentile(sorted(grouped_by_concurrency[label]), 95) for label in labels]
        plt.figure()
        plt.bar(labels, values)
        plt.ylabel("seconds")
        plt.xlabel("concurrency")
        plt.title("Activation P95 by observed concurrency")
        plt.savefig(path, bbox_inches="tight")
        plt.close()
        paths["activation_p95_by_concurrency"] = str(path)
    else:
        paths["activation_p95_by_concurrency"] = write_chart_limitation(path, "Per-concurrency chart is generated only for concurrency or pilot-capacity scenarios with finite observed activation samples.")
    observed_path = dirs["charts"] / "activation_p95_observed_dataset.png"
    if activation_values:
        plt.figure()
        plt.bar(["observed dataset"], [percentile(sorted(activation_values), 95) or 0])
        plt.ylabel("seconds")
        plt.title("Activation P95 for observed dataset")
        plt.savefig(observed_path, bbox_inches="tight")
        plt.close()
        paths["activation_p95_observed_dataset"] = str(observed_path)
    else:
        paths["activation_p95_observed_dataset"] = write_chart_limitation(observed_path, "No activation samples available for observed-dataset P95 chart.")

    resource = csv_rows(dirs["metrics"] / "resource_usage.csv")
    for chart_name, metric in [("resource_usage_cpu", "pod_cpu_rate"), ("resource_usage_memory", "pod_memory_working_set")]:
        path = dirs["charts"] / f"{chart_name}.png"
        rows = [r for r in resource if r.get("metric") == metric and r.get("samples") not in (None, "", "0")]
        if rows:
            plt.figure()
            plt.bar([r.get("component", metric) for r in rows], [float(r.get("mean") or 0) for r in rows])
            plt.ylabel("mean")
            plt.title(chart_name)
            plt.savefig(path, bbox_inches="tight")
            plt.close()
            paths[chart_name] = str(path)
        else:
            paths[chart_name] = write_chart_limitation(path, f"No Prometheus samples available for {metric}.")

    path = dirs["charts"] / "workers_over_time.png"
    prom = load_prometheus_evidence(dirs)
    worker_values = prom_values(prom.get("workers_active", {}))
    if worker_values:
        plt.figure()
        plt.plot(list(range(1, len(worker_values) + 1)), worker_values, marker="o")
        plt.ylabel("workers")
        plt.title("Workers active over time")
        plt.savefig(path, bbox_inches="tight")
        plt.close()
        paths["workers_over_time"] = str(path)
    else:
        paths["workers_over_time"] = write_chart_limitation(path, "No Prometheus workers_active samples available.")

    path = dirs["charts"] / "recovery_time.png"
    recovery_values = csv_float_column(dirs["metrics"] / "resilience_metrics.csv", "recovery_seconds")
    if recovery_values:
        plt.figure()
        plt.plot(list(range(1, len(recovery_values) + 1)), recovery_values, marker="o")
        plt.ylabel("seconds")
        plt.title("Recovery time")
        plt.savefig(path, bbox_inches="tight")
        plt.close()
        paths["recovery_time"] = str(path)
    else:
        paths["recovery_time"] = write_chart_limitation(path, "No recovery_seconds samples available.")
    return paths

def md_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_Sem dados disponíveis._\n"
    out = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for row in rows:
        out.append("| " + " | ".join(format_cell(row.get(col)) for col in columns) + " |")
    return "\n".join(out) + "\n"


def format_cell(value: Any) -> str:
    if value is None:
        return "não observável"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value).replace("|", "\\|")



def build_stream_result_rows(config: RunnerConfig, dirs: dict[str, Path]) -> list[dict[str, Any]]:
    activation = csv_rows(dirs["metrics"] / "activation_metrics.csv")
    release = csv_rows(dirs["metrics"] / "release_metrics.csv")
    correctness = {(row.get("run_id") or config.run_id, str(row.get("repetition")), row.get("stream_key")): row for row in csv_rows(dirs["metrics"] / "correctness_metrics.csv")}
    publishers = [row for row in read_jsonl(dirs["raw"] / "publishers.jsonl") if row.get("event") == "publisher_finished"]
    pods = extract_pod_rows(dirs, config.stream_keys)
    controller_events = read_jsonl(dirs["raw"] / "controller_events.jsonl")
    windows = load_run_windows(config, dirs)
    pod_history: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in pods:
        stream = row.get("stream_key")
        window = window_for_timestamp(windows, row.get("snapshot_at"), row.get("run_id"))
        if stream and window is not None and row.get("component") == "worker":
            run_id = str(window.get("run_id") or config.run_id)
            rep = str(window.get("repetition") or 1)
            pod_history.setdefault((run_id, rep, stream), []).append(row)
    # Enrich worker history with controller lifecycle events so short-lived workers
    # deleted between Kubernetes snapshots still appear in the per-stream report.
    for event in controller_events:
        stream = event.get("stream") or event.get("stream_key")
        worker = event.get("worker_pod")
        ts = event.get("timestamp_epoch")
        if not stream or not worker or not isinstance(ts, (int, float)):
            continue
        window = window_for_timestamp(windows, ts, scoped_identity_value(event.get("run_id")))
        if window is None:
            continue
        if not event_matches_window_identity(event, window) or not event_in_window(event, window, len(windows)):
            continue
        run_id = str(window.get("run_id") or config.run_id)
        rep = str(window.get("repetition") or 1)
        pod_history.setdefault((run_id, rep, stream), []).append({
            "snapshot_at": ts,
            "pod": worker,
            "component": "worker",
            "stream_key": stream,
            "proxy_pod": event.get("proxy_pod"),
            "phase": "ControllerEvent",
            "event_type": event.get("event_type"),
        })
    release_by_key = {(row.get("run_id") or config.run_id, str(row.get("repetition")), row.get("stream_key")): row for row in release}
    publisher_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in publishers:
        key = (str(row.get("run_id") or config.run_id), str(row.get("repetition")), row.get("stream_key"))
        # Prefer the first publisher for primary stream result rows.
        if key not in publisher_by_key or int(row.get("publisher_index") or 0) < int(publisher_by_key[key].get("publisher_index") or 9999):
            publisher_by_key[key] = row
    rows: list[dict[str, Any]] = []
    for row in activation:
        stream_key = row.get("stream_key")
        run_id = row.get("run_id") or config.run_id
        rep = str(row.get("repetition"))
        key = (run_id, rep, stream_key)
        rel = release_by_key.get(key, {})
        pub = publisher_by_key.get(key, {})
        corr = correctness.get(key, {})
        history = sorted(pod_history.get(key, []), key=lambda item: float(item.get("snapshot_at") or 0))
        worker_names = [str(item.get("pod")) for item in history if item.get("pod")]
        distinct_workers = []
        for name in worker_names:
            if name not in distinct_workers:
                distinct_workers.append(name)
        initial_pod = history[0] if history else {}
        final_pod = history[-1] if history else {}
        observed_worker = distinct_workers[0] if distinct_workers else None
        observed_proxy = final_pod.get("proxy_pod") or initial_pod.get("proxy_pod") or corr.get("primary_proxy_pod")
        rows.append({
            "run_id": run_id,
            "repetition": rep,
            "streamKey": stream_key,
            "initial_worker": initial_pod.get("pod") or observed_worker or "não observado",
            "final_worker": final_pod.get("pod") or observed_worker or "não observado",
            "worker_replacements_count": max(0, len(distinct_workers) - 1),
            "proxy_owner": observed_proxy or "não observado",
            "publisher_status": pub.get("publisher_status") or publisher_status(config, pub) if pub else "não observado",
            "activation_seconds": row.get("total_activation_seconds") or "não observável",
            "release_seconds": rel.get("total_release_seconds") or "não observável",
            "worker_observed": corr.get("worker_observed_for_stream"),
            "duplicate_worker": corr.get("duplicate_worker_detected"),
            "duplicate_streamkey_rejected": corr.get("duplicate_streamkey_rejected"),
            "duplicate_streamkey_unexpectedly_accepted": corr.get("duplicate_streamkey_unexpectedly_accepted"),
            "handover_accepted": corr.get("handover_accepted"),
            "handover_denied": corr.get("handover_denied"),
            "secondary_proxy_observed": corr.get("secondary_proxy_observed"),
            "scenario_inconclusive": corr.get("scenario_inconclusive"),
            "observations": corr.get("scenario_inconclusive_reason") or row.get("status"),
        })
    if not rows:
        for key in config.stream_keys:
            rows.append({"run_id": config.run_id, "repetition": "-", "streamKey": key, "initial_worker": "não observado", "final_worker": "não observado", "worker_replacements_count": "0", "proxy_owner": "não observado", "publisher_status": "não observado", "activation_seconds": "não observável", "release_seconds": "não observável", "worker_observed": "não observado", "duplicate_worker": "não observado", "handover_accepted": "0", "handover_denied": "0", "observations": "sem linhas de ativação"})
    return rows




def required_prometheus_metrics_for_analysis(config: RunnerConfig) -> set[str]:
    """Return the Prometheus metrics that must have samples for this scenario.

    The runner still collects every query in DEFAULT_PROMQL, but simplified-mode
    runs must not fail just because lifecycle histograms, handover, recovery,
    or orphan cleanup did not occur. Proxy/resource verification is CPU/memory only.
    """
    required = set(CORE_PROMETHEUS_METRICS_FOR_ANALYSIS) | set(SCENARIO_PROMETHEUS_METRICS_FOR_ANALYSIS.get(config.scenario, set()))
    return required


def metric_expected_for_analysis(config: RunnerConfig, metric: str) -> bool:
    return metric in required_prometheus_metrics_for_analysis(config)


def metrics_prometheus_analysis_ready(config: RunnerConfig, metrics: dict[str, Any]) -> bool:
    prom_coverage = metrics.get("prometheus_coverage") or {}
    expected_run_ids = set(prom_coverage.get("expected_run_ids") or [])
    if not bool(prom_coverage.get("complete")):
        return False
    return prometheus_required_metrics_ready(
        metrics.get("prometheus_metric_coverage") or [],
        required_prometheus_metrics_for_analysis(config),
        expected_run_ids,
    )


def automation_verdict(config: RunnerConfig, execution: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    duplicate_rows = metrics.get("duplicate_streamkey") or []
    hypothesis_inconclusive = config.scenario in {"handover", "duplicate-streamkey"} and any(bool(row.get("scenario_inconclusive")) for row in duplicate_rows)
    duplicate_process_invalid = config.scenario == "duplicate-streamkey" and any(bool(row.get("duplicate_publisher_nonzero_without_controller_rejection")) for row in duplicate_rows)
    status = str(execution.get("status") or "")
    restore_failed = config.patch_proxy_context and execution.get("restore_ok") is False
    context_unscoped = config.patch_proxy_context and execution.get("context_scope_ok") is False
    reasons: list[str] = []
    if execution.get("error"):
        reasons.append("top_level_execution_error")
    if status == "failed":
        reasons.append("experiment_status_failed")
    if status == "failed_restore" and not config.allow_restore_failure:
        reasons.append("context_restore_failed")
    if status.startswith("partial") and not config.allow_partial and not (status == "partial_context_patch" and config.allow_unscoped_context):
        reasons.append(f"experiment_status_{status}")
    if restore_failed and not config.allow_restore_failure:
        reasons.append("context_restore_failed")
    if context_unscoped and not config.allow_unscoped_context:
        reasons.append("context_scope_not_effective")
    if hypothesis_inconclusive and not config.allow_inconclusive:
        reasons.append("scenario_hypothesis_inconclusive")
    if duplicate_process_invalid and not config.allow_inconclusive:
        reasons.append("duplicate_publisher_nonzero_without_controller_rejection")
    if config.require_prometheus_analysis and config.prometheus_url and not metrics_prometheus_analysis_ready(config, metrics):
        reasons.append("prometheus_analysis_not_ready")
    # Preserve order while removing duplicates.
    deduped_reasons = list(dict.fromkeys(reasons))
    exit_code = 1 if deduped_reasons else 0
    return {
        "automation_status": "failed" if exit_code else "passed",
        "automation_exit_code": exit_code,
        "automation_failure_reasons": deduped_reasons,
    }


def generate_report(config: RunnerConfig, dirs: dict[str, Path], execution: dict[str, Any], metrics: dict[str, Any], charts: dict[str, str], verdict: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = json.loads((dirs["root"] / "metadata.json").read_text(encoding="utf-8"))
    publisher_rows = [r for r in read_jsonl(dirs["raw"] / "publishers.jsonl") if r.get("event") == "publisher_finished"]
    success_count = len([r for r in publisher_rows if r.get("publisher_status") in {"success", "expected_stopped"}])
    failure_count = len([r for r in publisher_rows if r.get("publisher_status") == "unexpected_failed"])
    publisher_nonzero_process_count = len([r for r in publisher_rows if (r.get("publisher_process_status") or publisher_process_status(r)) == "nonzero_exit"])
    unavailable = metrics.get("missing", [])
    activation_csv_rows = csv_rows(dirs["metrics"] / "activation_metrics.csv")
    valid_activation_samples = len([row for row in activation_csv_rows if finite_csv_number(row.get("total_activation_seconds"))])
    invalid_activation_samples = max(0, len(activation_csv_rows) - valid_activation_samples)
    duplicate_rows = csv_rows(dirs["metrics"] / "duplicate_streamkey_metrics.csv")
    correctness_rows = csv_rows(dirs["metrics"] / "correctness_metrics.csv")
    worker_observed_samples = len([row for row in correctness_rows if str(row.get("worker_observed_for_stream")).lower() == "true"])
    controller_events_observed = bool(read_jsonl(dirs["raw"] / "controller_events.jsonl"))
    prometheus_files = [path.name for path in prometheus_run_files(dirs)]
    prom_coverage = metrics.get("prometheus_coverage") or prometheus_run_coverage(config, dirs)
    prom_metric_coverage = metrics.get("prometheus_metric_coverage") or prometheus_metric_coverage_rows(config, dirs)
    incomplete_prom_metrics = incomplete_prometheus_metric_names(prom_metric_coverage, required_only=True)
    optional_incomplete_prom_metrics = incomplete_prometheus_metric_names(prom_metric_coverage, required_only=False)
    expected_prom_run_ids = set(prom_coverage.get("expected_run_ids") or [])
    prom_results_by_run = load_prometheus_results_by_run(dirs)
    if expected_prom_run_ids:
        prom_for_sample_summary = merge_prometheus_results([
            prom_results_by_run[run_id]
            for run_id in sorted(expected_prom_run_ids)
            if run_id in prom_results_by_run
        ])
    else:
        prom_for_sample_summary = load_prometheus_evidence(dirs)
    prometheus_samples_observed = any(
        prom_values(value)
        for name, value in prom_for_sample_summary.items()
        if not str(name).startswith("_") and isinstance(value, dict)
    )
    prometheus_evidence_files_complete = bool(prom_coverage.get("complete"))
    prometheus_analysis_ready = prometheus_evidence_files_complete and prometheus_required_metrics_ready(prom_metric_coverage, required_prometheus_metrics_for_analysis(config), expected_prom_run_ids)
    verdict = verdict or automation_verdict(config, execution, metrics)
    report_json = {
        "metadata": metadata,
        "summary": {
            "publishers": len(publisher_rows),
            "publisher_success_count": success_count,
            "publisher_failure_count": failure_count,
            "publisher_nonzero_process_count": publisher_nonzero_process_count,
            "valid_activation_samples": valid_activation_samples,
            "invalid_activation_samples": invalid_activation_samples,
            "duplicate_streamkey_rejected": any(str(row.get("duplicate_streamkey_rejected")).lower() == "true" for row in duplicate_rows),
            "duplicate_streamkey_unexpectedly_accepted": any(str(row.get("duplicate_streamkey_unexpectedly_accepted")).lower() == "true" for row in duplicate_rows),
            "scenario_inconclusive": any(str(row.get("scenario_inconclusive")).lower() == "true" for row in duplicate_rows),
            "duplicate_publisher_nonzero_without_controller_rejection": any(str(row.get("duplicate_publisher_nonzero_without_controller_rejection")).lower() == "true" for row in duplicate_rows),
            "secondary_proxy_observed": any(str(row.get("secondary_proxy_observed")).lower() == "true" for row in duplicate_rows),
            "second_attempt_proxy_correlated": any(str(row.get("second_attempt_proxy_correlated")).lower() == "true" for row in duplicate_rows),
            "between_proxy_claim_valid": any(str(row.get("between_proxy_validity_status")) == "valid_between_proxy_observation" for row in duplicate_rows),
            "restore_ok": execution.get("restore_ok"),
            "context_scope_ok": execution.get("context_scope_ok"),
            "context_patch_status": execution.get("context_patch_status"),
            "controller_scope_effective": bool(((execution.get("preflight") or {}).get("proxy_context_patch") or {}).get("controller_scope_effective")),
            "prometheus_resume_safe": prometheus_evidence_files_complete,
            "prometheus_evidence_files_complete": prometheus_evidence_files_complete,
            "prometheus_analysis_ready": prometheus_analysis_ready,
            "prometheus_evidence_files": prometheus_files,
            "prometheus_expected_run_ids": prom_coverage.get("expected_run_ids") or [],
            "prometheus_observed_run_ids": prom_coverage.get("observed_run_ids") or [],
            "prometheus_missing_run_ids": prom_coverage.get("missing_run_ids") or [],
            "prometheus_extra_run_ids": prom_coverage.get("extra_run_ids") or [],
            "prometheus_coverage_by_run": prom_coverage.get("coverage_by_run") or [],
            "prometheus_incomplete_metrics": incomplete_prom_metrics,
            "prometheus_optional_incomplete_metrics": [m for m in optional_incomplete_prom_metrics if m not in set(incomplete_prom_metrics)],
            "prometheus_samples_observed": prometheus_samples_observed,
            "resource_baseline_window_aware": True,
            "observable_activation_samples": valid_activation_samples,
            "worker_observed_samples": worker_observed_samples,
            "controller_events_observed": controller_events_observed,
            "automation_status": verdict.get("automation_status"),
            "automation_exit_code": verdict.get("automation_exit_code"),
            "automation_failure_reasons": verdict.get("automation_failure_reasons") or [],
            "missing_metrics": unavailable,
        },
        "metrics": metrics,
        "charts": charts,
    }
    write_json(dirs["root"] / "report.json", report_json)
    main_metric_rows = []
    for name, values in (metrics.get("activation") or {}).items():
        row = {"metric": name, **values}
        main_metric_rows.append(row)
    resource_rows = metrics.get("resources") or []
    cost_rows = metrics.get("cost") or []
    discussion = discussion_text(config, report_json)
    destination_received_limitation = (
        "- `t_destination_received` só pode ser sustentado se houver callback/observação no destino externo.\n"
        if config.require_destination_received
        else ""
    )
    report = f"""# Relatório Experimental LiveEdgeCast

## Resumo executivo

Experimento `{config.experiment_id}` executado no cenário `{config.scenario}` com {len(config.stream_keys)} streamKey(s), duração nominal de {config.duration_seconds}s e {config.repetitions} repetição(ões). Foram registrados {success_count} publisher(s) com encerramento bem-sucedido, {failure_count} publisher(s) com falha arquitetural observada e {publisher_nonzero_process_count} publisher(s) com saída de processo não-zero. O relatório diferencia métricas reais, inferidas e ausentes; conclusões sobre métricas ausentes não são assumidas.

Amostras de ativação válidas: {report_json["summary"]["valid_activation_samples"]}. Amostras de ativação sem métrica observável: {report_json["summary"]["invalid_activation_samples"]}.

## Ambiente experimental

- Namespace: `{config.namespace}`
- RTMP URL: `{config.rtmp_url}`
- Prometheus URL configurado: `{config.prometheus_url or 'não configurado'}`
- Controller URL configurado: `{config.controller_url or 'não configurado'}`
- Source file: `{config.source_file or f'gerado por lavfi/testsrc {config.testsrc_size}@{config.testsrc_rate}fps'}`
- Generated publisher bitrate: `{config.bitrate or '10000k'}`
- Bitrate: `{config.bitrate or 'padrão/copy'}`
- Baseline informado: `{config.baseline or 'não informado'}`
- Patch de contexto em deployments: `{'ativado' if config.patch_proxy_context else 'desativado'}`
- Restauração de contexto: `{report_json["summary"].get("restore_ok")}`
- Escopo efetivo de métricas do controller: `{report_json["summary"].get("controller_scope_effective")}`
- Status do patch de contexto: `{report_json["summary"].get("context_patch_status")}`

## Métricas principais

{md_table(main_metric_rows, ['metric','samples','mean','median','stddev','p50','p95','p99','min','max','ci95_low','ci95_high'])}

## Validação das evidências

{md_table([report_json["summary"]], ['automation_status','automation_exit_code','automation_failure_reasons','prometheus_resume_safe','prometheus_evidence_files_complete','prometheus_analysis_ready','prometheus_samples_observed','resource_baseline_window_aware','observable_activation_samples','worker_observed_samples','controller_events_observed','scenario_inconclusive','context_scope_ok','restore_ok'])}

### Cobertura Prometheus por execução

{md_table(report_json["summary"].get("prometheus_coverage_by_run") or [], ['run_id','has_prometheus_evidence','expected_by_run_windows'])}

Métricas Prometheus com cobertura incompleta por execução: {', '.join(report_json["summary"].get("prometheus_incomplete_metrics") or []) if report_json["summary"].get("prometheus_incomplete_metrics") else 'nenhuma detectada'}.

## Resultado por streamKey

{md_table(build_stream_result_rows(config, dirs), ['run_id','repetition','streamKey','initial_worker','final_worker','worker_replacements_count','proxy_owner','publisher_status','activation_seconds','release_seconds','worker_observed','duplicate_worker','duplicate_streamkey_rejected','duplicate_streamkey_unexpectedly_accepted','handover_accepted','handover_denied','secondary_proxy_observed','scenario_inconclusive','observations'])}

## Uso de recursos

{md_table(resource_rows, ['metric','component','samples','mean','median','p95','p99','min','max'])}

## Atividade relativa de recursos

{md_table(cost_rows, ['metric','value','source'])}

## Resiliência

{md_table(csv_rows(dirs["metrics"] / "resilience_metrics.csv"), ['run_id','repetition','type','stream_key','pod','recovery_seconds','recovery_event','status'])}

O tempo de recuperação só deve ser usado quando as métricas de recuperação do controller e/ou Prometheus estiverem disponíveis; linhas sem `recovery_seconds` indicam recuperação não observável nesta execução.

## Correção arquitetural

A verificação de um worker por streamKey e candidatos a órfãos foi salva em `metrics/correctness_metrics.csv`. Essa verificação combina snapshots de pods, eventos estruturados do controller e anotações Kubernetes; ela é uma evidência operacional, não substitui métricas per-stream completas do controller.

## Verificação de streamKey duplicada

{md_table(csv_rows(dirs["metrics"] / "duplicate_streamkey_metrics.csv"), ['run_id','repetition','stream_key','duplicate_streamkey_attempted','duplicate_streamkey_rejected','controller_rejection_status','between_proxy_validity_status','primary_proxy_pod','second_attempt_proxy_pod','secondary_proxy_pod','second_attempt_proxy_correlated','secondary_proxy_observed','scenario_inconclusive','scenario_inconclusive_reason','duplicate_publisher_count','duplicate_publisher_process_statuses','duplicate_publisher_nonzero_without_controller_rejection','controller_denial_events','status'])}

## Limitações

- Métricas ausentes ou não observáveis nesta execução: {', '.join(unavailable) if unavailable else 'nenhuma limitação automática detectada'}.
- Tempos per-stream de cold start dependem da exportação de timestamps pelo controller; quando não há endpoint per-stream, o relatório usa apenas histogramas Prometheus agregados.
{destination_received_limitation}- A seção de atividade relativa de recursos usa pod-seconds e séries de pods ativos; ela não representa custo financeiro real de provedor de nuvem sem CPU/memória request-seconds e modelo de preço.
- Métricas de cAdvisor/kube-state-metrics são isoladas por namespace; para evitar contaminação, execute cada experimento em namespace dedicado ou use `--patch-proxy-context` para escopo das métricas do controller. O escopo do controller só é aplicado quando o patch do deployment `controller` foi efetivamente concluído.
- Se a restauração de contexto falhar, o experimento deve ser tratado como inválido até limpeza manual do cluster.
- A validade estatística depende do número de repetições e da disponibilidade de amostras em Prometheus.

## Texto-base para Discussão dos Resultados

{discussion}
"""
    (dirs["root"] / "report.md").write_text(report, encoding="utf-8")
    return report_json


def discussion_text(config: RunnerConfig, report_json: dict[str, Any]) -> str:
    missing = report_json["summary"].get("missing_metrics") or []
    failures = report_json["summary"].get("publisher_failure_count", 0)
    success = report_json["summary"].get("publisher_success_count", 0)
    lines = []
    lines.append("Os resultados devem ser interpretados a partir da hipótese de que a arquitetura proposta reduz recursos continuamente ativos ao manter a borda RTMP disponível e provisionar workers sob demanda.")
    if success or failures:
        lines.append(f"Nesta execução, {success} publisher(s) finalizaram com sucesso e {failures} apresentaram falha observada, o que fornece evidência operacional inicial sobre estabilidade do cenário `{config.scenario}`.")
    if "stream_lifecycle_phase_seconds_p95" not in missing:
        lines.append("As métricas agregadas de fases do ciclo de vida permitem discutir o cold start por etapa, incluindo ativação do worker e progresso inicial do FFmpeg.")
    else:
        lines.append("A conclusão sobre cold start per-stream ainda é limitada, pois as métricas de ciclo de vida não estavam plenamente observáveis ou não retornaram amostras suficientes.")
    if config.prometheus_url:
        lines.append("As séries do Prometheus permitem relacionar latência e comportamento operacional com uso de CPU, memória e quantidade de pods ativos, desde que as consultas tenham retornado amostras válidas.")
    else:
        lines.append("Como o Prometheus não foi configurado, a discussão quantitativa de recursos e custo relativo não pode ser sustentada por séries temporais nesta execução.")
    if config.scenario in {"worker-failure", "proxy-failure", "handover", "duplicate-streamkey"}:
        lines.append("Nesta versão simplificada, cenários de falha, handover e streamKey duplicada são tratados como evidência qualitativa/reduzida; recuperação automática e handover não são responsabilidades ativas do controller.")
    if missing:
        lines.append("As métricas ausentes devem ser explicitadas como limitação metodológica; conclusões sobre elas não devem ser afirmadas sem nova instrumentação ou nova execução experimental.")
    return "\n\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_args(argv)
    report_root = prepare_report_root(config)
    dirs = ensure_layout(report_root)
    metadata = {**asdict(config), "output_dir": str(config.output_dir), "report_root": str(config.report_root), "started_at": now_epoch(), "started_at_iso": now_iso()}
    simplified_warning = simplified_lifecycle_warning(config)
    if simplified_warning:
        print(f"WARNING: {simplified_warning['message']}", file=sys.stderr)
        metadata["simplified_lifecycle_warning"] = simplified_warning
    write_json(dirs["root"] / "metadata.json", metadata)
    if config.dry_run:
        execution = {"dry_run": True, "config": metadata}
        if simplified_warning:
            execution["simplified_lifecycle_warning"] = simplified_warning
        write_json(dirs["root"] / "report.json", execution)
        dry_report = "# Dry run\n\nConfiguração validada. Nenhum experimento foi executado.\n"
        if simplified_warning:
            dry_report += f"\n> Aviso: {simplified_warning['message']}\n"
        (dirs["root"] / "report.md").write_text(dry_report, encoding="utf-8")
        return 0
    exit_code = 0
    try:
        execution = execute_experiment(config, dirs, simplified_warning=simplified_warning)
    except Exception as exc:
        execution = {"error": {"type": type(exc).__name__, "message": str(exc)}, "started_at": metadata["started_at"], "ended_at": now_epoch()}
        exit_code = 1
    metadata["ended_at"] = execution.get("ended_at", now_epoch())
    metadata["ended_at_iso"] = now_iso()
    write_json(dirs["root"] / "metadata.json", metadata)
    write_json(dirs["root"] / "execution.json", execution)
    metrics = build_metrics(config, dirs)
    charts = generate_charts(dirs)
    verdict = automation_verdict(config, execution, metrics)
    try:
        generate_report(config, dirs, execution, metrics, charts, verdict=verdict)
    except TypeError as exc:
        # Backward compatibility for tests or external callers that monkeypatch
        # generate_report with the pre-verdict 5-argument signature.
        if "verdict" not in str(exc) and "positional" not in str(exc) and "keyword" not in str(exc):
            raise
        generate_report(config, dirs, execution, metrics, charts)
    exit_code = max(exit_code, int(verdict.get("automation_exit_code") or 0))
    print(f"Report generated at: {dirs['root'] / 'report.md'}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
