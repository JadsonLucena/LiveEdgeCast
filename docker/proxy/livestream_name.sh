#!/bin/sh

# Derive a stable Kubernetes DNS label from an arbitrary stream key.
#
# Keys that already are valid DNS labels are returned unchanged. Other keys are
# lower-cased, every run of non [a-z0-9-] bytes is replaced by one hyphen, and
# leading/trailing hyphens are removed. A 12-hex SHA-256 suffix of the original
# key is then appended, so keys that have the same normalized text remain
# distinct. The readable prefix is truncated as needed to keep the result at
# the 63-byte DNS-label limit.
livestream_resource_name() (
    stream_key=$1
    hash=$(printf %s "$stream_key" | sha256sum | cut -c 1-12)
    normalized=$(printf %s "$stream_key" |
        LC_ALL=C tr '[:upper:]' '[:lower:]' |
        LC_ALL=C sed 's/[^a-z0-9-][^a-z0-9-]*/-/g; s/^-*//; s/-*$//')

    case "$stream_key" in
        [a-z0-9]* )
            if [ "${#stream_key}" -le 63 ] &&
                ! printf %s "$stream_key" | LC_ALL=C grep -q '[^a-z0-9-]' &&
                [ "${stream_key%?}" != "${stream_key%-}" ]; then
                printf '%s\n' "$stream_key"
                exit 0
            fi
            ;;
    esac

    [ -n "$normalized" ] || normalized=stream
    prefix=$(printf %.50s "$normalized" | sed 's/-*$//')
    [ -n "$prefix" ] || prefix=stream
    printf '%s-%s\n' "$prefix" "$hash"
)
