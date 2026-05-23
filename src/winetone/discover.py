"""Discover mode — query-less recommendations.

For a user with a fitted projection and at least one positive label,
surface the wines in the catalog that the user's palate centroid is
closest to *and* the user has not labelled. This is the "I have ten
minutes, show me something I'd love" loop — distinct from /ask
(query needed) and from /recommend (query also needed).

The math is a single k-NN sweep against the user's centroid:

  centroid = mean(W_i  for i in positive-labelled wines)         (in wine space)
  candidates = wines NOT in user_labels
  score(w)   = cosine(centroid, wine_embedding[w])
  return top-k

We project the centroid through the user's A·L+b only INDIRECTLY:
the user's positive labels are pairs (their description, the wine
they applied it to). Those labelled wines are already-projected
samples of what they value, so their centroid in wine space IS the
target. No separate query embedding to project.

Excluding already-labelled wines keeps the discovery genuinely
discovery-driven — no "here's the wine you literally just labelled"
results.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from sqlalchemy import text

from winetone import db, embed

log = logging.getLogger(__name__)


def discover_for(user_id: str, k: int = 30) -> pd.DataFrame:
    """Return up to k wines the user's palate centroid is most similar to,
    excluding any wine they've already labelled (positive OR negative).

    Returns columns: wine_id, producer_display, wine_display, vintage,
    variety, country, region, similarity. Sorted by similarity DESC.

    Returns an empty DataFrame if the user has no positive labels with
    embeddings — the caller should render an empty-state message.
    """
    centroid, n_used = _palate_centroid(user_id)
    if centroid is None:
        log.info("discover_for(%s): no centroid (no labels yet)", user_id)
        return pd.DataFrame(columns=[
            "wine_id", "producer_display", "wine_display", "vintage",
            "variety", "country", "region", "similarity",
        ])

    # Pull all candidate wine embeddings (excluding the user's labels).
    excluded_ids = _user_labelled_wine_ids(user_id)
    all_ids, all_vecs = embed.load_embeddings()

    if excluded_ids:
        # Mask out labelled wines so we don't recommend them back.
        mask = np.array([wid not in excluded_ids for wid in all_ids], dtype=bool)
        all_ids = [wid for wid, keep in zip(all_ids, mask, strict=False) if keep]
        all_vecs = all_vecs[mask]

    sims = all_vecs @ centroid  # all rows are already L2-normalized
    top_idx = np.argpartition(-sims, kth=min(k * 3, len(sims) - 1))[:k * 3]
    # Sort the top-k*3 candidates by similarity to get a stable top-k.
    top_sorted = sorted(top_idx, key=lambda i: -sims[i])[:k]
    top_ids = [all_ids[i] for i in top_sorted]
    top_scores = {all_ids[i]: float(sims[i]) for i in top_sorted}

    placeholders = ",".join(f"'{w}'" for w in top_ids)
    df = pd.read_sql(
        text(f"""
            SELECT wine_id, producer_display, wine_display, vintage,
                   variety, country, region
              FROM wines
             WHERE wine_id IN ({placeholders})
               AND producer_display IS NOT NULL
        """),
        db.engine(),
    )
    df["similarity"] = df["wine_id"].map(top_scores).astype(float)
    df = df.sort_values("similarity", ascending=False).reset_index(drop=True)
    log.info("discover_for(%s): %d candidates from %d labelled wines",
             user_id, len(df), n_used)
    return df


def _palate_centroid(user_id: str) -> tuple[np.ndarray | None, int]:
    """L2-normalized mean of the user's positive-labelled wines' embeddings."""
    rows = pd.read_sql(
        text("""
            SELECT we.embedding
              FROM user_labels ul
              JOIN wine_embeddings we ON we.wine_id = ul.wine_id
             WHERE ul.user_id = :u
               AND ul.sentiment = 'positive'
        """),
        db.engine(), params={"u": user_id},
    )
    if rows.empty:
        return None, 0
    vecs = []
    for raw in rows["embedding"]:
        if isinstance(raw, str):
            v = np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32)
        else:
            v = np.asarray(raw, dtype=np.float32)
        if v.shape[0] == embed.EMBEDDING_DIM:
            vecs.append(v)
    if not vecs:
        return None, 0
    centroid = np.mean(np.stack(vecs), axis=0)
    centroid /= (np.linalg.norm(centroid) + 1e-9)
    return centroid, len(vecs)


def _user_labelled_wine_ids(user_id: str) -> set[str]:
    """Wine IDs the user has labelled (any sentiment). Used to filter
    those out of discovery results."""
    rows = pd.read_sql(
        text("SELECT wine_id FROM user_labels WHERE user_id = :u"),
        db.engine(), params={"u": user_id},
    )
    return set(rows["wine_id"].tolist())
