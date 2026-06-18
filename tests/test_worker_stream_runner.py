import os
import shutil
import subprocess
from pathlib import Path

import pytest


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
printf '%s\n' "$@" > "$state_dir/ffmpeg_args"
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

    fake_ffprobe = bin_dir / "ffprobe"
    fake_ffprobe.write_text(
        r'''#!/bin/sh
if [ "${FAKE_FFPROBE_FAIL:-0}" = "1" ]; then
  exit 1
fi
if [ "${FAKE_FFPROBE_INVALID_JSON:-0}" = "1" ]; then
  printf 'not-json'
  exit 0
fi
width="${FAKE_FFPROBE_WIDTH:-1920}"
height="${FAKE_FFPROBE_HEIGHT:-1080}"
avg_fps="${FAKE_FFPROBE_AVG_FPS:-${FAKE_FFPROBE_FPS:-30000/1001}}"
r_fps="${FAKE_FFPROBE_R_FPS:-${FAKE_FFPROBE_FPS:-30000/1001}}"
cat <<EOF
{"streams":[{"codec_type":"video","width":${width},"height":${height},"avg_frame_rate":"${avg_fps}","r_frame_rate":"${r_fps}"}]}
EOF
exit 0
'''
    )
    fake_ffprobe.chmod(0o755)

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


def _run_runner(
    tmp_path: Path,
    scenario: str,
    stream_status: str = "inactive",
    status_sequence: str = "",
    timeout_seconds: int = 8,
    extra_env: dict[str, str] | None = None,
):
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
            "FFPROBE_TIMEOUT_SECONDS": "1",
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(RUNNER)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
    )


def test_input_connection_refused_exits_without_self_recovery(tmp_path):
    result = _run_runner(tmp_path, "input_refused_once_then_progress", status_sequence="active,active,ended")

    assert result.returncode == 251, result.stdout
    assert "ffmpeg_exit_251_no_self_recovery" in result.stdout
    assert "destination_open_failed" not in result.stdout


def test_input_connection_refused_is_not_classified_as_destination_failure(tmp_path):
    result = _run_runner(tmp_path, "input_refused_always", stream_status="active")

    assert result.returncode == 251, result.stdout
    assert "ffmpeg_exit_251_no_self_recovery" in result.stdout
    assert "destination_open_failed" not in result.stdout


def test_explicit_output_open_failure_exits_with_destination_code(tmp_path):
    result = _run_runner(tmp_path, "destination_refused", stream_status="active")

    assert result.returncode == 1, result.stdout
    assert "ffmpeg_exit_1_no_self_recovery" in result.stdout


def test_ffmpeg_transcodes_to_h264_and_youtube_1080p30_profile(tmp_path):
    result = _run_runner(tmp_path, "success", stream_status="active")

    assert result.returncode == 0, result.stdout
    assert "youtube_encoder_profile_selected" in result.stdout
    assert '"status":"1080p30"' in result.stdout
    args = (tmp_path / "state" / "ffmpeg_args").read_text()
    assert "-c:v\nlibx264" in args
    assert "-b:v\n10M" in args
    assert "-minrate\n10M" in args
    assert "-level:v\n4.0" in args
    assert "-bf\n2" in args
    assert "-refs\n1" in args
    assert "-coder\n1" in args
    assert "-x264-params\nnal-hrd=cbr:force-cfr=1" in args
    assert "-c:a\naac" in args
    assert "-r\n30" in args
    assert "-colorspace\nbt709" in args
    assert '"event_type":"youtube_encoder_output_selected"' in result.stdout


def test_ffmpeg_uses_closest_youtube_1440p60_profile(tmp_path):
    result = _run_runner(
        tmp_path,
        "success",
        stream_status="active",
        extra_env={
            "FAKE_FFPROBE_HEIGHT": "1500",
            "FAKE_FFPROBE_FPS": "60/1",
        },
    )

    assert result.returncode == 0, result.stdout
    assert '"status":"1440p60"' in result.stdout
    args = (tmp_path / "state" / "ffmpeg_args").read_text()
    assert "-b:v\n24M" in args
    assert "-level:v\n5.1" in args
    assert "-r\n60" in args


def test_ffmpeg_uses_720p30_when_it_is_the_closest_profile_above_720p(tmp_path):
    result = _run_runner(
        tmp_path,
        "success",
        stream_status="active",
        extra_env={
            "FAKE_FFPROBE_HEIGHT": "800",
            "FAKE_FFPROBE_FPS": "30/1",
        },
    )

    assert result.returncode == 0, result.stdout
    assert '"status":"240p-720p30"' in result.stdout
    args = (tmp_path / "state" / "ffmpeg_args").read_text()
    assert "-b:v\n4M" in args
    assert "-vf\nscale=w='if(lte(iw,ih),720,-2)':h='if(lte(iw,ih),-2,720)',setsar=1" in args


def test_ffmpeg_uses_720p60_profile(tmp_path):
    result = _run_runner(
        tmp_path,
        "success",
        stream_status="active",
        extra_env={
            "FAKE_FFPROBE_HEIGHT": "720",
            "FAKE_FFPROBE_FPS": "60/1",
        },
    )

    assert result.returncode == 0, result.stdout
    assert '"status":"720p60"' in result.stdout
    args = (tmp_path / "state" / "ffmpeg_args").read_text()
    assert "-b:v\n6M" in args
    assert "-level:v\n3.2" in args
    assert "-r\n60" in args
    assert "-vf\nscale=w='if(lte(iw,ih),720,-2)':h='if(lte(iw,ih),-2,720)',setsar=1" in args


def test_ffmpeg_preserves_sub_720p_height_with_240p_to_720p_profile(tmp_path):
    result = _run_runner(
        tmp_path,
        "success",
        stream_status="active",
        extra_env={
            "FAKE_FFPROBE_HEIGHT": "480",
            "FAKE_FFPROBE_FPS": "30/1",
        },
    )

    assert result.returncode == 0, result.stdout
    assert '"status":"240p-720p30"' in result.stdout
    args = (tmp_path / "state" / "ffmpeg_args").read_text()
    assert "-b:v\n4M" in args
    assert "-vf\nscale=w='if(lte(iw,ih),480,-2)':h='if(lte(iw,ih),-2,480)',setsar=1" in args


def test_ffmpeg_uses_2160p60_profile_with_4k_h264_level(tmp_path):
    result = _run_runner(
        tmp_path,
        "success",
        stream_status="active",
        extra_env={
            "FAKE_FFPROBE_HEIGHT": "2160",
            "FAKE_FFPROBE_FPS": "60/1",
        },
    )

    assert result.returncode == 0, result.stdout
    assert '"status":"2160p60"' in result.stdout
    args = (tmp_path / "state" / "ffmpeg_args").read_text()
    assert "-b:v\n35M" in args
    assert "-level:v\n5.2" in args
    assert "-vf\nscale=w='if(lte(iw,ih),2160,-2)':h='if(lte(iw,ih),-2,2160)',setsar=1" in args


def test_ffmpeg_falls_back_to_720p30_when_ffprobe_fails(tmp_path):
    result = _run_runner(
        tmp_path,
        "success",
        stream_status="active",
        extra_env={"FAKE_FFPROBE_FAIL": "1"},
    )

    assert result.returncode == 0, result.stdout
    assert "ffprobe_failed_using_youtube_240p_720p30_defaults" in result.stdout
    assert '"status":"240p-720p30"' in result.stdout
    args = (tmp_path / "state" / "ffmpeg_args").read_text()
    assert "-b:v\n4M" in args
    assert "-level:v\n3.1" in args


def test_ffmpeg_falls_back_to_720p30_when_ffprobe_returns_invalid_json(tmp_path):
    result = _run_runner(
        tmp_path,
        "success",
        stream_status="active",
        extra_env={"FAKE_FFPROBE_INVALID_JSON": "1"},
    )

    assert result.returncode == 0, result.stdout
    assert "ffprobe_invalid_json_using_youtube_240p_720p30_defaults" in result.stdout
    assert '"status":"240p-720p30"' in result.stdout
    args = (tmp_path / "state" / "ffmpeg_args").read_text()
    assert "-b:v\n4M" in args
    assert "-level:v\n3.1" in args


def test_ffmpeg_uses_r_frame_rate_when_avg_frame_rate_is_zero(tmp_path):
    result = _run_runner(
        tmp_path,
        "success",
        stream_status="active",
        extra_env={
            "FAKE_FFPROBE_HEIGHT": "1080",
            "FAKE_FFPROBE_AVG_FPS": "0/0",
            "FAKE_FFPROBE_R_FPS": "60/1",
        },
    )

    assert result.returncode == 0, result.stdout
    assert '"status":"1080p60"' in result.stdout
    args = (tmp_path / "state" / "ffmpeg_args").read_text()
    assert "-b:v\n12M" in args
    assert "-r\n60" in args


def test_ffmpeg_uses_60fps_profile_but_preserves_50fps_output_rate(tmp_path):
    result = _run_runner(
        tmp_path,
        "success",
        stream_status="active",
        extra_env={
            "FAKE_FFPROBE_HEIGHT": "1080",
            "FAKE_FFPROBE_FPS": "50/1",
        },
    )

    assert result.returncode == 0, result.stdout
    assert '"status":"1080p60"' in result.stdout
    args = (tmp_path / "state" / "ffmpeg_args").read_text()
    assert "-b:v\n12M" in args
    assert "-r\n50" in args


def test_ffmpeg_selects_profile_by_shorter_axis_for_portrait_streams(tmp_path):
    result = _run_runner(
        tmp_path,
        "success",
        stream_status="active",
        extra_env={
            "FAKE_FFPROBE_WIDTH": "1080",
            "FAKE_FFPROBE_HEIGHT": "1920",
            "FAKE_FFPROBE_FPS": "30/1",
        },
    )

    assert result.returncode == 0, result.stdout
    assert '"status":"1080p30"' in result.stdout
    args = (tmp_path / "state" / "ffmpeg_args").read_text()
    assert "-b:v\n10M" in args
    assert "-vf\nscale=w='if(lte(iw,ih),1080,-2)':h='if(lte(iw,ih),-2,1080)',setsar=1" in args


def test_real_ffmpeg_accepts_youtube_filter_and_encoder_options():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg binary is not available")

    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30",
            "-frames:v",
            "2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-level:v",
            "3.1",
            "-bf",
            "2",
            "-refs",
            "1",
            "-coder",
            "1",
            "-b:v",
            "4M",
            "-maxrate",
            "4M",
            "-minrate",
            "4M",
            "-bufsize",
            "8M",
            "-x264-params",
            "nal-hrd=cbr:force-cfr=1",
            "-r",
            "30",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
            "-vf",
            "scale=w='if(lte(iw,ih),720,-2)':h='if(lte(iw,ih),-2,720)',setsar=1",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-f",
            "null",
            "-",
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
