#!/usr/bin/env python3
"""Incremental concurrency pilot that stops when publishers fail at the configured threshold."""

from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    add_saturation_options,
    build_incremental_levels,
    build_parser,
    collect_controller_artifacts,
    config_from_args,
    prepare_run,
    process_wait_timeout_seconds,
    start_ffmpeg_streams,
    stop_processes,
    stream_key,
    validate_runtime_tools,
    wait_for_processes,
    write_json,
)

SCENARIO = "incremental_pilot"


def main() -> int:
    parser = build_parser(
        "Run an incremental LiveEdgeCast pilot until publisher failure-rate saturation.",
        [SCENARIO],
    )
    add_saturation_options(parser)
    args = parser.parse_args()
    config = config_from_args(args)
    logger = prepare_run(config)

    current_processes = []
    summary = {
        "started_at": time.time(),
        "scenario": SCENARIO,
        "saturation_criterion": "publisher_failure_rate",
        "levels": [],
        "saturated_at_concurrency": None,
    }
    exit_code = 0

    try:
        validate_runtime_tools(config, need_kubectl=False, logger=logger)
        for level in build_incremental_levels(config):
            logger.info("starting incremental level concurrency=%s", level)
            keys = [
                stream_key(config, index, suffix=f"c{level}")
                for index in range(1, level + 1)
            ]
            current_processes = start_ffmpeg_streams(config, keys, logger)
            results = wait_for_processes(
                current_processes,
                logger,
                timeout_seconds=process_wait_timeout_seconds(
                    config, process_count=len(current_processes)
                ),
                stop_grace_seconds=config.stop_grace_seconds,
            )
            current_processes = []
            failures = [result for result in results if result["returncode"] != 0]
            error_rate = len(failures) / len(results) if results else 1.0
            level_summary = {
                "concurrency": level,
                "stream_keys": keys,
                "processes": results,
                "failure_count": len(failures),
                "error_rate": error_rate,
                "saturated": error_rate >= config.saturation_error_rate,
            }
            summary["levels"].append(level_summary)
            write_json(config.artifact_dir / "summary.json", summary)
            logger.info(
                "completed incremental level concurrency=%s error_rate=%s",
                level,
                error_rate,
            )
            if level_summary["saturated"]:
                summary["saturated_at_concurrency"] = level
                logger.info(
                    "saturation detected concurrency=%s threshold=%s",
                    level,
                    config.saturation_error_rate,
                )
                break
    except Exception as exc:
        exit_code = 1
        logger.exception("incremental pilot failed: %s", exc)
        summary["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if current_processes:
            summary["stopped_processes"] = stop_processes(
                current_processes, config, logger
            )
    finally:
        summary["ended_at"] = time.time()
        summary["controller_artifacts"] = collect_controller_artifacts(config, logger)
        write_json(config.artifact_dir / "summary.json", summary)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
