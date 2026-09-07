#!/bin/sh

# Shared, in-cluster Kubernetes API client for the publication lifecycle hooks.
# Authentication material is read exclusively from the projected ServiceAccount.

SERVICEACCOUNT_DIR="${KUBERNETES_SERVICEACCOUNT_DIR:-/var/run/secrets/kubernetes.io/serviceaccount}"
KUBERNETES_TOKEN_FILE="${SERVICEACCOUNT_DIR}/token"
KUBERNETES_CA_FILE="${SERVICEACCOUNT_DIR}/ca.crt"
KUBERNETES_NAMESPACE_FILE="${SERVICEACCOUNT_DIR}/namespace"

kubernetes_api_init() {
    [ -r "$KUBERNETES_TOKEN_FILE" ] || {
        echo "ServiceAccount token is not readable" >&2
        return 1
    }
    [ -r "$KUBERNETES_CA_FILE" ] || {
        echo "ServiceAccount CA is not readable" >&2
        return 1
    }
    [ -r "$KUBERNETES_NAMESPACE_FILE" ] || {
        echo "ServiceAccount namespace is not readable" >&2
        return 1
    }

    KUBERNETES_NAMESPACE=$(tr -d '\r\n' <"$KUBERNETES_NAMESPACE_FILE")
    [ -n "$KUBERNETES_NAMESPACE" ] || return 1
    kubernetes_host=${KUBERNETES_SERVICE_HOST:?KUBERNETES_SERVICE_HOST is required}
    case "$kubernetes_host" in
        *:*) kubernetes_host="[$kubernetes_host]" ;;
    esac
    KUBERNETES_API_URL="https://${kubernetes_host}:${KUBERNETES_SERVICE_PORT_HTTPS:-443}"
    LIVESTREAMS_API_PATH="/apis/liveedgecast.io/v1alpha1/namespaces/${KUBERNETES_NAMESPACE}/livestreams"
    SECRETS_API_PATH="/api/v1/namespaces/${KUBERNETES_NAMESPACE}/secrets"
}

# Writes the response body to the supplied file and prints only the HTTP status.
kubernetes_api_request() {
    method=$1
    path=$2
    output_file=$3
    body_file=${4:-}
    content_type=${5:-application/json}

    set -- \
        --silent --show-error \
        --connect-timeout "${KUBERNETES_API_CONNECT_TIMEOUT_SECONDS:-2}" \
        --max-time "${KUBERNETES_API_REQUEST_TIMEOUT_SECONDS:-5}" \
        --request "$method" \
        --cacert "$KUBERNETES_CA_FILE" \
        --header "Authorization: Bearer $(tr -d '\r\n' <"$KUBERNETES_TOKEN_FILE")" \
        --header "Accept: application/json" \
        --output "$output_file" \
        --write-out '%{http_code}'

    if [ -n "$body_file" ]; then
        set -- "$@" \
            --header "Content-Type: $content_type" \
            --data-binary "@$body_file"
    fi

    curl "$@" "${KUBERNETES_API_URL}${path}"
}
