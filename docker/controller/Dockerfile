FROM python:3.11-slim

# Prevents Python from buffering logs
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install required system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY main.py /app/

# Create non-root user
RUN useradd -m appuser
USER appuser

# Expose API port
EXPOSE 8000

# Start FastAPI using Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
