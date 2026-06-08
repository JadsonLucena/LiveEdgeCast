# PromQL
- Cold start P50/P95/P99: `histogram_quantile(0.95, sum(rate(worker_ready_duration_seconds_bucket[5m])) by (le))`
- Allocation success: `sum(rate(worker_create_requests_total{status="created"}[5m])) / sum(rate(worker_create_requests_total[5m]))`
- Handover success: `sum(rate(handover_attempts_total{result="accepted"}[5m])) / sum(rate(handover_attempts_total[5m]))`
- Orphan removals: `increase(worker_orphan_deleted_total[1h])`
- Worker MTTR proxy: `histogram_quantile(0.95, sum(rate(worker_recovery_duration_seconds_bucket[15m])) by (le))`
- Active proxies/workers/streams: `sum(proxy_rtmp_stream_active)`, `sum(stream_allocation_active_total)`, `sum(stream_active_total)`
- Resource (real cAdvisor): `rate(container_cpu_usage_seconds_total[5m])`, `container_memory_working_set_bytes`, `rate(container_network_receive_bytes_total[5m])`, `rate(container_network_transmit_bytes_total[5m])`
