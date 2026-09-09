#!/bin/sh
set -eu

. /scripts/kubernetes_api.sh
. /scripts/publication_state.sh

log() {
    printf '%s publication_ended: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

stream_key=${1:-}
application=${2:-}
publisher_address=${3:-}
publication_id=${4:-}
expected_session_id=${5:-}
[ -n "$stream_key" ] && [ "$application" = live ] && \
    [ -n "$publisher_address" ] && [ -n "$publication_id" ] || {
    log "incomplete publication identity"
    exit 1
}

state_dir=$(publication_state_dir)
state_file=$(publication_state_file "$state_dir" "$stream_key" "$publication_id")
state_record=$(cat "$state_file" 2>/dev/null) || {
    log "no confirmed local session for '$stream_key'; ignoring"
    exit 0
}

session_id=$(jq -er --arg streamKey "$stream_key" --arg localConnectionId "$publication_id" \
    'select(.streamKey == $streamKey and .localConnectionId == $localConnectionId) | .sessionId' \
    <<EOF
$state_record
EOF
) || {
    log "local publication identity does not match; ignoring"
    exit 0
}
resource_name=$(printf '%s\n' "$state_record" | jq -er '.resourceName')
if [ -n "$expected_session_id" ] && [ "$session_id" != "$expected_session_id" ]; then
    rm -f "${state_file}.${expected_session_id}.terminate"
    log "retry belongs to stale session '$expected_session_id'; ignoring"
    exit 0
fi
retry_file="${state_file}.${session_id}.terminate"
touch "$retry_file"

kubernetes_api_init
work_dir=$(mktemp -d "${state_dir}/.ended.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM
response_file="${work_dir}/response.json"
request_file="${work_dir}/request.json"

status=$(kubernetes_api_request GET "${LIVESTREAMS_API_PATH}/${resource_name}" "$response_file")
if [ "$status" = 404 ]; then
    publication_state_remove_if_session "$state_file" "$session_id" "$retry_file"
    exit 0
fi
[ "$status" = 200 ] || { log "could not read LiveStream (HTTP $status)"; exit 1; }

remote_session=$(jq -er '.spec.source.sessionId' "$response_file") || {
    log "LiveStream has no source session; refusing deletion"
    exit 1
}
if [ "$remote_session" != "$session_id" ]; then
    log "session '$session_id' is stale; leaving LiveStream untouched"
    publication_state_remove_if_session "$state_file" "$session_id" "$retry_file"
    exit 0
fi

uid=$(jq -er '.metadata.uid' "$response_file")
resource_version=$(jq -er '.metadata.resourceVersion' "$response_file")
jq -n --arg uid "$uid" --arg resourceVersion "$resource_version" \
    '{kind: "DeleteOptions", apiVersion: "v1",
      preconditions: {uid: $uid, resourceVersion: $resourceVersion}}' >"$request_file"
status=$(kubernetes_api_request DELETE "${LIVESTREAMS_API_PATH}/${resource_name}" \
    "$response_file" "$request_file")
[ "$status" = 409 ] && {
    log "resource changed after session check; scheduling a fresh session check"
    exit 1
}
[ "$status" = 200 ] || [ "$status" = 202 ] || [ "$status" = 404 ] || {
    log "Kubernetes rejected LiveStream deletion (HTTP $status)"
    exit 1
}

publication_state_remove_if_session "$state_file" "$session_id" "$retry_file"
log "removed stream '$stream_key' for session '$session_id'"
