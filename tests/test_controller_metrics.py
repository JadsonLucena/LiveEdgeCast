import asyncio
import importlib.util
import json
import logging
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

with patch('kubernetes.config.load_incluster_config', return_value=None), \
     patch('kubernetes.config.load_kube_config', return_value=None):
    spec = importlib.util.spec_from_file_location('controller_main', Path('docker/controller/main.py'))
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)


def reset_state():
    main.stream_to_worker.clear()
    main.worker_to_stream.clear()
    main.stream_to_proxy.clear()
    main.stream_registry.clear()
    main.stream_generation.clear()
    main.proxy_health_failures.clear()
    main.proxy_ready_since.clear()
    main.worker_ready_since.clear()
    main.worker_health_failures.clear()
    main.worker_pod_uid_by_name.clear()
    main.worker_create_started_at.clear()
    main.proxy_rtmp_stats_observed_pods.clear()
    main.reset_controlled_metric_metadata_cache()


def counter_value(counter, **labels):
    return counter.labels(**labels)._value.get()


def sample_value(metric, name, labels=None):
    labels = labels or {}
    for family in metric.collect():
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0


def sample_exists(metric, name, labels=None):
    labels = labels or {}
    for family in metric.collect():
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return True
    return False


class JsonCaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = []
        self.setFormatter(main.StructuredMetadataFormatter())

    def emit(self, record):
        self.events.append(json.loads(self.format(record)))


def capture_controller_events(action):
    handler = JsonCaptureHandler()
    main.logger.addHandler(handler)
    try:
        result = action()
    finally:
        main.logger.removeHandler(handler)
    return result, handler.events


def test_metadata_extraction_precedence_and_sanitization(monkeypatch):
    monkeypatch.setenv('LIVEEDGECAST_TENANT', 'env-tenant')
    monkeypatch.setenv('LIVEEDGECAST_ENVIRONMENT', 'prod')
    monkeypatch.setenv('LIVEEDGECAST_REGION', 'us-west-2')
    request = SimpleNamespace(
        headers={
            'x-liveedgecast-tenant': ' header tenant ',
        },
        query_params={
            'tenant': 'query-tenant',
            'environment': 'stage*blue',
        },
    )

    metadata = main.extract_request_metadata(request)

    assert metadata.labels == {
        'tenant': 'header_tenant',
        'environment': 'stage_blue',
        'region': 'us-west-2',
    }
    assert metadata.sources == {
        'tenant': 'header',
        'environment': 'query',
        'region': 'env',
    }


def test_request_metadata_is_immutable():
    metadata = main.default_request_metadata()

    assert isinstance(metadata.labels, MappingProxyType)
    with pytest.raises(TypeError):
        metadata.labels['tenant'] = 'changed'


def test_blank_higher_priority_metadata_falls_back(monkeypatch):
    monkeypatch.setenv('LIVEEDGECAST_TENANT', 'env-tenant')
    request = SimpleNamespace(
        headers={
            'x-liveedgecast-tenant': '   ',
        },
        query_params={
            'tenant': 'query-tenant',
        },
    )

    metadata = main.extract_request_metadata(request)

    assert metadata.labels['tenant'] == 'query-tenant'
    assert metadata.sources['tenant'] == 'query'


def test_blank_metadata_uses_default_when_no_fallback(monkeypatch):
    monkeypatch.delenv('LIVEEDGECAST_TENANT', raising=False)
    monkeypatch.delenv('CONTROLLER_METADATA_TENANT', raising=False)
    request = SimpleNamespace(
        headers={
            'x-liveedgecast-tenant': '   ',
        },
        query_params={
            'tenant': '',
        },
    )

    metadata = main.extract_request_metadata(request)

    assert metadata.labels['tenant'] == 'unknown'
    assert metadata.sources['tenant'] == 'default'


def test_metric_wrapper_rejects_incorrect_positional_label_count():
    with pytest.raises(ValueError, match="Incorrect label count"):
        main.stream_registration_total.labels('success', 'reason', 'tenant-only', 'environment-only')


def test_metric_wrapper_rejects_mixed_positional_and_keyword_labels():
    with pytest.raises(ValueError, match="Cannot mix"):
        main.stream_registration_total.labels('success', reason='mixed')


def test_labeled_metric_rejects_direct_inc_and_observe():
    with pytest.raises(ValueError, match="Direct inc"):
        main.stream_registration_total.inc()
    with pytest.raises(ValueError, match="Direct observe"):
        main.stream_event_to_controller_seconds.observe(0.1)


def test_unlabeled_counter_direct_inc_uses_controlled_metadata(monkeypatch):
    main.reset_controlled_metric_metadata_cache()
    monkeypatch.setenv('LIVEEDGECAST_TENANT', 'counter-tenant')
    counter = main.controller_counter('test_direct_inc_counter', 'Test direct inc counter')
    labels = {
        'tenant': 'counter-tenant',
        'environment': 'unknown',
        'region': 'unknown',
    }
    before = sample_value(counter, 'test_direct_inc_counter_total', labels)

    counter.inc()

    assert sample_value(counter, 'test_direct_inc_counter_total', labels) == before + 1


def test_env_parsing_helpers_fall_back_for_invalid_values(monkeypatch):
    monkeypatch.setenv("TEST_INT_ENV", "invalid")
    monkeypatch.setenv("TEST_FLOAT_ENV", "0")

    assert main.get_int_env("TEST_INT_ENV", 10, min_value=1) == 10
    assert main.get_float_env("TEST_FLOAT_ENV", 2.0, min_value=0.1) == 2.0


def test_metric_remove_accepts_keyword_labels_without_private_labelnames():
    metric = main.controller_gauge('test_remove_keyword_gauge', 'Test keyword remove gauge', ('proxy_pod',))
    metric.labels(proxy_pod='proxy-remove').set(1)
    assert sample_exists(metric, 'test_remove_keyword_gauge', {'proxy_pod': 'proxy-remove'})

    metric.remove(proxy_pod='proxy-remove')

    assert not sample_exists(metric, 'test_remove_keyword_gauge', {'proxy_pod': 'proxy-remove'})


def test_app_lifespan_starts_and_cancels_background_tasks():
    tasks = []

    class FakeTask:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

        def __await__(self):
            async def cancelled_result():
                return None

            return cancelled_result().__await__()

    def create_task_side_effect(coro):
        coro.close()
        task = FakeTask()
        tasks.append(task)
        return task

    async def run_lifespan():
        async with main.app_lifespan(SimpleNamespace()):
            assert len(tasks) == 5
            assert all(not task.cancelled for task in tasks)

    with patch.object(main.asyncio, 'sleep', new_callable=AsyncMock), \
         patch.object(main, 'recover_state') as recover_state, \
         patch.object(main.asyncio, 'create_task', side_effect=create_task_side_effect):
        asyncio.run(run_lifespan())

    recover_state.assert_called_once_with()
    assert len(tasks) == 5
    assert all(task.cancelled for task in tasks)



def test_parse_proxy_rtmp_stats_xml_counts_streams_publishers_and_clients():
    payload = """
    <rtmp>
      <server>
        <application>
          <name>live</name>
          <live>
            <stream>
              <name>super-secret-stream-key</name>
              <client><id>1</id><publishing/></client>
              <client><id>2</id></client>
            </stream>
            <stream>
              <name>another-secret</name>
              <client publishing="true"><id>3</id></client>
            </stream>
          </live>
        </application>
      </server>
    </rtmp>
    """

    stats = main.parse_proxy_rtmp_stats(payload, "application/xml")

    assert stats.active_streams == 2
    assert stats.active_publishers == 2
    assert stats.active_clients == 3
    assert stats.stream_active == 1



def test_parse_proxy_rtmp_stats_xml_counts_realistic_nginx_rtmp_stat_payload():
    payload = """
    <?xml version="1.0" encoding="utf-8" ?>
    <?xml-stylesheet type="text/xsl" href="stat.xsl" ?>
    <rtmp>
      <nginx_version>1.25.3</nginx_version>
      <server>
        <application>
          <name>live</name>
          <live>
            <stream>
              <name>redacted-stream-key-a</name>
              <time>12345</time>
              <bw_in>1024</bw_in>
              <bw_out>2048</bw_out>
              <client>
                <id>10</id>
                <address>10.244.0.1</address>
                <publishing/>
              </client>
              <client>
                <id>11</id>
                <address>10.244.0.2</address>
              </client>
            </stream>
            <stream>
              <name>redacted-stream-key-b</name>
              <client>
                <id>12</id>
                <address>10.244.0.3</address>
                <publishing/>
              </client>
            </stream>
            <nclients>3</nclients>
          </live>
        </application>
        <application>
          <name>vod</name>
          <live>
            <nclients>0</nclients>
          </live>
        </application>
      </server>
    </rtmp>
    """

    stats = main.parse_proxy_rtmp_stats(payload, "text/xml")

    assert stats.active_streams == 2
    assert stats.active_publishers == 2
    assert stats.active_clients == 3


def test_parse_proxy_rtmp_stats_json_counts_nginx_rtmp_shape():
    payload = json.dumps({
        "rtmp": {
            "server": {
                "application": {
                    "name": "live",
                    "live": {
                        "stream": [
                            {
                                "name": "secret-stream-key",
                                "client": [
                                    {"id": 1, "publishing": {}},
                                    {"id": 2},
                                ],
                            }
                        ]
                    },
                }
            }
        }
    })

    stats = main.parse_proxy_rtmp_stats(payload, "application/json")

    assert stats.active_streams == 1
    assert stats.active_publishers == 1
    assert stats.active_clients == 2


def test_proxy_rtmp_stats_metrics_use_only_proxy_pod_as_rtmp_label():
    stats = main.ProxyRtmpStats(active_streams=1, active_publishers=1, active_clients=2)

    main.set_proxy_rtmp_stats_metrics("proxy-a", stats)

    samples = [
        sample
        for family in main.proxy_rtmp_active_streams.collect()
        for sample in family.samples
        if sample.name == "proxy_rtmp_active_streams" and sample.labels.get("proxy_pod") == "proxy-a"
    ]
    assert samples
    assert samples[-1].value == 1
    assert "stream" not in samples[-1].labels
    assert "streamKey" not in samples[-1].labels
    assert "stream_key" not in samples[-1].labels


def test_collect_proxy_rtmp_stats_once_scrapes_proxy_pods_and_counts_errors():
    reset_state()
    pod = SimpleNamespace(
        metadata=SimpleNamespace(name="proxy-scrape"),
        status=SimpleNamespace(pod_ip="10.0.0.10"),
    )
    response = SimpleNamespace(
        text="<rtmp><server><application><live><stream><client><publishing/></client></stream></live></application></server></rtmp>",
        headers={"Content-Type": "text/xml"},
        raise_for_status=lambda: None,
    )

    with patch.object(main.core, "list_namespaced_pod", return_value=SimpleNamespace(items=[pod])), \
         patch.object(main.requests, "get", return_value=response) as request_get:
        asyncio.run(main.collect_proxy_rtmp_stats_once())

    request_get.assert_called_once_with("http://10.0.0.10:8080/stats", timeout=main.PROXY_RTMP_STATS_TIMEOUT_SECONDS)
    assert sample_value(main.proxy_rtmp_active_streams, "proxy_rtmp_active_streams", {"proxy_pod": "proxy-scrape"}) == 1
    assert sample_value(main.proxy_rtmp_active_publishers, "proxy_rtmp_active_publishers", {"proxy_pod": "proxy-scrape"}) == 1
    assert sample_value(main.proxy_rtmp_active_clients, "proxy_rtmp_active_clients", {"proxy_pod": "proxy-scrape"}) == 1
    assert sample_value(main.proxy_rtmp_stream_active, "proxy_rtmp_stream_active", {"proxy_pod": "proxy-scrape"}) == 1
    assert sample_value(main.proxy_rtmp_stats_up, "proxy_rtmp_stats_up", {"proxy_pod": "proxy-scrape"}) == 1

    before_errors = counter_value(main.proxy_rtmp_stats_scrape_errors_total, proxy_pod="proxy-scrape")
    with patch.object(main.core, "list_namespaced_pod", return_value=SimpleNamespace(items=[pod])), \
         patch.object(main.requests, "get", side_effect=main.requests.Timeout("timed out")):
        asyncio.run(main.collect_proxy_rtmp_stats_once())

    assert counter_value(main.proxy_rtmp_stats_scrape_errors_total, proxy_pod="proxy-scrape") == before_errors + 1
    assert sample_value(main.proxy_rtmp_active_streams, "proxy_rtmp_active_streams", {"proxy_pod": "proxy-scrape"}) == 1
    assert sample_value(main.proxy_rtmp_stream_active, "proxy_rtmp_stream_active", {"proxy_pod": "proxy-scrape"}) == 1
    assert sample_value(main.proxy_rtmp_stats_up, "proxy_rtmp_stats_up", {"proxy_pod": "proxy-scrape"}) == 0


def test_collect_proxy_rtmp_stats_once_counts_discovery_errors():
    reset_state()
    before_errors = sample_value(
        main.proxy_rtmp_stats_discovery_errors_total,
        "proxy_rtmp_stats_discovery_errors_total",
    )

    with patch.object(main.core, "list_namespaced_pod", side_effect=RuntimeError("api unavailable")):
        asyncio.run(main.collect_proxy_rtmp_stats_once())

    assert sample_value(
        main.proxy_rtmp_stats_discovery_errors_total,
        "proxy_rtmp_stats_discovery_errors_total",
    ) == before_errors + 1


def test_collect_proxy_rtmp_stats_once_removes_stale_proxy_metrics():
    reset_state()
    main.set_proxy_rtmp_stats_metrics("proxy-stale", main.ProxyRtmpStats(2, 1, 3))
    main.proxy_rtmp_stats_scrape_errors_total.labels(proxy_pod="proxy-stale").inc()
    main.proxy_rtmp_stats_observed_pods.add("proxy-stale")
    assert sample_exists(
        main.proxy_rtmp_stats_scrape_errors_total,
        "proxy_rtmp_stats_scrape_errors_total",
        {"proxy_pod": "proxy-stale"},
    )

    with patch.object(main.core, "list_namespaced_pod", return_value=SimpleNamespace(items=[])):
        asyncio.run(main.collect_proxy_rtmp_stats_once())

    assert not sample_exists(
        main.proxy_rtmp_active_streams,
        "proxy_rtmp_active_streams",
        {"proxy_pod": "proxy-stale"},
    )
    assert not sample_exists(
        main.proxy_rtmp_stats_up,
        "proxy_rtmp_stats_up",
        {"proxy_pod": "proxy-stale"},
    )
    assert not sample_exists(
        main.proxy_rtmp_stats_scrape_errors_total,
        "proxy_rtmp_stats_scrape_errors_total",
        {"proxy_pod": "proxy-stale"},
    )
    assert "proxy-stale" not in main.proxy_rtmp_stats_observed_pods


def test_fastapi_middleware_sets_context_for_asgi_request(monkeypatch):
    monkeypatch.setenv('LIVEEDGECAST_REGION', 'env-region')

    if not any(getattr(route, 'path', None) == '/__metadata_test__' for route in main.app.routes):
        @main.app.get('/__metadata_test__')
        def metadata_test_route():
            metadata = main.current_request_metadata.get()
            return {
                'labels': dict(metadata.labels),
                'sources': dict(metadata.sources),
            }

    async def request_app():
        messages = []
        received = False
        scope = {
            'type': 'http',
            'asgi': {'version': '3.0'},
            'http_version': '1.1',
            'method': 'GET',
            'scheme': 'http',
            'path': '/__metadata_test__',
            'raw_path': b'/__metadata_test__',
            'query_string': b'environment=stage',
            'headers': [
                (b'host', b'testserver'),
                (b'x-liveedgecast-tenant', b'tenant-a'),
            ],
            'client': ('testclient', 50000),
            'server': ('testserver', 80),
        }

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            return {'type': 'http.disconnect'}

        async def send(message):
            messages.append(message)

        await main.app(scope, receive, send)
        return messages

    messages = asyncio.run(request_app())
    status = next(message['status'] for message in messages if message['type'] == 'http.response.start')
    body = b''.join(
        message.get('body', b'')
        for message in messages
        if message['type'] == 'http.response.body'
    )

    assert status == 200
    assert json.loads(body) == {
        'labels': {
            'tenant': 'tenant-a',
            'environment': 'stage',
            'region': 'env-region',
        },
        'sources': {
            'tenant': 'header',
            'environment': 'query',
            'region': 'env',
        },
    }
    assert main.current_request_metadata.get().labels == {
        'tenant': 'unknown',
        'environment': 'unknown',
        'region': 'unknown',
    }
    assert main.current_log_context.get().values == {
        'experiment_id': 'unknown',
        'scenario': 'unknown',
        'run_id': 'unknown',
    }


def test_structured_formatter_includes_request_metadata():
    metadata = main.RequestMetadata(
        labels={'tenant': 'tenant-a', 'environment': 'stage', 'region': 'us-east-1'},
        sources={'tenant': 'header', 'environment': 'query', 'region': 'env'},
    )
    record = logging.LogRecord(
        name='controller_main',
        level=logging.INFO,
        pathname='docker/controller/main.py',
        lineno=1,
        msg='metadata check',
        args=(),
        exc_info=None,
    )
    token = main.current_request_metadata.set(metadata)
    try:
        payload = json.loads(main.StructuredMetadataFormatter().format(record))
    finally:
        main.current_request_metadata.reset(token)

    assert payload['metadata'] == metadata.labels
    assert payload['metadata_sources'] == metadata.sources


def test_log_context_extraction_precedence_and_sanitization(monkeypatch):
    monkeypatch.setenv('LIVEEDGECAST_EXPERIMENT_ID', 'env-experiment')
    monkeypatch.setenv('CONTROLLER_SCENARIO', 'env-scenario')
    monkeypatch.setenv('CONTROLLER_RUN_ID', 'env-run')
    request = SimpleNamespace(
        headers={
            'x-liveedgecast-experiment-id': ' header experiment ',
        },
        query_params={
            'scenario': 'query*scenario',
        },
    )

    context = main.extract_log_context(request)

    assert context.values == {
        'experiment_id': 'header_experiment',
        'scenario': 'query_scenario',
        'run_id': 'env-run',
    }
    assert context.sources == {
        'experiment_id': 'header',
        'scenario': 'query',
        'run_id': 'env',
    }


def test_log_context_ignores_unprefixed_env(monkeypatch):
    monkeypatch.setenv('RUN_ID', 'generic-run')

    context = main.extract_log_context(SimpleNamespace(headers={}, query_params={}))

    assert context.values['run_id'] == 'unknown'
    assert context.sources['run_id'] == 'default'


def test_structured_formatter_includes_required_event_fields_and_log_context():
    context = main.LogContext(
        values={'experiment_id': 'experiment-a', 'scenario': 'handover', 'run_id': 'run-42'},
        sources={'experiment_id': 'header', 'scenario': 'query', 'run_id': 'env'},
    )
    record = logging.LogRecord(
        name='controller_main',
        level=logging.INFO,
        pathname='docker/controller/main.py',
        lineno=1,
        msg='event check',
        args=(),
        exc_info=None,
    )
    record.event_type = 'publish_received'
    record.stream = 'live'
    record.generation = 3
    record.proxy_pod = 'proxy-1'
    record.worker_pod = 'worker-1'
    record.duration_ms = 12.5
    record.status = 'received'
    token = main.current_log_context.set(context)
    try:
        payload = json.loads(main.StructuredMetadataFormatter().format(record))
    finally:
        main.current_log_context.reset(token)

    assert list(payload)[:len(main.LOG_EVENT_FIELDS)] == list(main.LOG_EVENT_FIELDS)
    assert {field: payload[field] for field in main.LOG_EVENT_FIELDS if field != 'timestamp'} == {
        'event_type': 'publish_received',
        'stream': 'live',
        'generation': 3,
        'proxy_pod': 'proxy-1',
        'worker_pod': 'worker-1',
        'experiment_id': 'experiment-a',
        'scenario': 'handover',
        'run_id': 'run-42',
        'duration_ms': 12.5,
        'status': 'received',
    }


def test_observability_metadata_middleware_sets_and_resets_context(monkeypatch):
    monkeypatch.setenv('LIVEEDGECAST_REGION', 'env-region')
    monkeypatch.setenv('CONTROLLER_RUN_ID', 'env-run')
    request = SimpleNamespace(
        headers={
            'x-liveedgecast-tenant': 'tenant-a',
            'x-liveedgecast-experiment-id': 'experiment-a',
        },
        query_params={
            'environment': 'stage',
            'scenario': 'scenario-a',
        },
    )
    seen_metadata = None
    seen_log_context = None

    async def call_next(_request):
        nonlocal seen_metadata, seen_log_context
        seen_metadata = main.current_request_metadata.get()
        seen_log_context = main.current_log_context.get()
        return {'status': 'ok'}

    result = asyncio.run(main.observability_metadata_middleware(request, call_next))

    assert result == {'status': 'ok'}
    assert seen_metadata.labels == {
        'tenant': 'tenant-a',
        'environment': 'stage',
        'region': 'env-region',
    }
    assert seen_log_context.values == {
        'experiment_id': 'experiment-a',
        'scenario': 'scenario-a',
        'run_id': 'env-run',
    }
    assert main.current_request_metadata.get().labels == {
        'tenant': 'unknown',
        'environment': 'unknown',
        'region': 'unknown',
    }
    assert main.current_log_context.get().values == {
        'experiment_id': 'unknown',
        'scenario': 'unknown',
        'run_id': 'unknown',
    }


def test_metrics_ignore_request_metadata_context_and_use_controlled_env(monkeypatch):
    main.reset_controlled_metric_metadata_cache()
    monkeypatch.setenv('LIVEEDGECAST_TENANT', 'configured-tenant')
    monkeypatch.setenv('LIVEEDGECAST_ENVIRONMENT', 'prod')
    monkeypatch.setenv('LIVEEDGECAST_REGION', 'us-east-1')
    metadata = main.RequestMetadata(
        labels={'tenant': 'request-tenant', 'environment': 'request-env', 'region': 'request-region'},
        sources={'tenant': 'header', 'environment': 'header', 'region': 'header'},
    )
    labels = {'status': 'success', 'reason': 'metadata_context'}
    controlled_labels = {
        **labels,
        'tenant': 'configured-tenant',
        'environment': 'prod',
        'region': 'us-east-1',
    }
    request_labels = {
        **labels,
        'tenant': 'request-tenant',
        'environment': 'request-env',
        'region': 'request-region',
    }
    before = sample_value(
        main.stream_registration_total,
        'stream_registration_total',
        controlled_labels,
    )
    token = main.current_request_metadata.set(metadata)
    try:
        main.stream_registration_total.labels(**labels).inc()
        main.stream_registration_total.labels(**request_labels).inc()
    finally:
        main.current_request_metadata.reset(token)

    assert sample_value(
        main.stream_registration_total,
        'stream_registration_total',
        controlled_labels,
    ) == before + 2
    assert sample_value(
        main.stream_registration_total,
        'stream_registration_total',
        request_labels,
    ) == 0


def test_stream_started_success_and_replay_metrics():
    reset_state()
    started_labels = {"status": "started_event_processed", "reason": "state_transition"}
    replay_labels = {"status": "idempotent_replay", "reason": "idempotent_replay"}
    idempotent_labels = {"status": "replay", "reason": "streams_started"}
    started_before = counter_value(main.stream_started_events_total, **started_labels)
    replay_before = counter_value(main.stream_started_events_total, **replay_labels)
    idempotent_before = counter_value(main.idempotent_replay_total, **idempotent_labels)

    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main, 'resolve_proxy_address', return_value='10.0.0.1'), \
         patch.object(main, 'create_worker_pod_for_stream', return_value='worker-a'):
        assert main.stream_started(stream='live', proxy_pod='proxy-1')['status'] == 'started_event_processed'
        assert main.stream_started(stream='live', proxy_pod='proxy-1')['status'] == 'idempotent_replay'

    assert counter_value(main.stream_started_events_total, **started_labels) == started_before + 1
    assert counter_value(main.stream_started_events_total, **replay_labels) == replay_before + 1
    assert counter_value(main.idempotent_replay_total, **idempotent_labels) == idempotent_before + 1


def test_stream_started_conflict():
    reset_state()
    event_labels = {"event": "started", "status": "error", "reason": "HTTPException"}
    started_error_labels = {"status": "error", "reason": "HTTPException"}
    event_before = counter_value(main.stream_event_to_controller_total, **event_labels)
    started_error_before = counter_value(main.stream_started_events_total, **started_error_labels)
    event_duration_before = sample_value(
        main.stream_event_to_controller_seconds,
        "stream_event_to_controller_seconds_count",
        {"event": "started"},
    )

    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main, 'get_proxy_health_status', return_value='healthy'):
        main.register_stream(stream='live', proxy_pod='proxy-1')
        with pytest.raises(main.HTTPException) as exc_info:
            main.stream_started(stream='live', proxy_pod='proxy-2')

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "stream 'live' already owned by proxy 'proxy-1'"

    assert counter_value(main.stream_event_to_controller_total, **event_labels) == event_before + 1
    assert counter_value(main.stream_started_events_total, **started_error_labels) == started_error_before + 1
    assert sample_value(
        main.stream_event_to_controller_seconds,
        "stream_event_to_controller_seconds_count",
        {"event": "started"},
    ) == event_duration_before + 1


def test_stream_ended_stale_event_is_ignored_without_releasing_active_state():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.stream_to_proxy['live'] = 'proxy-1'
    main.stream_to_worker['live'] = 'worker-a'
    main.worker_to_stream['worker-a'] = 'live'
    stale_labels = {"status": "ignored", "reason": "proxy_owner_mismatch"}
    ended_labels = {"status": "ignored", "reason": "stale_owner_mismatch"}
    stale_before = counter_value(main.stale_ended_events_ignored_total, **stale_labels)
    ended_before = counter_value(main.stream_ended_events_total, **ended_labels)

    with patch.object(main, 'release_worker', new_callable=AsyncMock) as release_worker:
        result, events = capture_controller_events(
            lambda: asyncio.run(main.stream_ended(stream='live', proxy_pod='proxy-2'))
        )

    event_types = [event['event_type'] for event in events]
    assert 'stream_ended_received' in event_types
    assert 'stale_event_ignored' in event_types
    stale_event = next(event for event in events if event['event_type'] == 'stale_event_ignored')
    assert stale_event['stream'] == 'live'
    assert stale_event['proxy_pod'] == 'proxy-2'
    assert stale_event['status'] == 'ignored'
    assert result['status'] == 'stale_ended_ignored'
    assert counter_value(main.stale_ended_events_ignored_total, **stale_labels) == stale_before + 1
    assert counter_value(main.stream_ended_events_total, **ended_labels) == ended_before + 1
    assert result['current_owner'] == 'proxy-1'
    release_worker.assert_not_called()
    assert main.stream_registry['live'] == {'proxy_pod': 'proxy-1'}
    assert main.stream_to_proxy['live'] == 'proxy-1'
    assert main.stream_to_worker['live'] == 'worker-a'
    assert main.worker_to_stream['worker-a'] == 'live'


def test_stream_ended_persists_owner_removal_before_release():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.stream_to_proxy['live'] = 'proxy-1'
    call_order = []

    async def release_worker_side_effect(stream):
        call_order.append('release')
        return {'status': 'released', 'stream': stream, 'worker': 'worker-a'}

    def persist_state_side_effect():
        call_order.append('persist')

    with patch.object(main, 'persist_state_locked', side_effect=persist_state_side_effect), \
         patch.object(main, 'release_worker', new_callable=AsyncMock, side_effect=release_worker_side_effect):
        result = asyncio.run(main.stream_ended(stream='live', proxy_pod='proxy-1'))

    assert result['status'] == 'ended'
    assert call_order == ['persist', 'release']
    assert 'live' not in main.stream_registry
    assert 'live' not in main.stream_to_proxy


def test_stream_ended_idempotent_replay():
    reset_state()
    ended_labels = {"status": "idempotent_replay", "reason": "idempotent_replay"}
    idempotent_labels = {"status": "replay", "reason": "streams_ended"}
    ended_before = counter_value(main.stream_ended_events_total, **ended_labels)
    idempotent_before = counter_value(main.idempotent_replay_total, **idempotent_labels)

    with patch.object(main, 'persist_state_locked', return_value=None):
        assert asyncio.run(main.stream_ended(stream='missing', proxy_pod='proxy-2'))['status'] == 'idempotent_replay'

    assert counter_value(main.stream_ended_events_total, **ended_labels) == ended_before + 1
    assert counter_value(main.idempotent_replay_total, **idempotent_labels) == idempotent_before + 1


def test_release_worker_api_error_metric_path():
    reset_state()
    main.stream_to_worker['live'] = 'worker-a'
    main.worker_to_stream['worker-a'] = 'live'
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.stream_to_proxy['live'] = 'proxy-1'
    release_labels = {"status": "warning", "reason": "delete_failed"}
    release_before = counter_value(main.stream_release_total, **release_labels)
    release_duration_before = sample_value(main.stream_release_duration_seconds, "stream_release_duration_seconds_count")

    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main.core, 'delete_namespaced_pod', side_effect=ApiException(status=500)):
        result, events = capture_controller_events(lambda: asyncio.run(main.release_worker(stream='live')))

    assert result['status'] == 'released'
    delete_event = next(event for event in events if event['event_type'] == 'worker_deleted')
    assert delete_event['stream'] == 'live'
    assert delete_event['proxy_pod'] == 'proxy-1'
    assert delete_event['worker_pod'] == 'worker-a'
    assert delete_event['status'] == 'delete_failed'
    assert counter_value(main.stream_release_total, **release_labels) == release_before + 1
    assert sample_value(main.stream_release_duration_seconds, "stream_release_duration_seconds_count") == release_duration_before + 1


def test_release_worker_pod_not_found_is_idempotent_metric_path():
    reset_state()
    main.stream_to_worker['live'] = 'worker-a'
    main.worker_to_stream['worker-a'] = 'live'
    already_deleted_labels = {"status": "success", "reason": "pod_already_deleted"}
    failed_labels = {"status": "warning", "reason": "delete_failed"}
    already_deleted_before = counter_value(main.stream_release_total, **already_deleted_labels)
    failed_before = counter_value(main.stream_release_total, **failed_labels)

    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main.core, 'delete_namespaced_pod', side_effect=ApiException(status=404)), \
         patch.object(main.logger, 'warning') as warning_log:
        assert asyncio.run(main.release_worker(stream='live'))['status'] == 'released'

    assert counter_value(main.stream_release_total, **already_deleted_labels) == already_deleted_before + 1
    assert counter_value(main.stream_release_total, **failed_labels) == failed_before
    warning_log.assert_not_called()


def test_create_worker_pod_emits_worker_create_events_without_stream_label():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.stream_to_proxy['live'] = 'proxy-1'
    main.stream_generation['live'] = 1
    container = SimpleNamespace(env=[])
    template = SimpleNamespace(
        spec=SimpleNamespace(containers=[container]),
        metadata=SimpleNamespace(labels={'app': 'worker'}),
    )
    deployment = SimpleNamespace(spec=SimpleNamespace(template=template))

    with patch.object(main, 'random_suffix', return_value='abcde'), \
         patch.object(main.apps, 'read_namespaced_deployment', return_value=deployment), \
         patch.object(main.core, 'create_namespaced_pod') as create_pod:
        pod_name, events = capture_controller_events(
            lambda: main.create_worker_pod_for_stream(stream='live', proxy_dns='10.0.0.1')
        )

    assert pod_name == 'worker-live-abcde'
    pod_manifest = create_pod.call_args.kwargs['body']
    assert pod_manifest.metadata.labels == {'app': 'worker'}
    event_types = [event['event_type'] for event in events]
    assert 'worker_create_requested' in event_types
    assert 'worker_created' in event_types
    for event_type in ('worker_create_requested', 'worker_created'):
        event = next(event for event in events if event['event_type'] == event_type)
        assert event['stream'] == 'live'
        assert event['proxy_pod'] == 'proxy-1'
        assert event['worker_pod'] == 'worker-live-abcde'


def test_release_worker_delete_event_includes_proxy_context():
    reset_state()
    main.stream_to_worker['live'] = 'worker-a'
    main.worker_to_stream['worker-a'] = 'live'
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.stream_to_proxy['live'] = 'proxy-1'
    main.stream_generation['live'] = 1

    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main.core, 'delete_namespaced_pod'):
        result, events = capture_controller_events(lambda: asyncio.run(main.release_worker(stream='live')))

    assert result['status'] == 'released'
    delete_event = next(event for event in events if event['event_type'] == 'worker_deleted')
    assert delete_event['stream'] == 'live'
    assert delete_event['proxy_pod'] == 'proxy-1'
    assert delete_event['worker_pod'] == 'worker-a'
    assert delete_event['status'] == 'deleted'


def test_handover_accepted_emitted_after_worker_replacement_succeeds():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-old'}
    main.stream_to_proxy['live'] = 'proxy-old'
    main.stream_to_worker['live'] = 'worker-old'
    main.proxy_health_failures['proxy-old'] = main.PROXY_HEALTHCHECK_MAX_FAILURES

    with patch.object(main, 'resolve_proxy_address', return_value='10.0.0.2'), \
         patch.object(main, 'replace_worker_pod_for_stream_locked', return_value='worker-new') as replace_worker:
        result, events = capture_controller_events(
            lambda: main.try_handover_stream_owner('live', 'proxy-new')
        )

    assert result is True
    replace_worker.assert_called_once_with(stream='live', proxy_dns='10.0.0.2')
    accepted_event = next(event for event in events if event['event_type'] == 'handover_accepted')
    assert accepted_event['stream'] == 'live'
    assert accepted_event['proxy_pod'] == 'proxy-new'
    assert accepted_event['status'] == 'accepted'


def test_handover_accepted_not_emitted_when_worker_replacement_fails():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-old'}
    main.stream_to_proxy['live'] = 'proxy-old'
    main.stream_to_worker['live'] = 'worker-old'
    main.proxy_health_failures['proxy-old'] = main.PROXY_HEALTHCHECK_MAX_FAILURES

    handler = JsonCaptureHandler()
    main.logger.addHandler(handler)
    try:
        with patch.object(main, 'resolve_proxy_address', return_value='10.0.0.2'), \
             patch.object(main, 'replace_worker_pod_for_stream_locked', side_effect=RuntimeError('replace failed')):
            with pytest.raises(RuntimeError, match='replace failed'):
                main.try_handover_stream_owner('live', 'proxy-new')
    finally:
        main.logger.removeHandler(handler)

    assert 'handover_accepted' not in [event['event_type'] for event in handler.events]
    assert main.stream_registry['live'] == {'proxy_pod': 'proxy-old'}
    assert main.stream_to_proxy['live'] == 'proxy-old'
    assert main.stream_to_worker['live'] == 'worker-old'
    assert 'proxy-new' not in main.proxy_health_failures


def test_handover_rollback_deletes_new_worker_after_partial_replacement_failure():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-old'}
    main.stream_to_proxy['live'] = 'proxy-old'
    main.stream_generation['live'] = 1
    main.stream_to_worker['live'] = 'worker-old'
    main.worker_to_stream['worker-old'] = 'live'
    main.proxy_health_failures['proxy-old'] = main.PROXY_HEALTHCHECK_MAX_FAILURES

    def partial_replacement(stream, proxy_dns):
        main.stream_to_worker[stream] = 'worker-new'
        main.worker_to_stream.pop('worker-old', None)
        main.worker_to_stream['worker-new'] = stream
        raise RuntimeError('delete old failed')

    handler = JsonCaptureHandler()
    main.logger.addHandler(handler)
    try:
        with patch.object(main, 'resolve_proxy_address', return_value='10.0.0.2'), \
             patch.object(main, 'replace_worker_pod_for_stream_locked', side_effect=partial_replacement), \
             patch.object(main.core, 'delete_namespaced_pod') as delete_pod:
            with pytest.raises(RuntimeError, match='delete old failed'):
                main.try_handover_stream_owner('live', 'proxy-new')
    finally:
        main.logger.removeHandler(handler)

    assert main.stream_registry['live'] == {'proxy_pod': 'proxy-old'}
    assert main.stream_to_proxy['live'] == 'proxy-old'
    assert main.stream_generation['live'] == 1
    assert main.stream_to_worker['live'] == 'worker-old'
    assert main.worker_to_stream['worker-old'] == 'live'
    assert 'worker-new' not in main.worker_to_stream
    delete_pod.assert_called_once_with(name='worker-new', namespace=main.NAMESPACE, grace_period_seconds=0)
    cleanup_event = next(event for event in handler.events if event['event_type'] == 'worker_deleted')
    assert cleanup_event['stream'] == 'live'
    assert cleanup_event['proxy_pod'] == 'proxy-new'
    assert cleanup_event['worker_pod'] == 'worker-new'
    assert cleanup_event['status'] == 'deleted'


def test_handover_rollback_deletes_worker_from_create_timestamp_delta():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-old'}
    main.stream_to_proxy['live'] = 'proxy-old'
    main.stream_generation['live'] = 1
    main.stream_to_worker['live'] = 'worker-old'
    main.worker_to_stream['worker-old'] = 'live'
    main.proxy_health_failures['proxy-old'] = main.PROXY_HEALTHCHECK_MAX_FAILURES

    def create_then_fail(stream, proxy_dns):
        main.worker_create_started_at['worker-new'] = 123.0
        raise RuntimeError('post create failed')

    handler = JsonCaptureHandler()
    main.logger.addHandler(handler)
    try:
        with patch.object(main, 'resolve_proxy_address', return_value='10.0.0.2'), \
             patch.object(main, 'create_worker_pod_for_stream', side_effect=create_then_fail), \
             patch.object(main.core, 'delete_namespaced_pod') as delete_pod:
            with pytest.raises(RuntimeError, match='post create failed'):
                main.try_handover_stream_owner('live', 'proxy-new')
    finally:
        main.logger.removeHandler(handler)

    assert main.stream_registry['live'] == {'proxy_pod': 'proxy-old'}
    assert main.stream_to_proxy['live'] == 'proxy-old'
    assert main.stream_generation['live'] == 1
    assert main.stream_to_worker['live'] == 'worker-old'
    assert 'worker-new' not in main.worker_create_started_at
    delete_pod.assert_called_once_with(name='worker-new', namespace=main.NAMESPACE, grace_period_seconds=0)
    cleanup_event = next(event for event in handler.events if event['event_type'] == 'worker_deleted')
    assert cleanup_event['worker_pod'] == 'worker-new'
    assert cleanup_event['status'] == 'deleted'


def test_handover_accepted_with_real_worker_replacement_updates_worker_state():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-old'}
    main.stream_to_proxy['live'] = 'proxy-old'
    main.stream_generation['live'] = 1
    main.stream_to_worker['live'] = 'worker-old'
    main.worker_to_stream['worker-old'] = 'live'
    main.worker_ready_since['worker-old'] = 111.0
    main.worker_create_started_at['worker-old'] = 222.0
    main.worker_pod_uid_by_name['worker-old'] = 'uid-old'
    main.worker_health_failures['uid-old'] = 2
    main.proxy_health_failures['proxy-old'] = main.PROXY_HEALTHCHECK_MAX_FAILURES

    with patch.object(main, 'resolve_proxy_address', return_value='10.0.0.2'), \
         patch.object(main, 'create_worker_pod_for_stream', return_value='worker-new'), \
         patch.object(main.core, 'delete_namespaced_pod') as delete_pod:
        result, events = capture_controller_events(
            lambda: main.try_handover_stream_owner('live', 'proxy-new')
        )

    assert result is True
    assert main.stream_registry['live'] == {'proxy_pod': 'proxy-new'}
    assert main.stream_to_proxy['live'] == 'proxy-new'
    assert main.stream_generation['live'] == 2
    assert main.stream_to_worker['live'] == 'worker-new'
    assert main.worker_to_stream['worker-new'] == 'live'
    assert 'worker-old' not in main.worker_to_stream
    assert 'worker-old' not in main.worker_ready_since
    assert 'worker-old' not in main.worker_create_started_at
    assert 'uid-old' not in main.worker_health_failures
    delete_pod.assert_called_once_with(name='worker-old', namespace=main.NAMESPACE, grace_period_seconds=0)
    accepted_event = next(event for event in events if event['event_type'] == 'handover_accepted')
    assert accepted_event['stream'] == 'live'
    assert accepted_event['proxy_pod'] == 'proxy-new'
    assert accepted_event['generation'] == 2
    assert accepted_event['status'] == 'accepted'


def test_allocate_worker_clears_ready_timestamp_for_concurrent_discard():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.stream_to_proxy['live'] = 'proxy-1'
    main.stream_generation['live'] = 1

    def create_worker_side_effect(stream, proxy_dns):
        main.worker_create_started_at['worker-new'] = 123.0
        main.stream_to_worker['live'] = 'worker-existing'
        main.worker_to_stream['worker-existing'] = 'live'
        return 'worker-new'

    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main, 'resolve_proxy_address', return_value='10.0.0.1'), \
         patch.object(main, 'create_worker_pod_for_stream', side_effect=create_worker_side_effect), \
         patch.object(main.core, 'delete_namespaced_pod') as delete_pod:
        result = main.allocate_worker(stream='live', proxy_pod='proxy-1', ownership_already_verified=True)

    assert result['status'] == 'idempotent_replay'
    assert result['name'] == 'worker-existing'
    assert 'worker-new' not in main.worker_create_started_at
    delete_pod.assert_called_once_with(name='worker-new', namespace=main.NAMESPACE, grace_period_seconds=0)


def test_allocate_worker_clears_ready_timestamp_for_stale_ownership_discard():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.stream_to_proxy['live'] = 'proxy-1'
    main.stream_generation['live'] = 1

    def create_worker_side_effect(stream, proxy_dns):
        main.worker_create_started_at['worker-new'] = 123.0
        main.stream_generation['live'] = 2
        return 'worker-new'

    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main, 'resolve_proxy_address', return_value='10.0.0.1'), \
         patch.object(main, 'create_worker_pod_for_stream', side_effect=create_worker_side_effect), \
         patch.object(main.core, 'delete_namespaced_pod') as delete_pod:
        with pytest.raises(main.HTTPException) as exc_info:
            main.allocate_worker(stream='live', proxy_pod='proxy-1', ownership_already_verified=True)

    assert exc_info.value.status_code == 409
    assert 'worker-new' not in main.worker_create_started_at
    delete_pod.assert_called_once_with(name='worker-new', namespace=main.NAMESPACE, grace_period_seconds=0)


def test_worker_uid_change_clears_stale_create_timestamp():
    reset_state()
    main.stream_to_worker['live'] = 'worker-a'
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.worker_pod_uid_by_name['worker-a'] = 'uid-old'
    main.worker_health_failures['uid-old'] = 2
    main.worker_ready_since['worker-a'] = 111.0
    main.worker_create_started_at['worker-a'] = 123.0
    pod = SimpleNamespace(
        metadata=SimpleNamespace(uid='uid-new'),
        status=SimpleNamespace(conditions=[SimpleNamespace(type='Ready', status='False')]),
    )

    async def sleep_side_effect(*args, **kwargs):
        if sleep_side_effect.calls == 0:
            sleep_side_effect.calls += 1
            return None
        raise asyncio.CancelledError

    sleep_side_effect.calls = 0

    with patch.object(main, 'WORKER_HEALTHCHECK_JITTER_SECONDS', 0), \
         patch.object(main.asyncio, 'sleep', side_effect=sleep_side_effect), \
         patch.object(main.core, 'read_namespaced_pod', return_value=pod):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(main.monitor_worker_health())

    assert main.worker_pod_uid_by_name['worker-a'] == 'uid-new'
    assert 'uid-old' not in main.worker_health_failures
    assert 'worker-a' not in main.worker_ready_since
    assert 'worker-a' not in main.worker_create_started_at


def test_handover_worker_replacement_clears_old_per_pod_tracking():
    reset_state()
    main.stream_to_worker['live'] = 'worker-old'
    main.worker_to_stream['worker-old'] = 'live'
    main.worker_ready_since['worker-old'] = 111.0
    main.worker_create_started_at['worker-old'] = 123.0
    main.worker_pod_uid_by_name['worker-old'] = 'uid-old'
    main.worker_health_failures['uid-old'] = 2

    def create_worker_side_effect(stream, proxy_dns):
        main.worker_create_started_at['worker-new'] = 456.0
        return 'worker-new'

    with patch.object(main, 'create_worker_pod_for_stream', side_effect=create_worker_side_effect), \
         patch.object(main.core, 'delete_namespaced_pod') as delete_pod:
        result = main.replace_worker_pod_for_stream_locked(stream='live', proxy_dns='10.0.0.2')

    assert result == 'worker-new'
    assert main.stream_to_worker['live'] == 'worker-new'
    assert main.worker_to_stream['worker-new'] == 'live'
    assert 'worker-old' not in main.worker_to_stream
    assert 'worker-old' not in main.worker_ready_since
    assert 'worker-old' not in main.worker_create_started_at
    assert 'worker-old' not in main.worker_pod_uid_by_name
    assert 'uid-old' not in main.worker_health_failures
    assert main.worker_create_started_at['worker-new'] == 456.0
    delete_pod.assert_called_once_with(name='worker-old', namespace=main.NAMESPACE, grace_period_seconds=0)
