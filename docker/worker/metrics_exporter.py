#!/usr/bin/env python3
"""Prometheus exporter for a worker-local FFmpeg ``-progress`` file.

The exporter intentionally uses only the Python standard library so it can run in
small worker images without adding a Prometheus client dependency.  It follows an
FFmpeg progress file across partial writes, truncation, and path rotation.
"""

from __future__ import annotations

import glob
import os
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from typing import Callable, Dict, Iterable, Optional, Tuple

DEFAULT_PROGRESS_STALE_SECONDS = 15.0
DEFAULT_LISTEN_ADDR = "0.0.0.0"
DEFAULT_LISTEN_PORT = 9113

_PROGRESS_LINE_RE = re.compile(r"^([A-Za-z0-9_]+)=(.*)$")
_TIME_RE = re.compile(r"^(?P<sign>-)?(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})(?:\.(?P<fraction>\d+))?$")


def _float_or_none(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _int_or_none(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_ffmpeg_time(value: Optional[str]) -> Optional[float]:
    """Parse FFmpeg progress time values into seconds."""

    if not value or value == "N/A":
        return None

    match = _TIME_RE.match(value.strip())
    if match:
        hours = int(match.group("hours"))
        minutes = int(match.group("minutes"))
        seconds = int(match.group("seconds"))
        fraction = match.group("fraction") or ""
        parsed = (hours * 3600) + (minutes * 60) + seconds
        if fraction:
            parsed += int(fraction) / (10 ** len(fraction))
        return -parsed if match.group("sign") else parsed

    return _float_or_none(value)


def parse_out_time_seconds(record: Dict[str, str]) -> Optional[float]:
    """Return the best available out_time value in seconds."""

    parsed = parse_ffmpeg_time(record.get("out_time"))
    if parsed is not None:
        return parsed

    for key in ("out_time_us", "out_time_ms"):
        value = _int_or_none(record.get(key))
        if value is not None:
            # FFmpeg's historical out_time_ms field is microseconds in practice.
            return value / 1_000_000.0

    return None


def parse_progress_records(text: str) -> Iterable[Dict[str, str]]:
    """Yield complete records from newline-delimited FFmpeg progress text.

    A final non-newline-terminated line is considered a partial write and is
    ignored until the next poll completes it.  Unknown or malformed lines are
    skipped, while duplicate keys keep the last value in the current record.
    """

    current: Dict[str, str] = {}
    lines = text.splitlines(keepends=True)
    for line in lines:
        if not line.endswith(("\n", "\r")):
            continue
        line = line.strip()
        if not line:
            continue
        match = _PROGRESS_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        current[key] = value
        if key == "progress":
            yield current
            current = {}

    if current:
        # FFmpeg progress files can be scraped before the progress delimiter is
        # flushed.  The complete key/value lines still contain useful gauges.
        yield current


@dataclass
class ProgressFollower:
    path: str
    read_chunk_size: int = 1024 * 1024
    _identity: Optional[Tuple[int, int]] = None
    _offset: int = 0
    _buffer: str = ""
    _prefix: str = ""
    _tail: str = ""
    _current_record: Dict[str, str] = field(default_factory=dict)
    latest_record: Dict[str, str] = field(default_factory=dict)
    latest_timestamp: Optional[float] = None

    def poll(self, now: Optional[float] = None) -> Dict[str, str]:
        now = time.time() if now is None else now
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            return self.latest_record

        identity = (stat.st_dev, stat.st_ino)
        if self._identity != identity or stat.st_size < self._offset:
            self._identity = identity
            self._offset = 0
            self._buffer = ""
            self._prefix = ""
            self._tail = ""
            self._current_record = {}
        elif not self._file_window_matches():
            self._offset = 0
            self._buffer = ""
            self._prefix = ""
            self._tail = ""
            self._current_record = {}

        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as progress_file:
                progress_file.seek(self._offset)
                while True:
                    read_offset = self._offset
                    data = progress_file.read(self.read_chunk_size)
                    if not data:
                        break
                    self._offset = progress_file.tell()
                    if read_offset == 0:
                        self._remember_prefix(data)
                    self._remember_tail(data)
                    self._process_data(data, now)
        except FileNotFoundError:
            # The file may be rotated between stat() and open(). Keep the last
            # known good record and retry from the new file on the next scrape.
            self._identity = None
            self._offset = 0
            self._buffer = ""
            self._prefix = ""
            self._tail = ""
            self._current_record = {}

        return self.latest_record

    def _file_window_matches(self) -> bool:
        if not self._prefix and not self._tail:
            return True
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as progress_file:
                if self._prefix and progress_file.read(len(self._prefix)) != self._prefix:
                    return False
                if self._tail:
                    tail_offset = max(0, self._offset - len(self._tail))
                    progress_file.seek(tail_offset)
                    if progress_file.read(len(self._tail)) != self._tail:
                        return False
        except FileNotFoundError:
            return True
        return True

    def _remember_prefix(self, data: str) -> None:
        if len(self._prefix) < 256:
            self._prefix = (self._prefix + data)[:256]

    def _remember_tail(self, data: str) -> None:
        self._tail = (self._tail + data)[-256:]

    def _process_data(self, data: str, now: float) -> None:
        complete_text, self._buffer = self._split_complete_text(self._buffer + data)
        observed_complete_line = False

        for line in complete_text.splitlines():
            line = line.strip()
            if not line:
                continue
            match = _PROGRESS_LINE_RE.match(line)
            if not match:
                continue

            observed_complete_line = True
            key, value = match.group(1), match.group(2)
            self._current_record[key] = value
            if key == "progress":
                self.latest_record = dict(self._current_record)
                self._current_record = {}

        if self._current_record:
            self.latest_record = {**self.latest_record, **self._current_record}

        if observed_complete_line:
            self.latest_timestamp = now

    @staticmethod
    def _split_complete_text(text: str) -> Tuple[str, str]:
        last_newline = max(text.rfind("\n"), text.rfind("\r"))
        if last_newline < 0:
            return "", text
        return text[: last_newline + 1], text[last_newline + 1 :]


def discover_progress_path() -> str:
    explicit = os.environ.get("FFMPEG_PROGRESS_FILE")
    if explicit:
        return explicit

    stream_key = os.environ.get("STREAM_KEY", "")
    if stream_key:
        return f"/tmp/ffmpeg_{stream_key}.progress"

    candidates = []
    for path in glob.glob("/tmp/ffmpeg_*.progress"):
        try:
            candidates.append((os.stat(path).st_mtime, path))
        except FileNotFoundError:
            continue
    if candidates:
        return max(candidates)[1]
    return "/tmp/ffmpeg.progress"


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(pid_file: str) -> Optional[int]:
    try:
        with open(pid_file, "r", encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def discover_pid_file() -> str:
    explicit = os.environ.get("FFMPEG_PID_FILE")
    if explicit:
        return explicit

    stream_key = os.environ.get("STREAM_KEY", "")
    if stream_key:
        return f"/tmp/ffmpeg_{stream_key}.pid"
    return "/tmp/ffmpeg.pid"


def discover_exit_file() -> str:
    explicit = os.environ.get("FFMPEG_EXIT_FILE")
    if explicit:
        return explicit

    stream_key = os.environ.get("STREAM_KEY", "")
    if stream_key:
        return f"/tmp/ffmpeg_{stream_key}.exit"
    return "/tmp/ffmpeg.exit"


@dataclass
class ExitCounterStore:
    exit_file: str
    state_file: str
    counts: Dict[str, int] = field(default_factory=dict)
    seen_events: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._load_state()

    def poll(self) -> Dict[str, int]:
        changed = False
        for event_id, exit_code in self._read_exit_events():
            if event_id in self.seen_events:
                continue
            self.seen_events.add(event_id)
            self.counts[exit_code] = self.counts.get(exit_code, 0) + 1
            changed = True
        if changed:
            self._save_state()
        return dict(self.counts)

    def _read_exit_events(self) -> Iterable[Tuple[str, str]]:
        try:
            with open(self.exit_file, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except FileNotFoundError:
            return []

        events = []
        for index, line in enumerate(lines):
            parts = line.strip().split()
            if not parts:
                continue
            exit_code = parts[-1]
            if not re.fullmatch(r"-?\d+", exit_code):
                continue
            event_id = " ".join(parts[:-1]) or f"{self.exit_file}:{index}:{exit_code}"
            events.append((event_id, exit_code))
        return events

    def _load_state(self) -> None:
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                for line in handle:
                    kind, _, rest = line.partition(" ")
                    rest = rest.strip()
                    if kind == "seen" and rest:
                        self.seen_events.add(rest)
                    elif kind == "count" and rest:
                        code, _, value = rest.partition(" ")
                        parsed = _int_or_none(value)
                        if code and parsed is not None:
                            self.counts[code] = parsed
        except FileNotFoundError:
            return

    def _save_state(self) -> None:
        tmp_path = f"{self.state_file}.tmp"
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for code, count in sorted(self.counts.items()):
                handle.write(f"count {code} {count}\n")
            for event_id in sorted(self.seen_events):
                handle.write(f"seen {event_id}\n")
        os.replace(tmp_path, self.state_file)


@dataclass
class MetricsCollector:
    follower: ProgressFollower
    pid_file: str
    exit_store: ExitCounterStore
    stale_seconds: float = DEFAULT_PROGRESS_STALE_SECONDS
    clock: Callable[[], float] = time.time
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def collect(self) -> str:
        with self._lock:
            return self._collect_unlocked()

    def _collect_unlocked(self) -> str:
        now = self.clock()
        record = self.follower.poll(now=now)
        last_timestamp = self.follower.latest_timestamp
        progress_age = (now - last_timestamp) if last_timestamp is not None else 0.0
        running = self._running_value()
        healthy = 1 if running and last_timestamp is not None and progress_age <= self.stale_seconds else 0

        out_time = parse_out_time_seconds(record) or 0.0
        total_size = _float_or_none(record.get("total_size")) or 0.0
        speed_raw = (record.get("speed") or "").rstrip("x")
        speed = _float_or_none(speed_raw) or 0.0

        lines = [
            "# HELP worker_ffmpeg_running Whether the worker FFmpeg process is currently running.",
            "# TYPE worker_ffmpeg_running gauge",
            f"worker_ffmpeg_running {running}",
            "# HELP worker_ffmpeg_health_state 1 when FFmpeg is running and recent progress was observed, otherwise 0.",
            "# TYPE worker_ffmpeg_health_state gauge",
            f"worker_ffmpeg_health_state {healthy}",
            "# HELP worker_ffmpeg_last_progress_timestamp_seconds Unix timestamp of the last complete FFmpeg progress update observed by this exporter.",
            "# TYPE worker_ffmpeg_last_progress_timestamp_seconds gauge",
            f"worker_ffmpeg_last_progress_timestamp_seconds {last_timestamp or 0.0}",
            "# HELP worker_ffmpeg_progress_age_seconds Seconds since the last complete FFmpeg progress update observed by this exporter.",
            "# TYPE worker_ffmpeg_progress_age_seconds gauge",
            f"worker_ffmpeg_progress_age_seconds {progress_age}",
            "# HELP worker_ffmpeg_out_time_seconds Last FFmpeg out_time value converted to seconds.",
            "# TYPE worker_ffmpeg_out_time_seconds gauge",
            f"worker_ffmpeg_out_time_seconds {out_time}",
            "# HELP worker_ffmpeg_total_size_bytes Last FFmpeg total_size value in bytes.",
            "# TYPE worker_ffmpeg_total_size_bytes gauge",
            f"worker_ffmpeg_total_size_bytes {total_size}",
            "# HELP worker_ffmpeg_speed Last FFmpeg speed multiplier.",
            "# TYPE worker_ffmpeg_speed gauge",
            f"worker_ffmpeg_speed {speed}",
            "# HELP worker_ffmpeg_exit_total Total unique FFmpeg exits observed by this worker exporter.",
            "# TYPE worker_ffmpeg_exit_total counter",
        ]
        for exit_code, count in sorted(self.exit_store.poll().items()):
            lines.append(f'worker_ffmpeg_exit_total{{exit_code="{exit_code}"}} {count}')
        return "\n".join(lines) + "\n"

    def _running_value(self) -> int:
        pid = read_pid(self.pid_file)
        return 1 if pid is not None and process_is_running(pid) else 0


def build_collector() -> MetricsCollector:
    progress_path = discover_progress_path()
    pid_file = discover_pid_file()
    exit_file = discover_exit_file()
    state_file = os.environ.get("FFMPEG_EXPORTER_STATE_FILE", f"{exit_file}.metrics_state")
    stale_seconds = _float_or_none(os.environ.get("FFMPEG_PROGRESS_STALE_SECONDS")) or DEFAULT_PROGRESS_STALE_SECONDS
    return MetricsCollector(
        follower=ProgressFollower(progress_path),
        pid_file=pid_file,
        exit_store=ExitCounterStore(exit_file=exit_file, state_file=state_file),
        stale_seconds=stale_seconds,
    )


class MetricsHandler(BaseHTTPRequestHandler):
    collector: MetricsCollector

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path not in ("/metrics", "/"):
            self.send_response(404)
            self.end_headers()
            return
        payload = self.collector.collect().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    collector = build_collector()
    MetricsHandler.collector = collector
    listen_addr = os.environ.get("METRICS_EXPORTER_ADDR", DEFAULT_LISTEN_ADDR)
    listen_port = int(os.environ.get("METRICS_EXPORTER_PORT", str(DEFAULT_LISTEN_PORT)))
    server = ThreadingHTTPServer((listen_addr, listen_port), MetricsHandler)

    def shutdown(_signum: int, _frame: object) -> None:
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
