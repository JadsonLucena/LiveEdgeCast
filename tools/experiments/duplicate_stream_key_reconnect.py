#!/usr/bin/env python3
"""Exercise reconnects and duplicate publish attempts with the same stream key."""

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
    sleep_with_log,
    start_ffmpeg_streams,
    start_process,
    stream_key,
    validate_runtime_tools,
    wait_for_processes,
    ffmpeg_command,
    write_json,
)

SCENARIO = "duplicate_stream_key_reconnect"


def main() -> int:
    parser = build_parser(
        "Run duplicate streamKey attempts and reconnects.", [SCENARIO]
    )
    args = parser.parse_args()
    config = config_from_args(args)
    logger = prepare_run(config)
    validate_runtime_tools(config, need_kubectl=False, logger=logger)

    primary_key = stream_key(config, 1)
    background_keys = [
        stream_key(config, index) for index in range(2, config.concurrency + 1)
    ]
    background = (
        start_ffmpeg_streams(config, background_keys, logger) if background_keys else []
    )

    primary_duration = max(1, config.duration_seconds)
    primary = start_process(
        ffmpeg_command(config, primary_key, primary_duration),
        f"ffmpeg-primary-{primary_key}",
        config.artifact_dir,
        logger,
    )
    sleep_with_log(
        config.duplicate_attempt_delay_seconds,
        logger,
        "before duplicate streamKey attempt",
    )
    duplicate = start_process(
        ffmpeg_command(config, primary_key, max(1, config.duration_seconds // 2)),
        f"ffmpeg-duplicate-{primary_key}",
        config.artifact_dir,
        logger,
    )

    duplicate_results = wait_for_processes([duplicate], logger)
    primary_result = wait_for_processes([primary], logger)

    reconnect_results = []
    for attempt in range(1, config.reconnect_attempts + 1):
        sleep_with_log(
            config.reconnect_delay_seconds,
            logger,
            f"before reconnect attempt {attempt}",
        )
        reconnect = start_process(
            ffmpeg_command(config, primary_key, max(1, config.duration_seconds // 2)),
            f"ffmpeg-reconnect-{attempt}-{primary_key}",
            config.artifact_dir,
            logger,
        )
        reconnect_results.extend(wait_for_processes([reconnect], logger))

    background_results = wait_for_processes(background, logger) if background else []
    summary = {
        "started_at": time.time(),
        "ended_at": time.time(),
        "scenario": SCENARIO,
        "stream_key": primary_key,
        "background_stream_keys": background_keys,
        "primary": primary_result,
        "duplicate_attempts": duplicate_results,
        "reconnect_attempts": reconnect_results,
        "background_processes": background_results,
        "controller_artifacts": collect_controller_artifacts(config, logger),
    }
    write_json(config.artifact_dir / "summary.json", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
