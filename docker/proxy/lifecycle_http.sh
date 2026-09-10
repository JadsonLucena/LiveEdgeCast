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

form_decode() {
    # nginx sends an application/x-www-form-urlencoded notify body. Decode it
    # exactly once here so every lifecycle hook receives the original values.
    # In form encoding, '+' represents a space. Decode percent octets rather
    # than applying an URL encoder again; reject malformed and NUL escapes.
    encoded=$1
    while [ -n "$encoded" ]; do
        character=${encoded%"${encoded#?}"}
        encoded=${encoded#?}
        case "$character" in
            +) printf ' ' ;;
            %)
                octet=${encoded%"${encoded#??}"}
                case "$octet" in
                    [0-9a-fA-F][0-9a-fA-F]) ;;
                    *) return 1 ;;
                esac
                [ "$octet" != 00 ] || return 1
                decimal=$(printf '%d' "0x$octet") || return 1
                octal=$(printf '%03o' "$decimal") || return 1
                printf "\\$octal"
                encoded=${encoded#??}
                ;;
            *) printf '%s' "$character" ;;
        esac
    done
}

field() {
    encoded=$(printf '%s' "$body" | tr '&' '\n' | sed -n "s/^$1=//p" | head -n 1)
    form_decode "$encoded"
}

if ! call=$(field call) ||
    ! name=$(field name) ||
    ! app=$(field app) ||
    ! addr=$(field addr) ||
    ! clientid=$(field clientid); then
    printf 'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
    exit 0
fi

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
