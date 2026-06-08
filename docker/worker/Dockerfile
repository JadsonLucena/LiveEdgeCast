FROM alpine:3.21 AS builder

ENV NGINX_VERSION=1.24.0 \
    NGINX_RTMP_MODULE_VERSION=1.2.2

RUN apk add --no-cache \
    build-base \
    ca-certificates \
    openssl-dev \
    pcre2-dev \
    zlib-dev \
    linux-headers \
    wget \
    tar

WORKDIR /tmp/build
RUN wget -q https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz \
    && wget -q https://github.com/arut/nginx-rtmp-module/archive/v${NGINX_RTMP_MODULE_VERSION}.tar.gz -O nginx-rtmp-module.tar.gz \
    && tar -xzf nginx-${NGINX_VERSION}.tar.gz \
    && tar -xzf nginx-rtmp-module.tar.gz

WORKDIR /tmp/build/nginx-${NGINX_VERSION}
RUN ./configure \
    --prefix=/usr/local/nginx \
    --conf-path=/etc/nginx/nginx.conf \
    --error-log-path=/var/log/nginx/error.log \
    --http-log-path=/var/log/nginx/access.log \
    --pid-path=/var/run/nginx.pid \
    --lock-path=/var/run/nginx.lock \
    --with-http_ssl_module \
    --with-http_stub_status_module \
    --with-threads \
    --with-file-aio \
    --add-module=../nginx-rtmp-module-${NGINX_RTMP_MODULE_VERSION} \
    && make -j$(nproc) \
    && make install

FROM alpine:3.21

ENV NGINX_VERSION=1.24.0

RUN apk add --no-cache \
    ca-certificates \
    openssl \
    pcre2 \
    zlib \
    ffmpeg \
    bash \
    python3 \
    py3-pip

COPY --from=builder /usr/local/nginx /usr/local/nginx
COPY --from=builder /tmp/build/nginx-${NGINX_VERSION}/conf/mime.types /etc/nginx/mime.types
RUN ln -s /usr/local/nginx/sbin/nginx /usr/sbin/nginx \
    && mkdir -p /var/log/nginx /scripts \
    && chmod 755 /scripts

COPY worker_stream_runner.sh /scripts/worker_stream_runner.sh
COPY entrypoint.sh /scripts/entrypoint.sh
COPY metrics_exporter.py /scripts/metrics_exporter.py
RUN pip3 install --no-cache-dir prometheus_client && chmod +x /scripts/*.sh /scripts/metrics_exporter.py
COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 1935 8080 9113
STOPSIGNAL SIGQUIT
ENTRYPOINT ["/scripts/entrypoint.sh"]
