#!/bin/sh
set -eu

. /scripts/kubernetes_api.sh
. /scripts/publication_state.sh

log() {
    printf '%s publication_ended: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

stream_key=${1:-}
connection_identity=${2:-}
expected_session_id=${3:-}
nginx_lifetime_id=${4:-${NGINX_LIFETIME_ID:-}}
state_file_override=${5:-}
[ -n "$stream_key" ] && [ -n "$connection_identity" ] || {
    log "incomplete publication identity"
    exit 1
}

state_dir=$(publication_state_dir)
if [ -n "$state_file_override" ]; then
    state_file=$state_file_override
else
    [ -n "$nginx_lifetime_id" ] || { log "nginx lifetime identity is required"; exit 1; }
    state_file=$(publication_state_file "$state_dir" "$stream_key" "$connection_identity" \
        "$nginx_lifetime_id")
fi
umask 077
mkdir -p "$state_dir"
exec 9>"${state_file}.lifecycle.lock"
flock 9

state_record=$(cat "$state_file" 2>/dev/null) || {
    if [ -f "${state_file}.ended" ]; then
        log "publication '$stream_key' was already terminated"
    else
        # Preserve an early end event until a concurrent start transaction has
        # committed its session. The entrypoint reaper consumes the marker that
        # publication_started creates after committing that state.
        touch "${state_file}.pending-terminate"
        log "publication ended before registration completed; termination retained"
    fi
    exit 0
}

session_id=$(jq -er --arg streamKey "$stream_key" --arg connectionIdentity "$connection_identity" \
    'select(.streamKey == $streamKey and .connectionIdentity == $connectionIdentity) | .sessionId' \
    <<EOF
$state_record
EOF
) || {
    log "local publication identity does not match; ignoring"
    exit 0
}
# Accept records left by an older container in the Pod's emptyDir while all new
# records use the field that explicitly describes the persisted association.
resource_name=$(printf '%s\n' "$state_record" | jq -er '.liveStreamName // .resourceName')
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
    touch "${state_file}.ended"
    rm -f "${state_file}.pending-terminate"
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
    touch "${state_file}.ended"
    rm -f "${state_file}.pending-terminate"
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
touch "${state_file}.ended"
rm -f "${state_file}.pending-terminate"
log "removed stream '$stream_key' for session '$session_id'"
