#!/usr/bin/env python3
"""Run streams and delete a proxy pod during the run."""

from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    build_parser,
    collect_controller_artifacts,
    collect_kubernetes_artifacts,
    config_from_args,
    delete_pod,
    prepare_run,
    select_pod,
    start_ffmpeg_streams,
    stream_key,
    validate_runtime_tools,
    wait_for_processes,
    write_json,
)

SCENARIO = "kill_proxy"


def main() -> int:
    parser = build_parser("Run a LiveEdgeCast proxy-failure experiment.", [SCENARIO])
    args = parser.parse_args()
    config = config_from_args(args)
    logger = prepare_run(config)
    validate_runtime_tools(config, need_kubectl=True, logger=logger)

    keys = [stream_key(config, index) for index in range(1, config.concurrency + 1)]
    processes = start_ffmpeg_streams(config, keys, logger)
    time.sleep(min(config.kill_after_seconds, config.duration_seconds))

    pod_name = select_pod(config, config.proxy_selector, logger)
    kill_result = {"pod": None, "status": "not_found"}
    if pod_name:
        kill_result = delete_pod(config, pod_name, logger)

    results = wait_for_processes(processes, logger)
    k8s_artifacts = collect_kubernetes_artifacts(config, logger)
    controller_artifacts = collect_controller_artifacts(config, logger)
    summary = {
        "started_at": min((p.started_at for p in processes), default=time.time()),
        "ended_at": time.time(),
        "scenario": SCENARIO,
        "stream_keys": keys,
        "killed_proxy": kill_result,
        "processes": results,
        "kubernetes_artifacts": k8s_artifacts,
        "controller_artifacts": controller_artifacts,
    }
    write_json(config.artifact_dir / "summary.json", summary)
    return 0 if pod_name else 2


if __name__ == "__main__":
    raise SystemExit(main())
