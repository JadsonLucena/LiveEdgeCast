#!/bin/sh
set -eu

. /scripts/publication_state.sh

state_dir=$(publication_state_dir)
umask 077
mkdir -p "$state_dir"
chmod 700 "$state_dir"

# nginx connection numbers restart at one after every process restart. Scope
# state keys to this lifetime so a retained emptyDir can never make a new
# connection collide with a tombstone from an earlier nginx process.
NGINX_LIFETIME_ID=$(cat /proc/sys/kernel/random/uuid)
export NGINX_LIFETIME_ID

# No callback from a previous nginx process can still be running here. Its
# completed/partial tombstones and lock inodes are therefore safe to discard;
# durable JSON session and termination markers remain available to the reaper.
rm -f "$state_dir"/*.ended "$state_dir"/*.pending-terminate \
    "$state_dir"/*.lifecycle.lock "$state_dir"/*.lock

socat TCP-LISTEN:18080,bind=127.0.0.1,reuseaddr,fork EXEC:/scripts/lifecycle_http.sh &

# Resume termination requests retained after a transient API failure or
# proxy restart.
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
            stream_key=$(jq -er '.streamKey' "$state" 2>/dev/null) || {
                rm -f "$marker"
                continue
            }
            connection_identity=$(jq -er '.connectionIdentity' "$state" 2>/dev/null) || {
                rm -f "$marker"
                continue
            }
            # Use the marker's exact state file so retries written by an older
            # nginx lifetime (including pre-lifetime records) remain recoverable.
            /scripts/publication_ended.sh "$stream_key" "$connection_identity" \
                "$marker_session" "" "$state" || true
        done
        sleep "${TERMINATION_RETRY_SECONDS:-2}"
    done
) &

exec nginx -g 'daemon off;'
