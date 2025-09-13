FROM alfg/nginx-rtmp:latest

RUN rm -rf /etc/nginx/conf.d/*

COPY docker/nginx.conf /etc/nginx/nginx.conf

EXPOSE 1935 8080

ENTRYPOINT ["sh", "-c", "sed -i 's|\\${RTMP_PUSH_URL}|'\"$RTMP_PUSH_URL\"'|g' /etc/nginx/nginx.conf && exec nginx -g 'daemon off;'"]