"""Phase 5 — KMeans clustering over the wine embedding space.

For visualization / exploration: which "neighborhoods" exist in
the wine embedding space? Each cluster gets a centroid + a sample
of representative wines + a most-common-variety / -country
summary.

This is exploratory analysis, not a recommendation primitive. The
recommender (see `recommend.py`) uses raw embedding nearest-neighbor
search; the clusters are for humans to look at.
"""

from __future__ import annotations

import logging
from collections import Counter

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sqlalchemy import text

from winetone import db, embed

log = logging.getLogger(__name__)

DEFAULT_K = 16


def build(k: int = DEFAULT_K) -> dict[str, object]:
    """Run KMeans on the wine embeddings and persist cluster assignments.

    Writes `cluster_id` onto a `wine_clusters` table in CedarDB. The
    centroid vectors are stored too so we can label new wines /
    user queries with the nearest cluster at query time.
    """
    if not db.ping():
        raise RuntimeError("CedarDB unreachable — run `make db-up-bg`")

    log.info("loading embeddings")
    wine_ids, vectors = embed.load_embeddings()
    if len(wine_ids) == 0:
        raise RuntimeError(
            "No embeddings — run `winetone build embeddings` first."
        )

    log.info("fitting KMeans(k=%d) on %d vectors", k, len(vectors))
    km = KMeans(n_clusters=k, n_init=8, random_state=42)
    labels = km.fit_predict(vectors)
    centroids = km.cluster_centers_.astype(np.float32)
    # Normalize centroids so dot-product == cosine for downstream queries.
    norms = np.linalg.norm(centroids, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    centroids = centroids / norms

    log.info("persisting cluster assignments")
    cluster_df = pd.DataFrame(
        {"wine_id": wine_ids, "cluster_id": labels.astype(np.int32)}
    )

    with db.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS wine_clusters"))
        conn.execute(text("DROP TABLE IF EXISTS wine_cluster_centroids"))
    cluster_df.to_sql(
        "wine_clusters", db.engine(), index=False, if_exists="replace"
    )

    # Persist centroids as pgvector. Same format as wine_embeddings.
    with db.connect() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE wine_cluster_centroids (
                    cluster_id INTEGER PRIMARY KEY,
                    centroid vector({embed.EMBEDDING_DIM}) NOT NULL,
                    n_wines INTEGER NOT NULL
                )
                """
            )
        )
        params = []
        for cid in range(k):
            vec = centroids[cid]
            vec_str = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
            params.append(
                {
                    "cluster_id": int(cid),
                    "centroid": vec_str,
                    "n_wines": int((labels == cid).sum()),
                }
            )
        conn.execute(
            text(
                f"INSERT INTO wine_cluster_centroids (cluster_id, centroid, n_wines) "
                f"VALUES (:cluster_id, CAST(:centroid AS vector({embed.EMBEDDING_DIM})), :n_wines)"
            ),
            params,
        )

    return {"n_clusters": k, "n_wines": len(wine_ids)}


def summarize(k: int = DEFAULT_K, top_n_examples: int = 5) -> pd.DataFrame:
    """Return a human-readable summary of each cluster."""
    if not db.ping():
        raise RuntimeError("CedarDB unreachable")

    # Join wines + cluster assignments. Limit to fields we'll display.
    df = pd.read_sql(
        """
        SELECT w.wine_id, w.producer_display, w.wine_display,
               w.vintage, w.variety, w.country, w.region,
               c.cluster_id
        FROM wines w
        JOIN wine_clusters c ON w.wine_id = c.wine_id
        """,
        db.engine(),
    )

    rows = []
    for cid, sub in df.groupby("cluster_id"):
        top_variety = (
            Counter(sub["variety"].dropna()).most_common(3)
        )
        top_country = (
            Counter(sub["country"].dropna()).most_common(3)
        )
        # Pick a few examples — wines with the most reviews (proxy for "famous").
        examples = sub.head(top_n_examples)[
            ["producer_display", "wine_display", "vintage"]
        ].to_dict("records")
        rows.append(
            {
                "cluster_id": int(cid),
                "n_wines": len(sub),
                "top_varieties": ", ".join(f"{v} ({n})" for v, n in top_variety),
                "top_countries": ", ".join(f"{c} ({n})" for c, n in top_country),
                "examples": "; ".join(
                    f"{e['producer_display']} {e['wine_display']} {e['vintage']}"
                    for e in examples
                ),
            }
        )
    return pd.DataFrame(rows).sort_values("n_wines", ascending=False)
