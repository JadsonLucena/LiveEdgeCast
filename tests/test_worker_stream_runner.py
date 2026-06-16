import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "docker" / "worker" / "worker_stream_runner.sh"


def _write_fake_tools(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_ffmpeg = bin_dir / "ffmpeg"
    fake_ffmpeg.write_text(
        r'''#!/bin/sh
scenario="${FAKE_FFMPEG_SCENARIO:-success}"
state_dir="${FAKE_STATE_DIR:?FAKE_STATE_DIR required}"
mkdir -p "$state_dir"
counter_file="$state_dir/ffmpeg_attempts"
if [ -f "$counter_file" ]; then
  attempt=$(( $(cat "$counter_file") + 1 ))
else
  attempt=1
fi
printf '%s' "$attempt" > "$counter_file"

progress_path=""
input_url=""
target_url=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-progress" ]; then
    progress_path="$arg"
  fi
  if [ "$prev" = "-i" ]; then
    input_url="$arg"
  fi
  target_url="$arg"
  prev="$arg"
done

if [ "$scenario" = "input_refused_once_then_progress" ] && [ "$attempt" -eq 1 ]; then
  echo "[in#0] Error opening input file ${input_url}: Connection refused" >&2
  exit 251
fi

if [ "$scenario" = "input_refused_always" ]; then
  echo "[in#0] Error opening input file ${input_url}: Connection refused" >&2
  exit 251
fi

if [ "$scenario" = "destination_refused" ]; then
  echo "Error opening output file ${target_url}: Connection refused" >&2
  exit 1
fi

if [ -n "$progress_path" ]; then
  printf 'frame=1
progress=end
' > "$progress_path"
fi
exit 0
'''
    )
    fake_ffmpeg.chmod(0o755)

    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        r'''#!/bin/sh
case "$*" in
  *"/streams/status"*)
    state="${FAKE_STREAM_STATUS:-inactive}"
    if [ -n "${FAKE_STREAM_STATUS_SEQUENCE:-}" ]; then
      mkdir -p "${FAKE_STATE_DIR:?FAKE_STATE_DIR required}"
      status_counter_file="${FAKE_STATE_DIR}/status_calls"
      if [ -f "$status_counter_file" ]; then
        call_index=$(( $(cat "$status_counter_file") + 1 ))
      else
        call_index=1
      fi
      printf '%s' "$call_index" > "$status_counter_file"
      state="$(printf '%s' "$FAKE_STREAM_STATUS_SEQUENCE" | cut -d, -f"$call_index")"
      if [ -z "$state" ]; then
        state="$(printf '%s' "$FAKE_STREAM_STATUS_SEQUENCE" | awk -F, '{print $NF}')"
      fi
    fi
    case "$state" in
      active) printf '{"status":"active","active":true,"terminal":false}
' ;;
      ended) printf '{"status":"ended_explicitly","active":false,"terminal":true}
' ;;
      not_visible) printf '{"status":"not_visible_in_proxy_stats","active":null,"terminal":false}
' ;;
      inactive) printf '{"status":"not_visible_in_proxy_stats","active":null,"terminal":false}
' ;;
      *) printf '{"status":"unknown","active":null,"terminal":false}
' ;;
    esac
    ;;
esac
exit 0
'''
    )
    fake_curl.chmod(0o755)
    return bin_dir


def _run_runner(tmp_path: Path, scenario: str, stream_status: str = "inactive", status_sequence: str = "", timeout_seconds: int = 8):
    bin_dir = _write_fake_tools(tmp_path)
    state_dir = tmp_path / "state"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "FAKE_STATE_DIR": str(state_dir),
            "FAKE_FFMPEG_SCENARIO": scenario,
            "FAKE_STREAM_STATUS": stream_status,
            "FAKE_STREAM_STATUS_SEQUENCE": status_sequence,
            "STREAM_KEY": f"test-stream-{tmp_path.name}",
            "PROXY_DNS": "10.0.0.10",
            "PROXY_POD": "proxy-a",
            "SESSION_ID": "session-a",
            "STREAM_GENERATION": "1",
            "WORKER_POD": "worker-a",
            "RTMP_PUSH_BASE_URL": "rtmp://destination.example/live",
            "CONTROLLER_API": "http://controller.local",
            "FFMPEG_INPUT_OPEN_TIMEOUT_SECONDS": "5",
            "FFMPEG_INPUT_ATTEMPT_TIMEOUT_SECONDS": "1",
            "FFMPEG_INPUT_RETRY_INTERVAL_SECONDS": "0",
            "PROGRESS_NOTIFY_POLL_SECONDS": "0.05",
            "CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS": "1",
            "CONTROLLER_CALLBACK_MAX_TIME_SECONDS": "1",
        }
    )
    return subprocess.run(
        ["bash", str(RUNNER)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
    )


def test_input_connection_refused_retries_and_then_succeeds(tmp_path):
    result = _run_runner(tmp_path, "input_refused_once_then_progress", status_sequence="active,active,ended")

    assert result.returncode == 0, result.stdout
    assert "ffmpeg_input_open_attempt_failed" in result.stdout
    assert "destination_open_failed" not in result.stdout
    assert "ffmpeg_first_progress" in result.stdout


def test_input_connection_refused_is_not_classified_as_destination_failure(tmp_path):
    result = _run_runner(tmp_path, "input_refused_always", stream_status="active")

    assert result.returncode == 251, result.stdout
    assert "ffmpeg_input_open_attempt_failed" in result.stdout
    assert "destination_open_failed" not in result.stdout


def test_explicit_output_open_failure_exits_with_destination_code(tmp_path):
    result = _run_runner(tmp_path, "destination_refused", stream_status="active")

    assert result.returncode == 252, result.stdout
    assert "destination_open_failed" in result.stdout
