FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /tmp/build
COPY requirements.txt ./
RUN pip wheel --wheel-dir /tmp/wheels -r requirements.txt

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/kubectl

WORKDIR /app
COPY requirements.txt /app/
COPY --from=builder /tmp/wheels /tmp/wheels
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels -r requirements.txt \
    && rm -rf /tmp/wheels

COPY main.py /app/
RUN useradd -m appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
