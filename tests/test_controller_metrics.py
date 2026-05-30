import asyncio
import importlib.util
from pathlib import Path
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
    main.worker_create_started_at.clear()


def counter_value(counter, **labels):
    return counter.labels(**labels)._value.get()


def sample_value(metric, name, labels=None):
    labels = labels or {}
    for family in metric.collect():
        for sample in family.samples:
            if sample.name == name and sample.labels == labels:
                return sample.value
    return 0


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
