#!/usr/bin/env python3
"""Shared helpers for LiveEdgeCast experiment scripts.

All experiment entrypoints intentionally use the same required CLI contract:
experiment_id, scenario, run_id, concurrency, duration_seconds, and bitrate.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class ExperimentConfig:
    experiment_id: str
    scenario: str
    run_id: str
    concurrency: int
    duration_seconds: int
    bitrate: str
    output_dir: Path
    rtmp_url: str
    namespace: str
    proxy_selector: str
    worker_selector: str
    target_proxy_pod: str | None
    target_worker_pod: str | None
    controller_url: str | None
    ffmpeg_path: str
    kubectl_path: str
    startup_interval_seconds: float
    stop_grace_seconds: float
    kill_after_seconds: int
    duplicate_attempt_delay_seconds: int
    reconnect_delay_seconds: int
    reconnect_attempts: int
    saturation_error_rate: float
    step_size: int

    @property
    def artifact_dir(self) -> Path:
        return self.output_dir / self.experiment_id / self.scenario / self.run_id


def validate_safe_id(value: str, field: str) -> str:
    if not value or not SAFE_ID_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"{field} must contain only letters, numbers, '_', '.', or '-'"
        )
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
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def rate(value: str) -> float:
    parsed = non_negative_float(value)
    if parsed > 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def bitrate_value(value: str) -> str:
    if not re.match(r"^[1-9][0-9]*(k|K|m|M)?$", value):
        raise argparse.ArgumentTypeError("bitrate must look like 800k, 2M, or 1200000")
    return value


def build_parser(
    description: str, scenario_choices: Sequence[str] | None = None
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--experiment-id",
        "--experiment_id",
        dest="experiment_id",
        required=True,
        type=lambda v: validate_safe_id(v, "experiment_id"),
    )
    if scenario_choices:
        parser.add_argument("--scenario", required=True, choices=scenario_choices)
    else:
        parser.add_argument(
            "--scenario", required=True, type=lambda v: validate_safe_id(v, "scenario")
        )
    parser.add_argument(
        "--run-id",
        "--run_id",
        dest="run_id",
        required=True,
        type=lambda v: validate_safe_id(v, "run_id"),
    )
    parser.add_argument("--concurrency", required=True, type=positive_int)
    parser.add_argument(
        "--duration-seconds",
        "--duration_seconds",
        dest="duration_seconds",
        required=True,
        type=positive_int,
    )
    parser.add_argument("--bitrate", required=True, type=bitrate_value)

    parser.add_argument(
        "--output-dir", "--output_dir", default="tools/experiments/artifacts", type=Path
    )
    parser.add_argument(
        "--rtmp-url",
        "--rtmp_url",
        default=os.getenv("LIVEEDGECAST_RTMP_URL", "rtmp://127.0.0.1:1935/live"),
    )
    parser.add_argument(
        "--controller-url",
        "--controller_url",
        default=os.getenv("LIVEEDGECAST_CONTROLLER_URL"),
    )
    parser.add_argument("--namespace", default=os.getenv("NAMESPACE", "media"))
    parser.add_argument(
        "--proxy-selector",
        "--proxy_selector",
        default=os.getenv("PROXY_SELECTOR", "app=proxy"),
    )
    parser.add_argument(
        "--worker-selector",
        "--worker_selector",
        default=os.getenv("WORKER_SELECTOR", "app=worker"),
    )
    parser.add_argument(
        "--ffmpeg-path", "--ffmpeg_path", default=os.getenv("FFMPEG", "ffmpeg")
    )
    parser.add_argument(
        "--kubectl-path", "--kubectl_path", default=os.getenv("KUBECTL", "kubectl")
    )
    parser.add_argument(
        "--startup-interval-seconds",
        "--startup_interval_seconds",
        type=non_negative_float,
        default=1.0,
    )
    parser.add_argument(
        "--stop-grace-seconds",
        "--stop_grace_seconds",
        type=non_negative_float,
        default=5.0,
    )
    return parser


def add_kill_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--kill-after-seconds",
        "--kill_after_seconds",
        type=non_negative_int,
        default=10,
        help="Seconds to wait before deleting the target pod; must be less than duration_seconds.",
    )
    parser.add_argument(
        "--target-proxy-pod",
        "--target_proxy_pod",
        default=os.getenv("TARGET_PROXY_POD"),
        type=lambda v: validate_safe_id(v, "target_proxy_pod"),
        help=(
            "Exact proxy pod to delete for kill_proxy. When omitted, the proxy "
            "selector must match exactly one Running/Pending pod."
        ),
    )
    parser.add_argument(
        "--target-worker-pod",
        "--target_worker_pod",
        default=os.getenv("TARGET_WORKER_POD"),
        type=lambda v: validate_safe_id(v, "target_worker_pod"),
        help=(
            "Exact worker pod to delete for kill_worker. When omitted and the "
            "stream annotation cannot identify a worker, the worker selector must "
            "match exactly one Running/Pending pod."
        ),
    )


def add_duplicate_reconnect_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--duplicate-attempt-delay-seconds",
        "--duplicate_attempt_delay_seconds",
        type=non_negative_int,
        default=5,
        help="Seconds to wait before starting a simultaneous publisher with the same streamKey; must be less than duration_seconds.",
    )
    parser.add_argument(
        "--reconnect-delay-seconds",
        "--reconnect_delay_seconds",
        type=non_negative_int,
        default=3,
        help="Seconds to wait between reconnect attempts.",
    )
    parser.add_argument(
        "--reconnect-attempts",
        "--reconnect_attempts",
        type=positive_int,
        default=1,
        help="Number of reconnect attempts after the primary publisher exits.",
    )


def add_saturation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--saturation-error-rate",
        "--saturation_error_rate",
        type=rate,
        default=0.20,
        help="Publisher failure-rate threshold from 0 to 1 used by the incremental pilot.",
    )
    parser.add_argument(
        "--step-size",
        "--step_size",
        type=positive_int,
        default=1,
        help="Concurrency increment between pilot levels.",
    )


def validate_kill_timing(
    parser: argparse.ArgumentParser, config: "ExperimentConfig"
) -> None:
    if config.kill_after_seconds >= config.duration_seconds:
        parser.error(
            "--kill-after-seconds must be less than --duration-seconds so the pod is killed during the run"
        )


def validate_duplicate_timing(
    parser: argparse.ArgumentParser, config: "ExperimentConfig"
) -> None:
    if config.duplicate_attempt_delay_seconds >= config.duration_seconds:
        parser.error(
            "--duplicate-attempt-delay-seconds must be less than --duration-seconds so the duplicate streamKey attempt overlaps the primary publisher"
        )


def build_incremental_levels(config: "ExperimentConfig") -> list[int]:
    """Return pilot concurrency levels, falling back to the target once for oversized steps."""
    levels = list(range(config.step_size, config.concurrency + 1, config.step_size))
    if not levels:
        return [config.concurrency]
    if levels[-1] != config.concurrency:
        levels.append(config.concurrency)
    return levels


def config_from_args(args: argparse.Namespace) -> ExperimentConfig:
    return ExperimentConfig(
        experiment_id=args.experiment_id,
        scenario=args.scenario,
        run_id=args.run_id,
        concurrency=args.concurrency,
        duration_seconds=args.duration_seconds,
        bitrate=args.bitrate,
        output_dir=args.output_dir,
        rtmp_url=args.rtmp_url.rstrip("/"),
        namespace=args.namespace,
        proxy_selector=args.proxy_selector,
        worker_selector=args.worker_selector,
        target_proxy_pod=getattr(args, "target_proxy_pod", None),
        target_worker_pod=getattr(args, "target_worker_pod", None),
        controller_url=args.controller_url.rstrip("/") if args.controller_url else None,
        ffmpeg_path=args.ffmpeg_path,
        kubectl_path=args.kubectl_path,
        startup_interval_seconds=args.startup_interval_seconds,
        stop_grace_seconds=args.stop_grace_seconds,
        kill_after_seconds=getattr(args, "kill_after_seconds", 0),
        duplicate_attempt_delay_seconds=getattr(
            args, "duplicate_attempt_delay_seconds", 0
        ),
        reconnect_delay_seconds=getattr(args, "reconnect_delay_seconds", 0),
        reconnect_attempts=getattr(args, "reconnect_attempts", 1),
        saturation_error_rate=getattr(args, "saturation_error_rate", 0.20),
        step_size=getattr(args, "step_size", 1),
    )


def prepare_run(config: ExperimentConfig) -> logging.Logger:
    artifact_dir = config.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(
        f"liveedgecast.{config.experiment_id}.{config.scenario}.{config.run_id}"
    )
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(artifact_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    write_json(artifact_dir / "config.json", serialize_config(config))
    logger.info("prepared artifact_dir=%s", artifact_dir)
    logger.info("config=%s", json.dumps(serialize_config(config), sort_keys=True))
    return logger


def redact_url(value: str | None) -> str | None:
    if not value:
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in {"rtmp", "rtmps", "http", "https"}:
        return value

    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    if parsed.username or parsed.password:
        host = f"***:***@{host}"

    redacted_path = "/..." if parsed.path else ""
    redacted_query = "..." if parsed.query else ""
    return urlunsplit((parsed.scheme, host, redacted_path, redacted_query, ""))


def sanitize_command(command: Sequence[str]) -> list[str]:
    return [
        (
            redact_url(part)
            if part.startswith(("rtmp://", "rtmps://", "http://", "https://"))
            else part
        )
        for part in command
    ]


def serialize_config(config: ExperimentConfig) -> dict[str, Any]:
    data = asdict(config)
    data["output_dir"] = str(config.output_dir)
    data["artifact_dir"] = str(config.artifact_dir)
    data["rtmp_url"] = redact_url(config.rtmp_url)
    data["controller_url"] = redact_url(config.controller_url)
    return data


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def stream_key(config: ExperimentConfig, index: int, suffix: str | None = None) -> str:
    base = f"{config.experiment_id}-{config.scenario}-{config.run_id}-{index:03d}"
    return f"{base}-{suffix}" if suffix else base


def rtmp_target(config: ExperimentConfig, key: str) -> str:
    return f"{config.rtmp_url}/{quote(key, safe='')}"


def ffmpeg_command(
    config: ExperimentConfig, key: str, duration_seconds: int | None = None
) -> list[str]:
    duration = str(duration_seconds or config.duration_seconds)
    return [
        config.ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1280x720:rate=30",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t",
        duration,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-b:v",
        config.bitrate,
        "-c:a",
        "aac",
        "-f",
        "flv",
        rtmp_target(config, key),
    ]


@dataclass
class ManagedProcess:
    name: str
    command: list[str]
    started_at: float
    process: subprocess.Popen[Any]
    stdout_path: Path
    stderr_path: Path
    ended_at: float | None = None

    def poll(self) -> int | None:
        return self.process.poll()

    def stop(self, grace_seconds: float) -> int | None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                self.process.send_signal(signal.SIGTERM)
            try:
                returncode = self.process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    self.process.kill()
                returncode = self.process.wait(timeout=10)
            self.ended_at = time.time()
            return returncode
        if self.ended_at is None:
            self.ended_at = time.time()
        return self.process.returncode

    def result(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": sanitize_command(self.command),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "returncode": self.process.poll(),
            "stdout": str(self.stdout_path),
            "stderr": str(self.stderr_path),
        }


def start_process(
    command: Sequence[str], name: str, artifact_dir: Path, logger: logging.Logger
) -> ManagedProcess:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    stdout_path = artifact_dir / f"{safe_name}.stdout.log"
    stderr_path = artifact_dir / f"{safe_name}.stderr.log"
    logger.info(
        "starting process name=%s command=%s", name, " ".join(sanitize_command(command))
    )
    with ExitStack() as stack:
        stdout = stack.enter_context(stdout_path.open("wb"))
        stderr = stack.enter_context(stderr_path.open("wb"))
        process = subprocess.Popen(
            list(command), stdout=stdout, stderr=stderr, start_new_session=True
        )
    return ManagedProcess(
        name=name,
        command=list(command),
        started_at=time.time(),
        process=process,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def start_ffmpeg_streams(
    config: ExperimentConfig,
    keys: Sequence[str],
    logger: logging.Logger,
    duration_seconds: int | None = None,
) -> list[ManagedProcess]:
    processes: list[ManagedProcess] = []
    try:
        for index, key in enumerate(keys):
            proc = start_process(
                ffmpeg_command(config, key, duration_seconds),
                f"ffmpeg-{key}",
                config.artifact_dir,
                logger,
            )
            processes.append(proc)
            if index < len(keys) - 1 and config.startup_interval_seconds:
                time.sleep(config.startup_interval_seconds)
        return processes
    except Exception:
        if processes:
            logger.exception(
                "failed starting all FFmpeg publishers; stopping %s already-started process(es)",
                len(processes),
            )
            stop_processes(processes, config, logger)
        raise


def process_wait_timeout_seconds(
    config: ExperimentConfig,
    duration_seconds: int | None = None,
    process_count: int | None = None,
) -> float:
    """Bound publisher waits using the actual batch size when available."""
    duration = (
        duration_seconds if duration_seconds is not None else config.duration_seconds
    )
    count = process_count if process_count is not None else config.concurrency
    startup_budget = max(0.0, config.startup_interval_seconds) * max(0, count - 1)
    return duration + startup_budget + config.stop_grace_seconds + 10


def wait_for_processes(
    processes: Iterable[ManagedProcess],
    logger: logging.Logger,
    timeout_seconds: float | None = None,
    stop_grace_seconds: float = 5.0,
) -> list[dict[str, Any]]:
    results = []
    for proc in processes:
        timed_out = False
        try:
            if timeout_seconds is None:
                returncode = proc.process.wait()
            else:
                remaining = proc.started_at + timeout_seconds - time.time()
                returncode = proc.process.wait(timeout=max(0.0, remaining))
            proc.ended_at = time.time()
            logger.info(
                "process completed name=%s returncode=%s", proc.name, returncode
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_error = None
            logger.warning(
                "process timed out name=%s timeout_seconds=%s; stopping",
                proc.name,
                timeout_seconds,
            )
            try:
                returncode = proc.stop(stop_grace_seconds)
                logger.info(
                    "process stopped after timeout name=%s returncode=%s",
                    proc.name,
                    returncode,
                )
            except Exception as exc:
                stop_error = {"type": type(exc).__name__, "message": str(exc)}
                logger.exception("failed stopping timed-out process name=%s", proc.name)
        result = proc.result()
        result["timed_out"] = timed_out
        if timed_out and stop_error is not None:
            result["stop_error"] = stop_error
        results.append(result)
    return results


def stop_processes(
    processes: Iterable[ManagedProcess],
    config: ExperimentConfig,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    results = []
    for proc in processes:
        try:
            returncode = proc.stop(config.stop_grace_seconds)
            logger.info("process stopped name=%s returncode=%s", proc.name, returncode)
            result = proc.result()
        except Exception as exc:
            logger.exception("failed stopping process during cleanup name=%s", proc.name)
            result = proc.result()
            result["stop_error"] = {"type": type(exc).__name__, "message": str(exc)}
        result["stopped_by"] = "cleanup"
        results.append(result)
    return results


def command_exists(binary: str) -> bool:
    return shutil.which(binary) is not None or Path(binary).exists()


def validate_runtime_tools(
    config: ExperimentConfig,
    need_kubectl: bool,
    logger: logging.Logger,
    check_rtmp: bool = True,
) -> None:
    missing = []
    if not command_exists(config.ffmpeg_path):
        missing.append(config.ffmpeg_path)
    if need_kubectl and not command_exists(config.kubectl_path):
        missing.append(config.kubectl_path)
    if missing:
        raise RuntimeError(f"missing required executable(s): {', '.join(missing)}")
    if check_rtmp:
        preflight_rtmp_endpoint(config, logger)
    logger.info(
        "runtime tools validated need_kubectl=%s check_rtmp=%s",
        need_kubectl,
        check_rtmp,
    )


def preflight_rtmp_endpoint(config: ExperimentConfig, logger: logging.Logger) -> None:
    parsed = urlsplit(config.rtmp_url)
    if parsed.scheme not in {"rtmp", "rtmps"} or not parsed.hostname:
        logger.info(
            "skipping RTMP preflight for unsupported URL=%s",
            redact_url(config.rtmp_url),
        )
        return

    port = parsed.port or (443 if parsed.scheme == "rtmps" else 1935)
    logger.info("preflighting RTMP endpoint host=%s port=%s", parsed.hostname, port)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=3):
            return
    except OSError as exc:
        raise RuntimeError(
            f"RTMP endpoint is not reachable: {redact_url(config.rtmp_url)} ({exc})"
        ) from exc


def truncate_for_log(value: str, limit: int = 2000) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...<truncated {len(value) - limit} chars>"


def run_command(
    command: Sequence[str], logger: logging.Logger, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    logger.info("running command=%s", " ".join(sanitize_command(command)))
    try:
        completed = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout
            if isinstance(exc.stdout, str)
            else (exc.stdout or b"").decode("utf-8", errors="replace")
        )
        stderr = (
            exc.stderr
            if isinstance(exc.stderr, str)
            else (exc.stderr or b"").decode("utf-8", errors="replace")
        )
        completed = subprocess.CompletedProcess(
            command,
            124,
            stdout=stdout,
            stderr=f"{stderr}\nTimeout after {timeout}s".strip(),
        )
    except OSError as exc:
        completed = subprocess.CompletedProcess(
            command, 127, stdout="", stderr=str(exc)
        )
    logger.info(
        "command returncode=%s stdout=%s stderr=%s",
        completed.returncode,
        truncate_for_log(completed.stdout.strip()),
        truncate_for_log(completed.stderr.strip()),
    )
    return completed


def kubectl(
    config: ExperimentConfig,
    args: Sequence[str],
    logger: logging.Logger,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return run_command([config.kubectl_path, *args], logger, timeout=timeout)


def select_pod_candidates(
    config: ExperimentConfig,
    selector: str,
    logger: logging.Logger,
    annotation_stream: str | None = None,
) -> list[str]:
    args = ["get", "pods", "-n", config.namespace, "-l", selector, "-o", "json"]
    completed = kubectl(config, args, logger)
    if completed.returncode != 0:
        return []
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        logger.warning(
            "failed parsing kubectl pod JSON selector=%s error=%s", selector, exc
        )
        return []

    candidates: list[str] = []
    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        annotations = metadata.get("annotations", {}) or {}
        status = item.get("status", {})
        phase = status.get("phase")
        name = metadata.get("name")
        if (
            annotation_stream
            and annotations.get("liveedgecast.io/stream") != annotation_stream
        ):
            continue
        if name and phase in {"Running", "Pending"}:
            candidates.append(name)
    return candidates


def select_pod(
    config: ExperimentConfig,
    selector: str,
    logger: logging.Logger,
    annotation_stream: str | None = None,
    require_single: bool = False,
) -> str | None:
    candidates = select_pod_candidates(config, selector, logger, annotation_stream)
    if not candidates:
        return None
    if require_single and len(candidates) != 1:
        logger.warning(
            "selector=%s matched %s candidate pods; refusing ambiguous selection candidates=%s",
            selector,
            len(candidates),
            ",".join(candidates),
        )
        return None
    return candidates[0]


def delete_pod(
    config: ExperimentConfig, pod_name: str, logger: logging.Logger
) -> dict[str, Any]:
    completed = kubectl(
        config,
        [
            "delete",
            "pod",
            pod_name,
            "-n",
            config.namespace,
            "--grace-period=0",
            "--force",
        ],
        logger,
        timeout=60,
    )
    return {
        "pod": pod_name,
        "status": "deleted" if completed.returncode == 0 else "delete_failed",
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def collect_kubernetes_artifacts(
    config: ExperimentConfig, logger: logging.Logger
) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for name, args in {
        "pods.txt": ["get", "pods", "-n", config.namespace, "-o", "wide"],
        "events.txt": [
            "get",
            "events",
            "-n",
            config.namespace,
            "--sort-by=.lastTimestamp",
        ],
    }.items():
        completed = kubectl(config, args, logger, timeout=30)
        path = config.artifact_dir / name
        path.write_text(
            (completed.stdout or "")
            + ("\n# STDERR\n" + completed.stderr if completed.stderr else ""),
            encoding="utf-8",
        )
        artifacts[name] = {
            "path": str(path),
            "returncode": completed.returncode,
            "status": "collected" if completed.returncode == 0 else "collection_failed",
        }
    return artifacts


def collect_controller_artifacts(
    config: ExperimentConfig, logger: logging.Logger
) -> dict[str, dict[str, Any]]:
    if not config.controller_url:
        return {}

    artifacts: dict[str, dict[str, Any]] = {}
    for name, endpoint in {
        "controller_health.json": "/health",
        "controller_metrics.txt": "/metrics",
    }.items():
        path = config.artifact_dir / name
        url = f"{config.controller_url}{endpoint}"
        status = "collected"
        error = None
        try:
            logger.info("fetching controller artifact url=%s", redact_url(url))
            request = Request(url, headers={"User-Agent": "liveedgecast-experiment/1"})
            with urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8", errors="replace")
            path.write_text(body, encoding="utf-8")
        except Exception as exc:
            # Best-effort artifact collection should not fail a run.
            status = "collection_failed"
            error = {"type": type(exc).__name__, "message": str(exc)}
            logger.warning(
                "failed fetching controller artifact url=%s error=%s",
                redact_url(url),
                exc,
            )
            path.write_text(
                f"failed fetching {redact_url(url)}: {exc}\n", encoding="utf-8"
            )
        artifact = {"path": str(path), "status": status}
        if error is not None:
            artifact["error"] = error
        artifacts[name] = artifact
    return artifacts


def sleep_with_log(seconds: int | float, logger: logging.Logger, reason: str) -> None:
    logger.info("sleeping seconds=%s reason=%s", seconds, reason)
    time.sleep(seconds)
