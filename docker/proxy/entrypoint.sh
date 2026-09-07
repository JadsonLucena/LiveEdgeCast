#!/bin/sh
set -eu

. /scripts/publication_state.sh

state_dir=$(publication_state_dir)
umask 077
mkdir -p "$state_dir"
chmod 700 "$state_dir"

cleanup() {
    kill "$lifecycle_pid" 2>/dev/null || true
    kill "$reaper_pid" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

socat TCP-LISTEN:18080,bind=127.0.0.1,reuseaddr,fork EXEC:/scripts/lifecycle_http.sh &
lifecycle_pid=$!

# Resume termination requests retained after a transient API failure or
# lifecycle-handler restart.
(
    while :; do
        for marker in "$state_dir"/*.json.*.terminate; do
            [ -f "$marker" ] || continue
            state=${marker%.*.terminate}
            marker_session=${marker##*.json.}
            marker_session=${marker_session%.terminate}
            state_session=$(jq -er '.sessionId' "$state" 2>/dev/null) || {
                rm -f "$marker"
                continue
            }
            if [ "$marker_session" != "$state_session" ]; then
                rm -f "$marker"
                continue
            fi
            stream_key=$(jq -er '.streamKey' "$state" 2>/dev/null) || continue
            connection_id=$(jq -er '.localConnectionId' "$state" 2>/dev/null) || continue
            /scripts/publication_ended.sh "$stream_key" live retry "$connection_id" "$marker_session" || true
        done
        sleep "${TERMINATION_RETRY_SECONDS:-2}"
    done
) &
reaper_pid=$!

exec nginx -g 'daemon off;'
