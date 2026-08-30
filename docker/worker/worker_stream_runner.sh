#!/bin/bash
set -uo pipefail

STREAM_KEY="${STREAM_KEY:-}"
SOURCE_RTMP_URL="${SOURCE_RTMP_URL:-}"
TARGET_RTMP_URL="${TARGET_RTMP_URL:-}"
MEDIA_HEALTH_INTERVAL_SECONDS="${MEDIA_HEALTH_INTERVAL_SECONDS:-10}"
FFMPEG_TERMINATION_GRACE_SECONDS=5
WATCHDOG_STALL_EXIT_CODE=75

ffmpeg_pid=""
reader_pid=""
watchdog_pid=""
monitor_dir=""

log() { echo "[$(date)] [worker_stream_runner] $1"; }

monotonic_milliseconds() {
  local uptime_seconds uptime_fraction ignored

  # /proc/uptime is monotonic and available in the Linux-based Alpine image.
  # Avoid GNU date's %N extension, which BusyBox date does not implement.
  IFS='. ' read -r uptime_seconds uptime_fraction ignored </proc/uptime
  uptime_fraction="${uptime_fraction}000"
  printf '%s%s\n' "$uptime_seconds" "${uptime_fraction:0:3}"
}

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

terminate_ffmpeg() {
  local signal="${1:-TERM}"
  local killer_pid

  if [ -z "$ffmpeg_pid" ] || ! kill -0 "$ffmpeg_pid" 2>/dev/null; then
    return
  fi

  forward_signal "$signal"
  (
    sleep "$FFMPEG_TERMINATION_GRACE_SECONDS"
    if kill -0 "$ffmpeg_pid" 2>/dev/null; then
      log "FFmpeg did not stop within ${FFMPEG_TERMINATION_GRACE_SECONDS}s after SIG${signal}; sending SIGKILL"
      kill -KILL "$ffmpeg_pid" 2>/dev/null || true
    fi
  ) &
  killer_pid=$!
  wait "$ffmpeg_pid" 2>/dev/null || true
  kill "$killer_pid" 2>/dev/null || true
  wait "$killer_pid" 2>/dev/null || true
}

cleanup() {
  trap - EXIT TERM INT
  terminate_ffmpeg TERM
  stop_auxiliary_processes
  [ -z "$monitor_dir" ] || rm -rf "$monitor_dir"
}

handle_signal() {
  local signal="$1"
  local exit_code="$2"
  trap - TERM INT
  log "Received SIG${signal}; forwarding it to FFmpeg"
  terminate_ffmpeg "$signal"
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
# Keep subsequent arithmetic explicitly decimal.
MEDIA_HEALTH_INTERVAL_SECONDS=$((10#$MEDIA_HEALTH_INTERVAL_SECONDS))

monitor_dir="$(mktemp -d)"
progress_fifo="$monitor_dir/ffmpeg-progress"
last_progress_file="$monitor_dir/last-progress"
mkfifo "$progress_fifo"

started_at_ms="$(monotonic_milliseconds)"
health_interval_ms=$((MEDIA_HEALTH_INTERVAL_SECONDS * 1000))
printf '%s\n' "$started_at_ms" >"$last_progress_file"

# Only increasing media timestamps from FFmpeg's dedicated progress FIFO count
# as health. A separate value is retained for each FFmpeg timestamp key.
(
  declare -A last_media_timestamp=([out_time_us]=0 [out_time_ms]=0)
  while IFS='=' read -r key value; do
    case "$key" in
      out_time_us|out_time_ms)
        if [[ "$value" =~ ^[0-9]+$ ]]; then
          if (( 10#$value > last_media_timestamp[$key] )); then
            last_media_timestamp[$key]=$((10#$value))
            monotonic_milliseconds >"$last_progress_file.tmp"
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
    now_ms="$(monotonic_milliseconds)"
    last_progress_ms="$(cat "$last_progress_file" 2>/dev/null || printf '%s' "$started_at_ms")"
    if (( now_ms - last_progress_ms >= health_interval_ms )); then
      exit "$WATCHDOG_STALL_EXIT_CODE"
    fi
  done
) &
watchdog_pid=$!

# Reap whichever process completes first. Only the parent decides to terminate
# FFmpeg, so a normal FFmpeg exit and a watchdog timeout cannot both claim the
# outcome through an independently written marker.
completed_pid=""
if wait -n -p completed_pid "$ffmpeg_pid" "$watchdog_pid"; then
  completed_exit_code=0
else
  completed_exit_code=$?
fi

if [ "$completed_pid" = "$watchdog_pid" ]; then
  watchdog_pid=""
  log "Media watchdog fired: no increasing FFmpeg media timestamp for ${MEDIA_HEALTH_INTERVAL_SECONDS}s; sending SIGTERM"
  terminate_ffmpeg TERM
  ffmpeg_pid=""
  stop_auxiliary_processes
  log "FFmpeg was terminated by the media watchdog; exiting non-zero"
  exit 1
fi

ffmpeg_pid=""
stop_auxiliary_processes
log "FFmpeg exited with code $completed_exit_code"
exit "$completed_exit_code"
