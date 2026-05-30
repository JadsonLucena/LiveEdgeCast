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


def counter_value(counter, **labels):
    return counter.labels(**labels)._value.get()


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
    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main, 'get_proxy_health_status', return_value='healthy'):
        main.register_stream(stream='live', proxy_pod='proxy-1')
        with pytest.raises(main.HTTPException) as exc_info:
            main.stream_started(stream='live', proxy_pod='proxy-2')

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "stream 'live' already owned by proxy 'proxy-1'"


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
    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main.core, 'delete_namespaced_pod', side_effect=ApiException(status=500)):
        assert asyncio.run(main.release_worker(stream='live'))['status'] == 'released'
