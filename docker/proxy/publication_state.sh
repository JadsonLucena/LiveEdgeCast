#!/bin/sh

publication_state_dir() {
    printf '%s\n' "${PUBLICATION_STATE_DIR:-/run/liveedgecast/sessions}"
}

publication_state_file() {
    state_dir=$1
    stream_key=$2
    connection_id=$3
    state_key=$(printf '%s\n%s' "$stream_key" "$connection_id" | sha256sum | cut -d ' ' -f 1)
    printf '%s/%s.json\n' "$state_dir" "$state_key"
}

publication_state_lock() {
    lock_dir=$1
    while ! mkdir "$lock_dir" 2>/dev/null; do
        sleep 0.05
    done
}

publication_state_commit() (
    temporary_file=$1
    state_file=$2
    lock_dir="${state_file}.lock"

    publication_state_lock "$lock_dir"
    trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT HUP INT TERM
    mv "$temporary_file" "$state_file"
)

publication_state_remove_if_session() (
    state_file=$1
    expected_session_id=$2
    retry_file=$3
    lock_dir="${state_file}.lock"

    publication_state_lock "$lock_dir"
    trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT HUP INT TERM
    current_session_id=$(jq -er '.sessionId' "$state_file" 2>/dev/null || true)
    if [ "$current_session_id" = "$expected_session_id" ]; then
        rm -f "$state_file"
    fi
    rm -f "$retry_file"
)
