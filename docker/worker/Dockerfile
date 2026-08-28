FROM alpine:3.21

RUN apk add --no-cache \
    ca-certificates \
    ffmpeg \
    bash

COPY worker_stream_runner.sh entrypoint.sh /scripts/
RUN chmod 755 /scripts/worker_stream_runner.sh /scripts/entrypoint.sh

ENTRYPOINT ["/scripts/entrypoint.sh"]
