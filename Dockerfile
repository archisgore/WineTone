# WineTone production image — built by .github/workflows/build-image.yml
# and pushed to ghcr.io/archisgore/winetone. The HF Spaces (prod + staging)
# pull this image via their own minimal Dockerfile (FROM ghcr.io/...), so
# none of this source code ends up in the public Space repo.
#
# Build context expects two things alongside the source tree:
#   ./data/canonical/sparse/      — TF-IDF joblibs, downloaded by the
#                                   workflow from the latest release
#                                   tarball before `docker build` runs.
# The encoder model is pre-fetched into the image at build time so the
# first /recommend doesn't stall on a 5-second model download.

FROM python:3.12-slim-bookworm

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
