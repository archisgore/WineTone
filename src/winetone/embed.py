"""Phase 3 — wine embedding generation.

We produce one 384-dimensional vector per canonical wine via
`BAAI/bge-small-en-v1.5` running on ONNX Runtime. The execution
provider is **auto-detected** to use the fastest accelerator
available on the host:

  1. **CoreMLExecutionProvider** — macOS only. Uses Apple's Neural
     Engine + Metal GPU. Available out of the box with onnxruntime
     on darwin/arm64; no extra install needed.
  2. **CUDAExecutionProvider** — Linux + NVIDIA. Requires the
     `onnxruntime-gpu` package (which is mutually exclusive with
     plain `onnxruntime`). Expected ~10–30× faster than CPU for
     transformer inference.
  3. **DmlExecutionProvider** — Windows DirectML. GPU on any
     DirectX12-capable adapter (NVIDIA, AMD, Intel Arc).
  4. **CPUExecutionProvider** — universal fallback, always
     available.

Override the auto-detect via `--providers` on the CLI or
`provider=` argument to `build()`.

Empirical note on CoreML
------------------------
For this specific encoder (`bge-small-en-v1.5` INT8-quantized via
fastembed's `bge-small-en-v1.5-onnx-Q`), CoreML and CPU benchmark
within ~0% of each other on M-series Macs. The expected GPU speedup
doesn't materialize because:

  - fastembed loads the INT8-quantized variant. Apple Silicon's
    CPU has dedicated AMX matrix extensions that already run INT8
    efficiently.
  - CoreML's graph-partitioning overhead (deciding which ops go to
    ANE / GPU / CPU + tensor copies) cancels per-op gains.
  - bge-small is small enough (33M params) that the CPU isn't the
    bottleneck.

The auto-detect logic is still correct in spirit — for larger
models (bge-large, etc.) or different providers (CUDA on
Linux+NVIDIA), the speedup is real. We document this here so the
next person doesn't waste a day expecting magic.

Why fastembed (vs. sentence-transformers + torch): pure-ONNX
runtime keeps the install small (~50MB vs ~800MB for torch CPU
wheels) and the execution-provider mechanism gives us
platform-native GPU access without writing platform-specific
code paths.
"""

from __future__ import annotations

import logging
import platform

import numpy as np
import pandas as pd
from fastembed import TextEmbedding
from sqlalchemy import text

from winetone import db

log = logging.getLogger(__name__)

# bge-small-en-v1.5 is a 33M-param sentence encoder, 384-dim output.
# Smaller models (e.g. all-MiniLM-L6-v2) are equally fastembed-supported
# but bge-small has better retrieval quality on short text.
import os as _os  # avoid clash with module-level "os" import order quirks

# Default to our wine-corpus fine-tune. The original base model (BAAI/
# bge-small-en-v1.5) is still loadable via the env override — useful
# for A/B comparing the fine-tune against baseline.
MODEL_NAME = _os.environ.get("WINETONE_ENCODER", "archisgore/bge-small-winetone")
EMBEDDING_DIM = 384

# Preference order for ONNX Runtime execution providers. We pick the
# first one available on the host. `CPUExecutionProvider` is always
# the final fallback (ORT requires it as a last resort anyway).
_PROVIDER_PRIORITY = (
    "CoreMLExecutionProvider",   # Apple Silicon, Neural Engine + Metal
    "CUDAExecutionProvider",     # NVIDIA, needs onnxruntime-gpu
    "DmlExecutionProvider",      # Windows DirectML
    "CPUExecutionProvider",      # always last resort
)


def detect_providers() -> list[str]:
    """Return ONNX Runtime providers in fastembed-preferred order.

    Inspects what's actually compiled into the installed onnxruntime
    package (via `onnxruntime.get_available_providers()`) and orders
    them by `_PROVIDER_PRIORITY`. The resulting list is what we pass
    to `fastembed.TextEmbedding(providers=...)`.

    CoreML EP is available on darwin/arm64 with the default
    onnxruntime install — no separate package required. CUDA EP
    requires `pip install onnxruntime-gpu` (which conflicts with
    plain `onnxruntime`).
    """
    try:
        import onnxruntime as ort
    except ImportError:
        return ["CPUExecutionProvider"]

    available = set(ort.get_available_providers())
    chosen = [p for p in _PROVIDER_PRIORITY if p in available]
    if "CPUExecutionProvider" not in chosen:
        chosen.append("CPUExecutionProvider")  # always include as fallback
    return chosen


def describe_providers(providers: list[str]) -> str:
    """Human-readable summary of a provider list."""
    pretty = {
        "CoreMLExecutionProvider": "CoreML (Apple Neural Engine + Metal GPU)",
        "CUDAExecutionProvider": "CUDA (NVIDIA GPU)",
        "DmlExecutionProvider": "DirectML (Windows GPU)",
        "CPUExecutionProvider": "CPU",
    }
    parts = [pretty.get(p, p) for p in providers]
    return " → ".join(parts)


def encoder_hints() -> dict[str, object]:
    """Diagnostic info for `winetone embed-backend` and similar."""
    providers = detect_providers()
    return {
        "platform": f"{platform.system()}/{platform.machine()}",
        "model": MODEL_NAME,
        "providers": providers,
        "providers_summary": describe_providers(providers),
    }

# We chunk the text encoder pass over wines so we don't hold all
# 164k wines + their reviews in memory simultaneously.
BATCH_SIZE = 256


# bge-small-en-v1.5 has a 512-token context, ~2,000 chars at typical
# English density. Bound the input here so a wine with 10+ concatenated
# reviews doesn't pay tokenizer cost for content the model will truncate
# anyway.
MAX_TEXT_CHARS = 2_000


def _build_embedding_text(row: pd.Series) -> str:
    """Compose the text we feed the encoder for one wine.

    Includes the structured prefix (variety, region, country) +
    the aggregated review text. The encoder learns to treat that
    prefix as semantic context.
    """
    parts: list[str] = []
    if row.get("variety"):
        parts.append(f"variety: {row['variety']}.")
    if row.get("region"):
        parts.append(f"region: {row['region']}.")
    if row.get("country"):
        parts.append(f"country: {row['country']}.")
    if row.get("vintage") and pd.notna(row["vintage"]):
        parts.append(f"vintage: {int(row['vintage'])}.")
    if row.get("review_text_all"):
        parts.append(str(row["review_text_all"]))
    text = " ".join(parts)
    return text[:MAX_TEXT_CHARS]


def _load_wines_for_embedding(sample: int | None = None) -> pd.DataFrame:
    """Load wine_features and project to the embedding input.

    If `sample` is given, return a deterministic random subsample of
    that many wines (with priority for wines with more reviews — these
    are the high-signal entries). Useful when full-corpus encoding is
    too slow on CPU.
    """
    if not db.ping():
        raise RuntimeError(
            "CedarDB unreachable — run `make db-up-bg`"
        )
    df = pd.read_sql(
        """
        SELECT wine_id, variety, region, country, vintage,
               review_text_all, n_reviews
        FROM wine_features
        WHERE review_text_all IS NOT NULL AND length(review_text_all) > 0
        """,
        db.engine(),
    )
    log.info("loaded %d wines with non-empty review text", len(df))
    if sample is not None and sample < len(df):
        # Stratify: take all wines with ≥2 reviews (rare, high-signal)
        # plus a random sample of the rest to fill the sample budget.
        multi = df[df["n_reviews"].fillna(0) >= 2]
        rest = df[df["n_reviews"].fillna(0) < 2]
        if len(multi) >= sample:
            df = multi.sample(n=sample, random_state=42)
        else:
            df = pd.concat(
                [multi, rest.sample(n=sample - len(multi), random_state=42)]
            )
        log.info(
            "sampled to %d wines (%d multi-review + remainder)",
            len(df), int((df["n_reviews"].fillna(0) >= 2).sum())
        )
    return df


def _embed_texts(texts: list[str], model: TextEmbedding) -> np.ndarray:
    """Run the encoder over a list of strings, return a (N, dim) array."""
    import time
    out = np.empty((len(texts), EMBEDDING_DIM), dtype=np.float32)
    # fastembed.embed() yields one (dim,) array per input. Report
    # progress every 5000 texts so a long encode doesn't go dark.
    report_every = 5000
    t0 = time.monotonic()
    last_report_t = t0
    for i, vec in enumerate(model.embed(texts, batch_size=BATCH_SIZE)):
        out[i] = vec.astype(np.float32)
        if (i + 1) % report_every == 0 or (i + 1) == len(texts):
            now = time.monotonic()
            rate = report_every / (now - last_report_t) if i + 1 != report_every else (i + 1) / (now - t0)
            pct = (i + 1) / len(texts) * 100
            eta_s = (len(texts) - (i + 1)) / max(rate, 1e-6)
            log.info(
                "encoded %d / %d (%.1f%%) · %.1f docs/sec · eta=%.0fs",
                i + 1, len(texts), pct, rate, eta_s
            )
            last_report_t = now
    # L2-normalize for cosine similarity to be a dot product downstream.
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return out / norms


def _persist_embeddings(wine_ids: list[str], vectors: np.ndarray) -> None:
    """Write embeddings to CedarDB.

    CedarDB supports pgvector-style `vector(N)` columns directly.
    We serialize the vector as the pgvector text literal
    `[v0,v1,...,vN-1]` for the bulk insert.
    """
    log.info("writing %d embeddings to CedarDB", len(wine_ids))
    # pgvector literal format.
    vec_strs = ["[" + ",".join(f"{v:.6f}" for v in row) + "]" for row in vectors]

    df = pd.DataFrame({"wine_id": wine_ids, "embedding": vec_strs})

    # Recreate the table fresh each build for PoC reproducibility.
    with db.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS wine_embeddings"))
        conn.execute(
            text(
                f"""
                CREATE TABLE wine_embeddings (
                    wine_id     TEXT PRIMARY KEY,
                    embedding   vector({EMBEDDING_DIM}) NOT NULL,
                    embedding_model TEXT NOT NULL
                )
                """
            )
        )

    # Bulk insert via SQLAlchemy executemany. The pgvector cast happens
    # via the table's column type; we pass the literal as TEXT and let
    # CedarDB cast.
    chunk = 5000
    for start in range(0, len(df), chunk):
        end = min(start + chunk, len(df))
        rows = df.iloc[start:end]
        params = [
            {"wine_id": w, "embedding": v, "embedding_model": MODEL_NAME}
            for w, v in zip(rows["wine_id"], rows["embedding"], strict=False)
        ]
        with db.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO wine_embeddings (wine_id, embedding, embedding_model) "
                    f"VALUES (:wine_id, CAST(:embedding AS vector({EMBEDDING_DIM})), :embedding_model)"
                ),
                params,
            )
        log.info("  wrote %d / %d", end, len(df))


def build(
    sample: int | None = None,
    providers: list[str] | None = None,
) -> dict[str, int]:
    """Run the full Phase 3 embedding pipeline end-to-end.

    Args:
        sample: if set, encode only this many wines (stratified to
                prefer multi-review entries). Useful on slow CPUs where
                full-corpus encode would take hours.
        providers: ONNX Runtime execution providers to pass to
                fastembed. If None, detect_providers() picks the best
                available — CoreML on Mac, CUDA on Linux+NVIDIA,
                DirectML on Windows, CPU as fallback.
    """
    if not db.ping():
        raise RuntimeError("CedarDB unreachable — run `make db-up-bg`")

    wines = _load_wines_for_embedding(sample=sample)
    if wines.empty:
        raise RuntimeError(
            "No wines with review text — run `winetone build canonical` first."
        )

    log.info("composing embedding texts")
    wines["__text__"] = wines.apply(_build_embedding_text, axis=1)

    if providers is None:
        providers = detect_providers()
    log.info(
        "loading encoder: %s (providers: %s)",
        MODEL_NAME, describe_providers(providers),
    )
    model = TextEmbedding(model_name=MODEL_NAME, providers=providers)

    log.info("encoding %d texts (batch=%d)", len(wines), BATCH_SIZE)
    vectors = _embed_texts(wines["__text__"].tolist(), model)

    _persist_embeddings(wines["wine_id"].tolist(), vectors)

    return {
        "n_wines": len(wines),
        "dim": EMBEDDING_DIM,
        "providers": providers,
    }


def load_embeddings() -> tuple[list[str], np.ndarray]:
    """Read all embeddings back as (wine_ids, vectors) for in-Python search."""
    if not db.ping():
        raise RuntimeError("CedarDB unreachable")
    df = pd.read_sql(
        "SELECT wine_id, embedding FROM wine_embeddings", db.engine()
    )
    if df.empty:
        return [], np.empty((0, EMBEDDING_DIM), dtype=np.float32)

    # CedarDB returns pgvector cells as a string literal "[v0,v1,...]"
    # or as a list. Coerce in both directions.
    def _parse(v: object) -> np.ndarray:
        if isinstance(v, list):
            return np.asarray(v, dtype=np.float32)
        s = str(v).strip("[]")
        return np.fromstring(s, sep=",", dtype=np.float32)

    vectors = np.vstack(df["embedding"].map(_parse).to_list())
    return df["wine_id"].tolist(), vectors


# Lazy query-time encoder. Construct once per process; subsequent
# queries reuse the loaded model (~30ms per encode instead of ~2s
# per encode-after-rebuild). The recommender hits this on every
# user query so it matters.
#
# We hold the encoder as Any here — the actual type depends on which
# backend ended up loading. sentence-transformers if available (default
# path with the fine-tuned model), fastembed as a legacy fallback if
# someone explicitly opts in via WINETONE_USE_FASTEMBED=1.
_QUERY_ENCODER: object | None = None


def _load_query_encoder() -> object:
    """Load the encoder once, on the best available device.

    sentence-transformers can load any HF Hub model by name — including
    our `archisgore/bge-small-winetone` fine-tune — and runs on MPS /
    CUDA / CPU automatically. We replaced fastembed because fastembed's
    custom-ONNX loading is fiddly enough that switching to ST is the
    cleaner deploy story for a single fine-tuned model.
    """
    from sentence_transformers import SentenceTransformer

    try:
        import torch
        if torch.cuda.is_available():
            device = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
    except ImportError:
        device = "cpu"

    log.info("loading query encoder: %s on %s", MODEL_NAME, device)
    return SentenceTransformer(MODEL_NAME, device=device)


def encode_query(query: str) -> np.ndarray:
    """Encode an arbitrary string into the embedding space.

    Used at recommend time. Model is cached at module scope so we don't
    pay the ~2s load cost per query. Returns an L2-normalized 384-dim
    float32 vector — same contract as before the sentence-transformers
    swap, so downstream code (cosine via dot product) is unchanged.
    """
    global _QUERY_ENCODER
    if _QUERY_ENCODER is None:
        _QUERY_ENCODER = _load_query_encoder()
    vec = _QUERY_ENCODER.encode(
        [query],
        batch_size=1,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )[0]
    return vec.astype(np.float32)
