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

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

DEFAULT_PROMQL = {
    "controller_active_streams": "controller_active_streams",
    "controller_active_allocations": "controller_active_allocations",
    "worker_pods_available": "worker_pods_available",
    "workers_active": 'count(kube_pod_info{namespace="$namespace", pod=~"worker-.*"})',
    "proxies_active": 'count(kube_pod_info{namespace="$namespace", pod=~"proxy-.*"})',
    "pod_cpu_rate": 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="$namespace", container!="", pod=~"(worker|proxy|controller).*"}[1m]))',
    "pod_memory_working_set": 'sum by (pod) (container_memory_working_set_bytes{namespace="$namespace", container!="", pod=~"(worker|proxy|controller).*"})',
    "proxy_network_receive_bps": 'sum by (pod) (rate(container_network_receive_bytes_total{namespace="$namespace", pod=~"proxy-.*"}[1m]))',
    "proxy_network_transmit_bps": 'sum by (pod) (rate(container_network_transmit_bytes_total{namespace="$namespace", pod=~"proxy-.*"}[1m]))',
    "stream_lifecycle_phase_seconds_p50": 'histogram_quantile(0.50, sum by (le, phase) (rate(stream_lifecycle_phase_seconds_bucket[5m])))',
    "stream_lifecycle_phase_seconds_p95": 'histogram_quantile(0.95, sum by (le, phase) (rate(stream_lifecycle_phase_seconds_bucket[5m])))',
    "stream_lifecycle_phase_seconds_p99": 'histogram_quantile(0.99, sum by (le, phase) (rate(stream_lifecycle_phase_seconds_bucket[5m])))',
    "handover_attempts_total": "handover_attempts_total",
    "handover_success_total": "handover_success_total",
    "handover_conflict_total": "handover_conflict_total",
    "orphan_workers_deleted_total": "orphan_workers_deleted_total",
    "worker_recovery_total": "worker_recovery_total",
    "worker_recovery_duration_seconds_p95": 'histogram_quantile(0.95, sum by (le) (rate(worker_recovery_duration_seconds_bucket[5m])))',
    "ffmpeg_running": "worker_ffmpeg_running",
    "ffmpeg_progress_age": "worker_ffmpeg_progress_age_seconds",
    "ffmpeg_out_time_seconds": "worker_ffmpeg_out_time_seconds",
    "proxy_rtmp_active_streams": "proxy_rtmp_active_streams",
    "proxy_rtmp_active_publishers": "proxy_rtmp_active_publishers",
    "proxy_rtmp_active_clients": "proxy_rtmp_active_clients",
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

    @property
    def report_root(self) -> Path:
        if self.output_dir.name == self.experiment_id:
            return self.output_dir
        return self.output_dir / self.experiment_id


def now_epoch() -> float:
    return time.time()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_id(value: str, field: str) -> str:
    if not value or not SAFE_ID_RE.match(value):
        raise argparse.ArgumentTypeError(f"{field} must use only letters, numbers, '_', '.', '-' ")
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
    parser.add_argument("--experiment-id", required=True, type=lambda v: safe_id(v, "experiment_id"))
    parser.add_argument("--run-id", default=None, type=lambda v: safe_id(v, "run_id"))
    parser.add_argument("--repetitions", type=positive_int, default=1)
    parser.add_argument("--duration-seconds", type=positive_int, default=120)
    parser.add_argument("--warmup-seconds", type=non_negative_int, default=0)
    parser.add_argument("--cooldown-seconds", type=non_negative_int, default=10)
    parser.add_argument("--rtmp-url", default=os.getenv("LIVEEDGECAST_RTMP_URL", "rtmp://127.0.0.1:1935/live"))
    parser.add_argument("--source-file", default=None)
    parser.add_argument("--bitrate", default=None)
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
    args = parser.parse_args(argv)

    keys = load_stream_keys(args.stream_keys, args.stream_keys_file)
    if not keys:
        parser.error("at least one streamKey is required")
    if args.scenario != "duplicate-streamkey" and len(keys) != len(set(keys)):
        parser.error("duplicated streamKeys are not allowed outside duplicate-streamkey scenario")
    if args.source_file and not Path(args.source_file).exists():
        parser.error(f"--source-file not found: {args.source_file}")
    if args.saturation_error_rate > 1:
        parser.error("--saturation-error-rate must be between 0 and 1")
    return RunnerConfig(
        stream_keys=keys,
        scenario=args.scenario,
        experiment_id=args.experiment_id,
        run_id=args.run_id or f"run-{int(time.time())}",
        repetitions=args.repetitions,
        duration_seconds=args.duration_seconds,
        warmup_seconds=args.warmup_seconds,
        cooldown_seconds=args.cooldown_seconds,
        rtmp_url=args.rtmp_url.rstrip("/"),
        source_file=args.source_file,
        bitrate=args.bitrate,
        namespace=args.namespace,
        prometheus_url=args.prometheus_url.rstrip("/") if args.prometheus_url else None,
        controller_url=args.controller_url.rstrip("/") if args.controller_url else None,
        output_dir=args.output_dir,
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
    )


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
        stream_key: str,
        command: list[str],
        process: subprocess.Popen,
        stdout_path: Path,
        stderr_path: Path,
        started_at: float,
        repetition: int,
        publisher_index: int,
    ):
        self.stream_key = stream_key
        self.command = command
        self.process = process
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.started_at = started_at
        self.ended_at: float | None = None
        self.repetition = repetition
        self.publisher_index = publisher_index

    def stop(self, grace_seconds: float = 5) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except Exception:
                self.process.terminate()
            try:
                self.process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except Exception:
                    self.process.kill()
                self.process.wait(timeout=10)
        self.ended_at = now_epoch()

    def result(self) -> dict[str, Any]:
        return {
            "stream_key": self.stream_key,
            "repetition": self.repetition,
            "publisher_index": self.publisher_index,
            "pid": self.process.pid,
            "returncode": self.process.poll(),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "stdout": str(self.stdout_path),
            "stderr": str(self.stderr_path),
            "command": redact_command(self.command),
        }



def redact_command(command: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    for part in command:
        if part.startswith(("rtmp://", "rtmps://")):
            scheme = part.split("://", 1)[0]
            redacted.append(f"{scheme}://...")
        else:
            redacted.append(part)
    return redacted



def ffmpeg_command(config: RunnerConfig, stream_key: str) -> list[str]:
    target = f"{config.rtmp_url}/{quote(stream_key, safe='')}"
    command = [config.ffmpeg_path, "-hide_banner", "-nostdin", "-re"]
    if config.source_file:
        command.extend(["-stream_loop", "-1", "-i", config.source_file, "-t", str(config.duration_seconds)])
        if config.bitrate:
            command.extend(["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-b:v", config.bitrate, "-c:a", "aac"])
        else:
            command.extend(["-c", "copy"])
    else:
        command.extend([
            "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", str(config.duration_seconds),
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-b:v", config.bitrate or "1200k", "-c:a", "aac",
        ])
    command.extend(["-f", "flv", target])
    return command


def start_publisher(
    config: RunnerConfig,
    dirs: dict[str, Path],
    stream_key: str,
    repetition: int,
    publisher_index: int,
    suffix: str = "",
) -> ManagedPublisher:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{stream_key}{suffix}")
    stdout_path = dirs["logs"] / f"publisher-{safe_name}.stdout.log"
    stderr_path = dirs["logs"] / f"publisher-{safe_name}.stderr.log"
    command = ffmpeg_command(config, stream_key)
    started_at = now_epoch()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True)
    append_jsonl(dirs["raw"] / "publishers.jsonl", {
        "event": "publisher_started",
        "stream_key": stream_key,
        "repetition": repetition,
        "publisher_index": publisher_index,
        "pid": process.pid,
        "timestamp": started_at,
        "command": redact_command(command),
    })
    return ManagedPublisher(stream_key, command, process, stdout_path, stderr_path, started_at, repetition, publisher_index)


def wait_or_stop_publishers(config: RunnerConfig, dirs: dict[str, Path], publishers: list[ManagedPublisher], wait: bool = True) -> list[dict[str, Any]]:
    deadline = now_epoch() + config.duration_seconds + config.cooldown_seconds + 30
    if wait:
        for publisher in publishers:
            remaining = max(0.1, deadline - now_epoch())
            try:
                publisher.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                publisher.stop()
            publisher.ended_at = publisher.ended_at or now_epoch()
    else:
        for publisher in publishers:
            publisher.stop()
    results = [publisher.result() for publisher in publishers]
    for result in results:
        append_jsonl(dirs["raw"] / "publishers.jsonl", {"event": "publisher_finished", **result})
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



def collect_logs(config: RunnerConfig, dirs: dict[str, Path]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    if not shutil.which(config.kubectl_path) and not Path(config.kubectl_path).exists():
        return {"available": False, "error": f"kubectl not found: {config.kubectl_path}"}
    selectors = {"controller": "app=controller", "proxy": "app=proxy", "worker": "app=worker"}
    for name, selector in selectors.items():
        out = run_cmd([config.kubectl_path, "logs", "-n", config.namespace, "-l", selector, "--all-containers=true", "--tail=-1"], timeout=120)
        path = dirs["logs"] / f"{name}.log"
        path.write_text((out.get("stdout") or "") + ("\n# STDERR\n" + out.get("stderr", "") if out.get("stderr") else ""), encoding="utf-8")
        event_count = extract_structured_events(path, dirs["raw"] / f"{name}_events.jsonl", component=name)
        results[name] = {"returncode": out["returncode"], "path": str(path), "structured_events": event_count}
    # Merge publisher logs for convenience.
    publisher_log = dirs["logs"] / "publishers.log"
    with publisher_log.open("w", encoding="utf-8") as merged:
        for path in sorted(dirs["logs"].glob("publisher-*.stderr.log")):
            merged.write(f"\n===== {path.name} =====\n")
            merged.write(path.read_text(encoding="utf-8", errors="replace"))
    results["publishers"] = {"path": str(publisher_log)}
    return results



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


def extract_structured_events(log_path: Path, output_path: Path, component: str) -> int:
    count = 0
    if not log_path.exists():
        return 0
    with output_path.open("w", encoding="utf-8") as out:
        for line_number, line in enumerate(log_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            event = parse_json_event_line(line)
            if not event:
                continue
            event["component"] = component
            event["source_log"] = str(log_path)
            event["source_line"] = line_number
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


def collect_controller_http(config: RunnerConfig, dirs: dict[str, Path], phase: str) -> dict[str, Any]:
    result = {
        "phase": phase,
        "timestamp": now_epoch(),
        "health": controller_get(config, "/health"),
        "metrics": controller_get(config, "/metrics"),
    }
    write_json(dirs["raw"] / f"controller_http_{phase}.json", result)
    return result


def patch_proxy_context(config: RunnerConfig, dirs: dict[str, Path]) -> dict[str, Any]:
    """Best-effort propagation of experiment context to proxy hook scripts."""
    if not shutil.which(config.kubectl_path) and not Path(config.kubectl_path).exists():
        result = {"available": False, "skipped": True, "reason": f"kubectl not found: {config.kubectl_path}"}
        write_json(dirs["raw"] / "proxy_context_patch.json", result)
        return result
    commands = []
    set_env = [
        config.kubectl_path, "set", "env", "deployment/proxy",
        f"EXPERIMENT_ID={config.experiment_id}",
        f"SCENARIO={config.scenario}",
        f"RUN_ID={config.run_id}",
        "-n", config.namespace,
    ]
    commands.append(run_cmd(set_env, timeout=60))
    if commands[-1].get("returncode") == 0:
        commands.append(run_cmd([config.kubectl_path, "rollout", "status", "deployment/proxy", "-n", config.namespace, "--timeout=120s"], timeout=150))
    result = {"available": True, "commands": commands, "ok": all(c.get("returncode") == 0 for c in commands)}
    write_json(dirs["raw"] / "proxy_context_patch.json", result)
    return result


def prometheus_query(config: RunnerConfig, query: str, start: float, end: float, step: int = 5) -> dict[str, Any]:
    if not config.prometheus_url:
        return {"available": False, "reason": "prometheus_url_not_configured", "query": query}
    params = urlencode({"query": query.replace("$namespace", config.namespace), "start": f"{start:.3f}", "end": f"{end:.3f}", "step": str(step)})
    url = f"{config.prometheus_url}/api/v1/query_range?{params}"
    try:
        with urlopen(Request(url, headers={"User-Agent": "liveedgecast-experiment/1"}), timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {"available": True, "query": query, "response": payload}
    except Exception as exc:
        return {"available": False, "query": query, "error": {"type": type(exc).__name__, "message": str(exc)}}


def collect_prometheus(config: RunnerConfig, dirs: dict[str, Path], start: float, end: float) -> dict[str, Any]:
    results = {}
    for name, query in DEFAULT_PROMQL.items():
        results[name] = prometheus_query(config, query, start, end)
    write_json(dirs["raw"] / "prometheus_range_queries.json", results)
    return results


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
    worker pods in the experiment namespace are deleted and the function waits until
    no active worker pods remain.
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
    append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_started", "repetition": repetition, "timestamp": run_started, "stream_keys": stream_keys})
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
            publishers.append(start_publisher(config, dirs, key, repetition, 2, suffix="-duplicate"))
        elif config.scenario == "handover":
            key = stream_keys[0]
            first = start_publisher(config, dirs, key, repetition, 1, suffix="-handover-a")
            publishers.append(first)
            time.sleep(min(config.reconnect_delay_seconds, config.duration_seconds))
            first.stop()
            append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "handover_primary_stopped", "stream_key": key, "timestamp": now_epoch()})
            time.sleep(config.reconnect_delay_seconds)
            publishers.append(start_publisher(config, dirs, key, repetition, 2, suffix="-handover-b"))
        else:
            for index, key in enumerate(stream_keys):
                publishers.append(start_publisher(config, dirs, key, repetition, index + 1))
                if index < len(stream_keys) - 1 and config.startup_interval_seconds:
                    time.sleep(config.startup_interval_seconds)
        if config.scenario == "worker-failure" or config.kill_worker:
            time.sleep(min(config.kill_after_seconds, max(1, config.duration_seconds - 1)))
            target_stream = stream_keys[0]
            pod = select_pod_by_selector(config, "app=worker", stream_key=target_stream) or select_pod_by_selector(config, "app=worker")
            if pod:
                deleted = delete_pod(config, pod)
                injected.append({"type": "worker-failure", "pod": pod, "timestamp": now_epoch(), "result": deleted})
            else:
                injected.append({"type": "worker-failure", "status": "pod_not_found", "timestamp": now_epoch()})
        if config.scenario == "proxy-failure" or config.kill_proxy:
            time.sleep(min(config.kill_after_seconds, max(1, config.duration_seconds - 1)))
            pod = select_pod_by_selector(config, "app=proxy")
            if pod:
                deleted = delete_pod(config, pod)
                injected.append({"type": "proxy-failure", "pod": pod, "timestamp": now_epoch(), "result": deleted})
            else:
                injected.append({"type": "proxy-failure", "status": "pod_not_found", "timestamp": now_epoch()})
        # Release scenario intentionally stops publishers and waits for cleanup observation.
        results = wait_or_stop_publishers(config, dirs, publishers, wait=(config.scenario != "release"))
        if config.cooldown_seconds:
            time.sleep(config.cooldown_seconds)
        collect_kubernetes(config, dirs, f"after-r{repetition}")
        failures = [r for r in results if r.get("returncode") not in (0, None)]
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
        append_jsonl(dirs["raw"] / "streams.jsonl", {"event": "run_finished", **run_summary})
        return run_summary
    except KeyboardInterrupt:
        for publisher in publishers:
            publisher.stop()
        raise
    except Exception as exc:
        for publisher in publishers:
            publisher.stop()
        return {"repetition": repetition, "started_at": run_started, "ended_at": now_epoch(), "stream_keys": stream_keys, "error": {"type": type(exc).__name__, "message": str(exc)}, "injected_failures": injected}




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


def execute_experiment(config: RunnerConfig, dirs: dict[str, Path]) -> dict[str, Any]:
    if config.dry_run:
        return {"dry_run": True, "would_run": config.scenario, "stream_keys": config.stream_keys}
    if not shutil.which(config.ffmpeg_path) and not Path(config.ffmpeg_path).exists():
        raise RuntimeError(f"ffmpeg not found: {config.ffmpeg_path}")
    start = now_epoch()
    preflight = {
        "proxy_context_patch": patch_proxy_context(config, dirs),
        "controller_before": collect_controller_http(config, dirs, "before"),
    }
    run_summaries: list[dict[str, Any]] = []
    if config.scenario == "pilot-capacity":
        for level in build_pilot_levels(len(config.stream_keys), config.pilot_step_size):
            keys = config.stream_keys[:level]
            summary = execute_single_run(config, dirs, level, keys)
            summary["pilot_concurrency"] = level
            run_summaries.append(summary)
            if summary.get("error_rate", 0) >= config.saturation_error_rate:
                summary["saturation_reason"] = "publisher_error_rate"
                break
    else:
        for repetition in range(1, config.repetitions + 1):
            run_summaries.append(execute_single_run(config, dirs, repetition, config.stream_keys))
    end = now_epoch()
    prometheus = collect_prometheus(config, dirs, start, end)
    logs = collect_logs(config, dirs)
    controller_after = collect_controller_http(config, dirs, "after")
    return {
        "started_at": start,
        "ended_at": end,
        "preflight": preflight,
        "runs": run_summaries,
        "prometheus": summarize_prometheus_availability(prometheus),
        "logs": logs,
        "controller_after": controller_after,
    }



def summarize_prometheus_availability(results: dict[str, Any]) -> dict[str, Any]:
    return {name: {"available": bool(value.get("available")), "status": (value.get("response") or {}).get("status"), "error": value.get("error") or value.get("reason")} for name, value in results.items()}


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
    windows: dict[int, dict[str, Any]] = {}
    for record in read_jsonl(dirs["raw"] / "streams.jsonl"):
        repetition = record.get("repetition")
        if not isinstance(repetition, int):
            continue
        current = windows.setdefault(repetition, {"repetition": repetition, "stream_keys": list(config.stream_keys)})
        if record.get("event") == "run_started":
            current["started_at"] = record.get("timestamp")
            current["stream_keys"] = record.get("stream_keys") or current["stream_keys"]
        elif record.get("event") == "run_finished":
            current["ended_at"] = record.get("ended_at") or record.get("timestamp")
            current["stream_keys"] = record.get("stream_keys") or current["stream_keys"]
    if windows:
        return [windows[key] for key in sorted(windows)]
    return [
        {"repetition": rep, "started_at": None, "ended_at": None, "stream_keys": list(config.stream_keys)}
        for rep in range(1, config.repetitions + 1)
    ]


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


def build_lifecycle_rows(config: RunnerConfig, dirs: dict[str, Path], publisher_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[int, str], dict[str, Any]]]:
    events = read_jsonl(dirs["raw"] / "controller_events.jsonl")
    run_windows = load_run_windows(config, dirs)
    grouped: dict[tuple[int, str], dict[str, Any]] = {}

    for window in run_windows:
        repetition = int(window.get("repetition") or 1)
        for key in (window.get("stream_keys") or config.stream_keys):
            grouped[(repetition, key)] = {"repetition": repetition, "stream_key": key}

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
        matching_windows = [w for w in run_windows if stream in (w.get("stream_keys") or config.stream_keys) and event_in_window(event, w, len(run_windows))]
        # Without usable run windows, only a single repetition can be safely inferred.
        if not matching_windows and len(run_windows) == 1:
            matching_windows = run_windows
        for window in matching_windows:
            repetition = int(window.get("repetition") or 1)
            entry = grouped.setdefault((repetition, stream), {"repetition": repetition, "stream_key": stream})
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
        if key in config.stream_keys:
            entry = grouped.setdefault((repetition, key), {"repetition": repetition, "stream_key": key})
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
    for repetition, key in sorted(grouped):
        entry = grouped[(repetition, key)]
        activation = {
            "repetition": repetition,
            "stream_key": key,
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
        }
        activation["event_detection_seconds"] = delta(activation.get("t_publish_start_proxy"), activation.get("t_controller_received_event"))
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
            "repetition": repetition,
            "stream_key": key,
            "release_detection_seconds": delta(entry.get("t_publish_end_client"), entry.get("t_controller_received_end")),
            "worker_delete_seconds": delta(entry.get("t_controller_received_end"), entry.get("t_worker_terminated")),
            "total_release_seconds": delta(entry.get("t_publish_end_client"), entry.get("t_worker_terminated")),
            "status": "derived_from_controller_structured_logs" if entry.get("t_controller_received_end") or entry.get("t_worker_terminated") else "not_observable",
        }
        release_rows.append(release)
    return activation_rows, release_rows, grouped

def delta(start: Any, end: Any) -> float | None:
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    value = float(end) - float(start)
    if value < 0:
        return None
    return value


def build_metrics(config: RunnerConfig, dirs: dict[str, Path]) -> dict[str, Any]:
    prom = json.loads((dirs["raw"] / "prometheus_range_queries.json").read_text(encoding="utf-8")) if (dirs["raw"] / "prometheus_range_queries.json").exists() else {}
    pod_rows = extract_pod_rows(dirs, config.stream_keys)
    publisher_rows = [r for r in read_jsonl(dirs["raw"] / "publishers.jsonl") if r.get("event") == "publisher_finished"]
    controller_events = read_jsonl(dirs["raw"] / "controller_events.jsonl")

    activation_rows, release_rows, lifecycle_by_stream = build_lifecycle_rows(config, dirs, publisher_rows)
    activation_fields = [
        "repetition", "stream_key", "t_publish_start_client", "t_publish_start_proxy", "t_controller_received_event", "t_worker_create_requested", "t_worker_pod_created", "t_worker_scheduled", "t_worker_container_started", "t_worker_ready", "t_ffmpeg_started", "t_ffmpeg_first_progress", "event_detection_seconds", "worker_create_seconds", "worker_scheduling_seconds", "worker_ready_seconds", "ffmpeg_start_seconds", "ffmpeg_first_progress_seconds", "total_activation_seconds", "status"
    ]
    write_csv(dirs["metrics"] / "activation_metrics.csv", activation_rows, activation_fields)
    write_csv(dirs["metrics"] / "release_metrics.csv", release_rows, ["repetition", "stream_key", "release_detection_seconds", "worker_delete_seconds", "total_release_seconds", "status"])

    resilience_rows = []
    for run in read_jsonl(dirs["raw"] / "streams.jsonl"):
        if run.get("event") == "run_finished":
            for injected in run.get("injected_failures") or []:
                injected_ts = injected.get("timestamp")
                recovery_end = None
                if injected.get("type") == "worker-failure":
                    candidates = [e.get("timestamp_epoch") for e in controller_events if e.get("event_type") in {"worker_deleted", "stream_lifecycle_timestamp_observed"} and isinstance(e.get("timestamp_epoch"), (int, float)) and (not injected_ts or e.get("timestamp_epoch") >= injected_ts)]
                    recovery_end = min(candidates) if candidates else None
                resilience_rows.append({"type": injected.get("type"), "pod": injected.get("pod"), "timestamp": injected_ts, "recovery_seconds": delta(injected_ts, recovery_end), "status": injected.get("status") or ("injected" if injected.get("pod") else "not_injected")})
    write_csv(dirs["metrics"] / "resilience_metrics.csv", resilience_rows, ["type", "pod", "timestamp", "recovery_seconds", "status"])

    resource_rows = []
    for metric_name, component in [("pod_cpu_rate", "cpu"), ("pod_memory_working_set", "memory"), ("proxy_network_receive_bps", "network_receive"), ("proxy_network_transmit_bps", "network_transmit")]:
        s = stats(prom_values(prom.get(metric_name, {})))
        resource_rows.append({"metric": metric_name, "component": component, **s})
    write_csv(dirs["metrics"] / "resource_usage.csv", resource_rows, ["metric", "component", "samples", "mean", "median", "stddev", "p50", "p95", "p99", "min", "max", "ci95_low", "ci95_high"])

    workers_by_stream = {}
    orphan_candidates = 0
    for row in pod_rows:
        if row.get("component") == "worker":
            stream = row.get("stream_key")
            if stream:
                workers_by_stream.setdefault(stream, set()).add(row.get("pod"))
            else:
                orphan_candidates += 1
    correctness_rows = []
    handover_accepted = sum(1 for e in controller_events if e.get("event_type") == "handover_accepted")
    handover_denied = sum(1 for e in controller_events if e.get("event_type") == "handover_denied")
    stale_ignored = sum(1 for e in controller_events if e.get("event_type") == "stale_event_ignored")
    for key in config.stream_keys:
        worker_count = len(workers_by_stream.get(key, set()))
        correctness_rows.append({"stream_key": key, "worker_count_observed": worker_count, "one_worker_per_stream": worker_count <= 1, "duplicate_worker_detected": worker_count > 1, "handover_accepted": handover_accepted, "handover_denied": handover_denied, "stale_events_ignored": stale_ignored})
    correctness_rows.append({"stream_key": "__orphans__", "worker_count_observed": orphan_candidates, "one_worker_per_stream": None, "duplicate_worker_detected": None, "handover_accepted": handover_accepted, "handover_denied": handover_denied, "stale_events_ignored": stale_ignored})
    write_csv(dirs["metrics"] / "correctness_metrics.csv", correctness_rows, ["stream_key", "worker_count_observed", "one_worker_per_stream", "duplicate_worker_detected", "handover_accepted", "handover_denied", "stale_events_ignored"])

    workers_active_values = prom_values(prom.get("workers_active", {}))
    proxies_active_values = prom_values(prom.get("proxies_active", {}))
    metadata = json.loads((config.report_root / "metadata.json").read_text(encoding="utf-8")) if (config.report_root / "metadata.json").exists() else {}
    duration = max(0.0, (metadata.get("ended_at") or 0) - (metadata.get("started_at") or 0))
    observed_worker_count = statistics.mean(workers_active_values) if workers_active_values else None
    observed_proxy_count = statistics.mean(proxies_active_values) if proxies_active_values else None
    worker_pod_seconds = (observed_worker_count or 0) * duration
    proxy_pod_seconds = (observed_proxy_count or 0) * duration
    controller_pod_seconds = duration
    always_on_worker_pod_seconds = max(1, len(config.stream_keys)) * duration
    economy_relative = None if (not workers_active_values or always_on_worker_pod_seconds <= 0) else 1 - (worker_pod_seconds / always_on_worker_pod_seconds)
    cost_rows = [
        {"metric": "worker_pod_seconds", "value": worker_pod_seconds, "source": "prometheus_workers_active_mean" if workers_active_values else "no_prometheus_samples"},
        {"metric": "proxy_pod_seconds", "value": proxy_pod_seconds, "source": "prometheus_proxies_active_mean" if proxies_active_values else "no_prometheus_samples"},
        {"metric": "controller_pod_seconds", "value": controller_pod_seconds, "source": "experiment_duration"},
        {"metric": "always_on_worker_pod_seconds_reference", "value": always_on_worker_pod_seconds, "source": "len_stream_keys_times_duration"},
        {"metric": "relative_savings_vs_always_on_workers", "value": economy_relative, "source": "derived_estimate" if workers_active_values else "not_supported_without_prometheus"},
    ]
    write_csv(dirs["metrics"] / "cost_estimation.csv", cost_rows, ["metric", "value", "source"])

    lifecycle_values = {name: prom_values(prom.get(name, {})) for name in ("stream_lifecycle_phase_seconds_p50", "stream_lifecycle_phase_seconds_p95", "stream_lifecycle_phase_seconds_p99")}
    activation_stats = {k: stats(v) for k, v in lifecycle_values.items()}
    activation_stats["total_activation_seconds_per_stream"] = stats([r["total_activation_seconds"] for r in activation_rows if r.get("total_activation_seconds") is not None])
    activation_stats["event_detection_seconds_per_stream"] = stats([r["event_detection_seconds"] for r in activation_rows if r.get("event_detection_seconds") is not None])
    activation_stats["worker_ready_seconds_per_stream"] = stats([r["worker_ready_seconds"] for r in activation_rows if r.get("worker_ready_seconds") is not None])
    return {"activation": activation_stats, "resources": resource_rows, "correctness": correctness_rows, "cost": cost_rows, "missing": missing_metrics(config, prom, activation_rows, release_rows)}


def missing_metrics(config: RunnerConfig, prom: dict[str, Any], activation_rows: list[dict[str, Any]] | None = None, release_rows: list[dict[str, Any]] | None = None) -> list[str]:
    missing = []
    for name, result in prom.items():
        if not result.get("available") or (result.get("response") or {}).get("status") != "success":
            missing.append(name)
    activation_rows = activation_rows or []
    release_rows = release_rows or []
    required_activation = ["t_controller_received_event", "t_worker_create_requested", "t_worker_ready", "t_ffmpeg_started", "t_ffmpeg_first_progress"]
    for field in required_activation:
        if not any(row.get(field) is not None for row in activation_rows):
            missing.append(f"per_stream_{field}")
    if not any(row.get("total_release_seconds") is not None for row in release_rows):
        missing.append("per_stream_total_release_seconds")
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
    try:
        import matplotlib.pyplot as plt  # type: ignore
    except Exception:
        for name in ("activation_boxplot", "activation_p95_by_concurrency", "resource_usage_cpu", "resource_usage_memory", "network_usage_proxy", "workers_over_time", "recovery_time"):
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

    # P95 by current run/concurrency is only meaningful when activation samples exist.
    path = dirs["charts"] / "activation_p95_by_concurrency.png"
    if activation_values:
        plt.figure()
        plt.bar(["current"], [percentile(sorted(activation_values), 95) or 0])
        plt.ylabel("seconds")
        plt.title("Activation P95")
        plt.savefig(path, bbox_inches="tight")
        plt.close()
        paths["activation_p95_by_concurrency"] = str(path)
    else:
        paths["activation_p95_by_concurrency"] = write_chart_limitation(path, "No activation samples available for P95 chart.")

    resource = csv_rows(dirs["metrics"] / "resource_usage.csv")
    for chart_name, metric in [("resource_usage_cpu", "pod_cpu_rate"), ("resource_usage_memory", "pod_memory_working_set"), ("network_usage_proxy", "proxy_network_receive_bps")]:
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
    prom = json.loads((dirs["raw"] / "prometheus_range_queries.json").read_text(encoding="utf-8")) if (dirs["raw"] / "prometheus_range_queries.json").exists() else {}
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


def generate_report(config: RunnerConfig, dirs: dict[str, Path], execution: dict[str, Any], metrics: dict[str, Any], charts: dict[str, str]) -> dict[str, Any]:
    metadata = json.loads((dirs["root"] / "metadata.json").read_text(encoding="utf-8"))
    publisher_rows = [r for r in read_jsonl(dirs["raw"] / "publishers.jsonl") if r.get("event") == "publisher_finished"]
    success_count = len([r for r in publisher_rows if r.get("returncode") == 0])
    failure_count = len([r for r in publisher_rows if r.get("returncode") not in (0, None)])
    unavailable = metrics.get("missing", [])
    report_json = {
        "metadata": metadata,
        "summary": {"publishers": len(publisher_rows), "publisher_success_count": success_count, "publisher_failure_count": failure_count, "missing_metrics": unavailable},
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
    report = f"""# Relatório Experimental LiveEdgeCast

## Resumo executivo

Experimento `{config.experiment_id}` executado no cenário `{config.scenario}` com {len(config.stream_keys)} streamKey(s), duração nominal de {config.duration_seconds}s e {config.repetitions} repetição(ões). Foram registrados {success_count} publisher(s) com encerramento bem-sucedido e {failure_count} publisher(s) com falha observada. O relatório diferencia métricas reais, inferidas e ausentes; conclusões sobre métricas ausentes não são assumidas.

## Ambiente experimental

- Namespace: `{config.namespace}`
- RTMP URL: `{config.rtmp_url}`
- Prometheus URL configurado: `{config.prometheus_url or 'não configurado'}`
- Controller URL configurado: `{config.controller_url or 'não configurado'}`
- Source file: `{config.source_file or 'gerado por lavfi/testsrc'}`
- Bitrate: `{config.bitrate or 'padrão/copy'}`
- Baseline informado: `{config.baseline or 'não informado'}`

## Métricas principais

{md_table(main_metric_rows, ['metric','samples','mean','median','stddev','p50','p95','p99','min','max','ci95_low','ci95_high'])}

## Resultado por streamKey

{md_table([{'streamKey': k, 'status': 'verificar metrics/correctness_metrics.csv e raw/publishers.jsonl'} for k in config.stream_keys], ['streamKey','status'])}

## Uso de recursos

{md_table(resource_rows, ['metric','component','samples','mean','median','p95','p99','min','max'])}

## Custo relativo

{md_table(cost_rows, ['metric','value','source'])}

## Resiliência

Os cenários de falha registram injeções em `metrics/resilience_metrics.csv` e nos logs de Kubernetes. O tempo de recuperação só deve ser usado quando as métricas de recuperação do controller e/ou Prometheus estiverem disponíveis.

## Correção arquitetural

A verificação de um worker por streamKey e candidatos a órfãos foi salva em `metrics/correctness_metrics.csv`. Essa verificação combina snapshots de pods e anotações Kubernetes; ela é uma evidência operacional, não substitui métricas per-stream completas do controller.

## Limitações

- Métricas ausentes ou não observáveis nesta execução: {', '.join(unavailable) if unavailable else 'nenhuma limitação automática detectada'}.
- Tempos per-stream de cold start dependem da exportação de timestamps pelo controller; quando não há endpoint per-stream, o relatório usa apenas histogramas Prometheus agregados.
- `t_destination_received` só pode ser sustentado se houver callback/observação no destino externo.
- A estimativa de custo relativo é uma aproximação por pod-seconds; não equivale a cobrança real de provedor de nuvem.
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
        lines.append("As séries do Prometheus permitem relacionar latência e comportamento operacional com uso de CPU, memória, rede e quantidade de pods ativos, desde que as consultas tenham retornado amostras válidas.")
    else:
        lines.append("Como o Prometheus não foi configurado, a discussão quantitativa de recursos e custo relativo não pode ser sustentada por séries temporais nesta execução.")
    if config.scenario in {"worker-failure", "proxy-failure", "handover", "duplicate-streamkey"}:
        lines.append("O cenário executado também contribui para a análise qualitativa de resiliência e correção arquitetural, especialmente quanto a recuperação de worker, limitação de falha de proxy, handover seguro ou rejeição de streamKey duplicada.")
    if missing:
        lines.append("As métricas ausentes devem ser explicitadas como limitação metodológica; conclusões sobre elas não devem ser afirmadas sem nova instrumentação ou nova execução experimental.")
    return "\n\n".join(lines)


def write_docs(repo_root: Path) -> None:
    docs_exp = repo_root / "docs" / "experiments"
    docs_obs = repo_root / "docs" / "observability"
    docs_exp.mkdir(parents=True, exist_ok=True)
    docs_obs.mkdir(parents=True, exist_ok=True)
    (docs_exp / "run-experiment.md").write_text("""# Executando experimentos com `run_experiment.py`

O runner unificado executa publishers RTMP com FFmpeg, coleta evidências de Kubernetes/Prometheus, consolida métricas e gera `report.md` e `report.json`.

Exemplo:

```bash
python tools/experiments/run_experiment.py \\
  --stream-keys-file ./tools/experiments/stream_keys.txt \\
  --scenario cold-start \\
  --rtmp-url rtmp://127.0.0.1:1935/live \\
  --duration-seconds 120 \\
  --repetitions 30 \\
  --prometheus-url http://localhost:9090 \\
  --namespace media \\
  --experiment-id exp-rtmp-coldstart-001 \\
  --output-dir ./reports
```

Cenários suportados: `cold-start`, `concurrency`, `release`, `worker-failure`, `proxy-failure`, `handover`, `duplicate-streamkey` e `pilot-capacity`.

Quando uma métrica não estiver disponível, o runner registra `null` nos CSVs e declara a limitação no relatório. O script não inventa tempos não observáveis.
""", encoding="utf-8")
    (docs_exp / "report-format.md").write_text("""# Formato do relatório experimental

Cada execução cria:

```text
reports/<experiment-id>/
  metadata.json
  raw/
  metrics/
  logs/
  charts/
  report.md
  report.json
```

Os CSVs em `metrics/` são a base para tabelas e discussão do artigo. Os arquivos em `raw/` preservam evidências brutas para auditoria e reprocessamento.
""", encoding="utf-8")
    promql_path = docs_obs / "promql.md"
    with promql_path.open("a", encoding="utf-8") as fh:
        fh.write("\n\n## Consultas usadas pelo runner unificado\n\n")
        for name, query in DEFAULT_PROMQL.items():
            fh.write(f"### {name}\n\n```promql\n{query}\n```\n\n")


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_args(argv)
    dirs = ensure_layout(config.report_root)
    metadata = {**asdict(config), "output_dir": str(config.output_dir), "report_root": str(config.report_root), "started_at": now_epoch(), "started_at_iso": now_iso()}
    write_json(dirs["root"] / "metadata.json", metadata)
    if config.dry_run:
        execution = {"dry_run": True, "config": metadata}
        write_json(dirs["root"] / "report.json", execution)
        (dirs["root"] / "report.md").write_text("# Dry run\n\nConfiguração validada. Nenhum experimento foi executado.\n", encoding="utf-8")
        return 0
    exit_code = 0
    try:
        execution = execute_experiment(config, dirs)
    except Exception as exc:
        execution = {"error": {"type": type(exc).__name__, "message": str(exc)}, "started_at": metadata["started_at"], "ended_at": now_epoch()}
        exit_code = 1
    metadata["ended_at"] = execution.get("ended_at", now_epoch())
    metadata["ended_at_iso"] = now_iso()
    write_json(dirs["root"] / "metadata.json", metadata)
    write_json(dirs["root"] / "execution.json", execution)
    metrics = build_metrics(config, dirs)
    charts = generate_charts(dirs)
    generate_report(config, dirs, execution, metrics, charts)
    print(f"Report generated at: {dirs['root'] / 'report.md'}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
