import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch
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


def test_stream_started_success_and_replay_metrics():
    reset_state()
    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main, 'resolve_proxy_address', return_value='10.0.0.1'), \
         patch.object(main, 'create_worker_pod_for_stream', return_value='worker-a'):
        assert main.stream_started(stream='live', proxy_pod='proxy-1')['status'] == 'started_event_processed'
        assert main.stream_started(stream='live', proxy_pod='proxy-1')['status'] == 'idempotent_replay'


def test_stream_started_conflict():
    reset_state()
    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main, 'get_proxy_health_status', return_value='healthy'):
        main.register_stream(stream='live', proxy_pod='proxy-1')
        try:
            main.stream_started(stream='live', proxy_pod='proxy-2')
            assert False
        except Exception as e:
            assert getattr(e, 'status_code', None) == 409


def test_stream_ended_stale_and_idempotent_replay():
    reset_state()
    with patch.object(main, 'persist_state_locked', return_value=None):
        main.stream_registry['live'] = {'proxy_pod': 'proxy-1'}
        main.stream_to_proxy['live'] = 'proxy-1'
        assert asyncio.run(main.stream_ended(stream='live', proxy_pod='proxy-2'))['status'] == 'ended'
        assert asyncio.run(main.stream_ended(stream='missing', proxy_pod='proxy-2'))['status'] == 'idempotent_replay'


def test_release_worker_api_error_metric_path():
    reset_state()
    main.stream_to_worker['live'] = 'worker-a'
    main.worker_to_stream['worker-a'] = 'live'
    with patch.object(main, 'persist_state_locked', return_value=None), \
         patch.object(main.core, 'delete_namespaced_pod', side_effect=ApiException(status=500)):
        assert asyncio.run(main.release_worker(stream='live'))['status'] == 'released'
