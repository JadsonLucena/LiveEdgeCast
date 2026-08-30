#!/bin/bash
set -uo pipefail

STREAM_KEY="${STREAM_KEY:-}"
SOURCE_RTMP_URL="${SOURCE_RTMP_URL:-}"
TARGET_RTMP_URL="${TARGET_RTMP_URL:-}"
MEDIA_HEALTH_INTERVAL_SECONDS="${MEDIA_HEALTH_INTERVAL_SECONDS:-10}"
FFMPEG_TERMINATION_GRACE_SECONDS=5

ffmpeg_pid=""
reader_pid=""
watchdog_pid=""
monitor_dir=""

log() { echo "[$(date)] [worker_stream_runner] $1"; }

stop_auxiliary_processes() {
  if [ -n "$watchdog_pid" ]; then
    kill "$watchdog_pid" 2>/dev/null || true
    wait "$watchdog_pid" 2>/dev/null || true
    watchdog_pid=""
  fi
  if [ -n "$reader_pid" ]; then
    kill "$reader_pid" 2>/dev/null || true
    wait "$reader_pid" 2>/dev/null || true
    reader_pid=""
  fi
}

forward_signal() {
  local signal="$1"
  if [ -n "$ffmpeg_pid" ] && kill -0 "$ffmpeg_pid" 2>/dev/null; then
    kill -s "$signal" "$ffmpeg_pid" 2>/dev/null || true
  fi
}

cleanup() {
  trap - EXIT TERM INT
  forward_signal TERM
  stop_auxiliary_processes
  [ -z "$monitor_dir" ] || rm -rf "$monitor_dir"
}

handle_signal() {
  local signal="$1"
  local exit_code="$2"
  trap - TERM INT
  log "Received SIG${signal}; forwarding it to FFmpeg"
  forward_signal "$signal"
  if [ -n "$ffmpeg_pid" ]; then
    wait "$ffmpeg_pid" 2>/dev/null || true
  fi
  exit "$exit_code"
}

trap cleanup EXIT
trap 'handle_signal TERM 143' TERM
trap 'handle_signal INT 130' INT

log "Starting worker stream runner for stream key '$STREAM_KEY'"

if [ -z "$SOURCE_RTMP_URL" ] || [ -z "$STREAM_KEY" ] || [ -z "$TARGET_RTMP_URL" ]; then
  log "Missing required startup args (SOURCE_RTMP_URL/STREAM_KEY/TARGET_RTMP_URL). Crashing worker."
  exit 1
fi

if ! [[ "$MEDIA_HEALTH_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  log "MEDIA_HEALTH_INTERVAL_SECONDS must be a positive integer (received '$MEDIA_HEALTH_INTERVAL_SECONDS')."
  exit 1
fi

monitor_dir="$(mktemp -d)"
progress_fifo="$monitor_dir/ffmpeg-progress"
last_progress_file="$monitor_dir/last-progress"
watchdog_fired_file="$monitor_dir/watchdog-fired"
mkfifo "$progress_fifo"

started_at="$(date +%s)"
printf '%s\n' "$started_at" >"$last_progress_file"

# Only increasing media timestamps from FFmpeg's dedicated progress FIFO count
# as health. A separate value is retained for each FFmpeg timestamp key.
(
  declare -A last_media_timestamp=()
  while IFS='=' read -r key value; do
    case "$key" in
      out_time_us|out_time_ms)
        if [[ "$value" =~ ^[0-9]+$ ]]; then
          if [ -z "${last_media_timestamp[$key]+set}" ]; then
            last_media_timestamp[$key]="$value"
          elif (( value > last_media_timestamp[$key] )); then
            last_media_timestamp[$key]="$value"
            printf '%s\n' "$(date +%s)" >"$last_progress_file.tmp"
            mv "$last_progress_file.tmp" "$last_progress_file"
          fi
        fi
        ;;
    esac
  done <"$progress_fifo"
) &
reader_pid=$!

log "Launching FFmpeg for stream '$STREAM_KEY' with a ${MEDIA_HEALTH_INTERVAL_SECONDS}s media health interval"
ffmpeg -loglevel warning -nostats -progress "$progress_fifo" \
  -rw_timeout 5000000 -i "$SOURCE_RTMP_URL" \
  -c:v copy -c:a copy -f flv "$TARGET_RTMP_URL" &
ffmpeg_pid=$!

(
  while kill -0 "$ffmpeg_pid" 2>/dev/null; do
    sleep 1
    now="$(date +%s)"
    last_progress="$(cat "$last_progress_file" 2>/dev/null || printf '%s' "$started_at")"
    if (( now - last_progress >= MEDIA_HEALTH_INTERVAL_SECONDS )); then
      printf '%s\n' "no increasing FFmpeg media timestamp for ${MEDIA_HEALTH_INTERVAL_SECONDS}s" >"$watchdog_fired_file"
      log "Media watchdog fired: no increasing FFmpeg media timestamp for ${MEDIA_HEALTH_INTERVAL_SECONDS}s; sending SIGTERM"
      kill -TERM "$ffmpeg_pid" 2>/dev/null || true
      sleep "$FFMPEG_TERMINATION_GRACE_SECONDS"
      if kill -0 "$ffmpeg_pid" 2>/dev/null; then
        log "FFmpeg did not stop within ${FFMPEG_TERMINATION_GRACE_SECONDS}s; sending SIGKILL"
        kill -KILL "$ffmpeg_pid" 2>/dev/null || true
      fi
      exit 0
    fi
  done
) &
watchdog_pid=$!

wait "$ffmpeg_pid"
ffmpeg_exit_code=$?
ffmpeg_pid=""
stop_auxiliary_processes

if [ -f "$watchdog_fired_file" ]; then
  log "FFmpeg was terminated by the media watchdog; exiting non-zero"
  exit 1
fi

log "FFmpeg exited with code $ffmpeg_exit_code"
exit "$ffmpeg_exit_code"
