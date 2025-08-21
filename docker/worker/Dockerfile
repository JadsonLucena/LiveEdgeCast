FROM alfg/nginx-rtmp:latest

RUN rm -rf /etc/nginx/conf.d/*

COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 1935 8080

ENTRYPOINT sed -i "s|\${RTMP_PUSH_URL}|$RTMP_PUSH_URL|g" /etc/nginx/nginx.conf && exec "$@"
CMD ["nginx", "-g", "daemon off;"]
