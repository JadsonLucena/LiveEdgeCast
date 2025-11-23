FROM alfg/nginx-rtmp:latest

# Instalar dependências: ffmpeg para re-streaming, curl/jq/wget/bash para scripts
RUN apk add --no-cache ffmpeg curl jq wget bash

# Criar diretório para scripts
RUN mkdir -p /scripts && chmod 755 /scripts

# Copiar scripts de publicação (multi-stream via ffmpeg)
COPY on_publish_start.sh /scripts/on_publish_start.sh
COPY on_publish_done.sh /scripts/on_publish_done.sh
COPY on_worker_publish.sh /scripts/on_worker_publish.sh

# Garantir permissões de execução
RUN chmod +x /scripts/on_publish_start.sh /scripts/on_publish_done.sh /scripts/on_worker_publish.sh

EXPOSE 1935 8080

ENTRYPOINT ["nginx", "-g", "daemon off;"]