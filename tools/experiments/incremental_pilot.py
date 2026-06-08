#!/usr/bin/env python3
"""Incremental concurrency pilot that stops when the run appears saturated."""

from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    build_parser,
    collect_controller_artifacts,
    config_from_args,
    prepare_run,
    start_ffmpeg_streams,
    stream_key,
    validate_runtime_tools,
    wait_for_processes,
    write_json,
)

SCENARIO = "incremental_pilot"


def main() -> int:
    parser = build_parser(
        "Run an incremental LiveEdgeCast pilot until saturation.", [SCENARIO]
    )
    args = parser.parse_args()
    config = config_from_args(args)
    logger = prepare_run(config)
    validate_runtime_tools(config, need_kubectl=False, logger=logger)

    levels = list(range(config.step_size, config.concurrency + 1, config.step_size))
    if levels[-1] != config.concurrency:
        levels.append(config.concurrency)

    summary = {
        "started_at": time.time(),
        "scenario": SCENARIO,
        "levels": [],
        "saturated_at_concurrency": None,
    }

    for level in levels:
        logger.info("starting incremental level concurrency=%s", level)
        keys = [
            stream_key(config, index, suffix=f"c{level}")
            for index in range(1, level + 1)
        ]
        processes = start_ffmpeg_streams(config, keys, logger)
        results = wait_for_processes(processes, logger)
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

    summary["ended_at"] = time.time()
    summary["controller_artifacts"] = collect_controller_artifacts(config, logger)
    write_json(config.artifact_dir / "summary.json", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
