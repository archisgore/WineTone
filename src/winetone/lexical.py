"""Postgres-native lexical search for the sparse channel.

Replaces the earlier joblib-on-disk TF-IDF setup (`embed_sparse.py`).
The old implementation built a scipy.sparse matrix once over the full
corpus and stored it as a joblib file. That couldn't grow — any wine
added after the joblib was written got dense-only scoring forever,
and on HF Spaces the ephemeral disk meant we couldn't even mutate
the file durably.

The new setup uses Postgres's full-text search:

  - Every row in `wines` has a `tsv tsvector` column with a GIN index.
  - `submit.submit_wine()` populates the tsv on INSERT, so new wines
    are lexically searchable the moment the INSERT commits.
  - The bulk pipeline (`canonicalize._persist_cedardb`) populates tsv
    on bulk import — same to_tsvector('english', ...) call.
  - At query time we score candidates with `ts_rank` against
    `plainto_tsquery`, then normalize to [0, 1] within the result set
    so the sparse channel is comparable in magnitude to the dense one.

The tradeoffs vs. the previous TF-IDF cosine setup:

  - Pro: incrementally updatable, persisted, no joblib loading at
    startup, no file-system dependency, the DB enforces consistency.
  - Pro: GIN index is fast — filtering 164K wines to matching ones
    happens in single-digit milliseconds.
  - Con: Postgres FTS is term-presence + tf-position + length-normed,
    not cosine over a full TF-IDF vocabulary. For queries dominated by
    common words ("red wine") it behaves a bit differently. For
    distinctive terms ("Nebbiolo," "Margaux") it matches the old
    behavior closely enough.
  - Con: At present, the tsv built from existing Neon rows uses ONLY
    display fields (producer / wine / variety / region / country) —
    we'd dropped `review_text_all` from Neon earlier to fit the free
    tier. The bulk pipeline going forward will fold review text in.
"""

from __future__ import annotations

import logging

import pandas as pd
from sqlalchemy import text

from winetone import db

log = logging.getLogger(__name__)


def score_candidates(
    query: str, limit: int = 200,
) -> dict[str, float]:
    """Return {wine_id: lexical_score in [0, 1]} for wines matching `query`.

    Empty dict when the query lexes to nothing or has no matches —
    callers should treat that as "the sparse channel contributes
    nothing this turn." Non-matching wines never appear, so the
    hybrid formula's missing-entry-is-zero logic does the right thing.
    """
    q = (query or "").strip()
    if not q:
        return {}

    sql = """
        SELECT wine_id,
               ts_rank(tsv, plainto_tsquery('english', :q)) AS rank
        FROM wines
        WHERE tsv @@ plainto_tsquery('english', :q)
        ORDER BY rank DESC
        LIMIT :n
    """
    try:
        df = pd.read_sql(
            text(sql), db.engine(), params={"q": q, "n": int(limit)},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("FTS query failed: %s", e)
        return {}

    if df.empty:
        return {}

    max_rank = float(df["rank"].max())
    if max_rank <= 0:
        return {}
    # Normalize within the result set so scores live in [0, 1] and are
    # comparable in magnitude to dense cosine.
    return dict(
        zip(
            df["wine_id"].astype(str),
            (df["rank"] / max_rank).astype(float).tolist(),
            strict=False,
        )
    )


def build_tsv_expression(
    producer: str = "",
    wine_name: str = "",
    variety: str = "",
    region: str = "",
    country: str = "",
    description: str = "",
) -> str:
    """The text that gets fed to `to_tsvector('english', ...)`.

    Centralised so submit.py and the bulk pipeline produce identical
    tokenisation. We deliberately repeat the producer-and-variety
    fields rather than rely on word boosting — those terms are the
    spine of any wine query, and seeing them twice in the tsv slightly
    increases their rank weight, which we want.
    """
    parts = [
        producer or "", wine_name or "",
        variety or "", region or "", country or "",
        description or "",
    ]
    return " ".join(p for p in parts if p).strip()
