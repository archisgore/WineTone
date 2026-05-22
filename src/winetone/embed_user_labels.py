"""Encode user-written tasting notes into the same 384-dim space as wines.

This is what powers "vocabulary search" — the feature where a visitor
can type a phrase and find wines that *some other user* has described
that way. "Sunshine in a bottle" finds the Tokaji bottles archis
labelled with that phrase. "Theatre popcorn butter" finds his
Louis Roederer. The corpus of personal vocabularies becomes a
searchable index in its own right.

Why a separate table from user_labels
-------------------------------------

`user_labels` is the source-of-truth, append-only history of every
labelling event. Its primary key is essentially the row identity
(no explicit id column).

`user_label_embeddings` is a derived index over it. We key by
`(user_id, wine_id, sha1(description))` so re-labelling the same
wine doesn't collide and re-running the backfill is idempotent.

We deliberately store the description text and embedding in the
same row even though description is also in user_labels — the
denormalisation lets vocab search return everything in one query
without joining back to user_labels.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

import pandas as pd
from sqlalchemy import text

from winetone import db, embed

log = logging.getLogger(__name__)

EMBEDDING_DIM = embed.EMBEDDING_DIM  # 384


def init_schema() -> None:
    """Create the user_label_embeddings table if absent."""
    autocommit = db.engine().execution_options(isolation_level="AUTOCOMMIT")
    existing = set(
        pd.read_sql(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            ),
            db.engine(),
        )["table_name"].tolist()
    )
    if "user_label_embeddings" in existing:
        return
    with autocommit.connect() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE user_label_embeddings (
                    user_id           TEXT NOT NULL,
                    wine_id           TEXT NOT NULL,
                    description       TEXT NOT NULL,
                    description_hash  TEXT NOT NULL,
                    embedding         vector({EMBEDDING_DIM}) NOT NULL,
                    encoded_at        TIMESTAMP NOT NULL,
                    PRIMARY KEY (user_id, wine_id, description_hash)
                )
                """
            )
        )
    log.info("created user_label_embeddings")


def _hash(description: str) -> str:
    return hashlib.sha1(description.strip().encode("utf-8")).hexdigest()


def encode_and_store(user_id: str, wine_id: str, description: str) -> None:
    """Encode a single (user, wine, description) and upsert it.

    Best-effort: failures here log a warning but do not raise — the
    label itself is already persisted in user_labels, and a later
    backfill will pick up anything missed.
    """
    try:
        init_schema()
        h = _hash(description)
        vec = embed.encode_query(description)
        vec_str = "[" + ",".join(f"{x:.6f}" for x in vec.tolist()) + "]"
        with db.connect() as conn:
            # Prune any older embedding(s) for this (user, wine) whose
            # description differs from what we're about to insert. The
            # user_labels parent is now unique per (user_id, wine_id),
            # so there can only ever be one current description per pair;
            # any embedding row with a different description_hash is a
            # leftover from a previous label that's been overwritten.
            conn.execute(
                text(
                    "DELETE FROM user_label_embeddings "
                    "WHERE user_id = :u AND wine_id = :w "
                    "  AND description_hash <> :h"
                ),
                {"u": user_id, "w": wine_id, "h": h},
            )
            conn.execute(
                text(
                    f"""
                    INSERT INTO user_label_embeddings
                        (user_id, wine_id, description, description_hash,
                         embedding, encoded_at)
                    VALUES (:u, :w, :d, :h,
                            CAST(:e AS vector({EMBEDDING_DIM})), :t)
                    ON CONFLICT (user_id, wine_id, description_hash)
                    DO UPDATE SET
                        description = EXCLUDED.description,
                        embedding   = EXCLUDED.embedding,
                        encoded_at  = EXCLUDED.encoded_at
                    """
                ),
                {
                    "u": user_id, "w": wine_id, "d": description, "h": h,
                    "e": vec_str, "t": datetime.utcnow(),
                },
            )
    except Exception as e:  # noqa: BLE001
        log.warning("user-label embed failed (%s); will pick up at next backfill", e)


def backfill() -> dict[str, int]:
    """Embed any rows in user_labels that don't yet have an embedding.

    Returns counts of {scanned, encoded, skipped}.
    """
    init_schema()

    eng = db.engine()
    labels = pd.read_sql(
        text(
            "SELECT user_id, wine_id, description FROM user_labels"
        ),
        eng,
    )
    if labels.empty:
        log.info("no user_labels yet — nothing to backfill")
        return {"scanned": 0, "encoded": 0, "skipped": 0}

    existing = pd.read_sql(
        text("SELECT user_id, wine_id, description_hash FROM user_label_embeddings"),
        eng,
    )
    existing_keys = set(
        (r.user_id, r.wine_id, r.description_hash) for r in existing.itertuples()
    )

    scanned = encoded = skipped = 0
    for row in labels.itertuples(index=False):
        scanned += 1
        h = _hash(row.description)
        if (row.user_id, row.wine_id, h) in existing_keys:
            skipped += 1
            continue
        vec = embed.encode_query(row.description)
        vec_str = "[" + ",".join(f"{x:.6f}" for x in vec.tolist()) + "]"
        with db.connect() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO user_label_embeddings
                        (user_id, wine_id, description, description_hash,
                         embedding, encoded_at)
                    VALUES (:u, :w, :d, :h,
                            CAST(:e AS vector({EMBEDDING_DIM})), :t)
                    """
                ),
                {
                    "u": row.user_id, "w": row.wine_id,
                    "d": row.description, "h": h,
                    "e": vec_str, "t": datetime.utcnow(),
                },
            )
        encoded += 1

    log.info("backfill: scanned=%d encoded=%d skipped=%d",
             scanned, encoded, skipped)
    return {"scanned": scanned, "encoded": encoded, "skipped": skipped}


def search(
    query: str,
    k: int = 10,
    user_id: str | None = None,
) -> pd.DataFrame:
    """Find wines that someone described using language similar to `query`.

    Args:
        query: free-text query, e.g. "sunshine in a bottle".
        k: how many wines to return.
        user_id: if set, restricts search to one user's labels.

    Returns a DataFrame with columns: wine_id, producer_display,
    wine_display, variety, country, region, similarity, description,
    user_display_name. One row per wine — if multiple labels match
    the same wine, we keep the one with the highest similarity.
    """
    init_schema()
    qvec = embed.encode_query(query)
    qvec_str = "[" + ",".join(f"{x:.6f}" for x in qvec.tolist()) + "]"

    filter_clause = "WHERE ule.user_id = :uid" if user_id else ""
    params: dict[str, object] = {"q": qvec_str, "k": k}
    if user_id:
        params["uid"] = user_id

    # 1 - cosine_distance gives similarity in [-1, 1]. We rank by
    # similarity, then dedupe to one row per wine.
    sql = f"""
        WITH ranked AS (
            SELECT
                ule.user_id,
                ule.wine_id,
                ule.description,
                1 - (ule.embedding <=> CAST(:q AS vector({EMBEDDING_DIM})))
                  AS similarity,
                ROW_NUMBER() OVER (
                    PARTITION BY ule.wine_id
                    ORDER BY ule.embedding <=> CAST(:q AS vector({EMBEDDING_DIM}))
                ) AS rn
            FROM user_label_embeddings ule
            {filter_clause}
        )
        SELECT
            r.wine_id,
            w.producer_display,
            w.wine_display,
            w.variety,
            w.country,
            w.region,
            r.similarity,
            r.description,
            COALESCE(u.display_name, '?') AS user_display_name
        FROM ranked r
        JOIN wines w ON w.wine_id = r.wine_id
        LEFT JOIN users u ON u.user_id = r.user_id
        WHERE r.rn = 1
        ORDER BY r.similarity DESC
        LIMIT :k
    """
    return pd.read_sql(text(sql), db.engine(), params=params)
