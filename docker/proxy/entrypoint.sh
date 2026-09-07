#!/bin/sh
set -eu

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
        for marker in /var/run/liveedgecast/publications/*.json.terminate; do
            [ -f "$marker" ] || continue
            state=${marker%.terminate}
            resource=$(jq -er '.resourceName' "$state" 2>/dev/null) || continue
            publication=$(jq -er '.publicationId' "$state" 2>/dev/null) || continue
            /scripts/publication_ended.sh "$resource" live retry "$publication" || true
        done
        sleep "${TERMINATION_RETRY_SECONDS:-2}"
    done
) &
reaper_pid=$!

exec nginx -g 'daemon off;'
