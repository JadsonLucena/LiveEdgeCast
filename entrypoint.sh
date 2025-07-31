#!/bin/sh

# Substitui a variável de ambiente no template
envsubst '$RTMP_PUSH_URL' < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

# Inicia o NGINX
exec "$@"
