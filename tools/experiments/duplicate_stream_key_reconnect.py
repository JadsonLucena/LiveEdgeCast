#!/usr/bin/env python3
"""Exercise reconnects and duplicate publish attempts with the same stream key."""

from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    add_duplicate_reconnect_options,
    build_parser,
    collect_controller_artifacts,
    config_from_args,
    ffmpeg_command,
    prepare_run,
    process_wait_timeout_seconds,
    sleep_with_log,
    start_ffmpeg_streams,
    start_process,
    stop_processes,
    stream_key,
    validate_duplicate_timing,
    validate_runtime_tools,
    wait_for_processes,
    write_json,
)

SCENARIO = "duplicate_stream_key_reconnect"


def main() -> int:
    parser = build_parser(
        "Run duplicate streamKey attempts and reconnects.", [SCENARIO]
    )
    add_duplicate_reconnect_options(parser)
    args = parser.parse_args()
    config = config_from_args(args)
    validate_duplicate_timing(parser, config)
    logger = prepare_run(config)

    run_started_at = time.time()
    primary_key = stream_key(config, 1)
    background_keys = [
        stream_key(config, index) for index in range(2, config.concurrency + 1)
    ]
    running_processes = []
    summary = {
        "started_at": run_started_at,
        "scenario": SCENARIO,
        "stream_key": primary_key,
        "background_stream_keys": background_keys,
    }
    exit_code = 0

    try:
        validate_runtime_tools(config, need_kubectl=False, logger=logger)
        background = (
            start_ffmpeg_streams(config, background_keys, logger)
            if background_keys
            else []
        )
        running_processes.extend(background)

        primary_duration = max(1, config.duration_seconds)
        primary = start_process(
            ffmpeg_command(config, primary_key, primary_duration),
            f"ffmpeg-primary-{primary_key}",
            config.artifact_dir,
            logger,
        )
        running_processes.append(primary)
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
        running_processes.append(duplicate)

        duplicate_results = wait_for_processes(
            [duplicate],
            logger,
            timeout_seconds=process_wait_timeout_seconds(
                config, max(1, config.duration_seconds // 2), process_count=1
            ),
            stop_grace_seconds=config.stop_grace_seconds,
        )
        running_processes.remove(duplicate)
        summary["duplicate_attempts"] = duplicate_results
        write_json(config.artifact_dir / "summary.json", summary)

        primary_result = wait_for_processes(
            [primary],
            logger,
            timeout_seconds=process_wait_timeout_seconds(
                config, primary_duration, process_count=1
            ),
            stop_grace_seconds=config.stop_grace_seconds,
        )
        running_processes.remove(primary)
        summary["primary"] = primary_result
        write_json(config.artifact_dir / "summary.json", summary)

        reconnect_results = []
        for attempt in range(1, config.reconnect_attempts + 1):
            sleep_with_log(
                config.reconnect_delay_seconds,
                logger,
                f"before reconnect attempt {attempt}",
            )
            reconnect = start_process(
                ffmpeg_command(
                    config, primary_key, max(1, config.duration_seconds // 2)
                ),
                f"ffmpeg-reconnect-{attempt}-{primary_key}",
                config.artifact_dir,
                logger,
            )
            running_processes.append(reconnect)
            reconnect_results.extend(
                wait_for_processes(
                    [reconnect],
                    logger,
                    timeout_seconds=process_wait_timeout_seconds(
                        config, max(1, config.duration_seconds // 2), process_count=1
                    ),
                    stop_grace_seconds=config.stop_grace_seconds,
                )
            )
            running_processes.remove(reconnect)
            summary["reconnect_attempts"] = reconnect_results
            write_json(config.artifact_dir / "summary.json", summary)

        background_results = (
            wait_for_processes(
                background,
                logger,
                timeout_seconds=process_wait_timeout_seconds(
                    config, process_count=len(background)
                ),
                stop_grace_seconds=config.stop_grace_seconds,
            )
            if background
            else []
        )
        for proc in background:
            if proc in running_processes:
                running_processes.remove(proc)
        summary.update(
            {
                "primary": primary_result,
                "duplicate_attempts": duplicate_results,
                "reconnect_attempts": reconnect_results,
                "background_processes": background_results,
            }
        )
        write_json(config.artifact_dir / "summary.json", summary)
    except Exception as exc:
        exit_code = 1
        logger.exception("duplicate streamKey reconnect experiment failed: %s", exc)
        summary["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if running_processes:
            summary["stopped_processes"] = stop_processes(
                running_processes, config, logger
            )
    finally:
        summary["ended_at"] = time.time()
        summary["controller_artifacts"] = collect_controller_artifacts(config, logger)
        write_json(config.artifact_dir / "summary.json", summary)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
