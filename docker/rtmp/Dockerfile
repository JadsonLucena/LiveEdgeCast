FROM alpine:3.18

# Versões
ENV NGINX_VERSION=1.24.0 \
    NGINX_RTMP_MODULE_VERSION=1.2.2

# Instalar dependências de compilação e runtime
RUN apk add --no-cache \
    # Build dependencies
    build-base \
    ca-certificates \
    openssl-dev \
    pcre-dev \
    zlib-dev \
    linux-headers \
    # Runtime dependencies
    openssl \
    pcre \
    zlib \
    ffmpeg \
    curl \
    jq \
    wget \
    bash \
    && mkdir -p /tmp/build

# Download e compilar NGINX com módulo RTMP
RUN cd /tmp/build \
    && wget -q https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz \
    && wget -q https://github.com/arut/nginx-rtmp-module/archive/v${NGINX_RTMP_MODULE_VERSION}.tar.gz -O nginx-rtmp-module.tar.gz \
    && tar -xzf nginx-${NGINX_VERSION}.tar.gz \
    && tar -xzf nginx-rtmp-module.tar.gz \
    && cd nginx-${NGINX_VERSION} \
    && ./configure \
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
    && make install \
    && ln -s /usr/local/nginx/sbin/nginx /usr/sbin/nginx \
    && rm -rf /tmp/build \
    && apk del build-base openssl-dev pcre-dev zlib-dev linux-headers

# Criar diretórios necessários
RUN mkdir -p /var/log/nginx /scripts /tmp/ffmpeg_pids \
    && chmod 755 /scripts

# Copiar scripts de publicação
COPY on_publish_start.sh /scripts/on_publish_start.sh
COPY on_publish_done.sh /scripts/on_publish_done.sh
COPY on_worker_publish.sh /scripts/on_worker_publish.sh

# Garantir permissões de execução
RUN chmod +x /scripts/*.sh

EXPOSE 1935 8080

STOPSIGNAL SIGQUIT

CMD ["nginx", "-g", "daemon off;"]