FROM alfg/nginx-rtmp:latest

# Remove previous configuration
RUN rm -rf /etc/nginx/conf.d/*

# Copy our configuration
COPY docker/nginx.conf /etc/nginx/nginx.conf

EXPOSE 1935 8080

ENTRYPOINT ["nginx", "-g", "daemon off;"]