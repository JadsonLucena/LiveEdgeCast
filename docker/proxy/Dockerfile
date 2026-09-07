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
    --with-threads \
    --with-file-aio \
    --add-module=../nginx-rtmp-module-${NGINX_RTMP_MODULE_VERSION} \
    && make -j$(nproc) \
    && make install

FROM alpine:3.21

ENV NGINX_VERSION=1.24.0

RUN apk add --no-cache \
    ca-certificates \
    curl \
    jq \
    libssl3 \
    pcre2 \
    socat \
    zlib

COPY --from=builder /usr/local/nginx /usr/local/nginx
RUN ln -s /usr/local/nginx/sbin/nginx /usr/sbin/nginx \
    && mkdir -p /var/log/nginx /scripts /run/liveedgecast/sessions \
    && chmod 700 /run/liveedgecast/sessions

COPY kubernetes_api.sh \
    publication_started.sh \
    publication_ended.sh \
    publication_state.sh \
    lifecycle_http.sh \
    entrypoint.sh \
    /scripts/
RUN chmod 755 /scripts/*.sh
COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 1935
STOPSIGNAL SIGQUIT
ENTRYPOINT ["/scripts/entrypoint.sh"]
