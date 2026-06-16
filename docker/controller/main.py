from fastapi import FastAPI, Query, HTTPException, Request
from kubernetes import client, config, watch
from kubernetes.client.exceptions import ApiException
import random
import string
import threading
import requests
import time
import logging
import asyncio
import json
import copy
import hashlib
import os
import re
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from fastapi.responses import Response


METADATA_LABEL_NAMES: Tuple[str, ...] = ("tenant", "environment", "region")
METADATA_DEFAULT_VALUE = "unknown"
METADATA_HEADER_PREFIXES: Tuple[str, ...] = ("x-liveedgecast", "x")
METADATA_QUERY_PREFIX = "metadata_"
METADATA_ENV_PREFIXES: Tuple[str, ...] = ("LIVEEDGECAST", "CONTROLLER_METADATA")
METADATA_ALLOWED_VALUE = re.compile(r"[^a-zA-Z0-9_.:-]+")
METADATA_MAX_LENGTH = 64
LOG_EVENT_FIELDS: Tuple[str, ...] = (
    "timestamp",
    "event_type",
    "stream",
    "generation",
    "proxy_pod",
    "worker_pod",
    "experiment_id",
    "scenario",
    "run_id",
    "duration_ms",
    "status",
)
LOG_CONTEXT_FIELDS: Tuple[str, ...] = ("experiment_id", "scenario", "run_id")
LOG_CONTEXT_ENV_PREFIXES: Tuple[str, ...] = ("LIVEEDGECAST", "CONTROLLER")
LOG_CONTEXT_DEFAULT_VALUE = "unknown"
LOG_STATUS_REQUESTED = "requested"
LOG_STATUS_CREATED = "created"
LOG_STATUS_DELETED = "deleted"
LOG_STATUS_DELETE_FAILED = "delete_failed"
LOG_STATUS_ALREADY_DELETED = "already_deleted"
LOG_STATUS_ACCEPTED = "accepted"
LOG_STATUS_DENIED = "denied"
LOG_STATUS_READY = "ready"
LOG_STATUS_IGNORED = "ignored"
LOG_STATUS_RECEIVED = "received"
LOG_STATUS_OBSERVED = "observed"
LOG_STATUS_FAILED = "failed"
LOG_STATUS_SUCCESS = "success"


@dataclass(frozen=True)
class RequestMetadata:
    labels: Mapping[str, str]
    sources: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


@dataclass(frozen=True)
class LogContext:
    values: Mapping[str, str]
    sources: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


@dataclass(frozen=True)
class ProxyRtmpStats:
    active_streams: int
    active_publishers: int
    active_clients: int
    stream_names: FrozenSet[str] = frozenset()

    @property
    def stream_active(self) -> int:
        return 1 if self.active_streams > 0 else 0

    def has_stream(self, stream: str) -> Optional[bool]:
        if self.stream_names:
            return stream in self.stream_names
        if self.active_streams == 0:
            return False
        return None


@dataclass(frozen=True)
class StateRestoreResult:
    restored: bool
    reason: str

    def __bool__(self) -> bool:
        return self.restored


def sanitize_metadata_value(value: Optional[str]) -> str:
    if value is None:
        return METADATA_DEFAULT_VALUE
    sanitized = METADATA_ALLOWED_VALUE.sub("_", value.strip())[:METADATA_MAX_LENGTH]
    return sanitized or METADATA_DEFAULT_VALUE


def non_blank_metadata_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def default_request_metadata() -> RequestMetadata:
    return RequestMetadata(
        labels={name: METADATA_DEFAULT_VALUE for name in METADATA_LABEL_NAMES},
        sources={name: "default" for name in METADATA_LABEL_NAMES},
    )


def default_log_context() -> LogContext:
    return LogContext(
        values={name: LOG_CONTEXT_DEFAULT_VALUE for name in LOG_CONTEXT_FIELDS},
        sources={name: "default" for name in LOG_CONTEXT_FIELDS},
    )


current_request_metadata: ContextVar[RequestMetadata] = ContextVar(
    "current_request_metadata",
    default=default_request_metadata(),
)
current_log_context: ContextVar[LogContext] = ContextVar(
    "current_log_context",
    default=default_log_context(),
)
controlled_metric_metadata_cache: Optional[RequestMetadata] = None


def extract_request_metadata(request: Optional[Request] = None) -> RequestMetadata:
    """
    Extracts low-cardinality observability metadata with a single precedence rule:
    HTTP headers > query parameters > environment variables > controlled default.
    """
    labels: Dict[str, str] = {}
    sources: Dict[str, str] = {}

    for name in METADATA_LABEL_NAMES:
        raw_value = None
        source = "default"

        if request is not None:
            for prefix in METADATA_HEADER_PREFIXES:
                header_name = f"{prefix}-{name}"
                candidate = non_blank_metadata_value(request.headers.get(header_name))
                if candidate is not None:
                    raw_value = candidate
                    source = "header"
                    break

        if raw_value is None and request is not None:
            for query_name in (name, f"{METADATA_QUERY_PREFIX}{name}"):
                candidate = non_blank_metadata_value(request.query_params.get(query_name))
                if candidate is not None:
                    raw_value = candidate
                    source = "query"
                    break

        if raw_value is None:
            env_suffix = name.upper()
            for prefix in METADATA_ENV_PREFIXES:
                candidate = non_blank_metadata_value(os.getenv(f"{prefix}_{env_suffix}"))
                if candidate is not None:
                    raw_value = candidate
                    source = "env"
                    break

        labels[name] = sanitize_metadata_value(raw_value)
        sources[name] = source if raw_value is not None else "default"

    return RequestMetadata(labels=labels, sources=sources)


def extract_log_context(request: Optional[Request] = None) -> LogContext:
    """Extracts low-cardinality run context for structured controller event logs."""
    values: Dict[str, str] = {}
    sources: Dict[str, str] = {}

    for name in LOG_CONTEXT_FIELDS:
        raw_value = None
        source = "default"

        if request is not None:
            header_candidates = (f"x-liveedgecast-{name.replace('_', '-')}", f"x-{name.replace('_', '-')}")
            for header_name in header_candidates:
                candidate = non_blank_metadata_value(request.headers.get(header_name))
                if candidate is not None:
                    raw_value = candidate
                    source = "header"
                    break

        if raw_value is None and request is not None:
            for query_name in (name, f"metadata_{name}"):
                candidate = non_blank_metadata_value(request.query_params.get(query_name))
                if candidate is not None:
                    raw_value = candidate
                    source = "query"
                    break

        if raw_value is None:
            env_suffix = name.upper()
            for prefix in LOG_CONTEXT_ENV_PREFIXES:
                env_name = f"{prefix}_{env_suffix}" if prefix else env_suffix
                candidate = non_blank_metadata_value(os.getenv(env_name))
                if candidate is not None:
                    raw_value = candidate
                    source = "env"
                    break

        values[name] = sanitize_metadata_value(raw_value) if raw_value is not None else LOG_CONTEXT_DEFAULT_VALUE
        sources[name] = source if raw_value is not None else "default"

    return LogContext(values=values, sources=sources)


def metric_label_names(*base_labels: str) -> Tuple[str, ...]:
    return tuple(base_labels) + METADATA_LABEL_NAMES


def reset_controlled_metric_metadata_cache() -> None:
    global controlled_metric_metadata_cache
    controlled_metric_metadata_cache = None


def controlled_metric_metadata() -> RequestMetadata:
    """Returns metric metadata from controlled env/default sources only.

    Environment variables are process-level configuration for the controller pod,
    so metric label values are resolved once and cached for hot paths.
    """
    global controlled_metric_metadata_cache
    if controlled_metric_metadata_cache is None:
        controlled_metric_metadata_cache = extract_request_metadata(None)
    return controlled_metric_metadata_cache


def metric_metadata_labels() -> Dict[str, str]:
    """Returns controlled metric label values that never come from request input."""
    return dict(controlled_metric_metadata().labels)


def get_current_request_metadata() -> RequestMetadata:
    metadata = current_request_metadata.get()
    if all(source == "default" for source in metadata.sources.values()):
        return extract_request_metadata(None)
    return metadata


def get_current_log_context() -> LogContext:
    context = current_log_context.get()
    if all(source == "default" for source in context.sources.values()):
        return extract_log_context(None)
    return context


class ControlledMetadataMetric:
    """Wrapper that appends controlled metadata labels to a prometheus_client metric.

    Request-supplied metadata labels are always overwritten with controlled
    env/default values to avoid unbounded Prometheus cardinality.
    """

    def __init__(self, metric, base_label_names: Tuple[str, ...]):
        self._metric = metric
        self._base_label_names = tuple(base_label_names)
        self._base_label_count = len(self._base_label_names)
        self._label_names = metric_label_names(*self._base_label_names)

    def labels(self, *labelvalues, **labelkwargs):
        if labelvalues and labelkwargs:
            raise ValueError("Cannot mix positional and keyword labels")

        metadata_labels = metric_metadata_labels()
        if labelkwargs:
            enriched = dict(labelkwargs)
            for key, value in metadata_labels.items():
                enriched[key] = value
            return self._metric.labels(**enriched)

        metadata_values = tuple(metadata_labels[name] for name in METADATA_LABEL_NAMES)
        if len(labelvalues) == self._base_label_count:
            labelvalues = tuple(labelvalues) + metadata_values
        elif len(labelvalues) == self._base_label_count + len(METADATA_LABEL_NAMES):
            labelvalues = tuple(labelvalues[:self._base_label_count]) + metadata_values
        else:
            expected_with_metadata = self._base_label_count + len(METADATA_LABEL_NAMES)
            raise ValueError(
                f"Incorrect label count: got {len(labelvalues)}, "
                f"expected {self._base_label_count} base labels or {expected_with_metadata} labels including metadata"
            )
        return self._metric.labels(*labelvalues)

    def inc(self, amount: float = 1, exemplar: Optional[Dict[str, str]] = None):
        if self._base_label_count != 0:
            raise ValueError(
                "Direct inc is only valid for metrics without base labels; "
                "call labels(...).inc(...) instead"
            )
        return self.labels().inc(amount, exemplar=exemplar)

    def observe(self, amount, *args, **kwargs):
        if self._base_label_count != 0:
            raise ValueError(
                "Direct observe is only valid for metrics without base labels; "
                "call labels(...).observe(...) instead"
            )
        return self.labels().observe(amount, *args, **kwargs)

    def set(self, value):
        if self._base_label_count != 0:
            raise ValueError(
                "Direct set is only valid for metrics without base labels; "
                "call labels(...).set(...) instead"
            )
        return self.labels().set(value)

    def remove(self, *labelvalues, **labelkwargs):
        if labelvalues and labelkwargs:
            raise ValueError("Cannot mix positional and keyword labels")

        metadata_labels = metric_metadata_labels()
        if labelkwargs:
            enriched = dict(labelkwargs)
            for key, value in metadata_labels.items():
                enriched[key] = value
            missing = [name for name in self._label_names if name not in enriched]
            if missing:
                raise ValueError(f"Missing label values for: {', '.join(missing)}")
            return self._metric.remove(*[enriched[name] for name in self._label_names])

        metadata_values = tuple(metadata_labels[name] for name in METADATA_LABEL_NAMES)
        if len(labelvalues) == self._base_label_count:
            labelvalues = tuple(labelvalues) + metadata_values
        elif len(labelvalues) == self._base_label_count + len(METADATA_LABEL_NAMES):
            labelvalues = tuple(labelvalues[:self._base_label_count]) + metadata_values
        else:
            expected_with_metadata = self._base_label_count + len(METADATA_LABEL_NAMES)
            raise ValueError(
                f"Incorrect label count: got {len(labelvalues)}, "
                f"expected {self._base_label_count} base labels or {expected_with_metadata} labels including metadata"
            )
        return self._metric.remove(*labelvalues)

    def collect(self):
        return self._metric.collect()

    def __getattr__(self, name: str):
        return getattr(self._metric, name)


def with_metric_metadata(metric, base_label_names: Tuple[str, ...]):
    return ControlledMetadataMetric(metric, base_label_names)


def controller_counter(name: str, description: str, base_labels=()):
    return with_metric_metadata(Counter(name, description, metric_label_names(*base_labels)), tuple(base_labels))


def controller_gauge(name: str, description: str, base_labels=()):
    return with_metric_metadata(Gauge(name, description, metric_label_names(*base_labels)), tuple(base_labels))


def controller_histogram(name: str, description: str, base_labels=()):
    return with_metric_metadata(Histogram(name, description, metric_label_names(*base_labels)), tuple(base_labels))


class StructuredMetadataFormatter(logging.Formatter):
    def format(self, record):
        metadata = get_current_request_metadata()
        context = get_current_log_context()
        required_values = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat().replace("+00:00", "Z"),
            "event_type": getattr(record, "event_type", "log"),
            "stream": getattr(record, "stream", None),
            "generation": getattr(record, "generation", None),
            "proxy_pod": getattr(record, "proxy_pod", None),
            "worker_pod": getattr(record, "worker_pod", None),
            "experiment_id": getattr(record, "experiment_id", context.values.get("experiment_id", LOG_CONTEXT_DEFAULT_VALUE)),
            "scenario": getattr(record, "scenario", context.values.get("scenario", LOG_CONTEXT_DEFAULT_VALUE)),
            "run_id": getattr(record, "run_id", context.values.get("run_id", LOG_CONTEXT_DEFAULT_VALUE)),
            "duration_ms": getattr(record, "duration_ms", None),
            "status": getattr(record, "status", record.levelname.lower()),
        }
        payload = {field: required_values[field] for field in LOG_EVENT_FIELDS}
        payload.update({
            "level": record.levelname,
            "message": record.getMessage(),
            "metadata": dict(metadata.labels),
            "metadata_sources": dict(metadata.sources),
        })
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def get_stream_proxy_pod(stream: Optional[str]) -> Optional[str]:
    if not stream:
        return None
    return stream_registry.get(stream, {}).get("proxy_pod")


def log_controller_event(
    event_type: str,
    *,
    stream: Optional[str] = None,
    generation: Optional[int] = None,
    proxy_pod: Optional[str] = None,
    worker_pod: Optional[str] = None,
    duration_ms: Optional[float] = None,
    started_at: Optional[float] = None,
    status: str = "success",
    level: int = logging.INFO,
    message: Optional[str] = None,
) -> None:
    if duration_ms is None and started_at is not None:
        duration_ms = round((time.monotonic() - started_at) * 1000, 3)
    if generation is None and stream:
        generation = stream_generation.get(stream)
    logger.log(
        level,
        message or event_type,
        extra={
            "event_type": event_type,
            "stream": stream,
            "generation": generation,
            "proxy_pod": proxy_pod,
            "worker_pod": worker_pod,
            "duration_ms": duration_ms,
            "status": status,
        },
    )


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.propagate = False
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
for handler in logger.handlers:
    handler.setFormatter(StructuredMetadataFormatter(datefmt='%Y-%m-%d %H:%M:%S'))


def get_int_env(name: str, default: int, min_value: Optional[int] = None) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        logger.warning(f"Invalid integer for {name}='{raw_value}', using default {default}")
        return default
    if min_value is not None and value < min_value:
        logger.warning(f"Invalid integer for {name}='{raw_value}', minimum is {min_value}; using default {default}")
        return default
    return value


def get_float_env(name: str, default: float, min_value: Optional[float] = None) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        logger.warning(f"Invalid float for {name}='{raw_value}', using default {default}")
        return default
    if value != value or value in (float("inf"), float("-inf")):
        logger.warning(f"Invalid float for {name}='{raw_value}', must be finite; using default {default}")
        return default
    if min_value is not None and value < min_value:
        logger.warning(f"Invalid float for {name}='{raw_value}', minimum is {min_value}; using default {default}")
        return default
    return value


def get_bool_env(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    logger.warning(f"Invalid boolean for {name}='{raw_value}', using default {default}")
    return default


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    global registry_health_task
    global worker_health_task
    global worker_orphan_sweeper_task
    global metrics_collection_task
    global proxy_rtmp_stats_task
    global worker_pod_events_task
    await asyncio.sleep(5)
    # Pragmatic controller mode: workers are created/deleted only by proxy
    # publish hooks (/streams/started and /streams/ended).  Background
    # health/recovery/handover/orphan cleanup loops are intentionally disabled
    # for this version. Metrics collectors are best-effort and must not affect
    # the worker lifecycle.
    metrics_collection_task = asyncio.create_task(collect_infrastructure_metrics())
    proxy_rtmp_stats_task = asyncio.create_task(collect_proxy_rtmp_stats())
    try:
        yield
    finally:
        pending_tasks = [
            task
            for task in (
                metrics_collection_task,
                proxy_rtmp_stats_task,
            )
            if task and not task.done()
        ]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)


app = FastAPI(lifespan=app_lifespan)


@app.middleware("http")
async def observability_metadata_middleware(request: Request, call_next):
    metadata_token = current_request_metadata.set(extract_request_metadata(request))
    log_context_token = current_log_context.set(extract_log_context(request))
    try:
        return await call_next(request)
    finally:
        current_log_context.reset(log_context_token)
        current_request_metadata.reset(metadata_token)


def get_experiment_tags(experiment_id: str = None, scenario: str = None, run_id: str = None):
    return {
        'experiment_id': experiment_id or 'default',
        'scenario': scenario or 'default',
        'run_id': run_id or 'default',
    }

def log_event(event_type: str, stream: str, status: str, tags: dict, generation=None, proxy_pod=None, worker_pod=None, duration_ms=None):
    payload = {
        'timestamp': time.time(), 'event_type': event_type, 'stream': stream, 'generation': generation,
        'proxy_pod': proxy_pod, 'worker_pod': worker_pod, 'experiment_id': tags['experiment_id'],
        'scenario': tags['scenario'], 'run_id': tags['run_id'], 'duration_ms': duration_ms, 'status': status
    }
    logger.info(json.dumps(payload))


def metric_best_effort(description: str, callback) -> None:
    """Run a metric update without allowing observability failures to affect lifecycle."""
    try:
        callback()
    except Exception as metric_error:
        logger.warning(f"[Metrics] Failed {description}: {metric_error}")

NAMESPACE = "media"
WORKER_DEPLOYMENT = "worker"
WORKER_SERVICE = "worker"

allocation_lock = threading.RLock()

stream_to_worker: Dict[str, str] = {}

worker_to_stream: Dict[str, str] = {}

stream_to_proxy: Dict[str, str] = {}
stream_generation: Dict[str, int] = {}
stream_generation_high_water: Dict[str, int] = {}
stream_registry: Dict[str, Dict[str, str]] = {}
# Explicit terminal sessions observed through /streams/ended. Proxy /stats absence is not terminal.
stream_terminal_sessions: Dict[str, Dict[str, Any]] = {}

registry_health_task: Optional[asyncio.Task] = None
worker_health_task: Optional[asyncio.Task] = None
worker_orphan_sweeper_task: Optional[asyncio.Task] = None
PROXY_HEALTHCHECK_INTERVAL_SECONDS = 3
PROXY_HEALTHCHECK_MAX_FAILURES = 3
PROXY_HEALTHCHECK_TIMEOUT_SECONDS = 2
PROXY_HEALTHCHECK_MAX_CONCURRENCY = 20
PROXY_HEALTHCHECK_JITTER_SECONDS = 1.5
WORKER_HEALTHCHECK_INTERVAL_SECONDS = get_int_env("WORKER_HEALTHCHECK_INTERVAL_SECONDS", 3, min_value=1)
WORKER_HEALTHCHECK_MAX_FAILURES = get_int_env("WORKER_HEALTHCHECK_MAX_FAILURES", 3, min_value=1)
WORKER_HEALTHCHECK_JITTER_SECONDS = float(os.getenv("WORKER_HEALTHCHECK_JITTER_SECONDS", "1.5"))
# Keep this longer than the worker input-open retry window so the controller does not
# replace a cold-starting worker while FFmpeg is still retrying the proxy input.
WORKER_READY_HEALTH_DELAY_SECONDS = get_int_env("WORKER_READY_HEALTH_DELAY_SECONDS", 50, min_value=0)
WORKER_NOT_READY_REPLACE_SECONDS = get_int_env(
    "WORKER_NOT_READY_REPLACE_SECONDS",
    max(WORKER_READY_HEALTH_DELAY_SECONDS + (WORKER_HEALTHCHECK_INTERVAL_SECONDS * WORKER_HEALTHCHECK_MAX_FAILURES), 90),
    min_value=1,
)
WORKER_HEALTHCHECK_PORT = get_int_env("WORKER_HEALTHCHECK_PORT", 9113, min_value=1)
WORKER_HEALTHCHECK_PATH = os.getenv("WORKER_HEALTHCHECK_PATH", "/readyz")
WORKER_HEALTHCHECK_TIMEOUT_SECONDS = float(os.getenv("WORKER_HEALTHCHECK_TIMEOUT_SECONDS", "2"))
ALLOW_LEGACY_ENDED_WITHOUT_SESSION_ID = os.getenv("ALLOW_LEGACY_ENDED_WITHOUT_SESSION_ID", "false").lower() in {"1", "true", "yes", "y"}
ALLOW_LEGACY_SESSION_REUSE = os.getenv("ALLOW_LEGACY_SESSION_REUSE", "false").lower() in {"1", "true", "yes", "y"}
WORKER_ORPHAN_SWEEP_INTERVAL_SECONDS = 60
WORKER_POD_LIFECYCLE_WATCH_TIMEOUT_SECONDS = get_int_env("WORKER_POD_LIFECYCLE_WATCH_TIMEOUT_SECONDS", 5, min_value=1)
CONTROLLER_DESTINATION_CALLBACK_ENABLED = get_bool_env("CONTROLLER_DESTINATION_CALLBACK_ENABLED", False)
STREAM_GENERATION_HIGH_WATER_MAX_ENTRIES = get_int_env("STREAM_GENERATION_HIGH_WATER_MAX_ENTRIES", 10000, min_value=1)
STREAM_TERMINAL_SESSION_MAX_ENTRIES = get_int_env("STREAM_TERMINAL_SESSION_MAX_ENTRIES", 20000, min_value=1)
PROXY_READY_HEALTH_DELAY_SECONDS = 3  # Wait after proxy Ready before proxy /health probes.
proxy_health_failures: Dict[str, int] = {}
worker_ready_since: Dict[str, float] = {}
worker_not_ready_since: Dict[str, float] = {}
worker_health_failures: Dict[str, int] = {}
worker_pod_uid_by_name: Dict[str, str] = {}
proxy_ready_since: Dict[str, float] = {}

STATE_CONFIGMAP_NAME = "controller-state"
STATE_CONFIGMAP_KEY = "state.json"
STATE_SCHEMA_VERSION = 3

metrics_collection_task: Optional[asyncio.Task] = None
proxy_rtmp_stats_task: Optional[asyncio.Task] = None
worker_pod_events_task: Optional[asyncio.Task] = None

PROXY_RTMP_STATS_INTERVAL_SECONDS = get_int_env("PROXY_RTMP_STATS_INTERVAL_SECONDS", 10, min_value=1)
PROXY_RTMP_STATS_TIMEOUT_SECONDS = get_float_env("PROXY_RTMP_STATS_TIMEOUT_SECONDS", 2.0, min_value=0.1)
PROXY_RTMP_STATS_MAX_CONCURRENCY = get_int_env("PROXY_RTMP_STATS_MAX_CONCURRENCY", 20, min_value=1)
WORKER_CONTROLLER_API = os.getenv("WORKER_CONTROLLER_API", "http://controller.media.svc.cluster.local:8000")
ALLOW_LEGACY_WORKER_REPLAY_WITHOUT_GENERATION = get_bool_env("ALLOW_LEGACY_WORKER_REPLAY_WITHOUT_GENERATION", False)
ALLOW_LEGACY_WORKER_REPLAY_WITHOUT_SESSION = get_bool_env("ALLOW_LEGACY_WORKER_REPLAY_WITHOUT_SESSION", False)
proxy_rtmp_stats_observed_pods: Set[str] = set()

pod_cpu_usage_percent = controller_gauge('pod_cpu_usage_percent','Pod CPU usage percentage (0-100)',('pod','namespace'))
pod_memory_usage_bytes = controller_gauge('pod_memory_usage_bytes','Pod memory usage in bytes',('pod','namespace'))
pod_memory_usage_percent = controller_gauge('pod_memory_usage_percent','Pod memory usage as percent of limit',('pod','namespace'))
pod_network_io_bytes_total = controller_counter('pod_network_io_bytes_total','Total network I/O bytes',('pod','direction'))
pod_ready_status = controller_gauge('pod_ready_status','Is pod ready (0 or 1)',('pod','namespace'))
proxy_active_connections = controller_gauge('proxy_active_connections','Active RTMP connections to proxy',('proxy_pod',))
proxy_bandwidth_mbps = controller_gauge('proxy_bandwidth_mbps','Current proxy bandwidth in Mbps',('proxy_pod',))
proxy_rtmp_active_streams = controller_gauge('proxy_rtmp_active_streams', 'Active RTMP streams reported by each proxy /stats endpoint', ('proxy_pod',))
proxy_rtmp_active_publishers = controller_gauge('proxy_rtmp_active_publishers', 'Active RTMP publishing clients reported by each proxy /stats endpoint', ('proxy_pod',))
proxy_rtmp_active_clients = controller_gauge('proxy_rtmp_active_clients', 'Active RTMP clients reported by each proxy /stats endpoint', ('proxy_pod',))
proxy_rtmp_stream_active = controller_gauge('proxy_rtmp_stream_active', 'Whether each proxy has at least one active RTMP stream', ('proxy_pod',))
proxy_rtmp_stats_up = controller_gauge('proxy_rtmp_stats_up', 'Whether the last proxy RTMP /stats scrape succeeded', ('proxy_pod',))
worker_pods_available = controller_gauge('worker_pods_available','Worker pods with processing readiness (/readyz); kept for backward-compatible dashboards',('namespace',))
worker_pods_running = controller_gauge('worker_pods_running','Worker pods in Running or Pending phase and not deleting',('namespace',))
worker_processing_ready = controller_gauge('worker_processing_ready','Worker pods whose Kubernetes Ready condition reflects FFmpeg processing readiness',('namespace',))
# Initialize labeled gauges so Prometheus sees series even before any worker is created.
worker_pods_available.labels(namespace=NAMESPACE).set(0)
worker_pods_running.labels(namespace=NAMESPACE).set(0)
worker_processing_ready.labels(namespace=NAMESPACE).set(0)
controller_active_streams = controller_gauge('controller_active_streams', 'Active streams currently tracked by the controller')
controller_active_allocations = controller_gauge('controller_active_allocations', 'Active stream-to-worker allocations currently tracked by the controller')
stream_proxy_handover_counter = controller_counter('stream_proxy_handover_total','Total proxy handovers accepted by controller')
handover_attempts_total = controller_counter('handover_attempts_total', 'Total proxy handover attempts')
handover_success_total = controller_counter('handover_success_total', 'Total successful proxy handovers')
handover_conflict_total = controller_counter('handover_conflict_total', 'Total conflicting proxy handovers denied')
stream_started_events_total = controller_counter('stream_started_events_total', 'Total /streams/started events', ('status', 'reason'))
stream_ended_events_total = controller_counter('stream_ended_events_total', 'Total /streams/ended events', ('status', 'reason'))
stale_ended_events_ignored_total = controller_counter('stale_ended_events_ignored_total', 'Total stale /streams/ended events ignored without cleanup', ('status', 'reason'))
idempotent_replay_total = controller_counter('idempotent_replay_total', 'Total idempotent replays', ('status', 'reason'))
stream_event_to_controller_seconds = controller_histogram('stream_event_to_controller_seconds', 'Duration of stream event controller handling in seconds', ('event',))
stream_registration_duration_seconds = controller_histogram('stream_registration_duration_seconds', 'Duration of stream registration handling in seconds')
stream_allocation_duration_seconds = controller_histogram('stream_allocation_duration_seconds', 'Duration of stream worker allocation handling in seconds')
worker_create_duration_seconds = controller_histogram('worker_create_duration_seconds', 'Duration of worker pod creation calls in seconds')
worker_ready_duration_seconds = controller_histogram('worker_ready_duration_seconds', 'Duration from worker pod creation request to first Ready observation in seconds')
stream_release_duration_seconds = controller_histogram('stream_release_duration_seconds', 'Duration of stream worker release handling in seconds')
worker_recovery_duration_seconds = controller_histogram('worker_recovery_duration_seconds', 'Duration of unhealthy worker recovery attempts in seconds')
proxy_healthcheck_duration_seconds = controller_histogram('proxy_healthcheck_duration_seconds', 'Duration of proxy healthcheck evaluation in seconds')
worker_healthcheck_duration_seconds = controller_histogram('worker_healthcheck_duration_seconds', 'Duration of worker processing readiness probes in seconds')
stream_event_to_controller_total = controller_counter('stream_event_to_controller_total', 'Total stream events handled by controller', ('event', 'status', 'reason'))
stream_registration_total = controller_counter('stream_registration_total', 'Total stream registration attempts', ('status', 'reason'))
stream_allocation_total = controller_counter('stream_allocation_total', 'Total stream allocation attempts', ('status', 'reason'))
worker_create_total = controller_counter('worker_create_total', 'Total worker pod creation attempts', ('status', 'reason'))
worker_ready_total = controller_counter('worker_ready_total', 'Total worker ready observations', ('status', 'reason'))
stream_release_total = controller_counter('stream_release_total', 'Total stream release attempts', ('status', 'reason'))
worker_recovery_total = controller_counter('worker_recovery_total', 'Total worker recovery attempts', ('status', 'reason'))
workers_deleted_total = controller_counter('workers_deleted_total', 'Total worker pod deletion attempts', ('status', 'reason'))
worker_replacements_total = controller_counter('worker_replacements_total', 'Total worker pod replacement attempts', ('status', 'reason'))
orphan_workers_deleted_total = controller_counter('orphan_workers_deleted_total', 'Total orphan worker pod deletion attempts', ('status', 'reason'))
kubernetes_api_errors_total = controller_counter('kubernetes_api_errors_total', 'Total Kubernetes API errors observed by the controller', ('operation', 'resource', 'reason'))
state_persistence_errors_total = controller_counter('state_persistence_errors_total', 'Total persisted-state read/write errors', ('operation', 'reason'))
state_persistence_duration_seconds = controller_histogram('state_persistence_duration_seconds', 'Duration of persisted-state ConfigMap operations in seconds', ('operation', 'status'))
state_recovery_total = controller_counter('state_recovery_total', 'Total controller state recovery outcomes', ('status', 'reason'))
proxy_healthcheck_total = controller_counter('proxy_healthcheck_total', 'Total proxy healthcheck evaluations', ('status', 'reason'))
worker_healthcheck_total = controller_counter('worker_healthcheck_total', 'Total worker processing readiness probes', ('status', 'reason'))
proxy_rtmp_stats_scrape_errors_total = controller_counter('proxy_rtmp_stats_scrape_errors_total', 'Total proxy RTMP /stats scrape failures', ('proxy_pod',))
proxy_rtmp_stats_discovery_errors_total = controller_counter('proxy_rtmp_stats_discovery_errors_total', 'Total failures listing proxy pods for RTMP stats scraping')
stream_lifecycle_timestamp_observed_total = controller_counter(
    'stream_lifecycle_timestamp_observed_total',
    'Total stream lifecycle timestamps observed by the controller',
    ('timestamp', 'source'),
)
stream_lifecycle_phase_seconds = controller_histogram(
    'stream_lifecycle_phase_seconds',
    'Derived stream startup phase durations from lifecycle timestamps in seconds',
    ('phase', 'start_timestamp', 'end_timestamp'),
)
stream_lifecycle_phase_observations_total = controller_counter(
    'stream_lifecycle_phase_observations_total',
    'Total derived stream lifecycle phase observations',
    ('phase', 'status', 'reason'),
)
stream_lifecycle_missing_timestamp_total = controller_counter(
    'stream_lifecycle_missing_timestamp_total',
    'Total lifecycle phase endpoints missing when stream lifecycle tracking is cleaned up',
    ('timestamp', 'phase', 'reason'),
)
stream_lifecycle_approximate_timestamp_total = controller_counter(
    'stream_lifecycle_approximate_timestamp_total',
    'Total approximate lifecycle timestamps accepted by the controller',
    ('timestamp', 'source'),
)
worker_pod_lifecycle_watch_errors_total = controller_counter(
    'worker_pod_lifecycle_watch_errors_total',
    'Total worker Pod lifecycle watch failures',
    ('status', 'reason'),
)
worker_pod_lifecycle_watch_up = controller_gauge(
    'worker_pod_lifecycle_watch_up',
    'Whether the worker Pod lifecycle watch is currently healthy',
)
worker_create_started_at: Dict[str, float] = {}
STREAM_LIFECYCLE_TIMESTAMP_FIELDS: Tuple[str, ...] = (
    't_publish_start_proxy',
    't_controller_received_event',
    't_worker_create_requested',
    't_worker_pod_created',
    't_worker_scheduled',
    't_worker_container_started',
    't_worker_ready',
    't_ffmpeg_started',
    't_ffmpeg_first_progress',
    't_destination_received',
)
STREAM_LIFECYCLE_DERIVED_PHASES: Tuple[Tuple[str, str, str], ...] = (
    ('proxy_to_controller', 't_publish_start_proxy', 't_controller_received_event'),
    ('controller_to_worker_create_request', 't_controller_received_event', 't_worker_create_requested'),
    ('worker_create_request_to_pod_created', 't_worker_create_requested', 't_worker_pod_created'),
    ('pod_created_to_scheduled', 't_worker_pod_created', 't_worker_scheduled'),
    ('scheduled_to_container_started', 't_worker_scheduled', 't_worker_container_started'),
    ('container_started_to_worker_ready', 't_worker_container_started', 't_worker_ready'),
    ('worker_ready_to_ffmpeg_started', 't_worker_ready', 't_ffmpeg_started'),
    ('ffmpeg_started_to_first_progress', 't_ffmpeg_started', 't_ffmpeg_first_progress'),
    ('ffmpeg_first_progress_to_destination', 't_ffmpeg_first_progress', 't_destination_received'),
    ('proxy_to_first_progress', 't_publish_start_proxy', 't_ffmpeg_first_progress'),
    ('controller_to_first_progress', 't_controller_received_event', 't_ffmpeg_first_progress'),
    ('proxy_to_destination', 't_publish_start_proxy', 't_destination_received'),
    ('controller_to_destination', 't_controller_received_event', 't_destination_received'),
)
stream_lifecycle_timestamps: Dict[str, Dict[int, Dict[str, Any]]] = {}
stream_lifecycle_observed_phases: Set[Tuple[str, int, str]] = set()
stream_lifecycle_pending_approximate_phases: Set[Tuple[str, int, str]] = set()
worker_lifecycle_index: Dict[str, Tuple[str, int]] = {}

try:
    config.load_incluster_config()
    logger.info("Loaded in-cluster Kubernetes config")
except:
    config.load_kube_config()
    logger.info("Loaded local kubeconfig")

apps = client.AppsV1Api()
core = client.CoreV1Api()


def update_controller_state_gauges_locked() -> None:
    """Updates controller state gauges from in-memory state.

    Must be called while holding allocation_lock when paired with a state mutation.
    """
    controller_active_streams.set(len(stream_registry))
    controller_active_allocations.set(len(stream_to_worker))


def kubernetes_api_error_reason(exc: Exception) -> str:
    """Maps Kubernetes exceptions to low-cardinality metric reasons."""
    if isinstance(exc, ApiException):
        status_reason = {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            429: "rate_limited",
        }.get(getattr(exc, "status", None))
        if status_reason:
            return status_reason
        status = getattr(exc, "status", None)
        if status and status >= 500:
            return "server_error"
        return "api_exception"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "client_exception"


def record_kubernetes_api_error(operation: str, resource: str, exc: Exception) -> None:
    kubernetes_api_errors_total.labels(
        operation=operation,
        resource=resource,
        reason=kubernetes_api_error_reason(exc),
    ).inc()


def record_worker_delete_metric(status: str, reason: str, *, orphan: bool = False) -> None:
    workers_deleted_total.labels(status=status, reason=reason).inc()
    if orphan:
        orphan_workers_deleted_total.labels(status=status, reason=reason).inc()


def log_orphan_worker_deleted(worker_pod: str, *, status: str, level: int = logging.INFO) -> None:
    # Preserve the documented orphan-deletion query contract: existing Loki
    # dashboards filter event_type="worker_deleted" with null stream context.
    log_controller_event(
        "worker_deleted",
        worker_pod=worker_pod,
        status=status,
        level=level,
    )
    log_controller_event(
        "orphan_worker_deleted",
        worker_pod=worker_pod,
        status=status,
        level=level,
    )


def timestamp_to_epoch_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def generation_to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_finite_number(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def set_stream_generation_high_water_locked(stream: str, generation: Any) -> None:
    normalized_generation = generation_to_int(generation)
    if normalized_generation is None:
        return
    stream_generation_high_water.pop(stream, None)
    stream_generation_high_water[stream] = normalized_generation


def stream_generation_high_water_entry_is_active_locked(stream: str) -> bool:
    return (
        stream in stream_generation
        or stream in stream_registry
        or stream in stream_lifecycle_timestamps
    )


def prune_stream_generation_high_water_locked() -> None:
    excess_entries = len(stream_generation_high_water) - STREAM_GENERATION_HIGH_WATER_MAX_ENTRIES
    if excess_entries <= 0:
        return
    removed_entries = 0
    for stream in list(stream_generation_high_water):
        if stream_generation_high_water_entry_is_active_locked(stream):
            continue
        stream_generation_high_water.pop(stream, None)
        removed_entries += 1
        if removed_entries >= excess_entries:
            break
    if removed_entries:
        logger.info(f"[GenerationHighWater] Pruned {removed_entries} inactive stream generation high-water entries")



def stream_terminal_session_key(stream: str, session_id: Optional[str], generation: Optional[int]) -> str:
    return f"{stream}\0{session_id or ''}\0{generation if generation is not None else ''}"


def prune_stream_terminal_sessions_locked() -> None:
    excess_entries = len(stream_terminal_sessions) - STREAM_TERMINAL_SESSION_MAX_ENTRIES
    if excess_entries <= 0:
        return
    removed_entries = 0
    for key in list(stream_terminal_sessions):
        stream_terminal_sessions.pop(key, None)
        removed_entries += 1
        if removed_entries >= excess_entries:
            break
    if removed_entries:
        logger.info(f"[TerminalSessions] Pruned {removed_entries} terminal stream session entries")


def mark_stream_terminal_locked(
    stream: str,
    proxy_pod: Optional[str],
    session_id: Optional[str],
    generation: Optional[int],
    reason: str = "streams_ended",
) -> Dict[str, Any]:
    normalized_generation = generation_to_int(generation)
    entry = {
        "stream": stream,
        "proxy_pod": proxy_pod,
        "session_id": session_id,
        "generation": normalized_generation,
        "ended_at": time.time(),
        "reason": reason,
    }
    stream_terminal_sessions[stream_terminal_session_key(stream, session_id, normalized_generation)] = entry
    prune_stream_terminal_sessions_locked()
    return entry


def lookup_stream_terminal_locked(
    stream: str,
    proxy_pod: Optional[str] = None,
    session_id: Optional[str] = None,
    generation: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    normalized_generation = generation_to_int(generation)
    candidates = []
    if session_id or normalized_generation is not None:
        candidates.append(stream_terminal_session_key(stream, session_id, normalized_generation))
    if session_id and normalized_generation is None:
        candidates.extend(
            key for key, value in stream_terminal_sessions.items()
            if value.get("stream") == stream and value.get("session_id") == session_id
        )
    for key in candidates:
        entry = stream_terminal_sessions.get(key)
        if not entry:
            continue
        if proxy_pod and entry.get("proxy_pod") and entry.get("proxy_pod") != proxy_pod:
            continue
        return dict(entry)
    return None


def stream_has_explicit_terminal_locked(stream: str, proxy_pod: Optional[str] = None) -> bool:
    for entry in stream_terminal_sessions.values():
        if entry.get("stream") != stream:
            continue
        if proxy_pod and entry.get("proxy_pod") and entry.get("proxy_pod") != proxy_pod:
            continue
        return True
    return False

def lifecycle_key_for_worker_locked(worker_pod: str) -> Optional[Tuple[str, int]]:
    indexed = worker_lifecycle_index.get(worker_pod)
    if indexed:
        return indexed
    stream = worker_to_stream.get(worker_pod)
    if not stream:
        return None
    generation = stream_generation.get(stream)
    if generation is None:
        return None
    worker_lifecycle_index[worker_pod] = (stream, generation)
    return stream, generation


def cleanup_worker_lifecycle_tracking_locked(worker_pod: Optional[str]) -> None:
    if worker_pod:
        worker_lifecycle_index.pop(worker_pod, None)


def record_missing_lifecycle_timestamps_locked(stream: str, generations: Mapping[int, Mapping[str, Any]]) -> None:
    for generation, entry in generations.items():
        for phase, start_field, end_field in STREAM_LIFECYCLE_DERIVED_PHASES:
            observed_key = (stream, generation, phase)
            if observed_key in stream_lifecycle_observed_phases:
                continue
            if timestamp_to_epoch_seconds(entry.get(start_field)) is None:
                stream_lifecycle_missing_timestamp_total.labels(
                    timestamp=start_field,
                    phase=phase,
                    reason='stream_cleanup',
                ).inc()
            if timestamp_to_epoch_seconds(entry.get(end_field)) is None:
                stream_lifecycle_missing_timestamp_total.labels(
                    timestamp=end_field,
                    phase=phase,
                    reason='stream_cleanup',
                ).inc()


def cleanup_stream_lifecycle_tracking_locked(stream: Optional[str]) -> None:
    if not stream:
        return
    generations = stream_lifecycle_timestamps.pop(stream, None)
    if generations:
        record_missing_lifecycle_timestamps_locked(stream, generations)
    stale_phase_keys = [key for key in stream_lifecycle_observed_phases if key[0] == stream]
    for key in stale_phase_keys:
        stream_lifecycle_observed_phases.discard(key)
    stale_pending_phase_keys = [key for key in stream_lifecycle_pending_approximate_phases if key[0] == stream]
    for key in stale_pending_phase_keys:
        stream_lifecycle_pending_approximate_phases.discard(key)
    stale_worker_keys = [
        worker_pod
        for worker_pod, (tracked_stream, _generation) in worker_lifecycle_index.items()
        if tracked_stream == stream
    ]
    for worker_pod in stale_worker_keys:
        worker_lifecycle_index.pop(worker_pod, None)


def remove_lifecycle_timestamp_locked(stream: str, generation: Optional[int], field: str) -> None:
    if generation is None:
        return
    entry = stream_lifecycle_timestamps.get(stream, {}).get(generation)
    if not entry:
        return
    entry.pop(field, None)
    for metadata_key in ("sources", "approximations"):
        metadata = entry.get(metadata_key)
        if isinstance(metadata, dict):
            metadata.pop(field, None)


def get_lifecycle_entry_locked(stream: str, generation: int) -> Dict[str, Any]:
    generations = stream_lifecycle_timestamps.setdefault(stream, {})
    entry = generations.setdefault(generation, {'stream': stream, 'generation': generation})
    return entry


def observe_ready_lifecycle_phases_locked(stream: str, generation: int, entry: Mapping[str, Any]) -> None:
    approximations = entry.get('approximations', {})
    for phase, start_field, end_field in STREAM_LIFECYCLE_DERIVED_PHASES:
        observed_key = (stream, generation, phase)
        if observed_key in stream_lifecycle_observed_phases:
            continue
        start_ts = timestamp_to_epoch_seconds(entry.get(start_field))
        end_ts = timestamp_to_epoch_seconds(entry.get(end_field))
        if start_ts is None or end_ts is None:
            continue
        if isinstance(approximations, Mapping) and (approximations.get(start_field) or approximations.get(end_field)):
            if observed_key not in stream_lifecycle_pending_approximate_phases:
                stream_lifecycle_phase_observations_total.labels(
                    phase=phase,
                    status='pending',
                    reason='approximate_endpoint',
                ).inc()
                stream_lifecycle_pending_approximate_phases.add(observed_key)
            continue
        duration = end_ts - start_ts
        if duration < 0:
            stream_lifecycle_phase_observations_total.labels(
                phase=phase,
                status='ignored',
                reason='negative_duration',
            ).inc()
            stream_lifecycle_observed_phases.add(observed_key)
            continue
        stream_lifecycle_phase_seconds.labels(
            phase=phase,
            start_timestamp=start_field,
            end_timestamp=end_field,
        ).observe(duration)
        stream_lifecycle_phase_observations_total.labels(
            phase=phase,
            status='observed',
            reason='complete',
        ).inc()
        stream_lifecycle_observed_phases.add(observed_key)


def record_stream_lifecycle_timestamp(
    stream: str,
    generation: Optional[int],
    field: str,
    *,
    timestamp: Optional[Any] = None,
    source: str,
    worker_pod: Optional[str] = None,
    proxy_pod: Optional[str] = None,
    overwrite: bool = False,
    approximate: bool = False,
) -> bool:
    if field not in STREAM_LIFECYCLE_TIMESTAMP_FIELDS:
        raise ValueError(f'unknown lifecycle timestamp field: {field}')
    resolved_generation = generation if generation is not None else stream_generation.get(stream)
    if resolved_generation is None:
        return False
    resolved_timestamp = timestamp_to_epoch_seconds(timestamp)
    if resolved_timestamp is None:
        resolved_timestamp = time.time()
    with allocation_lock:
        entry = get_lifecycle_entry_locked(stream, resolved_generation)
        sources = entry.setdefault('sources', {})
        approximations = entry.setdefault('approximations', {})
        existing_is_approximate = bool(approximations.get(field))
        if field in entry and not overwrite and not (existing_is_approximate and not approximate):
            return False
        entry[field] = resolved_timestamp
        sources[field] = source
        approximations[field] = bool(approximate)
        if worker_pod:
            entry['worker_pod'] = worker_pod
            worker_lifecycle_index[worker_pod] = (stream, resolved_generation)
        if proxy_pod:
            entry['proxy_pod'] = proxy_pod
        observe_ready_lifecycle_phases_locked(stream, resolved_generation, entry)
    stream_lifecycle_timestamp_observed_total.labels(timestamp=field, source=source).inc()
    if approximate:
        stream_lifecycle_approximate_timestamp_total.labels(timestamp=field, source=source).inc()
    log_controller_event(
        'stream_lifecycle_timestamp_observed',
        stream=stream,
        generation=resolved_generation,
        proxy_pod=proxy_pod,
        worker_pod=worker_pod,
        status=LOG_STATUS_OBSERVED,
        message=f'{field} observed from {source}',
    )
    return True


def get_pod_annotation(pod: Any, key: str) -> Optional[str]:
    metadata = getattr(pod, 'metadata', None)
    annotations = getattr(metadata, 'annotations', None) or {}
    value = annotations.get(key)
    return str(value) if value is not None else None


def stream_generation_from_worker_pod(pod: Any) -> Optional[Tuple[str, int]]:
    metadata = getattr(pod, 'metadata', None)
    pod_name = getattr(metadata, 'name', None) if metadata else None
    stream = get_pod_annotation(pod, 'liveedgecast.io/stream')
    raw_generation = get_pod_annotation(pod, 'liveedgecast.io/generation')
    if stream and raw_generation:
        try:
            generation = int(raw_generation)
        except ValueError:
            logger.warning(f"[WorkerPodEvents] Invalid generation annotation on pod '{pod_name}': {raw_generation}")
        else:
            with allocation_lock:
                indexed = worker_lifecycle_index.get(pod_name) if pod_name else None
                current_worker = stream_to_worker.get(stream)
                current_generation = stream_generation.get(stream)
                if indexed == (stream, generation) or (
                    pod_name == current_worker and current_generation == generation
                ):
                    return stream, generation
            logger.debug(
                f"[WorkerPodEvents] Ignoring stale pod event for pod '{pod_name}' "
                f"stream='{stream}' generation='{generation}'"
            )
            return None
    if pod_name:
        with allocation_lock:
            return lifecycle_key_for_worker_locked(pod_name)
    return None


def condition_transition_time(pod: Any, condition_type: str, status: str = 'True') -> Optional[float]:
    pod_status = getattr(pod, 'status', None)
    for condition in getattr(pod_status, 'conditions', None) or []:
        if getattr(condition, 'type', None) == condition_type and getattr(condition, 'status', None) == status:
            return timestamp_to_epoch_seconds(getattr(condition, 'last_transition_time', None))
    return None


def first_container_started_time(pod: Any) -> Optional[float]:
    pod_status = getattr(pod, 'status', None)
    started_times: List[float] = []
    for container_status in getattr(pod_status, 'container_statuses', None) or []:
        state = getattr(container_status, 'state', None)
        running = getattr(state, 'running', None) if state else None
        started_at = timestamp_to_epoch_seconds(getattr(running, 'started_at', None) if running else None)
        if started_at is not None:
            started_times.append(started_at)
    return min(started_times) if started_times else None


def process_worker_pod_event(event: Mapping[str, Any]) -> None:
    pod = event.get('object')
    if not pod:
        return
    metadata = getattr(pod, 'metadata', None)
    pod_name = getattr(metadata, 'name', None) if metadata else None
    if not pod_name:
        return
    key = stream_generation_from_worker_pod(pod)
    if not key:
        return
    stream, generation = key
    proxy_pod = get_pod_annotation(pod, 'liveedgecast.io/proxy-pod') or get_stream_proxy_pod(stream)
    created_at = timestamp_to_epoch_seconds(getattr(metadata, 'creation_timestamp', None))
    if created_at is not None:
        record_stream_lifecycle_timestamp(
            stream, generation, 't_worker_pod_created', timestamp=created_at,
            source='kubernetes_pod_metadata', worker_pod=pod_name, proxy_pod=proxy_pod,
        )
    scheduled_at = condition_transition_time(pod, 'PodScheduled')
    if scheduled_at is not None:
        record_stream_lifecycle_timestamp(
            stream, generation, 't_worker_scheduled', timestamp=scheduled_at,
            source='kubernetes_pod_condition', worker_pod=pod_name, proxy_pod=proxy_pod,
        )
    container_started_at = first_container_started_time(pod)
    if container_started_at is not None:
        record_stream_lifecycle_timestamp(
            stream, generation, 't_worker_container_started', timestamp=container_started_at,
            source='kubernetes_container_status', worker_pod=pod_name, proxy_pod=proxy_pod,
        )
    ready_at = condition_transition_time(pod, 'Ready')
    if ready_at is not None:
        record_stream_lifecycle_timestamp(
            stream, generation, 't_worker_ready', timestamp=ready_at,
            source='kubernetes_pod_condition', worker_pod=pod_name, proxy_pod=proxy_pod,
        )


def collect_worker_pod_lifecycle_events_once(timeout_seconds: Optional[int] = None) -> None:
    if timeout_seconds is None:
        timeout_seconds = WORKER_POD_LIFECYCLE_WATCH_TIMEOUT_SECONDS
    watcher = watch.Watch()
    worker_pod_lifecycle_watch_up.set(1)
    for event in watcher.stream(
        core.list_namespaced_pod,
        namespace=NAMESPACE,
        label_selector='app=worker',
        timeout_seconds=timeout_seconds,
    ):
        try:
            process_worker_pod_event(event)
        except Exception as e:
            worker_pod_lifecycle_watch_errors_total.labels(
                status='event_processing_error',
                reason=type(e).__name__,
            ).inc()
            logger.warning(f"[WorkerPodEvents] Failed processing worker pod event: {e}")


async def collect_worker_pod_lifecycle_events() -> None:
    while True:
        try:
            await asyncio.to_thread(collect_worker_pod_lifecycle_events_once)
        except ApiException as e:
            record_kubernetes_api_error("watch", "pod", e)
            worker_pod_lifecycle_watch_up.set(0)
            worker_pod_lifecycle_watch_errors_total.labels(
                status='watch_error',
                reason=type(e).__name__,
            ).inc()
            logger.warning(f"[WorkerPodEvents] Watch failed; retrying: {e}")
            await asyncio.sleep(5)
        except Exception as e:
            worker_pod_lifecycle_watch_up.set(0)
            worker_pod_lifecycle_watch_errors_total.labels(
                status='watch_error',
                reason=type(e).__name__,
            ).inc()
            logger.warning(f"[WorkerPodEvents] Watch failed; retrying: {e}")
            await asyncio.sleep(5)


def record_worker_progress_event(stream: str, worker_pod: str, field: str, source: str) -> Dict[str, Any]:
    with allocation_lock:
        key = lifecycle_key_for_worker_locked(worker_pod)
        allocated_worker = stream_to_worker.get(stream)
        if key is None or key[0] != stream:
            return {
                'status': 'ignored',
                'reason': 'unmapped_worker',
                'stream': stream,
                'worker_pod': worker_pod,
                'timestamp': field,
            }
        if allocated_worker and allocated_worker != worker_pod:
            return {
                'status': 'ignored',
                'reason': 'stale_worker',
                'stream': stream,
                'worker_pod': worker_pod,
                'timestamp': field,
            }
        generation = key[1]
        proxy_pod = stream_registry.get(stream, {}).get("proxy_pod")
        observed = record_stream_lifecycle_timestamp(
            stream,
            generation,
            field,
            source=source,
            worker_pod=worker_pod,
            proxy_pod=proxy_pod,
        )
    if observed and field in ("t_ffmpeg_started", "t_ffmpeg_first_progress"):
        log_controller_event(
            "ffmpeg_started" if field == "t_ffmpeg_started" else "ffmpeg_first_progress",
            stream=stream,
            generation=generation,
            proxy_pod=proxy_pod,
            worker_pod=worker_pod,
            status=LOG_STATUS_OBSERVED,
            message=f"{field} observed from {source}",
        )
    return {
        'status': 'observed' if observed else 'duplicate',
        'reason': 'recorded' if observed else 'already_observed',
        'stream': stream,
        'worker_pod': worker_pod,
        'timestamp': field,
    }


def persist_state_locked() -> None:
    """
    Persists critical controller state to a ConfigMap to survive pod restart/crash.
    Must be called only while holding allocation_lock.
    """
    started_at = time.monotonic()
    prune_stream_generation_high_water_locked()
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "stream_to_worker": stream_to_worker,
        "worker_to_stream": worker_to_stream,
        "stream_to_proxy": stream_to_proxy,
        "stream_registry": stream_registry,
        "stream_generation": stream_generation,
        "stream_generation_high_water": stream_generation_high_water,
        "stream_terminal_sessions": stream_terminal_sessions,
    }
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=STATE_CONFIGMAP_NAME, namespace=NAMESPACE),
        data={STATE_CONFIGMAP_KEY: json.dumps(payload)}
    )
    update_controller_state_gauges_locked()
    try:
        core.patch_namespaced_config_map(
            name=STATE_CONFIGMAP_NAME,
            namespace=NAMESPACE,
            body=body
        )
        state_persistence_duration_seconds.labels(operation="persist", status="success").observe(time.monotonic() - started_at)
    except ApiException as e:
        if e.status == 404:
            try:
                core.create_namespaced_config_map(namespace=NAMESPACE, body=body)
                state_persistence_duration_seconds.labels(operation="persist", status="success").observe(time.monotonic() - started_at)
            except ApiException as create_error:
                state_persistence_duration_seconds.labels(operation="persist", status="failed").observe(time.monotonic() - started_at)
                record_kubernetes_api_error("create", "configmap", create_error)
                state_persistence_errors_total.labels(operation="persist", reason="create_failed").inc()
                log_controller_event(
                    "state_persistence_failed",
                    started_at=started_at,
                    status=LOG_STATUS_FAILED,
                    level=logging.WARNING,
                    message="state persistence create failed",
                )
                raise
        else:
            state_persistence_duration_seconds.labels(operation="persist", status="failed").observe(time.monotonic() - started_at)
            record_kubernetes_api_error("patch", "configmap", e)
            state_persistence_errors_total.labels(operation="persist", reason="patch_failed").inc()
            log_controller_event(
                "state_persistence_failed",
                started_at=started_at,
                status=LOG_STATUS_FAILED,
                level=logging.WARNING,
                message="state persistence patch failed",
            )
            raise


def restore_persisted_state_locked() -> StateRestoreResult:
    """
    Restores persisted state from the ConfigMap.
    Returns a low-cardinality result reason for recovery metrics.
    Must be called only while holding allocation_lock.
    """
    try:
        cm = core.read_namespaced_config_map(name=STATE_CONFIGMAP_NAME, namespace=NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return StateRestoreResult(restored=False, reason="not_found")
        record_kubernetes_api_error("read", "configmap", e)
        state_persistence_errors_total.labels(operation="restore", reason="read_failed").inc()
        logger.warning(f"[State Recovery] Failed to read state ConfigMap: {e}")
        return StateRestoreResult(restored=False, reason="read_failed")

    raw = (cm.data or {}).get(STATE_CONFIGMAP_KEY)
    if not raw:
        return StateRestoreResult(restored=False, reason="empty")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        state_persistence_errors_total.labels(operation="restore", reason="invalid_json").inc()
        logger.warning("[State Recovery] Invalid JSON in persisted state, ignoring.")
        return StateRestoreResult(restored=False, reason="invalid_json")

    stream_to_worker.clear()
    stream_to_worker.update(data.get("stream_to_worker", {}))
    worker_to_stream.clear()
    worker_to_stream.update(data.get("worker_to_stream", {}))
    stream_to_proxy.clear()
    stream_to_proxy.update(data.get("stream_to_proxy", {}))
    stream_registry.clear()
    stream_registry.update(data.get("stream_registry", {}))
    stream_generation.clear()
    restored_generation = data.get("stream_generation", {})
    if isinstance(restored_generation, dict):
        for stream, generation in restored_generation.items():
            normalized_generation = generation_to_int(generation)
            if normalized_generation is None:
                state_persistence_errors_total.labels(operation="restore", reason="invalid_generation").inc()
                logger.warning(f"[State Recovery] Invalid stream generation for '{stream}': {generation}")
                continue
            stream_generation[stream] = normalized_generation

    stream_generation_high_water.clear()
    restored_high_water = data.get("stream_generation_high_water", {})
    if isinstance(restored_high_water, dict):
        for stream, generation in restored_high_water.items():
            normalized_generation = generation_to_int(generation)
            if normalized_generation is None:
                state_persistence_errors_total.labels(operation="restore", reason="invalid_generation_high_water").inc()
                logger.warning(f"[State Recovery] Invalid stream generation high-water for '{stream}': {generation}")
                continue
            set_stream_generation_high_water_locked(stream, normalized_generation)

    stream_terminal_sessions.clear()
    restored_terminal_sessions = data.get("stream_terminal_sessions", {})
    if isinstance(restored_terminal_sessions, dict):
        for key, value in restored_terminal_sessions.items():
            if isinstance(value, dict):
                stream_terminal_sessions[str(key)] = value
    prune_stream_terminal_sessions_locked()

    for stream, generation in list(stream_generation.items()):
        set_stream_generation_high_water_locked(
            stream,
            max(
                generation_to_int(stream_generation_high_water.get(stream)) or 0,
                generation,
            ),
        )
        if stream not in stream_registry:
            logger.warning(
                f"[State Recovery] Dropping registry-less generation for stream '{stream}' "
                f"after preserving high-water generation {stream_generation_high_water[stream]}"
            )
            stream_generation.pop(stream, None)
    prune_stream_generation_high_water_locked()
    update_controller_state_gauges_locked()
    return StateRestoreResult(restored=True, reason="restored")


def random_suffix():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))


K8S_NAME_FRAGMENT_MAX_LENGTH = 40


def k8s_name_fragment(value: str, max_len: int = K8S_NAME_FRAGMENT_MAX_LENGTH) -> str:
    """Return a DNS-1123-safe, collision-resistant fragment for Kubernetes names.

    Stream keys are publisher-controlled input. They may contain characters that
    are invalid in Kubernetes object names, so never embed the raw stream key in
    Pod names. The hash preserves uniqueness when two distinct stream keys
    normalize to the same visible prefix.
    """
    raw = value or "stream"
    normalized = re.sub(r"[^a-z0-9-]+", "-", raw.lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-") or "stream"
    suffix = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    prefix_budget = max(1, max_len - len(suffix) - 1)
    prefix = normalized[:prefix_budget].strip("-") or "stream"
    return f"{prefix}-{suffix}"




def create_worker_pod_for_stream(
    stream: str,
    proxy_dns: str,
    proxy_pod: Optional[str] = None,
    generation: Optional[int] = None,
    session_id: Optional[str] = None,
) -> str:
    """
    Cria Pod por stream reaproveitando o template do worker Deployment.

    STREAM_KEY identifica a publicação original. PROXY_DNS é o endereço usado
    pelo FFmpeg para consumir o RTMP no proxy. PROXY_POD, SESSION_ID e
    STREAM_GENERATION são contexto de controle usado para validar lifecycle no
    controller sem confundir endereço/IP com identidade do Pod proxy.
    """
    started_at = time.monotonic()
    metric_status = "error"
    metric_reason = "exception"
    try:
        pod_name = f"worker-{k8s_name_fragment(stream)}-{random_suffix()}"

        try:
            deployment = apps.read_namespaced_deployment(name=WORKER_DEPLOYMENT, namespace=NAMESPACE)
        except ApiException as e:
            record_kubernetes_api_error("read", "deployment", e)
            raise
        template = deployment.spec.template
        if not template or not template.spec or not template.spec.containers:
            metric_reason = "invalid_template"
            raise RuntimeError("worker deployment template is invalid or has no containers")

        pod_spec = copy.deepcopy(template.spec)
        pod_metadata = copy.deepcopy(template.metadata) if template.metadata else client.V1ObjectMeta()

        pod_spec.restart_policy = os.getenv("WORKER_POD_RESTART_POLICY", "Never")

        for c in pod_spec.containers:
            env = list(c.env or [])
            env = [
                e for e in env
                if e.name not in (
                    "STREAM_KEY",
                    "PROXY_DNS",
                    "PROXY_POD",
                    "SESSION_ID",
                    "STREAM_GENERATION",
                    "CONTROLLER_API",
                    "WORKER_POD",
                )
            ]
            generation_value = str(generation if generation is not None else stream_generation.get(stream, 1))
            owner_proxy_pod = str(proxy_pod if proxy_pod is not None else (get_stream_proxy_pod(stream) or ""))
            session_id_value = str(session_id if session_id is not None else ((stream_registry.get(stream) or {}).get("session_id") or ""))
            env.append(client.V1EnvVar(name="STREAM_KEY", value=stream))
            env.append(client.V1EnvVar(name="PROXY_DNS", value=proxy_dns))
            env.append(client.V1EnvVar(name="PROXY_POD", value=owner_proxy_pod))
            env.append(client.V1EnvVar(name="SESSION_ID", value=session_id_value))
            env.append(client.V1EnvVar(name="STREAM_GENERATION", value=generation_value))
            env.append(client.V1EnvVar(name="CONTROLLER_API", value=WORKER_CONTROLLER_API))
            env.append(client.V1EnvVar(name="WORKER_POD", value=pod_name))
            c.env = env

        labels = dict(pod_metadata.labels or {})
        labels.update({"app": "worker"})
        annotations = dict(getattr(pod_metadata, "annotations", None) or {})
        annotations.update({
            "liveedgecast.io/stream": stream,
            "liveedgecast.io/generation": generation_value,
            "liveedgecast.io/proxy-pod": owner_proxy_pod,
            "liveedgecast.io/session-id": session_id_value,
        })

        logger.debug(
            f"[Worker Pod Create] pod_name='{pod_name}' stream='{stream}' proxy_dns='{proxy_dns}'"
        )

        pod_manifest = client.V1Pod(
            metadata=client.V1ObjectMeta(name=pod_name, namespace=NAMESPACE, labels=labels, annotations=annotations),
            spec=pod_spec,
        )

        generation = generation_to_int(generation_value) or 1
        proxy_pod = owner_proxy_pod
        create_request_recorded = record_stream_lifecycle_timestamp(
            stream, generation, "t_worker_create_requested",
            timestamp=time.time(), source="controller", worker_pod=pod_name, proxy_pod=proxy_pod,
        )
        log_controller_event(
            "worker_create_requested",
            stream=stream,
            generation=generation,
            proxy_pod=proxy_pod,
            worker_pod=pod_name,
            status=LOG_STATUS_REQUESTED,
        )
        try:
            created_pod = core.create_namespaced_pod(namespace=NAMESPACE, body=pod_manifest)
        except ApiException as e:
            record_kubernetes_api_error("create", "pod", e)
            raise
        with allocation_lock:
            worker_create_started_at[pod_name] = started_at
            if generation is not None:
                worker_lifecycle_index[pod_name] = (stream, generation)
        created_timestamp = getattr(getattr(created_pod, "metadata", None), "creation_timestamp", None)
        record_stream_lifecycle_timestamp(
            stream, generation, "t_worker_pod_created",
            timestamp=created_timestamp or time.time(), source="kubernetes_create_response",
            worker_pod=pod_name, proxy_pod=proxy_pod, approximate=created_timestamp is None,
        )
        metric_status = "success"
        metric_reason = "created"
        log_controller_event(
            "worker_created",
            stream=stream,
            proxy_pod=proxy_pod,
            worker_pod=pod_name,
            started_at=started_at,
            status=LOG_STATUS_CREATED,
        )
        return pod_name
    except Exception as e:
        with allocation_lock:
            failed_pod_name = locals().get("pod_name")
            failed_generation = locals().get("generation")
            cleanup_worker_lifecycle_tracking_locked(failed_pod_name)
            if locals().get("create_request_recorded"):
                remove_lifecycle_timestamp_locked(stream, failed_generation, "t_worker_create_requested")
                failed_entry = stream_lifecycle_timestamps.get(stream, {}).get(failed_generation)
                if failed_entry and failed_entry.get("worker_pod") == failed_pod_name:
                    failed_entry.pop("worker_pod", None)
        if metric_reason == "exception":
            metric_reason = type(e).__name__
        raise
    finally:
        worker_create_duration_seconds.observe(time.monotonic() - started_at)
        worker_create_total.labels(status=metric_status, reason=metric_reason).inc()


def replace_worker_pod_for_stream_locked(stream: str, proxy_dns: str) -> Optional[str]:
    """Recria o worker da stream para aplicar novo PROXY_DNS (env imutável em Pod existente)."""
    replacement_status = "error"
    replacement_reason = "exception"
    old_worker = stream_to_worker.get(stream)
    if not old_worker:
        worker_replacements_total.labels(status="skipped", reason="worker_not_found").inc()
        return None

    try:
        new_worker = create_worker_pod_for_stream(
            stream=stream,
            proxy_dns=proxy_dns,
            proxy_pod=get_stream_proxy_pod(stream) or "",
            generation=generation_to_int(stream_generation.get(stream)) or 1,
            session_id=(stream_registry.get(stream) or {}).get("session_id"),
        )
    except Exception as e:
        worker_replacements_total.labels(status="error", reason="create_failed").inc()
        raise
    stream_to_worker[stream] = new_worker
    worker_to_stream.pop(old_worker, None)
    cleanup_worker_lifecycle_tracking_locked(old_worker)
    worker_to_stream[new_worker] = stream
    worker_ready_since.pop(old_worker, None)
    worker_create_started_at.pop(old_worker, None)
    old_uid = worker_pod_uid_by_name.pop(old_worker, None)
    if old_uid:
        worker_health_failures.pop(old_uid, None)

    try:
        core.delete_namespaced_pod(name=old_worker, namespace=NAMESPACE, grace_period_seconds=0)
        record_worker_delete_metric("success", "replaced")
        log_controller_event(
            "worker_deleted",
            stream=stream,
            proxy_pod=get_stream_proxy_pod(stream),
            worker_pod=old_worker,
            status=LOG_STATUS_DELETED,
        )
    except ApiException as e:
        record_kubernetes_api_error("delete", "pod", e)
        record_worker_delete_metric("warning", "delete_failed")
        log_controller_event(
            "worker_deleted",
            stream=stream,
            proxy_pod=get_stream_proxy_pod(stream),
            worker_pod=old_worker,
            status=LOG_STATUS_DELETE_FAILED,
            level=logging.WARNING,
        )
        logger.warning(f"[Handover] Failed deleting old worker pod {old_worker}: {e}")

    update_controller_state_gauges_locked()
    replacement_status = "success"
    replacement_reason = "replaced"
    worker_replacements_total.labels(status=replacement_status, reason=replacement_reason).inc()
    log_controller_event(
        "worker_replaced",
        stream=stream,
        proxy_pod=get_stream_proxy_pod(stream),
        worker_pod=new_worker,
        status=LOG_STATUS_SUCCESS,
        message=f"worker replaced old='{old_worker}' new='{new_worker}'",
    )

    logger.info(
        f"[Handover] Replaced worker pod for stream '{stream}' due to proxy change: "
        f"old='{old_worker}' new='{new_worker}' proxy_dns='{proxy_dns}'"
    )
    return new_worker

def delete_worker_pod_best_effort(worker_name: str, stream: Optional[str], proxy_pod: Optional[str], reason: str) -> None:
    """Delete a worker Pod outside allocation_lock and record best-effort evidence."""
    if not worker_name:
        return
    try:
        core.delete_namespaced_pod(name=worker_name, namespace=NAMESPACE, grace_period_seconds=0)
        record_worker_delete_metric("success", reason)
        log_controller_event(
            "worker_deleted",
            stream=stream,
            proxy_pod=proxy_pod,
            worker_pod=worker_name,
            status=LOG_STATUS_DELETED,
            message=f"deleted worker pod reason={reason}",
        )
    except ApiException as e:
        record_kubernetes_api_error("delete", "pod", e)
        if e.status == 404:
            record_worker_delete_metric("success", f"{reason}_already_deleted")
            log_controller_event(
                "worker_deleted",
                stream=stream,
                proxy_pod=proxy_pod,
                worker_pod=worker_name,
                status=LOG_STATUS_ALREADY_DELETED,
                message=f"worker pod already deleted reason={reason}",
            )
        else:
            record_worker_delete_metric("warning", f"{reason}_delete_failed")
            log_controller_event(
                "worker_deleted",
                stream=stream,
                proxy_pod=proxy_pod,
                worker_pod=worker_name,
                status=LOG_STATUS_DELETE_FAILED,
                level=logging.WARNING,
                message=f"failed deleting worker pod reason={reason}",
            )
            logger.warning(f"[WorkerDelete] Failed deleting worker '{worker_name}' reason='{reason}': {e}")
    except Exception as e:
        record_worker_delete_metric("warning", f"{reason}_delete_failed")
        log_controller_event(
            "worker_deleted",
            stream=stream,
            proxy_pod=proxy_pod,
            worker_pod=worker_name,
            status=LOG_STATUS_DELETE_FAILED,
            level=logging.WARNING,
            message=f"failed deleting worker pod reason={reason}",
        )
        logger.warning(f"[WorkerDelete] Failed deleting worker '{worker_name}' reason='{reason}': {e}")


def register_or_refresh_stream(
    stream: str,
    proxy_pod: str,
    session_id: Optional[str] = None,
    publish_start_ts: Optional[float] = None,
    force_new_generation: bool = False,
) -> Optional[str]:
    """Create or refresh canonical stream ownership on a proxy.

    Must be called while holding allocation_lock. Returns the stale worker Pod
    that should be deleted *after* the lock is released when a new RTMP session
    replaces an older session for the same stream key.

    Optional lifecycle fields are preserved when omitted. A partial callback or
    same-owner refresh must not erase session/publish metadata that was already
    observed for the active stream.
    """
    current = stream_registry.get(stream)
    current_session = current.get("session_id") if current else None
    previous_owner = current.get("proxy_pod") if current else None
    owner_changed = bool(current and previous_owner != proxy_pod)
    # A provided session_id is authoritative. If a current stream exists with a
    # different or missing session_id, treat it as a new RTMP session unless an
    # explicit legacy compatibility flag opts into reuse.
    session_changed = bool(
        session_id
        and current
        and current_session != session_id
        and not (ALLOW_LEGACY_SESSION_REUSE and not current_session)
    )
    old_worker_to_delete: Optional[str] = None
    if session_changed:
        old_worker = stream_to_worker.pop(stream, None)
        if old_worker:
            old_worker_to_delete = old_worker
            worker_to_stream.pop(old_worker, None)
            cleanup_worker_lifecycle_tracking_locked(old_worker)
            worker_ready_since.pop(old_worker, None)
            worker_not_ready_since.pop(old_worker, None)
            worker_create_started_at.pop(old_worker, None)
            old_uid = worker_pod_uid_by_name.pop(old_worker, None)
            if old_uid:
                worker_health_failures.pop(old_uid, None)
        log_controller_event(
            "stream_session_replaced",
            stream=stream,
            proxy_pod=proxy_pod,
            status=LOG_STATUS_SUCCESS,
            message=f"new RTMP session replaced previous session_id='{current_session or '<legacy/missing>'}'",
        )
    if stream not in stream_generation or stream not in stream_registry or session_changed or force_new_generation:
        previous_generations = stream_lifecycle_timestamps.get(stream, {})
        previous_lifecycle_generation = max(previous_generations) if previous_generations else 0
        previous_high_water_generation = generation_to_int(stream_generation_high_water.get(stream)) or 0
        previous_active_generation = generation_to_int(stream_generation.get(stream)) or 0
        stream_generation[stream] = max(
            previous_lifecycle_generation,
            previous_high_water_generation,
            previous_active_generation,
        ) + 1
    set_stream_generation_high_water_locked(
        stream,
        max(
            generation_to_int(stream_generation_high_water.get(stream)) or 0,
            stream_generation[stream],
        ),
    )
    prune_stream_generation_high_water_locked()
    registry_entry = dict(current or {})
    registry_entry["proxy_pod"] = proxy_pod
    if session_id:
        registry_entry["session_id"] = session_id
    # If session_id is omitted, keep the active session_id instead of degrading a
    # session-scoped stream to legacy state during a retry or rolling upgrade.
    if publish_start_ts is not None:
        registry_entry["publish_start_ts"] = str(publish_start_ts)
    stream_registry[stream] = registry_entry
    stream_to_proxy[stream] = proxy_pod
    proxy_health_failures[proxy_pod] = 0

    if owner_changed and force_new_generation:
        log_controller_event(
            "handover_accepted",
            stream=stream,
            generation=stream_generation.get(stream),
            proxy_pod=proxy_pod,
            status=LOG_STATUS_ACCEPTED,
            message=f"stream owner changed from '{previous_owner}' to '{proxy_pod}'",
        )
        handover_success_total.inc()
        stream_proxy_handover_counter.inc()

    return old_worker_to_delete


def try_handover_stream_owner(stream: str, candidate_proxy_pod: str) -> bool:
    """Evaluate whether a stream may be owned by candidate_proxy_pod.

    This function is intentionally decision-only: it must not mutate stream
    registry/session state or create/delete Kubernetes Pods. The caller performs
    the final registration with the full session/generation context.
    """
    with allocation_lock:
        return _try_handover_stream_owner_locked(stream, candidate_proxy_pod)


def _try_handover_stream_owner_locked(stream: str, candidate_proxy_pod: str) -> bool:
    """
    Ownership rule with safe handover. Must be called while holding allocation_lock.
    - no owner: candidate may register the stream
    - idempotent: current owner may refresh the stream
    - handover allowed only if previous owner is unhealthy/dead

    The function is side-effect-light: it records denial evidence but does not
    mutate canonical ownership or perform Kubernetes I/O. This avoids losing
    session context or creating replacement workers while holding allocation_lock.
    """
    handover_attempts_total.inc()
    current = stream_registry.get(stream)
    if not current:
        return True

    current_owner = current.get("proxy_pod")
    if current_owner == candidate_proxy_pod:
        return True

    owner_unhealthy = proxy_health_failures.get(current_owner, 0) >= PROXY_HEALTHCHECK_MAX_FAILURES
    if not owner_unhealthy:
        owner_unhealthy = get_proxy_health_status(current_owner) == "unhealthy"

    if owner_unhealthy:
        logger.info(
            f"[Handover] Stream '{stream}' ownership may move from '{current_owner}' "
            f"to '{candidate_proxy_pod}' (owner_unhealthy={owner_unhealthy})"
        )
        return True

    handover_conflict_total.inc()
    logger.warning(
        f"[Handover] Denied ownership change for stream '{stream}' from '{current_owner}' "
        f"to '{candidate_proxy_pod}' (owner_unhealthy={owner_unhealthy})"
    )
    log_controller_event(
        "handover_denied",
        stream=stream,
        generation=stream_generation.get(stream),
        proxy_pod=candidate_proxy_pod,
        status=LOG_STATUS_DENIED,
        level=logging.WARNING,
    )
    return False



async def monitor_stream_registry_health():
    """
    Controller-driven health monitoring:
    - A cada 3s verifica /health de cada proxy com stream ativa
    - Após 3 falhas consecutivas, expira todas as streams daquele proxy
    """
    semaphore = asyncio.Semaphore(PROXY_HEALTHCHECK_MAX_CONCURRENCY)

    async def run_proxy_check(proxy_pod: str):
        if PROXY_HEALTHCHECK_JITTER_SECONDS > 0:
            await asyncio.sleep(random.uniform(0, PROXY_HEALTHCHECK_JITTER_SECONDS))

        async with semaphore:
            health_status = await asyncio.to_thread(get_proxy_health_status, proxy_pod)

        with allocation_lock:
            if health_status == "healthy":
                proxy_health_failures[proxy_pod] = 0
            elif health_status == "warming_up":
                logger.debug(
                    f"[ProxyHealth] Proxy '{proxy_pod}' is warming up; "
                    "waiting before counting /health probe failures."
                )
            else:
                failures = proxy_health_failures.get(proxy_pod, 0) + 1
                proxy_health_failures[proxy_pod] = failures
                logger.warning(
                    f"[ProxyHealth] Proxy '{proxy_pod}' failed healthcheck "
                    f"({failures}/{PROXY_HEALTHCHECK_MAX_FAILURES})"
                )

                if failures >= PROXY_HEALTHCHECK_MAX_FAILURES:
                    impacted_streams = [
                        stream for stream, entry in stream_registry.items()
                        if entry.get("proxy_pod") == proxy_pod
                    ]
                    for stream in impacted_streams:
                        failed_generation = stream_generation.get(stream)
                        log_controller_event(
                            "proxy_failure_detected",
                            stream=stream,
                            generation=failed_generation,
                            proxy_pod=proxy_pod,
                            status="unhealthy",
                            level=logging.WARNING,
                        )
                        stream_registry.pop(stream, None)
                        stream_to_proxy.pop(stream, None)
                        stream_generation.pop(stream, None)
                        cleanup_stream_lifecycle_tracking_locked(stream)
                        logger.info(
                            f"[Registry] Stream '{stream}' expired after "
                            f"{PROXY_HEALTHCHECK_MAX_FAILURES} failed proxy healthchecks"
                        )
                        worker_name = stream_to_worker.pop(stream, None)
                        if worker_name:
                            worker_to_stream.pop(worker_name, None)
                            cleanup_worker_lifecycle_tracking_locked(worker_name)
                            worker_ready_since.pop(worker_name, None)
                            old_uid = worker_pod_uid_by_name.pop(worker_name, None)
                            if old_uid:
                                worker_health_failures.pop(old_uid, None)
                            try:
                                core.delete_namespaced_pod(name=worker_name, namespace=NAMESPACE, grace_period_seconds=0)
                                record_worker_delete_metric("success", "proxy_unhealthy")
                                log_controller_event(
                                    "worker_deleted",
                                    stream=stream,
                                    proxy_pod=proxy_pod,
                                    worker_pod=worker_name,
                                    status=LOG_STATUS_DELETED,
                                )
                            except ApiException as e:
                                record_kubernetes_api_error("delete", "pod", e)
                                record_worker_delete_metric("warning", "delete_failed")
                                log_controller_event(
                                    "worker_deleted",
                                    stream=stream,
                                    proxy_pod=proxy_pod,
                                    worker_pod=worker_name,
                                    status=LOG_STATUS_DELETE_FAILED,
                                    level=logging.WARNING,
                                )
                                logger.warning(f"[ProxyHealth] Failed deleting worker {worker_name}: {e}")
                            except Exception as e:
                                record_worker_delete_metric("warning", "delete_failed")
                                log_controller_event(
                                    "worker_deleted",
                                    stream=stream,
                                    proxy_pod=proxy_pod,
                                    worker_pod=worker_name,
                                    status=LOG_STATUS_DELETE_FAILED,
                                    level=logging.WARNING,
                                )
                                logger.warning(f"[ProxyHealth] Failed deleting worker {worker_name}: {e}")

                    update_controller_state_gauges_locked()
                    proxy_health_failures.pop(proxy_pod, None)
                    proxy_ready_since.pop(proxy_pod, None)
                    persist_state_locked()

    while True:
        await asyncio.sleep(PROXY_HEALTHCHECK_INTERVAL_SECONDS)

        with allocation_lock:
            proxies = {entry.get("proxy_pod") for entry in stream_registry.values() if entry.get("proxy_pod")}

        if proxies:
            await asyncio.gather(*(run_proxy_check(proxy_pod) for proxy_pod in proxies))

def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _json_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _collect_json_values(node: Any, target_key: str) -> List[Any]:
    matches: List[Any] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() == target_key:
                matches.extend(_json_list(value))
            matches.extend(_collect_json_values(value, target_key))
    elif isinstance(node, list):
        for item in node:
            matches.extend(_collect_json_values(item, target_key))
    return matches


def _json_has_truthy_key(node: Any, names: Iterable[str]) -> bool:
    wanted = {name.lower() for name in names}
    if not isinstance(node, dict):
        return False
    for key, value in node.items():
        if str(key).lower() in wanted:
            if isinstance(value, bool):
                return value
            if value is None:
                return False
            if isinstance(value, (dict, list)):
                return True
            return str(value).strip().lower() not in ("", "0", "false", "no")
    return False


def parse_proxy_rtmp_stats_xml(payload: str) -> ProxyRtmpStats:
    root = ET.fromstring(payload)
    streams = [element for element in root.iter() if _xml_local_name(element.tag) == "stream"]
    stream_names = frozenset(
        (child.text or "").strip()
        for stream_element in streams
        for child in list(stream_element)
        if _xml_local_name(child.tag) == "name" and (child.text or "").strip()
    )
    clients = [element for element in root.iter() if _xml_local_name(element.tag) == "client"]
    publishers = [
        client_element
        for client_element in clients
        if any(_xml_local_name(child.tag) == "publishing" for child in list(client_element))
        or str(client_element.attrib.get("publishing", "")).strip().lower() in ("1", "true", "yes")
    ]
    return ProxyRtmpStats(
        active_streams=len(streams),
        active_publishers=len(publishers),
        active_clients=len(clients),
        stream_names=stream_names,
    )


def parse_proxy_rtmp_stats_json(payload: str) -> ProxyRtmpStats:
    data = json.loads(payload)
    streams = [stream for stream in _collect_json_values(data, "stream") if isinstance(stream, dict)]
    stream_names = frozenset(
        str(stream.get("name") or stream.get("stream") or stream.get("streamName") or "").strip()
        for stream in streams
        if str(stream.get("name") or stream.get("stream") or stream.get("streamName") or "").strip()
    )
    clients = [client_item for client_item in _collect_json_values(data, "client") if isinstance(client_item, dict)]
    publishers = [
        client_item
        for client_item in clients
        if _json_has_truthy_key(client_item, ("publishing", "publisher", "is_publisher", "isPublisher"))
    ]

    if isinstance(data, dict):
        direct_streams = data.get("active_streams", data.get("activeStreams"))
        direct_publishers = data.get("active_publishers", data.get("activePublishers"))
        direct_clients = data.get("active_clients", data.get("activeClients"))
        if direct_streams is not None or direct_publishers is not None or direct_clients is not None:
            active_streams = _safe_int(direct_streams)
            active_publishers = _safe_int(direct_publishers)
            active_clients = _safe_int(direct_clients)
            return ProxyRtmpStats(active_streams, active_publishers, active_clients, stream_names=stream_names)

    return ProxyRtmpStats(
        active_streams=len(streams),
        active_publishers=len(publishers),
        active_clients=len(clients),
        stream_names=stream_names,
    )


def parse_proxy_rtmp_stats(payload: str, content_type: str = "") -> ProxyRtmpStats:
    stripped = payload.strip()
    if not stripped:
        raise ValueError("empty proxy RTMP stats response")
    if "json" in content_type.lower() or stripped[0] in "[{":
        return parse_proxy_rtmp_stats_json(stripped)
    return parse_proxy_rtmp_stats_xml(stripped)


def set_proxy_rtmp_stats_metrics(proxy_pod: str, stats: ProxyRtmpStats) -> None:
    proxy_rtmp_active_streams.labels(proxy_pod=proxy_pod).set(stats.active_streams)
    proxy_rtmp_active_publishers.labels(proxy_pod=proxy_pod).set(stats.active_publishers)
    proxy_rtmp_active_clients.labels(proxy_pod=proxy_pod).set(stats.active_clients)
    proxy_rtmp_stream_active.labels(proxy_pod=proxy_pod).set(stats.stream_active)
    proxy_rtmp_stats_up.labels(proxy_pod=proxy_pod).set(1)


def remove_proxy_rtmp_stats_metrics(proxy_pod: str) -> None:
    metrics = (
        proxy_rtmp_active_streams,
        proxy_rtmp_active_publishers,
        proxy_rtmp_active_clients,
        proxy_rtmp_stream_active,
        proxy_rtmp_stats_up,
        proxy_rtmp_stats_scrape_errors_total,
    )
    for metric in metrics:
        try:
            metric.remove(proxy_pod=proxy_pod)
        except KeyError:
            pass


def cleanup_stale_proxy_rtmp_stats_metrics(active_proxy_pods: Iterable[str]) -> None:
    active = set(active_proxy_pods)
    stale = proxy_rtmp_stats_observed_pods - active
    for proxy_pod in stale:
        remove_proxy_rtmp_stats_metrics(proxy_pod)
    proxy_rtmp_stats_observed_pods.clear()
    proxy_rtmp_stats_observed_pods.update(active)


def list_proxy_pods_for_stats() -> List[Tuple[str, str]]:
    pods = core.list_namespaced_pod(namespace=NAMESPACE, label_selector="app=proxy").items
    proxy_pods: List[Tuple[str, str]] = []
    for pod in pods:
        name = (pod.metadata.name or "").strip() if pod.metadata else ""
        pod_ip = (pod.status.pod_ip or "").strip() if pod.status else ""
        if name and pod_ip:
            proxy_pods.append((name, pod_ip))
    return proxy_pods


def scrape_proxy_rtmp_stats(proxy_pod: str, pod_ip: str) -> ProxyRtmpStats:
    if not pod_ip:
        raise RuntimeError(f"proxy pod '{proxy_pod}' has no assigned pod IP")
    response = requests.get(f"http://{pod_ip}:8080/stats", timeout=PROXY_RTMP_STATS_TIMEOUT_SECONDS)
    response.raise_for_status()
    return parse_proxy_rtmp_stats(response.text, response.headers.get("Content-Type", ""))


def get_stream_activity_on_proxy(stream: str, proxy_pod: str) -> Optional[bool]:
    """Return whether a specific stream is active on a proxy.

    True/False is returned when /stats gives enough evidence. None means the
    proxy could not be scraped or the stats format did not expose stream names
    while still reporting some active streams. Callers should treat None as
    inconclusive and avoid destructive replacement decisions.
    """
    try:
        proxy_ip = resolve_proxy_address(proxy_pod)
        stats = scrape_proxy_rtmp_stats(proxy_pod, proxy_ip)
        activity = stats.has_stream(stream)
        logger.debug(
            "[ProxyRtmpStats] Stream activity check "
            f"stream='{stream}' proxy_pod='{proxy_pod}' active={activity} "
            f"active_streams={stats.active_streams} stream_names={sorted(stats.stream_names)}"
        )
        return activity
    except Exception as e:
        logger.warning(
            f"[ProxyRtmpStats] Failed checking stream activity for stream '{stream}' "
            f"on proxy '{proxy_pod}': {e}"
        )
        return None


async def collect_proxy_rtmp_stats_once() -> None:
    try:
        proxy_pods = await asyncio.to_thread(list_proxy_pods_for_stats)
    except ApiException as e:
        record_kubernetes_api_error("list", "pod", e)
        proxy_rtmp_stats_discovery_errors_total.inc()
        logger.warning(f"[ProxyRtmpStats] Failed listing proxy pods for /stats scrape: {e}")
        return
    except Exception as e:
        proxy_rtmp_stats_discovery_errors_total.inc()
        logger.warning(f"[ProxyRtmpStats] Failed listing proxy pods for /stats scrape: {e}")
        return

    cleanup_stale_proxy_rtmp_stats_metrics(proxy_pod for proxy_pod, _pod_ip in proxy_pods)

    semaphore = asyncio.Semaphore(PROXY_RTMP_STATS_MAX_CONCURRENCY)

    async def scrape_one(proxy_pod: str, pod_ip: str) -> None:
        try:
            async with semaphore:
                stats = await asyncio.to_thread(scrape_proxy_rtmp_stats, proxy_pod, pod_ip)
            set_proxy_rtmp_stats_metrics(proxy_pod, stats)
            logger.debug(
                "[ProxyRtmpStats] Scraped proxy /stats "
                f"proxy_pod='{proxy_pod}' active_streams={stats.active_streams} "
                f"active_publishers={stats.active_publishers} active_clients={stats.active_clients}"
            )
        except Exception as e:
            proxy_rtmp_stats_up.labels(proxy_pod=proxy_pod).set(0)
            proxy_rtmp_stats_scrape_errors_total.labels(proxy_pod=proxy_pod).inc()
            logger.warning(f"[ProxyRtmpStats] Failed scraping /stats for proxy '{proxy_pod}': {e}")

    if proxy_pods:
        await asyncio.gather(*(scrape_one(proxy_pod, pod_ip) for proxy_pod, pod_ip in proxy_pods))


async def collect_proxy_rtmp_stats():
    while True:
        try:
            await collect_proxy_rtmp_stats_once()
        except Exception as e:
            logger.warning(f"[ProxyRtmpStats] Best-effort stats collection failed: {e}")
        await asyncio.sleep(PROXY_RTMP_STATS_INTERVAL_SECONDS)


def resolve_proxy_address(proxy_pod: str) -> str:
    """Retorna o IP atual do pod proxy owner da stream."""
    if not proxy_pod:
        raise RuntimeError("proxy_pod is required to resolve proxy address")

    try:
        pod = core.read_namespaced_pod(name=proxy_pod, namespace=NAMESPACE)
    except ApiException as e:
        record_kubernetes_api_error("read", "pod", e)
        raise
    pod_ip = (pod.status.pod_ip or "").strip() if pod and pod.status else ""
    if not pod_ip:
        raise RuntimeError(f"proxy pod '{proxy_pod}' has no assigned pod IP")
    return pod_ip


def get_proxy_health_status(proxy_pod: str) -> str:
    """Returns proxy health status without counting NotReady/warm-up as probe failures."""
    started_at = time.monotonic()
    metric_status = "unhealthy"
    metric_reason = "exception"
    try:
        pod = core.read_namespaced_pod(name=proxy_pod, namespace=NAMESPACE)
        ready = any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))
        if not ready:
            with allocation_lock:
                proxy_ready_since.pop(proxy_pod, None)
            metric_status = "not_ready"
            metric_reason = "pod_not_ready"
            return "not_ready"

        now = time.time()
        with allocation_lock:
            first_ready_at = proxy_ready_since.get(proxy_pod)
            if first_ready_at is None:
                proxy_ready_since[proxy_pod] = now
        if first_ready_at is None:
            logger.debug(
                f"[ProxyHealth] Proxy '{proxy_pod}' became Ready. Starting proxy delay timer "
                f"({PROXY_READY_HEALTH_DELAY_SECONDS}s) before /health probe."
            )
            metric_status = "warming_up"
            metric_reason = "ready_delay_started"
            return "warming_up"
        if (now - first_ready_at) < PROXY_READY_HEALTH_DELAY_SECONDS:
            logger.debug(
                f"[ProxyHealth] Waiting {PROXY_READY_HEALTH_DELAY_SECONDS}s after Ready for '{proxy_pod}' "
                f"before probing /health ({now - first_ready_at:.1f}s elapsed)."
            )
            metric_status = "warming_up"
            metric_reason = "ready_delay"
            return "warming_up"

        target = (pod.status.pod_ip or "").strip() if pod and pod.status else ""
        if not target:
            raise RuntimeError(f"proxy pod '{proxy_pod}' has no assigned pod IP")
        response = requests.get(f"http://{target}:8080/health", timeout=PROXY_HEALTHCHECK_TIMEOUT_SECONDS)
        if response.status_code == 200:
            metric_status = "healthy"
            metric_reason = "http_200"
            return "healthy"
        metric_status = "unhealthy"
        metric_reason = f"http_{response.status_code}"
        return "unhealthy"
    except ApiException as e:
        record_kubernetes_api_error("read", "pod", e)
        if e.status == 404:
            metric_status = "unhealthy"
            metric_reason = "pod_not_found"
            return "unhealthy"
        logger.warning(f"[ProxyHealth] Failed to read proxy pod '{proxy_pod}': {e}")
        metric_status = "unhealthy"
        metric_reason = "api_exception"
        return "unhealthy"
    except Exception as e:
        logger.warning(f"[ProxyHealth] Failed to check /health for proxy '{proxy_pod}': {e}")
        metric_status = "unhealthy"
        metric_reason = type(e).__name__
        return "unhealthy"
    finally:
        proxy_healthcheck_duration_seconds.observe(time.monotonic() - started_at)
        proxy_healthcheck_total.labels(status=metric_status, reason=metric_reason).inc()


def check_worker_health(
    pod_name: str,
    pod_ip: Optional[str] = None,
    stream: Optional[str] = None,
    proxy_pod: Optional[str] = None,
) -> bool:
    """Checks worker processing health using the FFmpeg exporter readiness endpoint.

    Kubernetes liveness only proves the container/exporter is alive. For recovery
    decisions the controller needs a stronger signal: FFmpeg must be running and
    producing recent progress. The monitor already waits WORKER_READY_HEALTH_DELAY_SECONDS
    after Pod Ready before calling this function, so /readyz is expected to be stable here.
    """
    started_at = time.monotonic()
    metric_status = "unhealthy"
    metric_reason = "exception"
    endpoint_path = WORKER_HEALTHCHECK_PATH if WORKER_HEALTHCHECK_PATH.startswith("/") else f"/{WORKER_HEALTHCHECK_PATH}"
    try:
        target = pod_ip if pod_ip else f"{pod_name}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local"
        response = requests.get(
            f"http://{target}:{WORKER_HEALTHCHECK_PORT}{endpoint_path}",
            timeout=WORKER_HEALTHCHECK_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            metric_status = "healthy"
            metric_reason = f"http_200_{endpoint_path.strip('/') or 'root'}"
            return True
        metric_status = "unhealthy"
        metric_reason = f"http_{response.status_code}_{endpoint_path.strip('/') or 'root'}"
        return False
    except Exception as e:
        logger.warning(f"Failed to check worker processing readiness for {pod_name}: {e}")
        metric_status = "unhealthy"
        metric_reason = type(e).__name__
        return False
    finally:
        duration_seconds = time.monotonic() - started_at
        worker_healthcheck_duration_seconds.observe(duration_seconds)
        worker_healthcheck_total.labels(status=metric_status, reason=metric_reason).inc()
        log_controller_event(
            "ffmpeg_health_observed",
            stream=stream,
            proxy_pod=proxy_pod,
            worker_pod=pod_name,
            duration_ms=round(duration_seconds * 1000, 3),
            status=metric_status,
            level=logging.INFO if metric_status == "healthy" else logging.WARNING,
            message=f"worker processing readiness checked via {endpoint_path} on port {WORKER_HEALTHCHECK_PORT}",
        )


def recover_state(
):
    """
    Recupera estado de alocações após reinício do controller.
    Verifica quais workers estão realmente ocupados consultando suas métricas RTMP.
    """
    logger.info("[State Recovery] Starting state recovery...")
    
    try:
        with allocation_lock:
            restore_result = restore_persisted_state_locked()
            if restore_result.restored:
                state_recovery_total.labels(status="success", reason="restored").inc()
                log_controller_event(
                    "state_recovery_completed",
                    status=LOG_STATUS_SUCCESS,
                    message="state recovery restored persisted state",
                )
                logger.info(
                    f"[State Recovery] Restored persisted state with {len(stream_to_worker)} active stream allocations."
                )
                return

            if restore_result.reason in ("not_found", "empty"):
                state_recovery_total.labels(status="skipped", reason="no_persisted_state").inc()
                log_controller_event(
                    "state_recovery_completed",
                    status="skipped",
                    message="state recovery skipped because no persisted state was found",
                )
                # Sem estado persistido: não reaproveitar pods já existentes.
                # No modelo por-env (STREAM_KEY/PROXY_DNS), reuso pode carregar config obsoleta.
                logger.info("[State Recovery] No persisted state found. Skipping worker auto-recovery to avoid stale env reuse.")
                return

            state_recovery_total.labels(status="error", reason=restore_result.reason).inc()
            log_controller_event(
                "state_recovery_failed",
                status=LOG_STATUS_FAILED,
                level=logging.WARNING,
                message=f"state recovery failed: {restore_result.reason}",
            )
            logger.warning(
                f"[State Recovery] Persisted state restore failed with reason '{restore_result.reason}'."
            )
    except Exception as e:
        state_recovery_total.labels(status="error", reason="exception").inc()
        log_controller_event(
            "state_recovery_failed",
            status=LOG_STATUS_FAILED,
            level=logging.WARNING,
            message="state recovery raised an exception",
        )
        raise


def update_worker_pods_available_metric() -> None:
    """Update controller-owned worker lifecycle gauges from the Kubernetes API.

    worker_pods_available is retained for backward compatibility, but with the
    v4 lifecycle semantics Kubernetes Ready maps to /readyz, i.e. FFmpeg
    processing readiness. Use worker_pods_running when the question is simply
    whether worker Pods exist and are not terminal/deleting.
    """
    try:
        pods = core.list_namespaced_pod(namespace=NAMESPACE, label_selector="app=worker").items
    except ApiException as e:
        record_kubernetes_api_error("list", "pod", e)
        logger.warning(f"[InfrastructureMetrics] Failed to list worker pods: {e}")
        return
    except Exception as e:
        logger.warning(f"[InfrastructureMetrics] Failed to list worker pods: {e}")
        return

    running_count = 0
    processing_ready_count = 0
    for pod in pods:
        metadata = getattr(pod, "metadata", None)
        status = getattr(pod, "status", None)
        if not metadata or getattr(metadata, "deletion_timestamp", None):
            continue
        if not status:
            continue
        phase = getattr(status, "phase", None)
        if phase in {"Running", "Pending"}:
            running_count += 1
        if phase != "Running":
            continue
        conditions = getattr(status, "conditions", None) or []
        if any(getattr(c, "type", None) == "Ready" and getattr(c, "status", None) == "True" for c in conditions):
            processing_ready_count += 1
    worker_pods_running.labels(namespace=NAMESPACE).set(running_count)
    worker_processing_ready.labels(namespace=NAMESPACE).set(processing_ready_count)
    worker_pods_available.labels(namespace=NAMESPACE).set(processing_ready_count)


async def collect_infrastructure_metrics():
    # Publish worker pod gauges as best-effort observability. Metrics failures
    # must never interrupt the simplified publish-start/publish-done lifecycle.
    while True:
        try:
            update_worker_pods_available_metric()
        except Exception as e:
            logger.warning(f"[Metrics] Best-effort infrastructure collection failed: {e}")
        await asyncio.sleep(5)



async def monitor_worker_health():
    """Controller-driven worker lifecycle and FFmpeg readiness monitor.

    Kubernetes Pod Ready now reflects /readyz, which means FFmpeg processing
    readiness. Therefore NotReady/Failed workers must be evaluated explicitly
    instead of being skipped, otherwise a failed worker can remain allocated for
    an active stream forever.
    """
    while True:
        await asyncio.sleep(WORKER_HEALTHCHECK_INTERVAL_SECONDS)
        to_replace = []

        with allocation_lock:
            allocations = list(stream_to_worker.items())
            stream_owner_snapshot = {
                stream: entry.get("proxy_pod")
                for stream, entry in stream_registry.items()
            }

        # Health check for allocated workers
        for stream, worker_pod in allocations:
            owner_proxy = stream_owner_snapshot.get(stream)
            if not owner_proxy:
                logger.debug(
                    f"[WorkerHealth] Stream '{stream}' has no owner proxy in snapshot; skipping worker '{worker_pod}' health action."
                )
                continue

            if WORKER_HEALTHCHECK_JITTER_SECONDS > 0:
                await asyncio.sleep(random.uniform(0, WORKER_HEALTHCHECK_JITTER_SECONDS))

            healthy = False
            current_uid = ""
            try:
                pod = core.read_namespaced_pod(name=worker_pod, namespace=NAMESPACE)
                current_uid = ((pod.metadata.uid or "").strip() if pod and pod.metadata else "")
                with allocation_lock:
                    prev_uid = worker_pod_uid_by_name.get(worker_pod)
                    if prev_uid and current_uid and prev_uid != current_uid:
                        worker_health_failures.pop(prev_uid, None)
                        worker_health_failures[current_uid] = 0
                        worker_ready_since.pop(worker_pod, None)
                        worker_not_ready_since.pop(worker_pod, None)
                        worker_create_started_at.pop(worker_pod, None)
                        cleanup_worker_lifecycle_tracking_locked(worker_pod)
                    if current_uid:
                        worker_pod_uid_by_name[worker_pod] = current_uid

                phase = (getattr(pod.status, "phase", None) or "").strip()
                ready = any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))
                now_monotonic = time.monotonic()

                if phase in {"Failed", "Succeeded"}:
                    logger.warning(
                        f"[WorkerHealth] Worker '{worker_pod}' for stream '{stream}' is in terminal pod phase '{phase}'. "
                        "Scheduling recovery/release evaluation."
                    )
                    to_replace.append((stream, worker_pod))
                    continue

                if phase not in {"Running", "Pending"}:
                    logger.warning(
                        f"[WorkerHealth] Worker '{worker_pod}' for stream '{stream}' is in unexpected pod phase '{phase}'. "
                        "Scheduling recovery/release evaluation."
                    )
                    to_replace.append((stream, worker_pod))
                    continue

                if not ready:
                    with allocation_lock:
                        first_not_ready_at = worker_create_started_at.get(worker_pod)
                        if first_not_ready_at is None:
                            first_not_ready_at = worker_not_ready_since.setdefault(worker_pod, now_monotonic)
                        worker_ready_since.pop(worker_pod, None)
                    not_ready_seconds = max(0.0, now_monotonic - first_not_ready_at)
                    if not_ready_seconds < WORKER_NOT_READY_REPLACE_SECONDS:
                        logger.debug(
                            f"[WorkerHealth] Worker '{worker_pod}' is {phase}/NotReady for stream '{stream}' "
                            f"for {not_ready_seconds:.1f}s; waiting up to {WORKER_NOT_READY_REPLACE_SECONDS}s before recovery."
                        )
                        continue
                    logger.warning(
                        f"[WorkerHealth] Worker '{worker_pod}' is {phase}/NotReady for stream '{stream}' "
                        f"for {not_ready_seconds:.1f}s; scheduling recovery/release evaluation."
                    )
                    to_replace.append((stream, worker_pod))
                    continue

                now = time.time()
                create_started_at = None
                with allocation_lock:
                    worker_not_ready_since.pop(worker_pod, None)
                    first_ready_at = worker_ready_since.get(worker_pod)
                    if first_ready_at is None:
                        worker_ready_since[worker_pod] = now
                        create_started_at = worker_create_started_at.pop(worker_pod, None)
                        if current_uid:
                            worker_health_failures[current_uid] = 0
                if first_ready_at is None:
                    if create_started_at is not None:
                        ready_duration_seconds = time.monotonic() - create_started_at
                        worker_ready_duration_seconds.observe(ready_duration_seconds)
                        worker_ready_total.labels(status=LOG_STATUS_READY, reason="processing_ready").inc()
                        ready_duration_ms = round(ready_duration_seconds * 1000, 3)
                    else:
                        worker_ready_total.labels(status=LOG_STATUS_READY, reason="start_time_unknown").inc()
                        ready_duration_ms = None
                    log_controller_event(
                        "worker_processing_ready_observed",
                        stream=stream,
                        proxy_pod=owner_proxy,
                        worker_pod=worker_pod,
                        duration_ms=ready_duration_ms,
                        status=LOG_STATUS_READY,
                        message="Kubernetes readiness is /readyz, indicating FFmpeg processing readiness",
                    )
                    logger.debug(
                        f"[WorkerHealth] Worker '{worker_pod}' became processing Ready. "
                        f"Starting delay timer ({WORKER_READY_HEALTH_DELAY_SECONDS}s) before probing {WORKER_HEALTHCHECK_PATH}."
                    )
                    continue

                if (now - first_ready_at) < WORKER_READY_HEALTH_DELAY_SECONDS:
                    logger.debug(
                        f"[WorkerHealth] Waiting worker delay of {WORKER_READY_HEALTH_DELAY_SECONDS}s after processing Ready for '{worker_pod}' "
                        f"before probing {WORKER_HEALTHCHECK_PATH} ({now - first_ready_at:.1f}s elapsed)."
                    )
                    continue

                with allocation_lock:
                    owner_proxy = stream_registry.get(stream, {}).get("proxy_pod")
                if not owner_proxy:
                    logger.debug(f"[WorkerHealth] Stream '{stream}' has no proxy owner; skipping worker check.")
                    if current_uid:
                        with allocation_lock:
                            worker_health_failures.pop(current_uid, None)
                    continue

                owner_proxy_health = get_proxy_health_status(owner_proxy)
                if owner_proxy_health != "healthy":
                    logger.debug(
                        f"[WorkerHealth] Skipping worker '{worker_pod}' check because owner proxy "
                        f"'{owner_proxy}' is {owner_proxy_health}."
                    )
                    if current_uid:
                        with allocation_lock:
                            worker_health_failures.pop(current_uid, None)
                    continue

                healthy = check_worker_health(worker_pod, pod.status.pod_ip, stream=stream, proxy_pod=owner_proxy)
            except ApiException as e:
                record_kubernetes_api_error("read", "pod", e)
                healthy = False
            except Exception:
                healthy = False

            if healthy:
                if current_uid:
                    with allocation_lock:
                        worker_health_failures[current_uid] = 0
                continue

            if current_uid:
                with allocation_lock:
                    failures = worker_health_failures.get(current_uid, 0) + 1
                    worker_health_failures[current_uid] = failures
            else:
                failures = 1
            logger.warning(
                f"[WorkerHealth] Worker '{worker_pod}' failed healthcheck for stream '{stream}' "
                f"({failures}/{WORKER_HEALTHCHECK_MAX_FAILURES})"
            )
            if failures >= WORKER_HEALTHCHECK_MAX_FAILURES:
                to_replace.append((stream, worker_pod))

        # Não reaproveitar pods prontos para pendências; sempre criar pod novo na alocação explícita.

        # Handle unhealthy workers
        if to_replace:
            for stream, worker_pod in to_replace:
                with allocation_lock:
                    allocated = stream_to_worker.get(stream)
                    owner_proxy = stream_registry.get(stream, {}).get("proxy_pod")

                if allocated != worker_pod:
                    continue

                if not owner_proxy:
                    logger.warning(
                        f"[WorkerHealth] Cannot replace worker '{worker_pod}' for stream '{stream}' "
                        "because the stream has no proxy owner."
                    )
                    continue

                with allocation_lock:
                    explicit_terminal = stream_has_explicit_terminal_locked(stream, owner_proxy)
                if explicit_terminal:
                    logger.info(
                        f"[WorkerHealth] Worker '{worker_pod}' is unhealthy, and stream '{stream}' "
                        f"has an explicit ended event. Releasing stale allocation."
                    )
                    await release_worker(stream=stream)
                    continue

                if get_proxy_health_status(owner_proxy) != "healthy":
                    logger.info(
                        f"[WorkerHealth] Delaying replacement of worker '{worker_pod}' for stream '{stream}' "
                        f"because owner proxy '{owner_proxy}' is not healthy and no explicit stream end was observed."
                    )
                    continue

                stream_active = get_stream_activity_on_proxy(stream, owner_proxy)
                if stream_active is False:
                    logger.info(
                        f"[WorkerHealth] Stream '{stream}' is not visible in proxy stats, but no explicit ended event "
                        f"was observed. Treating stats absence as non-terminal and attempting worker replacement."
                    )
                elif stream_active is None:
                    logger.info(
                        f"[WorkerHealth] Proxy stream activity is inconclusive for stream '{stream}', but no explicit "
                        "ended event was observed. Attempting worker replacement."
                    )

                logger.warning(f"[WorkerHealth] Worker '{worker_pod}' unhealthy for non-terminal stream '{stream}'. Replacing.")
                recovery_started_at = time.monotonic()
                recovery_status = "error"
                recovery_reason = "exception"
                try:
                    proxy_dns = resolve_proxy_address(owner_proxy)
                    new_worker = create_worker_pod_for_stream(
                        stream=stream,
                        proxy_dns=proxy_dns,
                        proxy_pod=owner_proxy,
                        generation=generation_to_int(stream_generation.get(stream)) or 1,
                        session_id=(stream_registry.get(stream) or {}).get("session_id"),
                    )
                except Exception as e:
                    recovery_reason = type(e).__name__
                    worker_recovery_duration_seconds.observe(time.monotonic() - recovery_started_at)
                    worker_recovery_total.labels(status=recovery_status, reason=recovery_reason).inc()
                    logger.warning(
                        f"[WorkerHealth] Failed to create replacement worker for stream '{stream}': {e}"
                    )
                    continue

                discard_new_worker = False
                old_worker_to_delete = None
                with allocation_lock:
                    allocated = stream_to_worker.get(stream)
                    current_owner = stream_registry.get(stream, {}).get("proxy_pod")
                    if allocated != worker_pod or current_owner != owner_proxy:
                        worker_create_started_at.pop(new_worker, None)
                        worker_not_ready_since.pop(new_worker, None)
                        cleanup_worker_lifecycle_tracking_locked(new_worker)
                        discard_new_worker = True
                    else:
                        stream_to_worker[stream] = new_worker
                        worker_to_stream.pop(worker_pod, None)
                        cleanup_worker_lifecycle_tracking_locked(worker_pod)
                        worker_to_stream[new_worker] = stream
                        worker_ready_since.pop(worker_pod, None)
                        worker_not_ready_since.pop(worker_pod, None)
                        worker_create_started_at.pop(worker_pod, None)
                        old_uid = worker_pod_uid_by_name.pop(worker_pod, None)
                        if old_uid:
                            worker_health_failures.pop(old_uid, None)
                        persist_state_locked()
                        old_worker_to_delete = worker_pod

                if discard_new_worker:
                    recovery_status = "warning"
                    recovery_reason = "stale_state"
                    worker_recovery_duration_seconds.observe(time.monotonic() - recovery_started_at)
                    worker_recovery_total.labels(status=recovery_status, reason=recovery_reason).inc()
                    logger.info(
                        f"[WorkerHealth] Discarding replacement worker '{new_worker}' for stream '{stream}' "
                        f"because allocation changed while recovery was creating it."
                    )
                    try:
                        core.delete_namespaced_pod(name=new_worker, namespace=NAMESPACE, grace_period_seconds=0)
                        record_worker_delete_metric("success", "stale_replacement")
                        log_controller_event(
                            "worker_deleted",
                            stream=stream,
                            proxy_pod=owner_proxy,
                            worker_pod=new_worker,
                            status=LOG_STATUS_DELETED,
                        )
                    except ApiException as e:
                        record_kubernetes_api_error("delete", "pod", e)
                        record_worker_delete_metric("warning", "delete_failed")
                        log_controller_event(
                            "worker_deleted",
                            stream=stream,
                            proxy_pod=owner_proxy,
                            worker_pod=new_worker,
                            status=LOG_STATUS_DELETE_FAILED,
                            level=logging.WARNING,
                        )
                        logger.warning(f"[WorkerHealth] Failed to delete stale replacement pod {new_worker}: {e}")
                    except Exception as e:
                        record_worker_delete_metric("warning", "delete_failed")
                        log_controller_event(
                            "worker_deleted",
                            stream=stream,
                            proxy_pod=owner_proxy,
                            worker_pod=new_worker,
                            status=LOG_STATUS_DELETE_FAILED,
                            level=logging.WARNING,
                        )
                        logger.warning(f"[WorkerHealth] Failed to delete stale replacement pod {new_worker}: {e}")
                    continue

                if old_worker_to_delete:
                    try:
                        core.delete_namespaced_pod(name=old_worker_to_delete, namespace=NAMESPACE, grace_period_seconds=0)
                        record_worker_delete_metric("success", "replaced")
                        log_controller_event(
                            "worker_deleted",
                            stream=stream,
                            proxy_pod=owner_proxy,
                            worker_pod=old_worker_to_delete,
                            status=LOG_STATUS_DELETED,
                        )
                    except ApiException as e:
                        record_kubernetes_api_error("delete", "pod", e)
                        record_worker_delete_metric("warning", "delete_failed")
                        log_controller_event(
                            "worker_deleted",
                            stream=stream,
                            proxy_pod=owner_proxy,
                            worker_pod=old_worker_to_delete,
                            status=LOG_STATUS_DELETE_FAILED,
                            level=logging.WARNING,
                        )
                        logger.warning(f"[WorkerHealth] Failed to delete pod {old_worker_to_delete}: {e}")
                    except Exception as e:
                        record_worker_delete_metric("warning", "delete_failed")
                        log_controller_event(
                            "worker_deleted",
                            stream=stream,
                            proxy_pod=owner_proxy,
                            worker_pod=old_worker_to_delete,
                            status=LOG_STATUS_DELETE_FAILED,
                            level=logging.WARNING,
                        )
                        logger.warning(f"[WorkerHealth] Failed to delete pod {old_worker_to_delete}: {e}")

                recovery_status = "success"
                recovery_reason = "replaced"
                worker_recovery_duration_seconds.observe(time.monotonic() - recovery_started_at)
                worker_recovery_total.labels(status=recovery_status, reason=recovery_reason).inc()
                worker_replacements_total.labels(status="success", reason="recovered").inc()
                logger.info(
                    f"[WorkerHealth] Replaced unhealthy worker for stream '{stream}': "
                    f"old='{worker_pod}' new='{new_worker}' proxy='{owner_proxy}'"
                )



async def sweep_orphan_workers():
    """Safety-net: periodically delete worker pods that are not mapped in controller state."""
    while True:
        await asyncio.sleep(WORKER_ORPHAN_SWEEP_INTERVAL_SECONDS)

        try:
            pods = core.list_namespaced_pod(namespace=NAMESPACE, label_selector="app=worker").items
        except ApiException as e:
            record_kubernetes_api_error("list", "pod", e)
            logger.warning(f"[OrphanSweeper] Failed to list worker pods: {e}")
            continue
        except Exception as e:
            logger.warning(f"[OrphanSweeper] Failed to list worker pods: {e}")
            continue

        with allocation_lock:
            mapped_workers = set(stream_to_worker.values())

        for pod in pods:
            pod_name = pod.metadata.name if pod and pod.metadata else None
            if not pod_name:
                continue
            if pod_name in mapped_workers:
                continue

            # Double-check under lock right before deletion to avoid race with recent allocations.
            with allocation_lock:
                still_orphan = pod_name not in set(stream_to_worker.values())

            if not still_orphan:
                logger.debug(f"[OrphanSweeper] Pod '{pod_name}' became mapped before deletion; skipping.")
                continue

            logger.warning(f"[OrphanSweeper] Deleting orphan worker pod '{pod_name}'")
            with allocation_lock:
                cleanup_worker_lifecycle_tracking_locked(pod_name)
            # Orphan workers have no stream/proxy mapping; required event keys remain present with null context.
            try:
                core.delete_namespaced_pod(name=pod_name, namespace=NAMESPACE, grace_period_seconds=0)
                record_worker_delete_metric("success", "orphan", orphan=True)
                log_orphan_worker_deleted(pod_name, status=LOG_STATUS_DELETED)
            except ApiException as e:
                record_kubernetes_api_error("delete", "pod", e)
                record_worker_delete_metric("warning", "delete_failed", orphan=True)
                log_orphan_worker_deleted(
                    pod_name,
                    status=LOG_STATUS_DELETE_FAILED,
                    level=logging.WARNING,
                )
                logger.warning(f"[OrphanSweeper] Failed deleting orphan worker pod '{pod_name}': {e}")
            except Exception as e:
                record_worker_delete_metric("warning", "delete_failed", orphan=True)
                log_orphan_worker_deleted(
                    pod_name,
                    status=LOG_STATUS_DELETE_FAILED,
                    level=logging.WARNING,
                )
                logger.warning(f"[OrphanSweeper] Failed deleting orphan worker pod '{pod_name}': {e}")

@app.get("/health")
def health():
    return {"status": "ok"}


def inspect_existing_worker_for_replay(
    worker_pod: str,
    expected_generation: Optional[int],
    expected_session_id: Optional[str],
) -> Tuple[bool, str, bool]:
    """Return (usable, reason, should_delete) for an existing stream->worker mapping."""
    try:
        pod = core.read_namespaced_pod(name=worker_pod, namespace=NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return False, "pod_not_found", False
        record_kubernetes_api_error("read", "pod", e)
        logger.warning(f"[Allocate] Could not inspect existing worker '{worker_pod}': {e}")
        # Preserve idempotency on transient API errors instead of creating duplicates.
        return True, "inspection_inconclusive", False
    except Exception as e:
        logger.warning(f"[Allocate] Could not inspect existing worker '{worker_pod}': {e}")
        return True, "inspection_inconclusive", False

    metadata = getattr(pod, "metadata", None)
    status = getattr(pod, "status", None)
    annotations = dict(getattr(metadata, "annotations", None) or {}) if metadata else {}

    if metadata and getattr(metadata, "deletion_timestamp", None):
        return False, "pod_deleting", False

    phase = (getattr(status, "phase", None) or "").strip() if status else ""
    if phase in {"Failed", "Succeeded"}:
        return False, f"pod_terminal_{phase.lower()}", True
    if phase and phase not in {"Running", "Pending"}:
        return False, f"pod_unusable_phase_{phase.lower()}", True

    annotated_generation = generation_to_int(annotations.get("liveedgecast.io/generation"))
    if expected_generation is not None:
        if annotated_generation is None:
            if not ALLOW_LEGACY_WORKER_REPLAY_WITHOUT_GENERATION:
                return False, "missing_generation_annotation", True
        elif annotated_generation != expected_generation:
            return False, "generation_mismatch", True

    annotated_session = annotations.get("liveedgecast.io/session-id")
    if expected_session_id:
        if not annotated_session:
            if not ALLOW_LEGACY_WORKER_REPLAY_WITHOUT_SESSION:
                return False, "missing_session_annotation", True
        elif annotated_session != expected_session_id:
            return False, "session_mismatch", True

    return True, "usable", False


@app.post("/workers/ffmpeg/started")
def worker_ffmpeg_started(
    stream: str = Query(..., description="Stream name"),
    worker_pod: str = Query(..., description="Worker pod reporting FFmpeg start"),
):
    return record_worker_progress_event(stream, worker_pod, "t_ffmpeg_started", "worker_hook")


@app.api_route("/workers/progress", methods=["POST", "PUT"])
def worker_ffmpeg_progress(
    stream: str = Query(..., description="Stream name"),
    worker_pod: str = Query(..., description="Worker pod reporting FFmpeg -progress"),
):
    # The worker posts once after reading the first FFmpeg -progress line locally;
    # do not wait for or parse a long-lived FFmpeg progress request body here.
    return record_worker_progress_event(stream, worker_pod, "t_ffmpeg_first_progress", "ffmpeg_progress")


def allocate_worker(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(None, description="Proxy pod name for pull-only architecture"),
    ownership_already_verified: bool = False,
):
    """Create one worker for the publish-start event.

    This version deliberately avoids handover, recovery and concurrency handling.
    If a worker is already mapped for the stream, the existing mapping is returned
    to keep duplicate proxy callbacks harmless.
    """
    started_at = time.monotonic()
    metric_status = "error"
    metric_reason = "exception"
    try:
        with allocation_lock:
            existing_worker = stream_to_worker.get(stream)
            owner_proxy = (stream_registry.get(stream) or {}).get("proxy_pod") or proxy_pod
            generation_snapshot = generation_to_int(stream_generation.get(stream)) or 1
            session_snapshot = (stream_registry.get(stream) or {}).get("session_id")

        if existing_worker:
            metric_status = "success"
            metric_reason = "existing_worker"
            proxy_address = resolve_proxy_address(owner_proxy) if owner_proxy else None
            return {
                "pod": f"{existing_worker}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local",
                "name": existing_worker,
                "proxy": proxy_address,
                "worker": existing_worker,
                "status": "existing_worker",
            }

        if not owner_proxy:
            metric_reason = "missing_proxy_owner"
            raise HTTPException(status_code=409, detail=f"stream '{stream}' has no proxy owner")

        proxy_address = resolve_proxy_address(owner_proxy)
        pod_name = create_worker_pod_for_stream(
            stream=stream,
            proxy_dns=proxy_address,
            proxy_pod=owner_proxy,
            generation=generation_snapshot,
            session_id=session_snapshot,
        )

        with allocation_lock:
            stream_to_worker[stream] = pod_name
            worker_to_stream[pod_name] = stream
            update_controller_state_gauges_locked()
            persist_state_locked()

        worker_dns = f"{pod_name}.{WORKER_SERVICE}.{NAMESPACE}.svc.cluster.local"
        logger.info(f"[Allocate] Created worker pod {pod_name} for stream '{stream}'")
        metric_status = "success"
        metric_reason = "created"
        return {"pod": worker_dns, "name": pod_name, "proxy": proxy_address, "worker": pod_name, "status": "created"}
    except Exception as e:
        if metric_reason == "exception":
            metric_reason = type(e).__name__
        raise
    finally:
        metric_best_effort(
            "recording allocation metrics",
            lambda: (
                stream_allocation_duration_seconds.observe(time.monotonic() - started_at),
                stream_allocation_total.labels(status=metric_status, reason=metric_reason).inc(),
            ),
        )


async def release_worker(stream: str = Query(..., description="Stream name to release")):
    """
    Libera worker alocado para uma stream e SEMPRE limpa estado canônico residual.
    Idempotente: se não houver worker, ainda remove ownership/mapeamentos restantes.
    """
    started_at = time.monotonic()
    metric_status = "success"
    metric_reason = "not_found"

    worker_name = None
    owner_proxy = None
    changed = False
    response_status = "not_found"

    try:
        with allocation_lock:
            owner_proxy = stream_registry.get(stream, {}).get("proxy_pod") or stream_to_proxy.get(stream)
            worker_name = stream_to_worker.pop(stream, None)

            if worker_name:
                changed = True
                response_status = "released"
                worker_to_stream.pop(worker_name, None)
                cleanup_worker_lifecycle_tracking_locked(worker_name)
                worker_ready_since.pop(worker_name, None)
                worker_not_ready_since.pop(worker_name, None)
                worker_create_started_at.pop(worker_name, None)
                old_uid = worker_pod_uid_by_name.pop(worker_name, None)
                if old_uid:
                    worker_health_failures.pop(old_uid, None)

            if stream_to_proxy.pop(stream, None) is not None:
                changed = True
            if stream_registry.pop(stream, None) is not None:
                changed = True
            if stream_generation.pop(stream, None) is not None:
                changed = True
            if changed:
                cleanup_stream_lifecycle_tracking_locked(stream)
                update_controller_state_gauges_locked()
                persist_state_locked()

        if worker_name:
            logger.info(f"[Release] Released worker {worker_name} from stream '{stream}'")
            try:
                core.delete_namespaced_pod(name=worker_name, namespace=NAMESPACE, grace_period_seconds=0)
                metric_best_effort("recording worker delete metric", lambda: record_worker_delete_metric("success", "released"))
                metric_reason = "released"
                log_controller_event(
                    "worker_deleted",
                    stream=stream,
                    proxy_pod=owner_proxy,
                    worker_pod=worker_name,
                    started_at=started_at,
                    status=LOG_STATUS_DELETED,
                )
            except ApiException as e:
                if e.status == 404:
                    metric_status = "success"
                    metric_reason = "pod_already_deleted"
                    metric_best_effort("recording worker already deleted metric", lambda: record_worker_delete_metric("success", "pod_already_deleted"))
                    logger.info(f"[Release] Worker pod {worker_name} was already deleted")
                    log_controller_event(
                        "worker_deleted",
                        stream=stream,
                        proxy_pod=owner_proxy,
                        worker_pod=worker_name,
                        started_at=started_at,
                        status=LOG_STATUS_ALREADY_DELETED,
                    )
                else:
                    metric_status = "warning"
                    metric_reason = "delete_failed"
                    metric_best_effort("recording Kubernetes delete error metric", lambda: record_kubernetes_api_error("delete", "pod", e))
                    metric_best_effort("recording worker delete failure metric", lambda: record_worker_delete_metric("warning", "delete_failed"))
                    log_controller_event(
                        "worker_deleted",
                        stream=stream,
                        proxy_pod=owner_proxy,
                        worker_pod=worker_name,
                        started_at=started_at,
                        status=LOG_STATUS_DELETE_FAILED,
                        level=logging.WARNING,
                    )
                    logger.warning(f"[Release] Failed deleting worker pod {worker_name}: {e}")
            return {
                "status": response_status,
                "stream": stream,
                "worker": worker_name
            }

        if changed:
            logger.info(f"[Release] Cleaned residual state for stream '{stream}' without active worker")
            metric_reason = "state_cleaned"
            return {"status": "state_cleaned", "stream": stream}

        logger.info(f"[Release] Idempotent replay: stream '{stream}' not found")
        metric_reason = "not_found"
        return {"status": "not_found", "stream": stream}
    except Exception as e:
        metric_status = "error"
        metric_reason = type(e).__name__
        raise
    finally:
        metric_best_effort(
            "recording release metrics",
            lambda: (
                stream_release_duration_seconds.observe(time.monotonic() - started_at),
                stream_release_total.labels(status=metric_status, reason=metric_reason).inc(),
            ),
        )


def register_stream(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(..., description="Proxy pod name"),
    session_id: Optional[str] = Query(None, description="RTMP publish session id"),
    publish_start_ts: Optional[float] = Query(None, description="Proxy-side publish-start epoch seconds"),
):
    """Record the proxy that received exec_publish without handover logic."""
    started_at = time.monotonic()
    metric_status = "error"
    metric_reason = "exception"
    try:
        with allocation_lock:
            previous_generation = generation_to_int(stream_generation.get(stream)) or 0
            stream_registry[stream] = {
                "proxy_pod": proxy_pod,
                "session_id": session_id,
                "publish_start_ts": publish_start_ts,
            }
            stream_to_proxy[stream] = proxy_pod
            stream_generation[stream] = previous_generation + 1
            generation = stream_generation[stream]
            update_controller_state_gauges_locked()
            persist_state_locked()

        log_controller_event(
            "stream_registered",
            stream=stream,
            proxy_pod=proxy_pod,
            started_at=started_at,
            status="registered",
        )
        metric_status = "success"
        metric_reason = "registered"
        return {
            "status": "registered",
            "stream": stream,
            "proxy_pod": proxy_pod,
            "generation": generation,
            "healthcheck_interval_seconds": None,
            "max_failed_healthchecks": None,
        }
    except Exception as e:
        if metric_reason == "exception":
            metric_reason = type(e).__name__
        raise
    finally:
        metric_best_effort(
            "recording registration metrics",
            lambda: (
                stream_registration_duration_seconds.observe(time.monotonic() - started_at),
                stream_registration_total.labels(status=metric_status, reason=metric_reason).inc(),
            ),
        )


@app.post("/streams/started")
def stream_started(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(..., description="Proxy pod that received publish"),
    t_publish_start_proxy: Optional[float] = Query(None, description="Proxy-side publish-start epoch seconds"),
    session_id: Optional[str] = Query(None, description="RTMP publish session id"),
):
    """Single controller entrypoint when proxy publish starts.
    Controller performs register/allocation/start orchestration.
    """
    started_at = time.monotonic()
    received_at = time.time()
    metric_status = "error"
    metric_reason = "exception"
    log_controller_event(
        "publish_received",
        stream=stream,
        proxy_pod=proxy_pod,
        duration_ms=0,
        status=LOG_STATUS_RECEIVED,
    )
    try:
        registration = register_stream(stream=stream, proxy_pod=proxy_pod, session_id=session_id, publish_start_ts=t_publish_start_proxy)
        generation = stream_generation.get(stream)
        if t_publish_start_proxy is not None:
            record_stream_lifecycle_timestamp(
                stream, generation, "t_publish_start_proxy",
                timestamp=t_publish_start_proxy, source="proxy_hook", proxy_pod=proxy_pod,
            )
        else:
            record_stream_lifecycle_timestamp(
                stream, generation, "t_publish_start_proxy",
                timestamp=received_at, source="controller_receive_approximation", proxy_pod=proxy_pod, approximate=True,
            )
        record_stream_lifecycle_timestamp(
            stream, generation, "t_controller_received_event",
            timestamp=received_at, source="controller", proxy_pod=proxy_pod,
        )
        allocation = allocate_worker(stream=stream, proxy_pod=proxy_pod, ownership_already_verified=True)

        replay = registration.get("status") == "idempotent_replay" and allocation.get("status") == "idempotent_replay"
        event_status = "idempotent_replay" if replay else "started_event_processed"
        metric_best_effort(
            "recording stream started metric",
            lambda: stream_started_events_total.labels(
                status=event_status,
                reason=("idempotent_replay" if replay else "state_transition"),
            ).inc(),
        )
        if replay:
            metric_best_effort(
                "recording stream started replay metric",
                lambda: idempotent_replay_total.labels(status="replay", reason="streams_started").inc(),
            )
        log_prefix = "Idempotent replay" if replay else "State changed"
        logger.info(f"[StreamsStarted] {log_prefix} for stream '{stream}' proxy='{proxy_pod}'")

        metric_status = "success"
        metric_reason = event_status
        return {
            "status": event_status,
            "registration": registration,
            "stream": stream,
            "generation": generation,
            "allocation": allocation,
        }
    except Exception as e:
        metric_reason = type(e).__name__
        metric_best_effort(
            "recording stream started error metric",
            lambda: stream_started_events_total.labels(status="error", reason=metric_reason).inc(),
        )
        raise
    finally:
        metric_best_effort(
            "recording stream started controller metrics",
            lambda: (
                stream_event_to_controller_seconds.labels(event="started").observe(time.monotonic() - started_at),
                stream_event_to_controller_total.labels(event="started", status=metric_status, reason=metric_reason).inc(),
            ),
        )



@app.post("/streams/destination-received")
def stream_destination_received(
    stream: str = Query(..., description="Stream name"),
    generation: int = Query(
        ...,
        description="Required stream generation; echo /streams/started generation to reject stale callbacks",
    ),
    t_destination_received: Optional[float] = Query(
        None,
        description="Experimental receiver destination-arrival epoch seconds",
    ),
):
    """Optional experimental receiver callback for external destination arrival.

    The callback is disabled unless CONTROLLER_DESTINATION_CALLBACK_ENABLED is true.
    """
    if not CONTROLLER_DESTINATION_CALLBACK_ENABLED:
        raise HTTPException(status_code=404, detail="destination callback endpoint is disabled")
    if t_destination_received is not None and not is_finite_number(t_destination_received):
        raise HTTPException(
            status_code=400,
            detail="t_destination_received must be finite epoch seconds",
        )

    received_at = time.time()
    timestamp_is_approximate = t_destination_received is None
    timestamp_value = t_destination_received if t_destination_received is not None else received_at
    with allocation_lock:
        current_generation = stream_generation.get(stream)
        registry_entry = stream_registry.get(stream)
        proxy_pod = registry_entry.get("proxy_pod") if registry_entry else None
        if current_generation is None or not registry_entry:
            log_controller_event(
                "destination_received_ignored",
                stream=stream,
                generation=generation,
                proxy_pod=proxy_pod,
                status=LOG_STATUS_IGNORED,
                message="destination callback ignored because stream has no active owner",
            )
            return {
                "status": "ignored",
                "reason": "stream_not_active",
                "stream": stream,
                "timestamp": "t_destination_received",
            }
        if generation != current_generation:
            log_controller_event(
                "destination_received_ignored",
                stream=stream,
                generation=generation,
                proxy_pod=proxy_pod,
                status=LOG_STATUS_IGNORED,
                message=f"stale destination generation current={current_generation}",
            )
            return {
                "status": "ignored",
                "reason": "stale_generation",
                "stream": stream,
                "generation": current_generation,
                "received_generation": generation,
                "timestamp": "t_destination_received",
            }
        resolved_generation = current_generation
        observed = record_stream_lifecycle_timestamp(
            stream,
            resolved_generation,
            "t_destination_received",
            timestamp=timestamp_value,
            source="experimental_receiver",
            proxy_pod=proxy_pod,
            approximate=timestamp_is_approximate,
        )

    log_controller_event(
        "destination_received",
        stream=stream,
        generation=resolved_generation,
        proxy_pod=proxy_pod,
        status=LOG_STATUS_OBSERVED if observed else "duplicate",
        message="t_destination_received observed from experimental receiver" if observed else "duplicate t_destination_received callback",
    )
    return {
        "status": "observed" if observed else "duplicate",
        "reason": "recorded" if observed else "already_observed",
        "stream": stream,
        "generation": resolved_generation,
        "timestamp": "t_destination_received",
        "source": "experimental_receiver",
        "approximate": timestamp_is_approximate,
    }

@app.get("/streams/status")
def stream_status(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: Optional[str] = Query(None, description="Expected proxy owner"),
    session_id: Optional[str] = Query(None, description="Expected RTMP session id"),
    requested_generation: Optional[int] = Query(None, alias="generation", description="Expected stream generation"),
):
    """Returns stream lifecycle state for workers and diagnostics.

    terminal=true is returned only from explicit /streams/ended evidence.
    Absence from proxy /stats is non-terminal because stats can lag or be incomplete during cold start.
    """
    with allocation_lock:
        terminal_entry = lookup_stream_terminal_locked(
            stream,
            proxy_pod=proxy_pod,
            session_id=session_id,
            generation=requested_generation,
        )
        if terminal_entry:
            return {
                "status": "ended_explicitly",
                "stream": stream,
                "active": False,
                "terminal": True,
                "reason": terminal_entry.get("reason", "streams_ended"),
                "proxy_pod": terminal_entry.get("proxy_pod"),
                "worker_pod": stream_to_worker.get(stream),
                "generation": terminal_entry.get("generation"),
                "session_id": terminal_entry.get("session_id"),
                "ended_at": terminal_entry.get("ended_at"),
            }

        current = dict(stream_registry.get(stream) or {})
        owner_proxy = current.get("proxy_pod")
        worker_pod = stream_to_worker.get(stream)
        active_generation = stream_generation.get(stream)

    if not current or not owner_proxy:
        return {
            "status": "registry_missing",
            "stream": stream,
            "active": None,
            "terminal": False,
            "reason": "stream_not_registered_and_no_explicit_end",
            "requested_proxy_pod": proxy_pod,
            "requested_session_id": session_id,
            "requested_generation": int(requested_generation) if requested_generation is not None else None,
        }

    if proxy_pod and owner_proxy != proxy_pod:
        return {
            "status": "owner_mismatch",
            "stream": stream,
            "active": None,
            "terminal": False,
            "reason": "proxy_owner_mismatch",
            "requested_proxy_pod": proxy_pod,
            "owner_proxy_pod": owner_proxy,
            "worker_pod": worker_pod,
            "generation": active_generation,
            "session_id": current.get("session_id"),
        }

    current_session = current.get("session_id")
    if session_id and not current_session:
        return {
            "status": "session_missing",
            "stream": stream,
            "active": None,
            "terminal": False,
            "reason": "current_stream_has_no_session_id",
            "requested_session_id": session_id,
            "session_id": current_session,
            "proxy_pod": owner_proxy,
            "worker_pod": worker_pod,
            "generation": active_generation,
        }
    if session_id and current_session and session_id != current_session:
        return {
            "status": "session_mismatch",
            "stream": stream,
            "active": None,
            "terminal": False,
            "reason": "session_id_mismatch",
            "requested_session_id": session_id,
            "session_id": current_session,
            "proxy_pod": owner_proxy,
            "worker_pod": worker_pod,
            "generation": active_generation,
        }

    if requested_generation is not None:
        current_generation = generation_to_int(active_generation)
        if current_generation is None:
            return {
                "status": "generation_missing",
                "stream": stream,
                "active": None,
                "terminal": False,
                "reason": "current_stream_has_no_generation",
                "requested_generation": int(requested_generation),
                "generation": active_generation,
                "session_id": current_session,
                "proxy_pod": owner_proxy,
                "worker_pod": worker_pod,
            }
        if int(requested_generation) != current_generation:
            return {
                "status": "generation_mismatch",
                "stream": stream,
                "active": None,
                "terminal": False,
                "reason": "stream_generation_mismatch",
                "requested_generation": int(requested_generation),
                "generation": current_generation,
                "session_id": current_session,
                "proxy_pod": owner_proxy,
                "worker_pod": worker_pod,
            }

    activity = get_stream_activity_on_proxy(stream, owner_proxy)
    if activity is True:
        return {
            "status": "active",
            "stream": stream,
            "active": True,
            "terminal": False,
            "reason": "observed_on_proxy",
            "proxy_pod": owner_proxy,
            "worker_pod": worker_pod,
            "generation": active_generation,
            "session_id": current.get("session_id"),
        }

    if activity is False:
        return {
            "status": "not_visible_in_proxy_stats",
            "stream": stream,
            "active": None,
            "terminal": False,
            "reason": "proxy_stats_did_not_contain_stream_not_terminal",
            "proxy_pod": owner_proxy,
            "worker_pod": worker_pod,
            "generation": active_generation,
            "session_id": current.get("session_id"),
        }

    return {
        "status": "unknown",
        "stream": stream,
        "active": None,
        "terminal": False,
        "reason": "proxy_stats_inconclusive",
        "proxy_pod": owner_proxy,
        "worker_pod": worker_pod,
        "generation": active_generation,
        "session_id": current.get("session_id"),
    }

@app.post("/streams/ended")
async def stream_ended(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(None, description="Proxy pod that ended publish"),
    session_id: Optional[str] = Query(None, description="RTMP publish session id"),
    t_publish_start_proxy: Optional[float] = Query(None, description="Proxy-side publish-start epoch seconds echoed from start hook"),
):
    """Single lifecycle entrypoint for exec_publish_done.

    In the simplified controller, publish_done always delegates to release_worker:
    delete the mapped worker if one exists and clean all in-memory/persisted stream
    state.  It intentionally does not perform ownership, session, handover or
    recovery decisions.
    """
    started_at = time.monotonic()
    metric_status = "error"
    metric_reason = "exception"
    log_controller_event(
        "stream_ended_received",
        stream=stream,
        proxy_pod=proxy_pod,
        duration_ms=0,
        status=LOG_STATUS_RECEIVED,
    )
    try:
        release_result = await release_worker(stream=stream)
        replay = release_result.get("status") == "not_found"
        event_status = "not_found" if replay else "ended"
        event_reason = "no_worker_mapping" if replay else "publish_done_cleanup"
        metric_best_effort(
            "recording stream ended metric",
            lambda: stream_ended_events_total.labels(status=event_status, reason=event_reason).inc(),
        )
        if replay:
            metric_best_effort(
                "recording stream ended replay metric",
                lambda: idempotent_replay_total.labels(status="replay", reason="streams_ended").inc(),
            )
        logger.info(
            f"[StreamsEnded] exec_publish_done cleanup for stream '{stream}' "
            f"proxy='{proxy_pod}' release_status='{release_result.get('status')}'"
        )
        metric_status = "success"
        metric_reason = event_status
        return {"status": event_status, "stream": stream, "release": release_result}
    except Exception as e:
        metric_reason = type(e).__name__
        metric_best_effort(
            "recording stream ended error metric",
            lambda: stream_ended_events_total.labels(status="error", reason=metric_reason).inc(),
        )
        raise
    finally:
        metric_best_effort(
            "recording stream ended controller metrics",
            lambda: (
                stream_event_to_controller_seconds.labels(event="ended").observe(time.monotonic() - started_at),
                stream_event_to_controller_total.labels(event="ended", status=metric_status, reason=metric_reason).inc(),
            ),
        )

@app.get('/metrics')
def metrics():
    try:
        payload = generate_latest()
    except Exception as e:
        logger.warning(f"[Metrics] Failed to generate Prometheus payload: {e}")
        payload = b"# metrics temporarily unavailable\n"
    return Response(payload, media_type='text/plain; version=0.0.4; charset=utf-8')
