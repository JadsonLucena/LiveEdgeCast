#!/bin/sh
set -eu

. /scripts/kubernetes_api.sh
. /scripts/livestream_name.sh
. /scripts/publication_state.sh

log() {
    printf '%s publication_started: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

stream_key=${1:-}
application=${2:-}
publisher_address=${3:-}
publication_id=${4:-}

[ -n "$stream_key" ] || { log "stream key is required"; exit 1; }
[ "$application" = live ] || { log "unexpected RTMP application"; exit 1; }
[ -n "$publisher_address" ] || { log "publisher address is required"; exit 1; }
[ -n "$publication_id" ] || { log "publication identifier is required"; exit 1; }

encoded_stream_key=$(printf '%s' "$stream_key" | jq -sRr @uri)
resource_name=$(livestream_resource_name "$stream_key")

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
# The original key may contain any non-empty text, so encode it only at the URL
# boundary while preserving its exact value in spec.streamKey.
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
    status=$(kubernetes_api_request GET "${LIVESTREAMS_API_PATH}/${resource_name}" "$response_file")
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
        ;;
    404)
        # This configuration is needed only to create a resource. Existing
        # resources retain their own target when a publication reconnects.
        target_base_url=${RTMP_TARGET_BASE_URL:-}
        if ! jq -en --arg url "$target_base_url" \
            '$url | test("^rtmps?://[^/[:space:]?#]+(/[^[:space:]?#]*)?$")' >/dev/null; then
            log "RTMP_TARGET_BASE_URL must be a non-empty rtmp:// or rtmps:// URL without a query or fragment; rejecting publication"
            exit 1
        fi
        target_url="${target_base_url%/}/${encoded_stream_key}"
        if ! jq -en --arg url "$target_url" \
            '$url | test("^rtmps?://[^/[:space:]?#]+(/[^[:space:]?#]*)+$")' >/dev/null; then
            log "could not construct a valid RTMP target URL; rejecting publication"
            exit 1
        fi
        # Creation owns only metadata and the desired spec (in addition to the
        # Kubernetes type identifiers). Lifecycle status belongs exclusively
        # to the Operator and is deliberately absent from this request.
        jq -n \
            --arg name "$resource_name" \
            --arg streamKey "$stream_key" \
            --arg targetUrl "$target_url" \
            --argjson source "$source_json" \
            '{apiVersion: "liveedgecast.io/v1alpha1", kind: "LiveStream",
              metadata: {name: $name},
              spec: {streamKey: $streamKey, source: $source,
                target: {url: $targetUrl},
                recoveryPolicy: {retryIntervalSeconds: 2, maxRetries: 5,
                  interruptionTTLSeconds: 10}}}' >"$request_file"
        status=$(kubernetes_api_request POST "$LIVESTREAMS_API_PATH" \
            "$response_file" "$request_file")
        if [ "$status" = 409 ]; then
            # A concurrent publication won creation. Re-read it before updating.
            status=$(kubernetes_api_request GET "${LIVESTREAMS_API_PATH}/${resource_name}" \
                "$response_file")
        fi
        [ "$status" = 201 ] || [ "$status" = 200 ] || {
            log "Kubernetes rejected LiveStream creation (HTTP $status)"
            exit 1
        }
        ;;
    *) log "could not read LiveStream (HTTP $status)"; exit 1 ;;
esac

if [ "$status" = 200 ]; then
    # A reconnect owns only spec.source. In particular, never echo a GET body
    # back to Kubernetes because it contains status, finalizers, and fields
    # managed by the Operator.
    jq -n --argjson source "$source_json" '{spec: {source: $source}}' >"$request_file"
    update_attempt=0
    while :; do
        if ! jq -e --arg streamKey "$stream_key" \
            '.spec.streamKey == $streamKey' "$response_file" >/dev/null; then
            log "derived resource name collision for stream '$stream_key'; rejecting publication"
            exit 1
        fi
        status=$(kubernetes_api_request PATCH "${LIVESTREAMS_API_PATH}/${resource_name}" \
            "$response_file" "$request_file" 'application/merge-patch+json')
        [ "$status" = 409 ] || break

        update_attempt=$((update_attempt + 1))
        [ "$update_attempt" -lt "${SOURCE_UPDATE_ATTEMPTS:-3}" ] || break
        status=$(kubernetes_api_request GET "${LIVESTREAMS_API_PATH}/${resource_name}" \
            "$response_file")
        [ "$status" = 200 ] || break
    done
    [ "$status" = 200 ] || {
        log "Kubernetes rejected LiveStream source update (HTTP $status)"
        exit 1
    }
fi

# The session is durable locally only after Kubernetes accepted the desired source.
state_tmp="${state_file}.tmp.$$"
jq -n \
    --arg streamKey "$stream_key" \
    --arg resourceName "$resource_name" \
    --arg localConnectionId "$publication_id" \
    --arg sessionId "$session_id" \
    '{streamKey: $streamKey, sessionId: $sessionId,
      localConnectionId: $localConnectionId, resourceName: $resourceName}' >"$state_tmp"
publication_state_commit "$state_tmp" "$state_file"
log "registered stream '$stream_key' with session '$session_id'"
