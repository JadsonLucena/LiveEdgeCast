FROM alfg/nginx-rtmp:latest

RUN rm -rf /etc/nginx/conf.d/*

COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 1935 8080

CMD ["nginx", "-g", "daemon off;"]
