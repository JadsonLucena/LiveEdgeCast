#!/bin/sh

# Substitute environment variable in template
envsubst '$RTMP_PUSH_URL' < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

exec "$@"
