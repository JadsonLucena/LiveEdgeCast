#!/usr/bin/env python3
"""Run streams and delete a proxy pod during the run."""

from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    add_kill_options,
    build_parser,
    collect_controller_artifacts,
    collect_kubernetes_artifacts,
    config_from_args,
    delete_pod,
    prepare_run,
    process_wait_timeout_seconds,
    select_pod_candidates_with_status,
    start_ffmpeg_streams,
    stop_processes,
    stream_key,
    validate_kill_timing,
    validate_runtime_tools,
    wait_for_processes,
    write_json,
)

SCENARIO = "kill_proxy"


def main() -> int:
    parser = build_parser("Run a LiveEdgeCast proxy-failure experiment.", [SCENARIO])
    add_kill_options(parser, include_worker_target=False)
    args = parser.parse_args()
    config = config_from_args(args)
    validate_kill_timing(parser, config)
    logger = prepare_run(config)

    run_started_at = time.time()
    keys = [stream_key(config, index) for index in range(1, config.concurrency + 1)]
    processes = []
    summary = {"started_at": run_started_at, "scenario": SCENARIO, "stream_keys": keys}
    exit_code = 1

    try:
        validate_runtime_tools(config, need_kubectl=True, logger=logger)
        processes = start_ffmpeg_streams(config, keys, logger)
        time.sleep(config.kill_after_seconds)

        selection = {
            "source": "target_proxy_pod" if config.target_proxy_pod else "selector",
            "selector": config.proxy_selector,
            "target_proxy_pod": config.target_proxy_pod,
            "candidate_pods": [],
        }
        pod_name = config.target_proxy_pod
        if pod_name:
            selection["status"] = "selected"
            selection["pod"] = pod_name
        else:
            selector_selection = select_pod_candidates_with_status(
                config, config.proxy_selector, logger, require_single=True
            )
            selection.update(selector_selection)
            selection["source"] = "selector"
            if selection["status"] == "selected":
                pod_name = selection["pod"]

        kill_result = {
            "pod": None,
            "status": selection["status"],
            "returncode": 1,
            "selection": selection,
        }
        if pod_name:
            kill_result = delete_pod(config, pod_name, logger)
            kill_result["selection"] = selection

        summary["killed_proxy"] = kill_result
        write_json(config.artifact_dir / "summary.json", summary)

        results = wait_for_processes(
            processes,
            logger,
            timeout_seconds=process_wait_timeout_seconds(
                config, process_count=len(processes)
            ),
            stop_grace_seconds=config.stop_grace_seconds,
        )
        processes = []
        summary.update({"killed_proxy": kill_result, "processes": results})
        exit_code = 0 if kill_result.get("returncode") == 0 else 2
    except Exception as exc:
        logger.exception("kill proxy experiment failed: %s", exc)
        summary["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if processes:
            summary["stopped_processes"] = stop_processes(processes, config, logger)
    finally:
        summary["ended_at"] = time.time()
        summary["kubernetes_artifacts"] = collect_kubernetes_artifacts(config, logger)
        summary["controller_artifacts"] = collect_controller_artifacts(config, logger)
        write_json(config.artifact_dir / "summary.json", summary)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
