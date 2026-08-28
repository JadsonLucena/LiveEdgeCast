FROM alpine:3.21

RUN apk add --no-cache \
    ca-certificates \
    ffmpeg \
    bash

COPY --chmod=755 worker_stream_runner.sh entrypoint.sh /scripts/

ENTRYPOINT ["/scripts/entrypoint.sh"]
