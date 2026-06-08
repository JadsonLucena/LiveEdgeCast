import logging
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS_DIR = ROOT / "tools" / "experiments"
sys.path.insert(0, str(EXPERIMENTS_DIR))

from common import (  # noqa: E402
    add_duplicate_reconnect_options,
    add_kill_options,
    add_saturation_options,
    build_parser,
    config_from_args,
    redact_url,
    run_command,
    sanitize_command,
    validate_duplicate_timing,
    validate_kill_timing,
    validate_pilot_config,
)


def parsed_config(parser, *extra_args):
    args = parser.parse_args(
        [
            "--experiment-id",
            "exp-001",
            "--scenario",
            parser.scenario,
            "--run-id",
            "run-001",
            "--concurrency",
            "2",
            "--duration-seconds",
            "10",
            "--bitrate",
            "800k",
            *extra_args,
        ]
    )
    return config_from_args(args)


def scenario_parser(scenario):
    parser = build_parser("test parser", [scenario])
    parser.scenario = scenario
    return parser


def test_kill_timing_requires_failure_injection_during_active_run():
    parser = scenario_parser("kill_proxy")
    add_kill_options(parser)
    config = parsed_config(parser, "--kill-after-seconds", "10")

    with pytest.raises(SystemExit) as exc:
        validate_kill_timing(parser, config)

    assert exc.value.code == 2


def test_duplicate_attempt_must_overlap_primary_publisher():
    parser = scenario_parser("duplicate_stream_key_reconnect")
    add_duplicate_reconnect_options(parser)
    config = parsed_config(parser, "--duplicate-attempt-delay-seconds", "10")

    with pytest.raises(SystemExit) as exc:
        validate_duplicate_timing(parser, config)

    assert exc.value.code == 2


def test_incremental_step_size_cannot_exceed_max_concurrency():
    parser = scenario_parser("incremental_pilot")
    add_saturation_options(parser)
    config = parsed_config(parser, "--step-size", "3")

    with pytest.raises(SystemExit) as exc:
        validate_pilot_config(parser, config)

    assert exc.value.code == 2


def test_urls_are_redacted_in_logged_commands():
    assert redact_url("rtmp://user:secret@example.com:1935/live/token") == "rtmp://***:***@example.com:1935/..."
    assert sanitize_command(["ffmpeg", "rtmp://example.com/live/key"])[1] == "rtmp://example.com/..."


def test_run_command_timeout_is_best_effort():
    logger = logging.getLogger("test_run_command_timeout")
    result = run_command(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        logger,
        timeout=1,
    )

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 124
    assert "Timeout after 1s" in result.stderr
