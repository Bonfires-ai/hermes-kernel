# The turn service (kernel-turns). Build context: the repo root (see compose.yaml).
#
# It never imports memory_kernel: it calls `mk serve` over HTTP, so no private package or token is needed to build it.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY service /tmp/service
RUN pip install /tmp/service && rm -rf /tmp/service

RUN useradd --create-home --home-dir /srv/kturns --uid 10002 kturns
USER kturns
WORKDIR /srv/kturns
EXPOSE 8000
CMD ["python", "-m", "kernel_turns"]
