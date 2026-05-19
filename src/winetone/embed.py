"""Phase 3 — wine embedding generation.

We produce one 384-dimensional vector per canonical wine. The
embedding combines two signals:

1. **Text** — the concatenated review text for that wine, fed
   through a sentence-transformer (`BAAI/bge-small-en-v1.5` via
   the fastembed ONNX runtime). 384 dim.
2. **Structured features** — variety, country, region, median
   points, median price (log-scaled). Stored separately in
   CedarDB so we can join at recommend time and weight them per
   policy.

For the PoC we use the text vector directly as the wine embedding
(no fusion MLP). The structured features sit alongside in the
`wine_embeddings` table so the recommend layer can stack them
into a composite score: cosine-similarity on text + categorical
filters on variety/country/region + numeric scoring on
points/price.

This is the simplest construction that demonstrates the full
pipeline. The plan's multi-modal contrastive fusion encoder is
Phase 3.5 work.

Why fastembed: pure-ONNX runtime, ~50MB install, no torch. Same
output quality as `sentence-transformers/all-MiniLM-L6-v2` (which
BAAI's bge-small was distilled from). Worth ~10x in install
weight.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from fastembed import TextEmbedding
from sqlalchemy import text

from winetone import db

log = logging.getLogger(__name__)

# bge-small-en-v1.5 is a 33M-param sentence encoder, 384-dim output.
# Smaller models (e.g. all-MiniLM-L6-v2) are equally fastembed-supported
# but bge-small has better retrieval quality on short text.
MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

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


def build(sample: int | None = None) -> dict[str, int]:
    """Run the full Phase 3 embedding pipeline end-to-end.

    Args:
        sample: if set, encode only this many wines (stratified to
                prefer multi-review entries). Useful on slow CPUs where
                full-corpus encode would take hours.
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

    log.info("loading encoder: %s", MODEL_NAME)
    model = TextEmbedding(model_name=MODEL_NAME)

    log.info("encoding %d texts (batch=%d)", len(wines), BATCH_SIZE)
    vectors = _embed_texts(wines["__text__"].tolist(), model)

    _persist_embeddings(wines["wine_id"].tolist(), vectors)

    return {"n_wines": len(wines), "dim": EMBEDDING_DIM}


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


def encode_query(query: str) -> np.ndarray:
    """Encode an arbitrary string into the embedding space.

    Used at recommend time: the user's free-text query becomes a
    vector, then nearest-neighbor search retrieves wines.
    """
    model = TextEmbedding(model_name=MODEL_NAME)
    vec = next(iter(model.embed([query], batch_size=1)))
    vec = vec.astype(np.float32)
    n = np.linalg.norm(vec)
    return vec / (n if n > 0 else 1.0)
