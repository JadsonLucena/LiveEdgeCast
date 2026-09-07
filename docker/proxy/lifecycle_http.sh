#!/bin/sh
set -u

content_length=0
while IFS= read -r header; do
    header=$(printf '%s' "$header" | tr -d '\r')
    [ -n "$header" ] || break
    case "$header" in
        [Cc]ontent-[Ll]ength:*) content_length=$(printf '%s' "${header#*:}" | tr -d ' ') ;;
    esac
done

case "$content_length" in ''|*[!0-9]*) content_length=0 ;; esac
[ "$content_length" -le 8192 ] || content_length=0
body=$(dd bs=1 count="$content_length" 2>/dev/null)

field() {
    printf '%s' "$body" | tr '&' '\n' | sed -n "s/^$1=//p" | head -n 1
}

call=$(field call)
name=$(field name)
app=$(field app)
addr=$(field addr)
clientid=$(field clientid)

status='200 OK'
if [ "$call" = publish ]; then
    if ! /scripts/publication_started.sh "$name" "$app" "$addr" "$clientid"; then
        status='403 Forbidden'
    fi
elif [ "$call" = publish_done ]; then
    while ! /scripts/publication_ended.sh "$name" "$app" "$addr" "$clientid"; do
        sleep "${TERMINATION_RETRY_SECONDS:-2}"
    done
else
    status='400 Bad Request'
fi

printf 'HTTP/1.1 %s\r\nContent-Length: 0\r\nConnection: close\r\n\r\n' "$status"
