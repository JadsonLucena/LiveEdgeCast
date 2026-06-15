import asyncio
import importlib.util
import json
import logging
import os
import subprocess
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
kubernetes_exceptions = pytest.importorskip("kubernetes.client.exceptions")
ApiException = kubernetes_exceptions.ApiException

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
    main.stream_generation_high_water.clear()
    main.proxy_health_failures.clear()
    main.proxy_ready_since.clear()
    main.worker_ready_since.clear()
    main.worker_health_failures.clear()
    main.worker_pod_uid_by_name.clear()
    main.worker_create_started_at.clear()
    main.stream_lifecycle_timestamps.clear()
    main.stream_lifecycle_observed_phases.clear()
    main.stream_lifecycle_pending_approximate_phases.clear()
    main.worker_lifecycle_index.clear()
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


def assert_required_log_fields(event):
    assert list(event)[:len(main.LOG_EVENT_FIELDS)] == list(main.LOG_EVENT_FIELDS)
    for field in main.LOG_EVENT_FIELDS:
        assert field in event


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


def test_controller_state_gauges_update_from_locked_state():
    reset_state()
    main.stream_registry.update({'live-1': {'proxy_pod': 'proxy-1'}, 'live-2': {'proxy_pod': 'proxy-2'}})
    main.stream_to_worker['live-1'] = 'worker-1'

    with main.allocation_lock:
        main.update_controller_state_gauges_locked()

    assert sample_value(main.controller_active_streams, 'controller_active_streams') == 2
    assert sample_value(main.controller_active_allocations, 'controller_active_allocations') == 1


def test_persist_state_records_persistence_and_kubernetes_error_metrics():
    reset_state()
    labels = {'operation': 'persist', 'reason': 'patch_failed'}
    kube_labels = {'operation': 'patch', 'resource': 'configmap', 'reason': 'server_error'}
    before = counter_value(main.state_persistence_errors_total, **labels)
    kube_before = counter_value(main.kubernetes_api_errors_total, **kube_labels)

    handler = JsonCaptureHandler()
    main.logger.addHandler(handler)
    try:
        with patch.object(main.core, 'patch_namespaced_config_map', side_effect=ApiException(status=500)):
            with pytest.raises(ApiException):
                with main.allocation_lock:
                    main.persist_state_locked()
    finally:
        main.logger.removeHandler(handler)

    failure_event = next(event for event in handler.events if event['event_type'] == 'state_persistence_failed')
    assert_required_log_fields(failure_event)
    assert failure_event['status'] == 'failed'
    assert counter_value(main.state_persistence_errors_total, **labels) == before + 1
    assert counter_value(main.kubernetes_api_errors_total, **kube_labels) == kube_before + 1


def test_restore_persisted_state_sets_gauges_and_records_invalid_json_metric():
    reset_state()
    invalid_labels = {'operation': 'restore', 'reason': 'invalid_json'}
    invalid_before = counter_value(main.state_persistence_errors_total, **invalid_labels)
    bad_config_map = SimpleNamespace(data={main.STATE_CONFIGMAP_KEY: '{bad json'})

    with patch.object(main.core, 'read_namespaced_config_map', return_value=bad_config_map):
        with main.allocation_lock:
            assert main.restore_persisted_state_locked() == main.StateRestoreResult(restored=False, reason='invalid_json')

    assert counter_value(main.state_persistence_errors_total, **invalid_labels) == invalid_before + 1

    restored_payload = {
        'stream_to_worker': {'live': 'worker-a'},
        'worker_to_stream': {'worker-a': 'live'},
        'stream_to_proxy': {'live': 'proxy-1'},
        'stream_registry': {'live': {'proxy_pod': 'proxy-1'}},
        'stream_generation': {'live': 3},
    }
    good_config_map = SimpleNamespace(data={main.STATE_CONFIGMAP_KEY: json.dumps(restored_payload)})

    with patch.object(main.core, 'read_namespaced_config_map', return_value=good_config_map):
        with main.allocation_lock:
            assert main.restore_persisted_state_locked() == main.StateRestoreResult(restored=True, reason='restored')

    assert sample_value(main.controller_active_streams, 'controller_active_streams') == 1
    assert sample_value(main.controller_active_allocations, 'controller_active_allocations') == 1


def test_restore_drops_registryless_generation_but_preserves_high_water():
    reset_state()
    payload = {
        'stream_to_worker': {'live': 'worker-a'},
        'worker_to_stream': {'worker-a': 'live'},
        'stream_to_proxy': {},
        'stream_registry': {},
        'stream_generation': {'live': 3},
    }
    config_map = SimpleNamespace(data={main.STATE_CONFIGMAP_KEY: json.dumps(payload)})

    with patch.object(main.core, 'read_namespaced_config_map', return_value=config_map):
        with main.allocation_lock:
            assert main.restore_persisted_state_locked() == main.StateRestoreResult(
                restored=True,
                reason='restored',
            )
            assert 'live' not in main.stream_generation
            assert main.stream_generation_high_water['live'] == 3

            main.register_or_refresh_stream('live', 'proxy-2')

    assert main.stream_generation['live'] == 4


def test_register_bumps_stale_registryless_generation():
    reset_state()
    with main.allocation_lock:
        main.stream_generation['live'] = 3
        main.stream_generation_high_water['live'] = 3

        main.register_or_refresh_stream('live', 'proxy-2')

    assert main.stream_generation['live'] == 4
    assert main.stream_generation_high_water['live'] == 4


def test_generation_high_water_prunes_only_inactive_entries():
    reset_state()
    main.stream_generation_high_water.update({
        'old-a': 1,
        'active': 2,
        'old-b': 3,
    })
    main.stream_generation['active'] = 2

    with patch.object(main, 'STREAM_GENERATION_HIGH_WATER_MAX_ENTRIES', 2):
        with main.allocation_lock:
            main.prune_stream_generation_high_water_locked()

    assert 'old-a' not in main.stream_generation_high_water
    assert main.stream_generation_high_water == {
        'active': 2,
        'old-b': 3,
    }


def test_recover_state_records_restored_and_missing_outcomes():
    reset_state()
    restored_labels = {'status': 'success', 'reason': 'restored'}
    skipped_labels = {'status': 'skipped', 'reason': 'no_persisted_state'}
    restored_before = counter_value(main.state_recovery_total, **restored_labels)
    skipped_before = counter_value(main.state_recovery_total, **skipped_labels)

    with patch.object(
        main,
        'restore_persisted_state_locked',
        return_value=main.StateRestoreResult(restored=True, reason='restored'),
    ):
        _, restored_events = capture_controller_events(main.recover_state)
    with patch.object(
        main,
        'restore_persisted_state_locked',
        return_value=main.StateRestoreResult(restored=False, reason='not_found'),
    ):
        _, skipped_events = capture_controller_events(main.recover_state)

    restored_event = next(event for event in restored_events if event['event_type'] == 'state_recovery_completed')
    skipped_event = next(event for event in skipped_events if event['event_type'] == 'state_recovery_completed')
    assert_required_log_fields(restored_event)
    assert_required_log_fields(skipped_event)
    assert restored_event['status'] == 'success'
    assert skipped_event['status'] == 'skipped'
    assert counter_value(main.state_recovery_total, **restored_labels) == restored_before + 1
    assert counter_value(main.state_recovery_total, **skipped_labels) == skipped_before + 1


def test_recover_state_records_restore_failures_without_skipped_metric():
    reset_state()
    invalid_json_labels = {'status': 'error', 'reason': 'invalid_json'}
    skipped_labels = {'status': 'skipped', 'reason': 'no_persisted_state'}
    invalid_before = counter_value(main.state_recovery_total, **invalid_json_labels)
    skipped_before = counter_value(main.state_recovery_total, **skipped_labels)

    bad_config_map = SimpleNamespace(data={main.STATE_CONFIGMAP_KEY: '{bad json'})

    with patch.object(main.core, 'read_namespaced_config_map', return_value=bad_config_map):
        _, events = capture_controller_events(main.recover_state)

    failure_event = next(event for event in events if event['event_type'] == 'state_recovery_failed')
    assert_required_log_fields(failure_event)
    assert failure_event['status'] == 'failed'
    assert counter_value(main.state_recovery_total, **invalid_json_labels) == invalid_before + 1
    assert counter_value(main.state_recovery_total, **skipped_labels) == skipped_before


def test_release_worker_records_delete_and_api_error_metrics():
    reset_state()
    main.stream_to_worker['live'] = 'worker-a'
    main.worker_to_stream['worker-a'] = 'live'
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.stream_generation['live'] = 1
    delete_labels = {'status': 'warning', 'reason': 'delete_failed'}
    kube_labels = {'operation': 'delete', 'resource': 'pod', 'reason': 'forbidden'}
    delete_before = counter_value(main.workers_deleted_total, **delete_labels)
    kube_before = counter_value(main.kubernetes_api_errors_total, **kube_labels)

    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main.core, 'delete_namespaced_pod', side_effect=ApiException(status=403)):
        result = asyncio.run(main.release_worker(stream='live'))

    assert result['status'] == 'released'
    assert counter_value(main.workers_deleted_total, **delete_labels) == delete_before + 1
    assert counter_value(main.kubernetes_api_errors_total, **kube_labels) == kube_before + 1
    assert sample_value(main.controller_active_streams, 'controller_active_streams') == 0
    assert sample_value(main.controller_active_allocations, 'controller_active_allocations') == 0


def test_create_worker_pod_records_kubernetes_create_error_metric():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.stream_generation['live'] = 1
    container = SimpleNamespace(env=[])
    template = SimpleNamespace(spec=SimpleNamespace(containers=[container]), metadata=SimpleNamespace(labels={}))
    deployment = SimpleNamespace(spec=SimpleNamespace(template=template))
    labels = {'operation': 'create', 'resource': 'pod', 'reason': 'rate_limited'}
    before = counter_value(main.kubernetes_api_errors_total, **labels)

    with patch.object(main, 'random_suffix', return_value='abcde'), \
         patch.object(main.apps, 'read_namespaced_deployment', return_value=deployment), \
         patch.object(main.core, 'create_namespaced_pod', side_effect=ApiException(status=429)):
        with pytest.raises(ApiException):
            main.create_worker_pod_for_stream(stream='live', proxy_dns='10.0.0.1')

    assert counter_value(main.kubernetes_api_errors_total, **labels) == before + 1


def test_replace_worker_records_replacement_and_delete_metrics():
    reset_state()
    main.stream_to_worker['live'] = 'worker-old'
    main.worker_to_stream['worker-old'] = 'live'
    replace_labels = {'status': 'success', 'reason': 'replaced'}
    delete_labels = {'status': 'success', 'reason': 'replaced'}
    replace_before = counter_value(main.worker_replacements_total, **replace_labels)
    delete_before = counter_value(main.workers_deleted_total, **delete_labels)

    with patch.object(main, 'create_worker_pod_for_stream', return_value='worker-new'), \
         patch.object(main.core, 'delete_namespaced_pod'):
        result, events = capture_controller_events(lambda: main.replace_worker_pod_for_stream_locked('live', '10.0.0.1'))

    assert result == 'worker-new'
    replaced_event = next(event for event in events if event['event_type'] == 'worker_replaced')
    assert_required_log_fields(replaced_event)
    assert replaced_event['stream'] == 'live'
    assert replaced_event['worker_pod'] == 'worker-new'
    assert replaced_event['status'] == 'success'
    assert counter_value(main.worker_replacements_total, **replace_labels) == replace_before + 1
    assert counter_value(main.workers_deleted_total, **delete_labels) == delete_before + 1
    assert sample_value(main.controller_active_allocations, 'controller_active_allocations') == 1


def test_sweep_orphan_workers_records_orphan_delete_metrics():
    reset_state()
    orphan = SimpleNamespace(metadata=SimpleNamespace(name='worker-orphan'))
    orphan_labels = {'status': 'success', 'reason': 'orphan'}
    all_delete_labels = {'status': 'success', 'reason': 'orphan'}
    orphan_before = counter_value(main.orphan_workers_deleted_total, **orphan_labels)
    all_delete_before = counter_value(main.workers_deleted_total, **all_delete_labels)

    async def sleep_side_effect(*args, **kwargs):
        if sleep_side_effect.calls == 0:
            sleep_side_effect.calls += 1
            return None
        raise asyncio.CancelledError

    sleep_side_effect.calls = 0

    handler = JsonCaptureHandler()
    main.logger.addHandler(handler)
    try:
        with patch.object(main.asyncio, 'sleep', side_effect=sleep_side_effect), \
             patch.object(main.core, 'list_namespaced_pod', return_value=SimpleNamespace(items=[orphan])), \
             patch.object(main.core, 'delete_namespaced_pod'):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(main.sweep_orphan_workers())
    finally:
        main.logger.removeHandler(handler)

    legacy_event = next(event for event in handler.events if event['event_type'] == 'worker_deleted')
    orphan_event = next(event for event in handler.events if event['event_type'] == 'orphan_worker_deleted')
    for event in (legacy_event, orphan_event):
        assert_required_log_fields(event)
        assert event['stream'] is None
        assert event['worker_pod'] == 'worker-orphan'
        assert event['status'] == 'deleted'
    assert counter_value(main.orphan_workers_deleted_total, **orphan_labels) == orphan_before + 1
    assert counter_value(main.workers_deleted_total, **all_delete_labels) == all_delete_before + 1


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
            assert len(tasks) == 6
            assert all(not task.cancelled for task in tasks)

    with patch.object(main.asyncio, 'sleep', new_callable=AsyncMock), \
         patch.object(main, 'recover_state') as recover_state, \
         patch.object(main.asyncio, 'create_task', side_effect=create_task_side_effect):
        asyncio.run(run_lifespan())

    recover_state.assert_called_once_with()
    assert len(tasks) == 6
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


def test_handover_decision_does_not_replace_worker_or_mutate_registry():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-old', 'session_id': 'session-old'}
    main.stream_to_proxy['live'] = 'proxy-old'
    main.stream_generation['live'] = 1
    main.stream_to_worker['live'] = 'worker-old'
    main.proxy_health_failures['proxy-old'] = main.PROXY_HEALTHCHECK_MAX_FAILURES

    with patch.object(main, 'replace_worker_pod_for_stream_locked') as replace_worker, \
         patch.object(main, 'create_worker_pod_for_stream') as create_worker, \
         patch.object(main.core, 'delete_namespaced_pod') as delete_pod:
        result, events = capture_controller_events(
            lambda: main.try_handover_stream_owner('live', 'proxy-new')
        )

    assert result is True
    replace_worker.assert_not_called()
    create_worker.assert_not_called()
    delete_pod.assert_not_called()
    assert main.stream_registry['live'] == {'proxy_pod': 'proxy-old', 'session_id': 'session-old'}
    assert main.stream_to_proxy['live'] == 'proxy-old'
    assert main.stream_generation['live'] == 1
    assert main.stream_to_worker['live'] == 'worker-old'
    assert 'handover_accepted' not in [event['event_type'] for event in events]


def test_register_stream_commits_handover_with_session_and_preserves_context():
    reset_state()
    main.stream_registry['live'] = {
        'proxy_pod': 'proxy-old',
        'session_id': 'session-old',
        'publish_start_ts': '100.0',
    }
    main.stream_to_proxy['live'] = 'proxy-old'
    main.stream_generation['live'] = 1
    main.stream_to_worker['live'] = 'worker-old'
    main.proxy_health_failures['proxy-old'] = main.PROXY_HEALTHCHECK_MAX_FAILURES

    with patch.object(main, 'persist_state_locked', return_value=None):
        result, events = capture_controller_events(
            lambda: main.register_stream(
                stream='live',
                proxy_pod='proxy-new',
                session_id='session-new',
                publish_start_ts=200.0,
            )
        )

    assert result['status'] == 'registered'
    assert result['generation'] == 2
    assert main.stream_registry['live'] == {
        'proxy_pod': 'proxy-new',
        'session_id': 'session-new',
        'publish_start_ts': '200.0',
    }
    assert main.stream_to_proxy['live'] == 'proxy-new'
    assert main.stream_generation['live'] == 2
    accepted_event = next(event for event in events if event['event_type'] == 'handover_accepted')
    assert accepted_event['stream'] == 'live'
    assert accepted_event['proxy_pod'] == 'proxy-new'
    assert accepted_event['generation'] == 2


def test_register_refresh_without_session_preserves_existing_session_context():
    reset_state()
    main.stream_registry['live'] = {
        'proxy_pod': 'proxy-1',
        'session_id': 'session-a',
        'publish_start_ts': '123.0',
    }
    main.stream_to_proxy['live'] = 'proxy-1'
    main.stream_generation['live'] = 7

    with main.allocation_lock:
        stale_worker = main.register_or_refresh_stream('live', 'proxy-1')

    assert stale_worker is None
    assert main.stream_generation['live'] == 7
    assert main.stream_registry['live'] == {
        'proxy_pod': 'proxy-1',
        'session_id': 'session-a',
        'publish_start_ts': '123.0',
    }


def test_register_stream_denies_handover_when_owner_healthy_without_mutation():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-old', 'session_id': 'session-old'}
    main.stream_to_proxy['live'] = 'proxy-old'
    main.stream_generation['live'] = 1
    main.proxy_health_failures['proxy-old'] = 0

    with patch.object(main, 'get_proxy_health_status', return_value='healthy'), \
         patch.object(main, 'persist_state_locked', return_value=None):
        with pytest.raises(main.HTTPException):
            main.register_stream(
                stream='live',
                proxy_pod='proxy-new',
                session_id='session-new',
                publish_start_ts=200.0,
            )

    assert main.stream_registry['live'] == {'proxy_pod': 'proxy-old', 'session_id': 'session-old'}
    assert main.stream_to_proxy['live'] == 'proxy-old'
    assert main.stream_generation['live'] == 1


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

    def create_worker_side_effect(*args, **kwargs):
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


def test_worker_pod_event_records_kubernetes_lifecycle_timestamps():
    reset_state()
    main.stream_generation['live'] = 7
    main.stream_to_worker['live'] = 'worker-live'
    main.worker_to_stream['worker-live'] = 'live'
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            name='worker-live',
            creation_timestamp='2026-06-01T00:00:01Z',
            annotations={
                'liveedgecast.io/stream': 'live',
                'liveedgecast.io/generation': '7',
                'liveedgecast.io/proxy-pod': 'proxy-1',
            },
        ),
        status=SimpleNamespace(
            conditions=[
                SimpleNamespace(type='PodScheduled', status='True', last_transition_time='2026-06-01T00:00:02Z'),
                SimpleNamespace(type='Ready', status='True', last_transition_time='2026-06-01T00:00:04Z'),
            ],
            container_statuses=[
                SimpleNamespace(
                    state=SimpleNamespace(
                        running=SimpleNamespace(started_at='2026-06-01T00:00:03Z')
                    )
                )
            ],
        ),
    )

    main.process_worker_pod_event({'type': 'MODIFIED', 'object': pod})

    entry = main.stream_lifecycle_timestamps['live'][7]
    assert entry['worker_pod'] == 'worker-live'
    assert entry['proxy_pod'] == 'proxy-1'
    assert entry['t_worker_pod_created'] == main.timestamp_to_epoch_seconds('2026-06-01T00:00:01Z')
    assert entry['t_worker_scheduled'] == main.timestamp_to_epoch_seconds('2026-06-01T00:00:02Z')
    assert entry['t_worker_container_started'] == main.timestamp_to_epoch_seconds('2026-06-01T00:00:03Z')
    assert entry['t_worker_ready'] == main.timestamp_to_epoch_seconds('2026-06-01T00:00:04Z')


def test_stale_worker_pod_event_does_not_recreate_lifecycle_state_after_cleanup():
    reset_state()
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            name='worker-old',
            creation_timestamp='2026-06-01T00:00:01Z',
            annotations={
                'liveedgecast.io/stream': 'live',
                'liveedgecast.io/generation': '1',
                'liveedgecast.io/proxy-pod': 'proxy-1',
            },
        ),
        status=SimpleNamespace(
            conditions=[
                SimpleNamespace(type='PodScheduled', status='True', last_transition_time='2026-06-01T00:00:02Z'),
            ],
            container_statuses=[],
        ),
    )

    main.process_worker_pod_event({'type': 'MODIFIED', 'object': pod})

    assert 'live' not in main.stream_lifecycle_timestamps


def test_stale_same_generation_worker_pod_event_is_ignored_when_worker_is_not_current():
    reset_state()
    main.stream_generation['live'] = 1
    main.stream_to_worker['live'] = 'worker-current'
    main.worker_to_stream['worker-current'] = 'live'
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            name='worker-old',
            creation_timestamp='2026-06-01T00:00:01Z',
            annotations={
                'liveedgecast.io/stream': 'live',
                'liveedgecast.io/generation': '1',
                'liveedgecast.io/proxy-pod': 'proxy-1',
            },
        ),
        status=SimpleNamespace(
            conditions=[
                SimpleNamespace(type='PodScheduled', status='True', last_transition_time='2026-06-01T00:00:02Z'),
            ],
            container_statuses=[],
        ),
    )

    main.process_worker_pod_event({'type': 'MODIFIED', 'object': pod})

    assert 'live' not in main.stream_lifecycle_timestamps


def test_approximate_timestamps_do_not_emit_canonical_phase_histograms_until_exact():
    reset_state()
    main.stream_generation['live'] = 1
    labels = {
        'phase': 'worker_create_request_to_pod_created',
        'start_timestamp': 't_worker_create_requested',
        'end_timestamp': 't_worker_pod_created',
    }
    before = sample_value(
        main.stream_lifecycle_phase_seconds,
        'stream_lifecycle_phase_seconds_count',
        labels,
    )
    pending_labels = {
        'phase': 'worker_create_request_to_pod_created',
        'status': 'pending',
        'reason': 'approximate_endpoint',
    }
    pending_before = counter_value(main.stream_lifecycle_phase_observations_total, **pending_labels)

    main.record_stream_lifecycle_timestamp(
        'live', 1, 't_worker_create_requested',
        timestamp='2026-06-01T00:00:01Z', source='controller',
    )
    main.record_stream_lifecycle_timestamp(
        'live', 1, 't_worker_pod_created',
        timestamp='2026-06-01T00:00:02Z', source='kubernetes_create_response', approximate=True,
    )

    assert sample_value(
        main.stream_lifecycle_phase_seconds,
        'stream_lifecycle_phase_seconds_count',
        labels,
    ) == before
    assert counter_value(main.stream_lifecycle_phase_observations_total, **pending_labels) == pending_before + 1
    # Duplicate approximate observations should not repeatedly count pending phases.
    main.record_stream_lifecycle_timestamp(
        'live', 1, 't_worker_pod_created',
        timestamp='2026-06-01T00:00:02Z', source='kubernetes_create_response', approximate=True,
    )
    assert counter_value(main.stream_lifecycle_phase_observations_total, **pending_labels) == pending_before + 1

    main.record_stream_lifecycle_timestamp(
        'live', 1, 't_worker_pod_created',
        timestamp='2026-06-01T00:00:02Z', source='kubernetes_pod_metadata',
    )

    assert sample_value(
        main.stream_lifecycle_phase_seconds,
        'stream_lifecycle_phase_seconds_count',
        labels,
    ) == before + 1


def test_worker_pod_lifecycle_watch_records_processing_errors():
    reset_state()

    class FakeWatch:
        def stream(self, *_args, **_kwargs):
            yield {'type': 'MODIFIED', 'object': object()}

    before = counter_value(
        main.worker_pod_lifecycle_watch_errors_total,
        status='event_processing_error',
        reason='RuntimeError',
    )

    with patch.object(main.watch, 'Watch', return_value=FakeWatch()), \
         patch.object(main, 'process_worker_pod_event', side_effect=RuntimeError('bad event')):
        main.collect_worker_pod_lifecycle_events_once(timeout_seconds=1)

    assert counter_value(
        main.worker_pod_lifecycle_watch_errors_total,
        status='event_processing_error',
        reason='RuntimeError',
    ) == before + 1
    assert sample_value(main.worker_pod_lifecycle_watch_up, 'worker_pod_lifecycle_watch_up') == 1



def test_worker_pod_lifecycle_watch_default_timeout_is_short_for_shutdown():
    reset_state()
    seen_kwargs = {}

    class FakeWatch:
        def stream(self, *_args, **kwargs):
            seen_kwargs.update(kwargs)
            return iter(())

    with patch.object(main.watch, 'Watch', return_value=FakeWatch()):
        main.collect_worker_pod_lifecycle_events_once()

    assert seen_kwargs['timeout_seconds'] == main.WORKER_POD_LIFECYCLE_WATCH_TIMEOUT_SECONDS
    assert seen_kwargs['timeout_seconds'] <= 5


def test_worker_progress_records_first_ffmpeg_progress_once():
    reset_state()
    main.stream_generation['live'] = 3
    main.worker_to_stream['worker-live'] = 'live'
    main.stream_to_worker['live'] = 'worker-live'
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}

    first = main.record_worker_progress_event('live', 'worker-live', 't_ffmpeg_first_progress', 'ffmpeg_progress')
    second = main.record_worker_progress_event('live', 'worker-live', 't_ffmpeg_first_progress', 'ffmpeg_progress')

    assert first['status'] == 'observed'
    assert second['status'] == 'duplicate'
    assert second['reason'] == 'already_observed'
    entry = main.stream_lifecycle_timestamps['live'][3]
    assert 't_ffmpeg_first_progress' in entry
    assert entry['worker_pod'] == 'worker-live'
    assert entry['sources']['t_ffmpeg_first_progress'] == 'ffmpeg_progress'


def test_worker_progress_emits_explicit_ffmpeg_events():
    reset_state()
    main.stream_generation['live'] = 3
    main.worker_to_stream['worker-live'] = 'live'
    main.stream_to_worker['live'] = 'worker-live'
    main.worker_lifecycle_index['worker-live'] = ('live', 3)
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}

    _, started_events = capture_controller_events(
        lambda: main.record_worker_progress_event('live', 'worker-live', 't_ffmpeg_started', 'worker_hook')
    )
    _, progress_events = capture_controller_events(
        lambda: main.record_worker_progress_event('live', 'worker-live', 't_ffmpeg_first_progress', 'ffmpeg_progress')
    )

    started_event = next(event for event in started_events if event['event_type'] == 'ffmpeg_started')
    progress_event = next(event for event in progress_events if event['event_type'] == 'ffmpeg_first_progress')
    for event in (started_event, progress_event):
        assert_required_log_fields(event)
        assert event['stream'] == 'live'
        assert event['generation'] == 3
        assert event['proxy_pod'] == 'proxy-1'
        assert event['worker_pod'] == 'worker-live'
        assert event['status'] == 'observed'


def test_proxy_healthcheck_emits_proxy_failure_detected_event():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.stream_to_proxy['live'] = 'proxy-1'
    main.stream_to_worker['live'] = 'worker-a'
    main.worker_to_stream['worker-a'] = 'live'
    main.stream_generation['live'] = 7
    main.proxy_health_failures['proxy-1'] = main.PROXY_HEALTHCHECK_MAX_FAILURES - 1

    async def sleep_side_effect(*args, **kwargs):
        if sleep_side_effect.calls == 0:
            sleep_side_effect.calls += 1
            return None
        raise asyncio.CancelledError

    sleep_side_effect.calls = 0
    handler = JsonCaptureHandler()
    main.logger.addHandler(handler)
    try:
        with patch.object(main, 'PROXY_HEALTHCHECK_JITTER_SECONDS', 0), \
             patch.object(main.asyncio, 'sleep', side_effect=sleep_side_effect), \
             patch.object(main, 'get_proxy_health_status', return_value='unhealthy'), \
             patch.object(main.core, 'delete_namespaced_pod'), \
             patch.object(main, 'persist_state_locked', return_value=None):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(main.monitor_stream_registry_health())
    finally:
        main.logger.removeHandler(handler)

    failure_event = next(event for event in handler.events if event['event_type'] == 'proxy_failure_detected')
    assert_required_log_fields(failure_event)
    assert failure_event['stream'] == 'live'
    assert failure_event['generation'] == 7
    assert failure_event['proxy_pod'] == 'proxy-1'
    assert failure_event['status'] == 'unhealthy'


def test_controller_metrics_do_not_add_worker_or_generation_cardinality_labels():
    forbidden_labels = {'streamKey', 'worker_pod', 'generation'}
    for metric_name in (
        'worker_replacements_total',
        'orphan_workers_deleted_total',
        'state_recovery_total',
        'state_persistence_errors_total',
        'proxy_healthcheck_total',
    ):
        metric = getattr(main, metric_name)
        assert forbidden_labels.isdisjoint(metric._base_label_names)


def test_failed_worker_create_does_not_remove_existing_create_timestamp():
    reset_state()
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.stream_to_proxy['live'] = 'proxy-1'
    main.stream_generation['live'] = 1
    main.stream_lifecycle_timestamps['live'] = {
        1: {
            't_worker_create_requested': 100.0,
            'sources': {'t_worker_create_requested': 'controller'},
            'approximations': {'t_worker_create_requested': False},
            'worker_pod': 'worker-existing',
        }
    }
    container = SimpleNamespace(env=[])
    template = SimpleNamespace(
        spec=SimpleNamespace(containers=[container]),
        metadata=SimpleNamespace(labels={'app': 'worker'}),
    )
    deployment = SimpleNamespace(spec=SimpleNamespace(template=template))

    with patch.object(main, 'random_suffix', return_value='abcde'), \
         patch.object(main.apps, 'read_namespaced_deployment', return_value=deployment), \
         patch.object(main.core, 'create_namespaced_pod', side_effect=RuntimeError('create failed')):
        with pytest.raises(RuntimeError, match='create failed'):
            main.create_worker_pod_for_stream(stream='live', proxy_dns='10.0.0.1')

    entry = main.stream_lifecycle_timestamps['live'][1]
    assert entry['t_worker_create_requested'] == 100.0
    assert entry['worker_pod'] == 'worker-existing'
    assert 'worker-live-abcde' not in main.worker_lifecycle_index


def test_worker_and_proxy_scripts_url_encode_controller_callbacks():
    worker_script = Path('docker/worker/worker_stream_runner.sh').read_text()
    proxy_script = Path('docker/proxy/on_publish_start.sh').read_text()
    worker_dockerfile = Path('docker/worker/Dockerfile').read_text()

    assert 'curl' in worker_dockerfile
    assert 'chmod +x /scripts/*.sh /scripts/metrics_exporter.py' in worker_dockerfile
    assert '--data-urlencode "stream=${STREAM_KEY}"' in worker_script
    assert '--connect-timeout "${CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS:-1}"' in worker_script
    assert '--data-urlencode "worker_pod=${WORKER_POD}"' in worker_script
    assert '-progress "$PROGRESS_FILE"' in worker_script
    assert 'date +%s%N' not in worker_script
    assert 'FFMPEG_RUN_ID="${EPOCHREALTIME:-$(date +%s)}-${FFMPEG_PID}-${RANDOM}"' in worker_script
    assert '--data-urlencode "stream=${STREAM_NAME}"' in proxy_script
    worker_deployment = Path('k8s/worker-deployment.yaml').read_text()
    assert 'containerPort: 9113' in worker_deployment
    assert 'name: metrics' in worker_deployment
    assert 'CONTROLLER_API="${CONTROLLER_API:-http://controller.media.svc.cluster.local:8000}"' in proxy_script
    assert '--connect-timeout "${CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS:-1}"' in proxy_script
    assert '--data-urlencode "proxy_pod=${PROXY_POD}"' in proxy_script


def test_worker_runner_reports_first_progress_once_with_stubbed_ffmpeg_and_curl(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    curl_log = tmp_path / 'curl.log'
    ffmpeg_log = tmp_path / 'ffmpeg.log'
    curl_stub = bin_dir / 'curl'
    curl_stub.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$CURL_LOG"\n'
        'exit 0\n'
    )
    ffmpeg_stub = bin_dir / 'ffmpeg'
    ffmpeg_stub.write_text(
        '#!/bin/sh\n'
        'progress=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "-progress" ]; then shift; progress="$1"; fi\n'
        '  shift || true\n'
        'done\n'
        'printf "frame=1" > "$progress"\n'
        'sleep 0.3\n'
        'if grep -q "/workers/progress" "$CURL_LOG" 2>/dev/null; then exit 77; fi\n'
        'printf "\\nprogress=continue\\n" >> "$progress"\n'
        'printf "ffmpeg-stub-ran\\n" >> "$FFMPEG_LOG"\n'
        'exit 0\n'
    )
    curl_stub.chmod(0o755)
    ffmpeg_stub.chmod(0o755)
    env = {
        **os.environ,
        'PATH': f'{bin_dir}:{os.environ["PATH"]}',
        'STREAM_KEY': 'live&special',
        'PROXY_DNS': 'proxy.local',
        'RTMP_PUSH_BASE_URL': 'rtmp://target/live',
        'CONTROLLER_API': 'http://controller.test',
        'HOSTNAME': 'worker-test',
        'CURL_LOG': str(curl_log),
        'FFMPEG_LOG': str(ffmpeg_log),
        'PROGRESS_NOTIFY_POLL_SECONDS': '0.05',
    }

    result = subprocess.run(
        ['bash', 'docker/worker/worker_stream_runner.sh'],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    curl_calls = curl_log.read_text().splitlines()
    progress_calls = [call for call in curl_calls if '/workers/progress' in call]
    started_calls = [call for call in curl_calls if '/workers/ffmpeg/started' in call]
    assert len(started_calls) == 1
    assert len(progress_calls) == 1
    assert ffmpeg_log.read_text().strip() == 'ffmpeg-stub-ran'



def test_worker_runner_continues_when_controller_callbacks_fail(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    curl_log = tmp_path / 'curl.log'
    ffmpeg_log = tmp_path / 'ffmpeg.log'
    curl_stub = bin_dir / 'curl'
    curl_stub.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$CURL_LOG"\n'
        'exit 22\n'
    )
    ffmpeg_stub = bin_dir / 'ffmpeg'
    ffmpeg_stub.write_text(
        '#!/bin/sh\n'
        'progress=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "-progress" ]; then shift; progress="$1"; fi\n'
        '  shift || true\n'
        'done\n'
        'printf "%s\\n" "frame=1" > "$progress"\n'
        'printf "ffmpeg-stub-ran\\n" >> "$FFMPEG_LOG"\n'
        'exit 0\n'
    )
    curl_stub.chmod(0o755)
    ffmpeg_stub.chmod(0o755)
    env = {
        **os.environ,
        'PATH': f'{bin_dir}:{os.environ["PATH"]}',
        'STREAM_KEY': 'live',
        'PROXY_DNS': 'proxy.local',
        'RTMP_PUSH_BASE_URL': 'rtmp://target/live',
        'CONTROLLER_API': 'http://controller.test',
        'HOSTNAME': 'worker-test',
        'CURL_LOG': str(curl_log),
        'FFMPEG_LOG': str(ffmpeg_log),
        'CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS': '1',
        'CONTROLLER_CALLBACK_MAX_TIME_SECONDS': '1',
    }

    result = subprocess.run(
        ['bash', 'docker/worker/worker_stream_runner.sh'],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert '/workers/ffmpeg/started' in curl_log.read_text()
    assert '/workers/progress' in curl_log.read_text()
    assert ffmpeg_log.read_text().strip() == 'ffmpeg-stub-ran'


def test_worker_runner_retries_first_progress_callback_after_transient_failure(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    curl_log = tmp_path / 'curl.log'
    progress_attempts = tmp_path / 'progress_attempts'
    ffmpeg_log = tmp_path / 'ffmpeg.log'
    curl_stub = bin_dir / 'curl'
    curl_stub.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$CURL_LOG"\n'
        'case "$*" in\n'
        '  */workers/progress*)\n'
        '    attempts=0\n'
        '    if [ -f "$PROGRESS_ATTEMPTS" ]; then attempts=$(cat "$PROGRESS_ATTEMPTS"); fi\n'
        '    attempts=$((attempts + 1))\n'
        '    printf "%s" "$attempts" > "$PROGRESS_ATTEMPTS"\n'
        '    if [ "$attempts" -eq 1 ]; then exit 22; fi\n'
        '    ;;\n'
        'esac\n'
        'exit 0\n'
    )
    ffmpeg_stub = bin_dir / 'ffmpeg'
    ffmpeg_stub.write_text(
        '#!/bin/sh\n'
        'progress=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "-progress" ]; then shift; progress="$1"; fi\n'
        '  shift || true\n'
        'done\n'
        'printf "%s\\n" "frame=1" "progress=continue" > "$progress"\n'
        'sleep 0.25\n'
        'printf "ffmpeg-stub-ran\\n" >> "$FFMPEG_LOG"\n'
        'exit 0\n'
    )
    curl_stub.chmod(0o755)
    ffmpeg_stub.chmod(0o755)
    env = {
        **os.environ,
        'PATH': f'{bin_dir}:{os.environ["PATH"]}',
        'STREAM_KEY': 'retry-progress',
        'PROXY_DNS': 'proxy.local',
        'RTMP_PUSH_BASE_URL': 'rtmp://target/live',
        'CONTROLLER_API': 'http://controller.test',
        'HOSTNAME': 'worker-test',
        'CURL_LOG': str(curl_log),
        'PROGRESS_ATTEMPTS': str(progress_attempts),
        'FFMPEG_LOG': str(ffmpeg_log),
        'PROGRESS_NOTIFY_POLL_SECONDS': '0.05',
    }

    result = subprocess.run(
        ['bash', 'docker/worker/worker_stream_runner.sh'],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    progress_calls = [call for call in curl_log.read_text().splitlines() if '/workers/progress' in call]
    assert len(progress_calls) == 2
    assert progress_attempts.read_text() == '2'
    assert ffmpeg_log.read_text().strip() == 'ffmpeg-stub-ran'


def test_worker_progress_records_under_allocation_lock_to_prevent_stale_race():
    reset_state()
    main.stream_generation['live'] = 5
    main.worker_to_stream['worker-live'] = 'live'
    main.stream_to_worker['live'] = 'worker-live'
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    seen_allocated_workers = []

    class MutatingRLock:
        def __init__(self):
            self.depth = 0

        def __enter__(self):
            self.depth += 1
            return self

        def __exit__(self, exc_type, exc, tb):
            self.depth -= 1
            if self.depth == 0:
                main.stream_to_worker['live'] = 'worker-new'
            return False

    original_record_timestamp = main.record_stream_lifecycle_timestamp

    def record_with_allocation_snapshot(*args, **kwargs):
        seen_allocated_workers.append(main.stream_to_worker.get('live'))
        return original_record_timestamp(*args, **kwargs)

    with patch.object(main, 'allocation_lock', MutatingRLock()), \
         patch.object(main, 'record_stream_lifecycle_timestamp', side_effect=record_with_allocation_snapshot):
        result = main.record_worker_progress_event('live', 'worker-live', 't_ffmpeg_started', 'worker_hook')

    assert result['status'] == 'observed'
    assert seen_allocated_workers == ['worker-live']
    assert main.stream_to_worker['live'] == 'worker-new'


def test_worker_progress_ignores_stale_or_unmapped_workers():
    reset_state()
    main.stream_generation['live'] = 4
    main.worker_to_stream['worker-current'] = 'live'
    main.stream_to_worker['live'] = 'worker-current'
    main.worker_lifecycle_index['worker-old'] = ('live', 3)

    stale = main.record_worker_progress_event('live', 'worker-old', 't_ffmpeg_first_progress', 'ffmpeg_progress')
    unknown = main.record_worker_progress_event('live', 'worker-unknown', 't_ffmpeg_first_progress', 'ffmpeg_progress')

    assert stale == {
        'status': 'ignored',
        'reason': 'stale_worker',
        'stream': 'live',
        'worker_pod': 'worker-old',
        'timestamp': 't_ffmpeg_first_progress',
    }
    assert unknown == {
        'status': 'ignored',
        'reason': 'unmapped_worker',
        'stream': 'live',
        'worker_pod': 'worker-unknown',
        'timestamp': 't_ffmpeg_first_progress',
    }
    assert 'live' not in main.stream_lifecycle_timestamps


def test_release_worker_cleans_lifecycle_state():
    reset_state()
    main.stream_to_worker['live'] = 'worker-a'
    main.worker_to_stream['worker-a'] = 'live'
    main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
    main.stream_to_proxy['live'] = 'proxy-1'
    main.stream_generation['live'] = 1
    main.worker_lifecycle_index['worker-a'] = ('live', 1)
    main.stream_lifecycle_timestamps['live'] = {1: {'t_ffmpeg_first_progress': 1.0}}
    main.stream_lifecycle_observed_phases.add(('live', 1, 'controller_to_first_progress'))

    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main.core, 'delete_namespaced_pod'):
        result = asyncio.run(main.release_worker(stream='live'))

    assert result['status'] == 'released'
    assert 'live' not in main.stream_lifecycle_timestamps
    assert ('live', 1, 'controller_to_first_progress') not in main.stream_lifecycle_observed_phases
    assert 'worker-a' not in main.worker_lifecycle_index


def test_stream_started_records_proxy_and_controller_lifecycle_timestamps():
    reset_state()
    publish_ts = main.timestamp_to_epoch_seconds('2026-06-01T00:00:00Z')

    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main, 'resolve_proxy_address', return_value='10.0.0.1'), \
         patch.object(main, 'create_worker_pod_for_stream', return_value='worker-a'):
        result = main.stream_started(stream='live', proxy_pod='proxy-1', t_publish_start_proxy=publish_ts)

    assert result['status'] == 'started_event_processed'
    entry = main.stream_lifecycle_timestamps['live'][1]
    assert entry['t_publish_start_proxy'] == publish_ts
    assert entry['sources']['t_publish_start_proxy'] == 'proxy_hook'
    assert 't_controller_received_event' in entry


def test_destination_received_requires_generation_query_parameter():
    schema = main.app.openapi()
    parameters = schema['paths']['/streams/destination-received']['post']['parameters']
    generation_parameter = next(
        parameter for parameter in parameters
        if parameter['name'] == 'generation'
    )

    assert generation_parameter['in'] == 'query'
    assert generation_parameter['required'] is True


@pytest.mark.parametrize('timestamp', [float('nan'), float('inf'), float('-inf')])
def test_destination_received_rejects_non_finite_timestamp(timestamp):
    reset_state()
    with patch.object(main, 'CONTROLLER_DESTINATION_CALLBACK_ENABLED', True):
        with pytest.raises(main.HTTPException) as exc_info:
            main.stream_destination_received(
                stream='live',
                generation=1,
                t_destination_received=timestamp,
            )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == 't_destination_received must be finite epoch seconds'


def test_destination_received_ignores_registryless_generation():
    reset_state()
    main.stream_generation['live'] = 3
    main.stream_generation_high_water['live'] = 3
    main.stream_lifecycle_timestamps['live'] = {
        3: {
            'stream': 'live',
            'generation': 3,
            't_publish_start_proxy': 30.0,
            't_controller_received_event': 31.0,
            't_ffmpeg_first_progress': 32.0,
            'sources': {},
            'approximations': {},
        }
    }

    with patch.object(main, 'CONTROLLER_DESTINATION_CALLBACK_ENABLED', True):
        result = main.stream_destination_received(
            stream='live',
            generation=3,
            t_destination_received=33.0,
        )

    assert result['status'] == 'ignored'
    assert result['reason'] == 'stream_not_active'
    assert 't_destination_received' not in main.stream_lifecycle_timestamps['live'][3]


def test_reused_stream_generation_rejects_delayed_destination_callback():
    reset_state()
    with main.allocation_lock:
        main.register_or_refresh_stream('live', 'proxy-1')
        first_generation = main.stream_generation['live']
        main.stream_lifecycle_timestamps['live'] = {
            first_generation: {
                'stream': 'live',
                'generation': first_generation,
                't_publish_start_proxy': 10.0,
                't_controller_received_event': 11.0,
                't_ffmpeg_first_progress': 12.0,
                'sources': {},
                'approximations': {},
            }
        }
        main.cleanup_stream_lifecycle_tracking_locked('live')
        main.stream_registry.pop('live', None)
        main.stream_to_proxy.pop('live', None)
        main.stream_generation.pop('live', None)

        main.register_or_refresh_stream('live', 'proxy-2')
        second_generation = main.stream_generation['live']
        main.stream_lifecycle_timestamps['live'] = {
            second_generation: {
                'stream': 'live',
                'generation': second_generation,
                't_publish_start_proxy': 20.0,
                't_controller_received_event': 21.0,
                't_ffmpeg_first_progress': 22.0,
                'sources': {},
                'approximations': {},
            }
        }

    assert first_generation == 1
    assert second_generation == 2

    with patch.object(main, 'CONTROLLER_DESTINATION_CALLBACK_ENABLED', True):
        stale = main.stream_destination_received(
            stream='live',
            generation=first_generation,
            t_destination_received=13.0,
        )
        accepted = main.stream_destination_received(
            stream='live',
            generation=second_generation,
            t_destination_received=23.0,
        )

    assert stale['status'] == 'ignored'
    assert stale['reason'] == 'stale_generation'
    assert accepted['status'] == 'observed'
    assert (
        main.stream_lifecycle_timestamps['live'][second_generation]['t_destination_received']
        == 23.0
    )


def test_k8s_name_fragment_sanitizes_stream_key_and_preserves_uniqueness():
    unsafe = "../Live Stream:@ç/with spaces+symbols"
    fragment = main.k8s_name_fragment(unsafe)

    assert fragment == fragment.lower()
    assert len(fragment) <= main.K8S_NAME_FRAGMENT_MAX_LENGTH
    assert fragment.strip("-") == fragment
    assert all(ch.isalnum() or ch == "-" for ch in fragment)
    assert main.k8s_name_fragment("a/b") != main.k8s_name_fragment("a b")


def test_existing_worker_replay_rejects_missing_generation_annotation(monkeypatch):
    reset_state()
    monkeypatch.setattr(main, "ALLOW_LEGACY_WORKER_REPLAY_WITHOUT_GENERATION", False)
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            annotations={"liveedgecast.io/session-id": "session-1"},
            deletion_timestamp=None,
        ),
        status=SimpleNamespace(phase="Running"),
    )

    with patch.object(main.core, "read_namespaced_pod", return_value=pod):
        usable, reason, should_delete = main.inspect_existing_worker_for_replay(
            "worker-1",
            expected_generation=3,
            expected_session_id="session-1",
        )

    assert usable is False
    assert reason == "missing_generation_annotation"
    assert should_delete is True


def test_existing_worker_replay_rejects_missing_session_annotation(monkeypatch):
    reset_state()
    monkeypatch.setattr(main, "ALLOW_LEGACY_WORKER_REPLAY_WITHOUT_SESSION", False)
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            annotations={"liveedgecast.io/generation": "3"},
            deletion_timestamp=None,
        ),
        status=SimpleNamespace(phase="Running"),
    )

    with patch.object(main.core, "read_namespaced_pod", return_value=pod):
        usable, reason, should_delete = main.inspect_existing_worker_for_replay(
            "worker-1",
            expected_generation=3,
            expected_session_id="session-1",
        )

    assert usable is False
    assert reason == "missing_session_annotation"
    assert should_delete is True


def test_existing_worker_replay_accepts_matching_generation_and_session():
    reset_state()
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            annotations={
                "liveedgecast.io/generation": "3",
                "liveedgecast.io/session-id": "session-1",
            },
            deletion_timestamp=None,
        ),
        status=SimpleNamespace(phase="Running"),
    )

    with patch.object(main.core, "read_namespaced_pod", return_value=pod):
        usable, reason, should_delete = main.inspect_existing_worker_for_replay(
            "worker-1",
            expected_generation=3,
            expected_session_id="session-1",
        )

    assert usable is True
    assert reason == "usable"
    assert should_delete is False
