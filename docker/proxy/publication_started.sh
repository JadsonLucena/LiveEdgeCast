#!/bin/sh
set -eu

. /scripts/kubernetes_api.sh

log() {
    printf '%s publication_started: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

stream_key=${1:-}
application=${2:-}
publisher_address=${3:-}
publication_id=${4:-}

# Using the stream key as the resource name makes the lookup deterministic.
# Requiring a DNS label also prevents path, JSON, and shell injection.
case "$stream_key" in
    ''|*[!a-z0-9-]*|-*|*-) log "invalid stream key"; exit 1 ;;
esac
[ "${#stream_key}" -le 63 ] || { log "stream key is longer than 63 characters"; exit 1; }
[ "$application" = live ] || { log "unexpected RTMP application"; exit 1; }
[ -n "$publisher_address" ] || { log "publisher address is required"; exit 1; }
[ -n "$publication_id" ] || { log "publication identifier is required"; exit 1; }

kubernetes_api_init

proxy_name=${POD_NAME:-$(hostname)}
proxy_host=${POD_IP:-$proxy_name}
session_id=$(cat /proc/sys/kernel/random/uuid)
source_url="rtmp://${proxy_host}:1935/${application}/${stream_key}"
state_dir=${PUBLICATION_STATE_DIR:-/var/run/liveedgecast/publications}
state_key=$(printf '%s\n%s' "$stream_key" "$publication_id" | sha256sum | cut -d ' ' -f 1)
state_file="${state_dir}/${state_key}.json"

umask 077
mkdir -p "$state_dir"
work_dir=$(mktemp -d "${state_dir}/.started.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM
response_file="${work_dir}/response.json"
request_file="${work_dir}/request.json"

source_json=$(jq -n \
    --arg proxyName "$proxy_name" \
    --arg sessionId "$session_id" \
    --arg url "$source_url" \
    '{proxyName: $proxyName, sessionId: $sessionId, url: $url}')

status=$(kubernetes_api_request GET "${LIVESTREAMS_API_PATH}/${stream_key}" "$response_file")
case "$status" in
    200)
        jq -n --argjson source "$source_json" '{spec: {source: $source}}' >"$request_file"
        status=$(kubernetes_api_request PATCH "${LIVESTREAMS_API_PATH}/${stream_key}" \
            "$response_file" "$request_file" 'application/merge-patch+json')
        [ "$status" = 200 ] || { log "Kubernetes rejected LiveStream source update (HTTP $status)"; exit 1; }
        ;;
    404)
        target_url=${TARGET_RTMP_URL:?TARGET_RTMP_URL is required to create a LiveStream}
        jq -n \
            --arg name "$stream_key" \
            --arg streamKey "$stream_key" \
            --arg targetUrl "$target_url" \
            --argjson source "$source_json" \
            '{apiVersion: "liveedgecast.io/v1alpha1", kind: "LiveStream",
              metadata: {name: $name},
              spec: {streamKey: $streamKey, source: $source, target: {url: $targetUrl}}}' >"$request_file"
        status=$(kubernetes_api_request POST "$LIVESTREAMS_API_PATH" \
            "$response_file" "$request_file")
        if [ "$status" = 409 ]; then
            # A concurrent publication may have won creation. Only source is mutable here.
            jq -n --argjson source "$source_json" '{spec: {source: $source}}' >"$request_file"
            status=$(kubernetes_api_request PATCH "${LIVESTREAMS_API_PATH}/${stream_key}" \
                "$response_file" "$request_file" 'application/merge-patch+json')
        fi
        [ "$status" = 201 ] || [ "$status" = 200 ] || {
            log "Kubernetes rejected LiveStream creation (HTTP $status)"
            exit 1
        }
        ;;
    *) log "could not read LiveStream (HTTP $status)"; exit 1 ;;
esac

# The session is durable locally only after Kubernetes accepted the desired source.
state_tmp="${state_file}.tmp.$$"
jq -n \
    --arg resourceName "$stream_key" \
    --arg publicationId "$publication_id" \
    --arg sessionId "$session_id" \
    '{resourceName: $resourceName, publicationId: $publicationId, sessionId: $sessionId}' >"$state_tmp"
mv "$state_tmp" "$state_file"
log "registered stream '$stream_key' with session '$session_id'"
