import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
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


def counter_value(counter, **labels):
    return counter.labels(**labels)._value.get()


def sample_value(metric, name, labels=None):
    labels = labels or {}
    for family in metric.collect():
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0


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


def test_metrics_use_current_metadata_context():
    metadata = main.RequestMetadata(
        labels={'tenant': 'tenant-a', 'environment': 'prod', 'region': 'us-east-1'},
        sources={'tenant': 'header', 'environment': 'header', 'region': 'header'},
    )
    labels = {'status': 'success', 'reason': 'metadata_context'}
    before = counter_value(main.stream_registration_total, **labels)
    token = main.current_request_metadata.set(metadata)
    try:
        main.stream_registration_total.labels(**labels).inc()
    finally:
        main.current_request_metadata.reset(token)

    assert counter_value(
        main.stream_registration_total,
        **labels,
        tenant='tenant-a',
        environment='prod',
        region='us-east-1',
    ) == before + 1


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
        result = asyncio.run(main.stream_ended(stream='live', proxy_pod='proxy-2'))

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
    release_labels = {"status": "warning", "reason": "delete_failed"}
    release_before = counter_value(main.stream_release_total, **release_labels)
    release_duration_before = sample_value(main.stream_release_duration_seconds, "stream_release_duration_seconds_count")

    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main.core, 'delete_namespaced_pod', side_effect=ApiException(status=500)):
        assert asyncio.run(main.release_worker(stream='live'))['status'] == 'released'

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
