# WineTone container image — a self-contained build of the web app from
# this checkout. The HF Spaces (prod + staging) do NOT use this file: each
# Space repo carries its own Dockerfile that clones this repo at a pinned
# ref and fetches the data release (see release/hf-space-dockerfile,
# gitignored). Keep their install steps in sync. (As of 2026-11 the Space
# Dockerfile is still on python:3.12-slim-bookworm; this image is on 3.14.)
#
# Build context expects the TF-IDF joblibs at ./data/canonical/sparse/
# (from the latest data release tarball). The encoder model is
# pre-fetched into the image at build time so the first /recommend
# doesn't stall on a 5-second model download.

FROM python:3.14-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/tmp/hf-cache

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

# HF Spaces run as user 1000 by convention; matching that here means the
# image runs identically locally (docker run …) and on the Space.
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

# Install the package first. Splitting this from the rest of the source
# means dependency changes get a cache hit while source-only changes
# rebuild only the COPY layers below.
COPY --chown=user:user pyproject.toml ./
COPY --chown=user:user src/ ./src/
COPY --chown=user:user www/ ./www/
COPY --chown=user:user migrations/ ./migrations/
COPY --chown=user:user alembic.ini ./
COPY --chown=user:user README.md ./

USER user
ENV PATH="/home/user/.local/bin:${PATH}"
RUN pip install --user -e .

# Pre-cache the ONNX encoder so the first request doesn't pay a model
# download latency. Same model the runtime resolves at request time.
RUN python -c \
    "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5'); print('encoder cached')"

# Sparse joblibs (TF-IDF matrix + vectorizer). The workflow extracts
# these from the latest data release tarball into ./data/canonical/sparse/
# in the build context BEFORE invoking docker build, so the COPY here
# is a plain file copy — no auth or network at image-build time.
COPY --chown=user:user data/canonical/sparse/ ./data/canonical/sparse/

EXPOSE 7860

# DATABASE_URL / WINETONE_DB_URL / Clerk / Anthropic / HF tokens come
# from the HF Space's runtime Secrets — never baked into the image.
CMD ["winetone", "serve", "--host", "0.0.0.0", "--port", "7860"]
