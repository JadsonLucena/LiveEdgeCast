#!/bin/sh
set -eu

. /scripts/publication_state.sh

state_dir=$(publication_state_dir)
umask 077
mkdir -p "$state_dir"
chmod 700 "$state_dir"

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
            /scripts/publication_ended.sh "$stream_key" "$connection_identity" "$marker_session" || true
        done
        sleep "${TERMINATION_RETRY_SECONDS:-2}"
    done
) &

exec nginx -g 'daemon off;'
