#!/bin/bash
set -euo pipefail

# Bash executes EXIT traps in command-substitution subshells as well. The runner
# uses command substitutions for timestamps, curl output and parsing helpers;
# cleanup must run only in the top-level worker process or those subshells can
# delete progress files while FFmpeg is still starting.
MAIN_BASHPID="${BASHPID:-$$}"

STREAM_KEY="${STREAM_KEY:-}"
SAFE_STREAM_KEY="$(printf '%s' "$STREAM_KEY" | sed 's/[^a-zA-Z0-9_.:-]/_/g' | cut -c1-160)"
if [ -z "$SAFE_STREAM_KEY" ]; then
  SAFE_STREAM_KEY="stream"
fi
PID_FILE="/tmp/ffmpeg_${SAFE_STREAM_KEY}.pid"
PROGRESS_FILE="/tmp/ffmpeg_${SAFE_STREAM_KEY}.progress"
EXIT_EVENT_FILE="${FFMPEG_EXIT_FILE:-/tmp/ffmpeg_${SAFE_STREAM_KEY}.exit_events}"
LAST_EXIT_FILE="/tmp/ffmpeg_${SAFE_STREAM_KEY}.last_exit"
PROGRESS_NOTIFY_FILE="/tmp/ffmpeg_${SAFE_STREAM_KEY}.progress_notified"
PROGRESS_NOTIFY_LOCK="/tmp/ffmpeg_${SAFE_STREAM_KEY}.progress_notify.lock"
PROGRESS_NOTIFY_ERROR_FILE="/tmp/ffmpeg_${SAFE_STREAM_KEY}.progress_notify_error_logged"
PROGRESS_LOG_FILE="/tmp/ffmpeg_${SAFE_STREAM_KEY}.first_progress_logged"
PROGRESS_LOG_LOCK="/tmp/ffmpeg_${SAFE_STREAM_KEY}.first_progress_log.lock"
STARTED_NOTIFY_FILE="/tmp/ffmpeg_${SAFE_STREAM_KEY}.started_notified"
STARTED_NOTIFY_LOCK="/tmp/ffmpeg_${SAFE_STREAM_KEY}.started_notify.lock"
STARTED_NOTIFY_ERROR_FILE="/tmp/ffmpeg_${SAFE_STREAM_KEY}.started_notify_error_logged"
STARTED_LOG_FILE="/tmp/ffmpeg_${SAFE_STREAM_KEY}.started_logged"
STARTED_LOG_LOCK="/tmp/ffmpeg_${SAFE_STREAM_KEY}.started_log.lock"
PROGRESS_READER_PID=""
FFMPEG_PID=""
CURRENT_ATTEMPT=""
FFMPEG_RUN_ID=""
RUNNER_START_MS="$(python3 - <<'PYMS'
import time
print(time.time_ns() // 1_000_000)
PYMS
)"
FFMPEG_START_MS=""

# Simplified worker mode: start FFmpeg once and let it run until it exits or the
# controller deletes the worker pod on exec_publish_done. No input-open deadline,
# no per-attempt timeout and no retry/self-recovery loop are used here.
FFMPEG_PROGRESS_NOTIFY_POLL_SECONDS="${PROGRESS_NOTIFY_POLL_SECONDS:-0.2}"
FFMPEG_LOGLEVEL="${FFMPEG_LOGLEVEL:-warning}"
FFPROBE_TIMEOUT_SECONDS="${FFPROBE_TIMEOUT_SECONDS:-10}"

json_escape() {
  local value="${1:-}"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

current_epoch_ms() {
  python3 - <<'PYMS'
import time
print(time.time_ns() // 1_000_000)
PYMS
}

elapsed_ms() {
  local start_ms="${1:-$RUNNER_START_MS}"
  local now_ms
  now_ms="$(current_epoch_ms)"
  if [[ "$start_ms" =~ ^[0-9]+$ ]] && [ "$now_ms" -ge "$start_ms" ]; then
    printf '%s' "$((now_ms - start_ms))"
  else
    printf '0'
  fi
}

log_json() {
  local event_type="$1"
  local status="${2:-ok}"
  local duration_ms="${3:-$(elapsed_ms "$RUNNER_START_MS")}"
  local timestamp
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{"timestamp":"%s","event_type":"%s","stream":"%s","generation":"%s","session_id":"%s","proxy_pod":"%s","worker_pod":"%s","experiment_id":"%s","scenario":"%s","run_id":"%s","duration_ms":%s,"status":"%s","attempt":"%s","ffmpeg_run_id":"%s"}\n' \
    "$(json_escape "$timestamp")" \
    "$(json_escape "$event_type")" \
    "$(json_escape "${STREAM_KEY:-}")" \
    "$(json_escape "${STREAM_GENERATION:-}")" \
    "$(json_escape "${SESSION_ID:-}")" \
    "$(json_escape "${PROXY_POD:-}")" \
    "$(json_escape "${WORKER_POD:-${HOSTNAME:-unknown-worker}}")" \
    "$(json_escape "${EXPERIMENT_ID:-}")" \
    "$(json_escape "${SCENARIO:-}")" \
    "$(json_escape "${RUN_ID:-}")" \
    "$duration_ms" \
    "$(json_escape "$status")" \
    "$(json_escape "${CURRENT_ATTEMPT:-}")" \
    "$(json_escape "${FFMPEG_RUN_ID:-}")"
}

notify_controller() {
  local path="$1"
  curl -sf --connect-timeout "${CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS:-1}" --max-time "${CONTROLLER_CALLBACK_MAX_TIME_SECONDS:-2}" -X POST --get \
    --data-urlencode "stream=${STREAM_KEY}" \
    --data-urlencode "worker_pod=${WORKER_POD}" \
    "${CONTROLLER_API}${path}"
}

controller_stream_activity_state() {
  local response rc
  local curl_args=(
    -sf
    --connect-timeout "${CONTROLLER_CALLBACK_CONNECT_TIMEOUT_SECONDS:-1}"
    --max-time "${CONTROLLER_CALLBACK_MAX_TIME_SECONDS:-2}"
    -G
    --data-urlencode "stream=${STREAM_KEY}"
  )
  if [ -n "${PROXY_POD:-}" ]; then
    curl_args+=(--data-urlencode "proxy_pod=${PROXY_POD}")
  fi
  if [ -n "${SESSION_ID:-}" ]; then
    curl_args+=(--data-urlencode "session_id=${SESSION_ID}")
  fi
  if [ -n "${STREAM_GENERATION:-}" ]; then
    curl_args+=(--data-urlencode "generation=${STREAM_GENERATION}")
  fi
  set +e
  response="$({ curl "${curl_args[@]}" "${CONTROLLER_API}/streams/status"; } 2>/dev/null)"
  rc=$?
  set -e
  if [ "$rc" -ne 0 ] || [ -z "$response" ]; then
    printf 'unknown'
    return 0
  fi
  RESPONSE_JSON="$response" python3 - <<'PYSTATUS' 2>/dev/null || printf 'unknown'
import json, os
try:
    data = json.loads(os.environ.get("RESPONSE_JSON", "{}"))
except json.JSONDecodeError:
    print("unknown")
else:
    status = str(data.get("status") or "")
    terminal = data.get("terminal") is True
    active = data.get("active")
    if terminal or status in {"ended_explicitly", "stream_ended", "terminal"}:
        print("ended")
    elif active is True or status == "active":
        print("active")
    else:
        print("unknown")
PYSTATUS
}

exit_cleanly_if_stream_terminal() {
  local state
  state="$(controller_stream_activity_state)"
  if [ "$state" = "ended" ]; then
    log_json "worker_shutdown" "stream_already_ended" "$(elapsed_ms "$RUNNER_START_MS")"
    exit 0
  fi
}

progress_file_has_complete_line() {
  local progress_line
  [ -f "$PROGRESS_FILE" ] || return 1
  while IFS= read -r progress_line; do
    case "$progress_line" in
      *=*)
        if [ -n "${progress_line%%=*}" ]; then
          return 0
        fi
        ;;
    esac
  done < "$PROGRESS_FILE"
  return 1
}

ffmpeg_attempt_log_has_destination_open_error() {
  local log_file="$1"
  [ -f "$log_file" ] || return 1

  # Keep output classification contextual. Generic network errors such as
  # "Connection refused" can also happen while opening the input proxy RTMP URL,
  # so they must not be classified as destination errors unless the output URL
  # appears in the same log line or FFmpeg explicitly reports an output failure.
  if grep -qiE 'Error opening output|Error opening output file|Could not write header|Failed to update header|av_interleaved_write_frame|Output file.*I/O error' "$log_file"; then
    return 0
  fi

  if [ -n "${TARGET_RTMP:-}" ] \
    && grep -F "$TARGET_RTMP" "$log_file" | grep -qiE 'Connection refused|Connection reset by peer|Broken pipe|I/O error|Immediate exit requested|Operation timed out|Connection timed out'; then
    return 0
  fi

  return 1
}

ffmpeg_attempt_log_has_input_open_error() {
  local log_file="$1"
  [ -f "$log_file" ] || return 1

  if grep -qiE 'Error opening input|Error opening input file|Input/output error|Server error: Already publishing|Operation timed out|Connection timed out' "$log_file"; then
    return 0
  fi

  if [ -n "${PROXY_RTMP:-}" ] \
    && grep -F "$PROXY_RTMP" "$log_file" | grep -qiE 'Connection refused|Connection reset by peer|I/O error|Operation timed out|Connection timed out'; then
    return 0
  fi

  return 1
}

log_ffmpeg_started_once() {
  if [ -f "$STARTED_LOG_FILE" ]; then
    return 0
  fi
  if mkdir "$STARTED_LOG_LOCK" 2>/dev/null; then
    if [ ! -f "$STARTED_LOG_FILE" ]; then
      # This means FFmpeg produced observable progress, not merely that the process was spawned.
      log_json "ffmpeg_started" "ok" "$(elapsed_ms "${FFMPEG_START_MS:-$RUNNER_START_MS}")"
      : > "$STARTED_LOG_FILE"
    fi
    rmdir "$STARTED_LOG_LOCK" 2>/dev/null || true
  fi
  return 0
}

notify_ffmpeg_started_once() {
  if [ -f "$STARTED_NOTIFY_FILE" ]; then
    return 0
  fi
  if mkdir "$STARTED_NOTIFY_LOCK" 2>/dev/null; then
    set +e
    notify_controller "/workers/ffmpeg/started"
    notify_exit=$?
    set -e
    if [ "$notify_exit" -eq 0 ]; then
      : > "$STARTED_NOTIFY_FILE"
      rmdir "$STARTED_NOTIFY_LOCK" 2>/dev/null || true
      return 0
    fi
    if [ ! -f "$STARTED_NOTIFY_ERROR_FILE" ]; then
      log_json "worker_error" "ffmpeg_start_notify_failed" "$(elapsed_ms "$RUNNER_START_MS")"
      : > "$STARTED_NOTIFY_ERROR_FILE"
    fi
    rmdir "$STARTED_NOTIFY_LOCK" 2>/dev/null || true
  fi
  return 1
}

log_first_progress_once() {
  log_ffmpeg_started_once
  if [ -f "$PROGRESS_LOG_FILE" ]; then
    return 0
  fi
  if mkdir "$PROGRESS_LOG_LOCK" 2>/dev/null; then
    if [ ! -f "$PROGRESS_LOG_FILE" ]; then
      log_json "ffmpeg_first_progress" "ok" "$(elapsed_ms "${FFMPEG_START_MS:-$RUNNER_START_MS}")"
      : > "$PROGRESS_LOG_FILE"
    fi
    rmdir "$PROGRESS_LOG_LOCK" 2>/dev/null || true
  fi
  return 0
}

notify_first_progress_once() {
  log_first_progress_once
  notify_ffmpeg_started_once || true
  if [ -f "$PROGRESS_NOTIFY_FILE" ]; then
    return 0
  fi
  if mkdir "$PROGRESS_NOTIFY_LOCK" 2>/dev/null; then
    set +e
    notify_controller "/workers/progress"
    notify_exit=$?
    set -e
    if [ "$notify_exit" -eq 0 ]; then
      : > "$PROGRESS_NOTIFY_FILE"
      rmdir "$PROGRESS_NOTIFY_LOCK" 2>/dev/null || true
      return 0
    fi
    if [ ! -f "$PROGRESS_NOTIFY_ERROR_FILE" ]; then
      log_json "worker_error" "first_progress_notify_failed" "$(elapsed_ms "$RUNNER_START_MS")"
      : > "$PROGRESS_NOTIFY_ERROR_FILE"
    fi
    rmdir "$PROGRESS_NOTIFY_LOCK" 2>/dev/null || true
  fi
  return 1
}

stop_progress_reader() {
  if [ -n "${PROGRESS_READER_PID:-}" ]; then
    kill -TERM "$PROGRESS_READER_PID" 2>/dev/null || true
    wait "$PROGRESS_READER_PID" 2>/dev/null || true
    PROGRESS_READER_PID=""
  fi
}

cleanup() {
  if [ "${BASH_SUBSHELL:-0}" != "0" ] || [ "${BASHPID:-$$}" != "$MAIN_BASHPID" ]; then
    return 0
  fi
  stop_progress_reader
  if [ -n "${FFMPEG_PID:-}" ] && kill -0 "$FFMPEG_PID" 2>/dev/null; then
    kill -TERM "$FFMPEG_PID" 2>/dev/null || true
    wait "$FFMPEG_PID" 2>/dev/null || true
  fi
  if [ -n "${STREAM_KEY:-}" ]; then
    rm -f "$PID_FILE" "$PROGRESS_NOTIFY_FILE" "$PROGRESS_NOTIFY_ERROR_FILE" "$PROGRESS_LOG_FILE" \
      "$STARTED_NOTIFY_FILE" "$STARTED_NOTIFY_ERROR_FILE" "$STARTED_LOG_FILE"
    rm -rf "$PROGRESS_NOTIFY_LOCK" "$PROGRESS_LOG_LOCK" "$STARTED_NOTIFY_LOCK" "$STARTED_LOG_LOCK"
  fi
}
trap cleanup TERM INT HUP

wait_for_ffmpeg_exit() {
  local pid="$1"
  local exit_code
  set +e
  wait "$pid"
  exit_code=$?
  set -e
  printf '%s' "$exit_code"
}

start_progress_reader() {
  (
    progress_notified=0
    while [ "$progress_notified" -eq 0 ]; do
      if progress_file_has_complete_line && notify_first_progress_once; then
        progress_notified=1
        break
      fi
      if [ -n "${FFMPEG_PID:-}" ] && ! kill -0 "$FFMPEG_PID" 2>/dev/null; then
        # One final pass in case FFmpeg exited immediately after writing progress.
        if progress_file_has_complete_line && notify_first_progress_once; then
          progress_notified=1
        fi
        break
      fi
      sleep "$FFMPEG_PROGRESS_NOTIFY_POLL_SECONDS"
    done
  ) &
  PROGRESS_READER_PID=$!
}

json_is_valid() {
  local value="${1:-}"
  JSON_VALUE="$value" python3 - <<'PYJSON'
import json
import os
import sys

try:
    json.loads(os.environ.get("JSON_VALUE", ""))
except json.JSONDecodeError:
    sys.exit(1)
PYJSON
}

select_youtube_encoder_settings() {
  local probe_json="${1:-}"
  PROBE_JSON="$probe_json" python3 - <<'PYYT'
import json
import os

profiles = [
    {"name": "2160p60", "height": 2160, "fps": 60, "bitrate": "35M"},
    {"name": "2160p30", "height": 2160, "fps": 30, "bitrate": "30M"},
    {"name": "1440p60", "height": 1440, "fps": 60, "bitrate": "24M"},
    {"name": "1440p30", "height": 1440, "fps": 30, "bitrate": "15M"},
    {"name": "1080p60", "height": 1080, "fps": 60, "bitrate": "12M"},
    {"name": "1080p30", "height": 1080, "fps": 30, "bitrate": "10M"},
    {"name": "720p60", "height": 720, "fps": 60, "bitrate": "6M"},
    {"name": "240p-720p30", "height": 720, "fps": 30, "bitrate": "4M"},
]

def parse_fps(value):
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den = float(den)
            return float(num) / den if den else 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0

def first_positive_fps(*values):
    for value in values:
        fps_value = parse_fps(value)
        if fps_value > 0:
            return fps_value
    return 30.0

try:
    data = json.loads(os.environ.get("PROBE_JSON", "{}"))
except json.JSONDecodeError:
    data = {}

video_stream = next(
    (stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"),
    {},
)
width = int(video_stream.get("width") or 1280)
height = int(video_stream.get("height") or 720)
# Treat the shorter display axis as the YouTube resolution class so portrait
# streams such as 1080x1920 map to 1080p instead of being mistaken for 2160p.
resolution_axis = min(width, height)
fps = first_positive_fps(video_stream.get("avg_frame_rate"), video_stream.get("r_frame_rate"), "30/1")
fps_bucket = 60 if fps > 45 else 30
output_fps = int(round(min(fps, fps_bucket)))
if output_fps <= 0:
    output_fps = fps_bucket

if not video_stream:
    selected = profiles[-1]
else:
    candidates = [profile for profile in profiles if profile["fps"] == fps_bucket]
    selected = min(candidates, key=lambda profile: (abs(profile["height"] - resolution_axis), profile["height"]))
# Normalize to the chosen profile axis. The 240p-720p30 bucket is a range, so
# sub-720p inputs keep their original shorter axis instead of being upscaled.
output_resolution_axis = min(resolution_axis, selected["height"]) if selected["height"] <= 720 else selected["height"]

print(f'{selected["name"]}|{output_resolution_axis}|{output_fps}|{selected["bitrate"]}')
PYYT
}

build_ffmpeg_transcode_args() {
  local probe_json profile_name profile_axis profile_fps video_bitrate
  set +e
  probe_json="$(timeout "$FFPROBE_TIMEOUT_SECONDS" ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_type,width,height,avg_frame_rate,r_frame_rate \
    -of json "$PROXY_RTMP" 2>/dev/null)"
  probe_exit=$?
  set -e
  if [ "$probe_exit" -ne 0 ] || [ -z "$probe_json" ]; then
    probe_json='{}'
    log_json "worker_warning" "ffprobe_failed_using_youtube_240p_720p30_defaults" "$(elapsed_ms "$RUNNER_START_MS")"
  elif ! json_is_valid "$probe_json"; then
    probe_json='{}'
    log_json "worker_warning" "ffprobe_invalid_json_using_youtube_240p_720p30_defaults" "$(elapsed_ms "$RUNNER_START_MS")"
  fi
  IFS='|' read -r profile_name profile_axis profile_fps video_bitrate < <(select_youtube_encoder_settings "$probe_json")
  YOUTUBE_PROFILE_NAME="$profile_name"
  YOUTUBE_OUTPUT_AXIS="$profile_axis"
  YOUTUBE_PROFILE_FPS="$profile_fps"
  YOUTUBE_VIDEO_BITRATE="$video_bitrate"
  log_json "youtube_encoder_profile_selected" "$YOUTUBE_PROFILE_NAME" "$(elapsed_ms "$RUNNER_START_MS")"
  log_json "youtube_encoder_output_selected" "axis_${YOUTUBE_OUTPUT_AXIS}_fps_${YOUTUBE_PROFILE_FPS}" "$(elapsed_ms "$RUNNER_START_MS")"

  FFMPEG_TRANSCODE_ARGS=(
    -c:v libx264
    -preset veryfast
    -pix_fmt yuv420p
    -profile:v high
    -bf 2
    -refs 1
    -coder 1
    -b:v "$YOUTUBE_VIDEO_BITRATE"
    -maxrate "$YOUTUBE_VIDEO_BITRATE"
    -minrate "$YOUTUBE_VIDEO_BITRATE"
    -bufsize "$(( ${YOUTUBE_VIDEO_BITRATE%M} * 2 ))M"
    -x264-params "nal-hrd=cbr:force-cfr=1"
    -r "$YOUTUBE_PROFILE_FPS"
    -g "$(( YOUTUBE_PROFILE_FPS * 2 ))"
    -keyint_min "$(( YOUTUBE_PROFILE_FPS * 2 ))"
    -sc_threshold 0
    -vf "scale=w='if(lte(iw,ih),$YOUTUBE_OUTPUT_AXIS,-2)':h='if(lte(iw,ih),-2,$YOUTUBE_OUTPUT_AXIS)',setsar=1"
    -colorspace bt709
    -color_primaries bt709
    -color_trc bt709
    -c:a aac
    -b:a 128k
    -ar 44100
    -ac 2
    -flvflags no_duration_filesize
  )
}

RTMP_PUSH_BASE_URL="${RTMP_PUSH_BASE_URL:-}"
PROXY_ADDR="${PROXY_DNS:-}"
CONTROLLER_API="${CONTROLLER_API:-http://controller.media.svc.cluster.local:8000}"
WORKER_POD="${WORKER_POD:-${HOSTNAME:-unknown-worker}}"

if [ -z "$PROXY_ADDR" ] || [ -z "$STREAM_KEY" ] || [ -z "$RTMP_PUSH_BASE_URL" ]; then
  log_json "worker_error" "missing_required_startup_args" "$(elapsed_ms "$RUNNER_START_MS")"
  exit 1
fi

PROXY_RTMP="rtmp://${PROXY_ADDR}:1935/live/${STREAM_KEY}"
TARGET_RTMP="${RTMP_PUSH_BASE_URL}/${STREAM_KEY}"
rm -f "$PROGRESS_FILE" "$PROGRESS_NOTIFY_FILE" "$PROGRESS_NOTIFY_ERROR_FILE" "$PROGRESS_LOG_FILE" \
  "$STARTED_NOTIFY_FILE" "$STARTED_NOTIFY_ERROR_FILE" "$STARTED_LOG_FILE"
rm -rf "$PROGRESS_NOTIFY_LOCK" "$PROGRESS_LOG_LOCK" "$STARTED_NOTIFY_LOCK" "$STARTED_LOG_LOCK"
: > "$PROGRESS_FILE"

attempt=1
CURRENT_ATTEMPT="$attempt"
FFMPEG_START_MS="$(current_epoch_ms)"
FFMPEG_RUN_ID=""
ATTEMPT_LOG_FILE="/tmp/ffmpeg_${SAFE_STREAM_KEY}.attempt_${attempt}.log"
: > "$ATTEMPT_LOG_FILE"
log_json "ffmpeg_process_spawned" "single_attempt_no_timeout" "$(elapsed_ms "$FFMPEG_START_MS")"
build_ffmpeg_transcode_args

ffmpeg \
  -loglevel "$FFMPEG_LOGLEVEL" \
  -nostats \
  -progress "/tmp/ffmpeg_${SAFE_STREAM_KEY}.progress" \
  -i "$PROXY_RTMP" \
  "${FFMPEG_TRANSCODE_ARGS[@]}" \
  -progress "$PROGRESS_FILE" \
  -f flv "$TARGET_RTMP" \
  >> "$ATTEMPT_LOG_FILE" 2>&1 &

FFMPEG_PID=$!
FFMPEG_RUN_ID="${EPOCHREALTIME:-$(date +%s)}-${FFMPEG_PID}-${RANDOM}"
echo "$FFMPEG_PID" > "$PID_FILE"
start_progress_reader

set +e
wait "$FFMPEG_PID"
EXIT_CODE=$?
set -e

echo "$EXIT_CODE" > "$LAST_EXIT_FILE"
printf '%s %s\n' "$FFMPEG_RUN_ID" "$EXIT_CODE" >> "$EXIT_EVENT_FILE"
cat "$ATTEMPT_LOG_FILE" >> "/tmp/ffmpeg_${SAFE_STREAM_KEY}.log" 2>/dev/null || true
rm -f "$PID_FILE"

if progress_file_has_complete_line; then
  for progress_notify_retry in 1 2 3; do
    if notify_first_progress_once; then
      break
    fi
    sleep "$FFMPEG_PROGRESS_NOTIFY_POLL_SECONDS"
  done
fi
stop_progress_reader

log_json "ffmpeg_exited" "exit_${EXIT_CODE}_no_retry" "$(elapsed_ms "$FFMPEG_START_MS")"
if [ "$EXIT_CODE" -ne 0 ]; then
  log_json "worker_error" "ffmpeg_exit_${EXIT_CODE}_no_self_recovery" "$(elapsed_ms "$RUNNER_START_MS")"
  tail -n 40 "/tmp/ffmpeg_${SAFE_STREAM_KEY}.log" | sed 's/^/[ffmpeg] /' || true
fi

# Do not retry or recover locally. restartPolicy=Never on worker pods keeps this
# as a terminal worker result; the controller will only delete workers when the
# proxy emits exec_publish_done.
exit "$EXIT_CODE"
