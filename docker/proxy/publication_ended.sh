#!/bin/sh
set -eu

. /scripts/kubernetes_api.sh

log() {
    printf '%s publication_ended: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

stream_key=${1:-}
application=${2:-}
publisher_address=${3:-}
publication_id=${4:-}
[ -n "$stream_key" ] && [ "$application" = live ] && \
    [ -n "$publisher_address" ] && [ -n "$publication_id" ] || {
    log "incomplete publication identity"
    exit 1
}

state_dir=${PUBLICATION_STATE_DIR:-/var/run/liveedgecast/publications}
state_key=$(printf '%s\n%s' "$stream_key" "$publication_id" | sha256sum | cut -d ' ' -f 1)
state_file="${state_dir}/${state_key}.json"
[ -f "$state_file" ] || { log "no confirmed local session for '$stream_key'; ignoring"; exit 0; }

session_id=$(jq -er --arg publicationId "$publication_id" \
    'select(.publicationId == $publicationId) | .sessionId' "$state_file") || {
    log "local publication identity does not match; ignoring"
    exit 0
}
resource_name=$(jq -er '.resourceName' "$state_file")

kubernetes_api_init
work_dir=$(mktemp -d "${state_dir}/.ended.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM
response_file="${work_dir}/response.json"
request_file="${work_dir}/request.json"

status=$(kubernetes_api_request GET "${LIVESTREAMS_API_PATH}/${resource_name}" "$response_file")
if [ "$status" = 404 ]; then
    rm -f "$state_file"
    exit 0
fi
[ "$status" = 200 ] || { log "could not read LiveStream (HTTP $status)"; exit 1; }

remote_session=$(jq -er '.spec.source.sessionId' "$response_file") || {
    log "LiveStream has no source session; refusing deletion"
    exit 1
}
if [ "$remote_session" != "$session_id" ]; then
    log "session '$session_id' is stale; leaving LiveStream untouched"
    rm -f "$state_file"
    exit 0
fi

uid=$(jq -er '.metadata.uid' "$response_file")
jq -n --arg uid "$uid" '{kind: "DeleteOptions", apiVersion: "v1", preconditions: {uid: $uid}}' >"$request_file"
status=$(kubernetes_api_request DELETE "${LIVESTREAMS_API_PATH}/${resource_name}" \
    "$response_file" "$request_file")
[ "$status" = 200 ] || [ "$status" = 202 ] || [ "$status" = 404 ] || {
    log "Kubernetes rejected LiveStream deletion (HTTP $status)"
    exit 1
}

rm -f "$state_file"
log "removed stream '$stream_key' for session '$session_id'"
