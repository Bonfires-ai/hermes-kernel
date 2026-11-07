# syntax=docker/dockerfile:1.6
#
# The turn service (kernel-turns). Build context: service/
#
#   docker build service -t hermes-kernel-turns:latest
#
# It never imports memory_kernel: it calls `mk serve` over HTTP, so no private package or token is needed to build it.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

# Keep in step with the dependencies in pyproject.toml.
RUN pip install "fastapi>=0.115.0" "uvicorn>=0.30.0" "httpx>=0.27.0" "pydantic>=2.13.0" \
    "pydantic-settings>=2.6.0" "pymongo>=4.13"

RUN useradd --create-home --uid 10002 kturns && mkdir -p /app
COPY src /app/src

USER kturns
WORKDIR /app
EXPOSE 8000
CMD ["python", "-m", "kernel_turns"]
