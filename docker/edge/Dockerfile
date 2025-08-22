FROM alfg/nginx-rtmp:latest

RUN rm -rf /etc/nginx/conf.d/*

COPY docker/edge/nginx.conf /etc/nginx/nginx.conf

RUN mkdir -p /var/www/nginx

EXPOSE 1935 8080

CMD ["nginx", "-g", "daemon off;"]
