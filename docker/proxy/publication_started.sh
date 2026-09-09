#!/bin/sh
set -eu

. /scripts/kubernetes_api.sh
. /scripts/publication_state.sh

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

proxy_name=${POD_NAME:?POD_NAME is required}
proxy_host=${POD_IP:?POD_IP is required}
# The Downward API supplies the address, but constrain it before interpolating it
# so a misconfigured environment cannot alter the RTMP URL structure.
case "$proxy_host" in
    *[!0-9a-fA-F:.]*|''|.*|*.) log "invalid pod IP"; exit 1 ;;
esac
case "$proxy_host" in *:*) proxy_host="[$proxy_host]" ;; esac
session_id=$(cat /proc/sys/kernel/random/uuid)
# Admitted keys are already RFC 3986 unreserved characters. Still encode at the
# URL boundary so this remains safe if the resource-name policy evolves.
encoded_stream_key=$(printf '%s' "$stream_key" | jq -sRr @uri)
source_url="rtmp://${proxy_host}:1935/${application}/${encoded_stream_key}"
state_dir=$(publication_state_dir)
state_file=$(publication_state_file "$state_dir" "$stream_key" "$publication_id")

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
    '{proxyName: $proxyName, sessionId: $sessionId, url: $url, available: true}')

attempt=0
while :; do
    status=$(kubernetes_api_request GET "${LIVESTREAMS_API_PATH}/${stream_key}" "$response_file")
    if [ "$status" != 200 ] || ! jq -e '.metadata.deletionTimestamp != null' "$response_file" >/dev/null; then
        break
    fi
    attempt=$((attempt + 1))
    [ "$attempt" -lt "${TERMINATION_WAIT_ATTEMPTS:-30}" ] || {
        log "LiveStream is still terminating; rejecting reconnect"
        exit 1
    }
    sleep 1
done
case "$status" in
    200)
        jq -n --argjson source "$source_json" '{spec: {source: $source}}' >"$request_file"
        status=$(kubernetes_api_request PATCH "${LIVESTREAMS_API_PATH}/${stream_key}" \
            "$response_file" "$request_file" 'application/merge-patch+json')
        [ "$status" = 200 ] || { log "Kubernetes rejected LiveStream source update (HTTP $status)"; exit 1; }
        ;;
    404)
        target_secret_name=${TARGET_RTMP_SECRET_NAME:?TARGET_RTMP_SECRET_NAME is required to create a LiveStream}
        target_secret_key=${TARGET_RTMP_SECRET_KEY:-base-url}
        status=$(kubernetes_api_request GET "${SECRETS_API_PATH}/${target_secret_name}" "$response_file")
        [ "$status" = 200 ] && jq -e --arg key "$target_secret_key" \
            '.data[$key] | type == "string" and length > 0' "$response_file" >/dev/null || {
            log "target Secret or key is unavailable; rejecting publication"
            exit 1
        }
        jq -n \
            --arg name "$stream_key" \
            --arg streamKey "$stream_key" \
            --arg targetSecretName "$target_secret_name" \
            --arg targetSecretKey "$target_secret_key" \
            --argjson source "$source_json" \
            '{apiVersion: "liveedgecast.io/v1alpha1", kind: "LiveStream",
              metadata: {name: $name},
              spec: {streamKey: $streamKey, source: $source,
                target: {baseUrlSecretRef: {name: $targetSecretName, key: $targetSecretKey}}}}' >"$request_file"
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
    --arg streamKey "$stream_key" \
    --arg resourceName "$stream_key" \
    --arg localConnectionId "$publication_id" \
    --arg sessionId "$session_id" \
    '{streamKey: $streamKey, sessionId: $sessionId,
      localConnectionId: $localConnectionId, resourceName: $resourceName}' >"$state_tmp"
publication_state_commit "$state_tmp" "$state_file"
log "registered stream '$stream_key' with session '$session_id'"
