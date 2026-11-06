# The memory-kernel service image: mk serve, with the runtime data baked in at build time.
# The first layers match other memory-kernel images so Docker's build cache can reuse them.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

# git: the kernel is a git install. build-essential: a few transitive wheels (pyfcg, sentencepiece) may compile.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# The parser extra pulls torch; the default Linux wheel carries CUDA (gigabytes) and this service has no GPU.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN pip install --index-url "${TORCH_INDEX}" "torch>=2.5,<3"

# pymongo>=4.13: the kernel's `ops` extra asks only for >=4.
RUN pip install "arq>=0.26.0" "pydantic>=2.13.0" "pydantic-settings>=2.6.0" "pymongo>=4.13" httpx

# build: the FCG engine; parser: the syntactic parser; serve: fastapi/uvicorn; ops: pymongo for kernel_ops.
ARG MK_VERSION=2.0.0
RUN pip install "memory-kernel[build,parser,serve,ops] @ git+https://github.com/Bonfires-ai/memory-kernel@v${MK_VERSION}"

# Runtime data lives in the image, not on a volume. `mk setup --check` fails the build if anything is missing.
ENV MK_DATA_DIR=/opt/mk
RUN mk setup
RUN mk setup --component spacy-model-trf
RUN mk setup --check

RUN useradd --create-home --uid 10001 kernel \
    && chown -R kernel:kernel /opt/mk
USER kernel
WORKDIR /opt/mk
CMD ["mk", "serve", "--host", "0.0.0.0", "--port", "8000"]
