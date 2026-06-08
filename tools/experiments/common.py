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
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote
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
    parser.add_argument(
        "--kill-after-seconds",
        "--kill_after_seconds",
        type=non_negative_int,
        default=10,
    )
    parser.add_argument(
        "--duplicate-attempt-delay-seconds",
        "--duplicate_attempt_delay_seconds",
        type=non_negative_int,
        default=5,
    )
    parser.add_argument(
        "--reconnect-delay-seconds",
        "--reconnect_delay_seconds",
        type=non_negative_int,
        default=3,
    )
    parser.add_argument(
        "--reconnect-attempts", "--reconnect_attempts", type=positive_int, default=1
    )
    parser.add_argument(
        "--saturation-error-rate", "--saturation_error_rate", type=rate, default=0.20
    )
    parser.add_argument("--step-size", "--step_size", type=positive_int, default=1)
    return parser


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
        controller_url=args.controller_url.rstrip("/") if args.controller_url else None,
        ffmpeg_path=args.ffmpeg_path,
        kubectl_path=args.kubectl_path,
        startup_interval_seconds=args.startup_interval_seconds,
        stop_grace_seconds=args.stop_grace_seconds,
        kill_after_seconds=args.kill_after_seconds,
        duplicate_attempt_delay_seconds=args.duplicate_attempt_delay_seconds,
        reconnect_delay_seconds=args.reconnect_delay_seconds,
        reconnect_attempts=args.reconnect_attempts,
        saturation_error_rate=args.saturation_error_rate,
        step_size=args.step_size,
    )


def prepare_run(config: ExperimentConfig) -> logging.Logger:
    artifact_dir = config.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(
        f"liveedgecast.{config.experiment_id}.{config.scenario}.{config.run_id}"
    )
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

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


def serialize_config(config: ExperimentConfig) -> dict[str, Any]:
    data = asdict(config)
    data["output_dir"] = str(config.output_dir)
    data["artifact_dir"] = str(config.artifact_dir)
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

    def poll(self) -> int | None:
        return self.process.poll()

    def stop(self, grace_seconds: float) -> int | None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                return self.process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                self.process.kill()
                return self.process.wait(timeout=10)
        return self.process.returncode

    def result(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "started_at": self.started_at,
            "ended_at": time.time() if self.process.poll() is not None else None,
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
    logger.info("starting process name=%s command=%s", name, " ".join(command))
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    process = subprocess.Popen(
        list(command), stdout=stdout, stderr=stderr, start_new_session=True
    )
    stdout.close()
    stderr.close()
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


def wait_for_processes(
    processes: Iterable[ManagedProcess], logger: logging.Logger
) -> list[dict[str, Any]]:
    results = []
    for proc in processes:
        returncode = proc.process.wait()
        logger.info("process completed name=%s returncode=%s", proc.name, returncode)
        results.append(proc.result())
    return results


def stop_processes(
    processes: Iterable[ManagedProcess],
    config: ExperimentConfig,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    results = []
    for proc in processes:
        returncode = proc.stop(config.stop_grace_seconds)
        logger.info("process stopped name=%s returncode=%s", proc.name, returncode)
        results.append(proc.result())
    return results


def command_exists(binary: str) -> bool:
    return shutil.which(binary) is not None or Path(binary).exists()


def validate_runtime_tools(
    config: ExperimentConfig, need_kubectl: bool, logger: logging.Logger
) -> None:
    missing = []
    if not command_exists(config.ffmpeg_path):
        missing.append(config.ffmpeg_path)
    if need_kubectl and not command_exists(config.kubectl_path):
        missing.append(config.kubectl_path)
    if missing:
        raise RuntimeError(f"missing required executable(s): {', '.join(missing)}")
    logger.info("runtime tools validated need_kubectl=%s", need_kubectl)


def run_command(
    command: Sequence[str], logger: logging.Logger, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    logger.info("running command=%s", " ".join(command))
    completed = subprocess.run(
        command, text=True, capture_output=True, timeout=timeout, check=False
    )
    logger.info(
        "command returncode=%s stdout=%s stderr=%s",
        completed.returncode,
        completed.stdout.strip(),
        completed.stderr.strip(),
    )
    return completed


def kubectl(
    config: ExperimentConfig,
    args: Sequence[str],
    logger: logging.Logger,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return run_command([config.kubectl_path, *args], logger, timeout=timeout)


def select_pod(
    config: ExperimentConfig,
    selector: str,
    logger: logging.Logger,
    annotation_stream: str | None = None,
) -> str | None:
    args = ["get", "pods", "-n", config.namespace, "-l", selector, "-o", "json"]
    completed = kubectl(config, args, logger)
    if completed.returncode != 0:
        return None
    data = json.loads(completed.stdout or "{}")
    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        annotations = metadata.get("annotations", {}) or {}
        status = item.get("status", {})
        phase = status.get("phase")
        if (
            annotation_stream
            and annotations.get("liveedgecast.io/stream") != annotation_stream
        ):
            continue
        if phase in {"Running", "Pending"}:
            return metadata.get("name")
    return None


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
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def collect_kubernetes_artifacts(
    config: ExperimentConfig, logger: logging.Logger
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
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
        artifacts[name] = str(path)
    return artifacts


def collect_controller_artifacts(
    config: ExperimentConfig, logger: logging.Logger
) -> dict[str, str]:
    if not config.controller_url:
        return {}

    artifacts: dict[str, str] = {}
    for name, endpoint in {
        "controller_health.json": "/health",
        "controller_metrics.txt": "/metrics",
    }.items():
        path = config.artifact_dir / name
        url = f"{config.controller_url}{endpoint}"
        try:
            logger.info("fetching controller artifact url=%s", url)
            request = Request(url, headers={"User-Agent": "liveedgecast-experiment/1"})
            with urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8", errors="replace")
            path.write_text(body, encoding="utf-8")
        except Exception as exc:
            # Best-effort artifact collection should not fail a run.
            logger.warning(
                "failed fetching controller artifact url=%s error=%s", url, exc
            )
            path.write_text(f"failed fetching {url}: {exc}\n", encoding="utf-8")
        artifacts[name] = str(path)
    return artifacts


def sleep_with_log(seconds: int | float, logger: logging.Logger, reason: str) -> None:
    logger.info("sleeping seconds=%s reason=%s", seconds, reason)
    time.sleep(seconds)
