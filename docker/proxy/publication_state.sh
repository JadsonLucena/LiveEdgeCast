#!/bin/sh

publication_state_dir() {
    printf '%s\n' "${PUBLICATION_STATE_DIR:-/run/liveedgecast/sessions}"
}

publication_state_file() (
    state_dir=$1
    stream_key=$2
    connection_id=$3
    state_key=$(printf '%s\n%s' "$stream_key" "$connection_id" | sha256sum | cut -d ' ' -f 1)
    printf '%s/%s.json\n' "$state_dir" "$state_key"
)

publication_state_commit() (
    temporary_file=$1
    state_file=$2

    flock --close "${state_file}.lock" mv "$temporary_file" "$state_file"
)

publication_state_remove_if_session() (
    state_file=$1
    expected_session_id=$2
    retry_file=$3

    flock --close "${state_file}.lock" sh -c '
        current_session_id=$(jq -er ".sessionId" "$1" 2>/dev/null || true)
        if [ "$current_session_id" = "$2" ]; then
            rm -f "$1"
        fi
        rm -f "$3"
    ' sh "$state_file" "$expected_session_id" "$retry_file"
)
