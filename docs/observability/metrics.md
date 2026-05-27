# LiveEdgeCast Metrics

Este documento define métricas para controller, proxy e worker (com `experiment_id`, `scenario`, `run_id` como labels de correlação).

## Controller counters
- stream_started_events_total{status,...}
- stream_ended_events_total{status,...}
- stream_active_total
- stream_allocation_active_total
- worker_create_requests_total{status,...}
- worker_delete_requests_total{reason,status,...}
- worker_replacement_total{reason,...}
- worker_orphan_deleted_total{...}
- handover_attempts_total{result,...}
- stale_ended_events_ignored_total{...}
- idempotent_replay_total{operation,...}
- controller_kubernetes_api_errors_total{operation,...}
- controller_state_persist_errors_total{...}
- controller_recovered_allocations_total{...}

## Histograms
- stream_event_to_controller_seconds
- stream_registration_duration_seconds
- stream_allocation_duration_seconds
- worker_create_duration_seconds
- worker_ready_duration_seconds
- stream_release_duration_seconds
- worker_recovery_duration_seconds
- proxy_healthcheck_duration_seconds
- worker_healthcheck_duration_seconds

## Proxy RTMP
- proxy_rtmp_active_streams
- proxy_rtmp_active_publishers
- proxy_rtmp_active_clients
- proxy_rtmp_stream_active{proxy_pod}
- proxy_rtmp_stats_scrape_errors_total

## Worker FFmpeg
- worker_ffmpeg_running
- worker_ffmpeg_health_state
- worker_ffmpeg_last_progress_timestamp_seconds
- worker_ffmpeg_progress_age_seconds
- worker_ffmpeg_out_time_seconds
- worker_ffmpeg_total_size_bytes
- worker_ffmpeg_speed
- worker_ffmpeg_exit_total{exit_code}

Cardinalidade: `streamKey` somente em logs estruturados/debug endpoint.
