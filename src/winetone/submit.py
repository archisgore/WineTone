"""User-submitted wine entries.

Lets a signed-in user add a wine that isn't in the corpus yet. Mirrors
the canonical pipeline:

  1. canonicalize_producer / canonicalize_wine + extract_vintage gives
     us the (producer_canonical, wine_canonical, vintage) tuple.
  2. wine_id = uuid5(namespace, canonical_key) — same namespace as the
     bulk pipeline, so user-added wines collide cleanly with future
     pipeline imports of the same wine.
  3. Insert rows into `wines`, `wine_features`, `wine_embeddings`.
     `n_source_records` = 1, `sources_seen` = 'user'. If the user
     provided a description, it lives in `wine_features.review_text_all`
     and counts as `n_reviews = 1`.

Sparse index is intentionally skipped — the TF-IDF matrix is built once
over the full corpus and lives as a joblib file. Mutating it on every
submission gets fiddly fast (locking, ephemeral container storage on
HF Spaces). New wines get dense-only scoring; the hybrid score treats
a missing sparse entry as 0, which is the right behavior.

Idempotency: if the canonical key already maps to an existing wine_id,
we return that ID without modifying anything — including when the
existing row came from the bulk pipeline. The user has effectively
"identified" their wine; the existing data wins.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import TypedDict

from sqlalchemy import text

from winetone import canonicalize, db, embed, lexical

log = logging.getLogger(__name__)

# Same namespace as canonicalize.py — keep these in sync.
_WINE_ID_NAMESPACE = uuid.UUID("a1f9e8c5-3a4e-4d12-9c33-2b5fcf1b9b51")


class SubmittedWine(TypedDict):
    wine_id: str
    producer_display: str
    wine_display: str
    vintage: int | None
    variety: str
    country: str
    region: str
    was_already_present: bool


def submit_wine(
    *,
    producer: str,
    wine_name: str = "",
    vintage: int | None = None,
    variety: str = "",
    country: str = "",
    region: str = "",
    description: str = "",
    submitted_by: str = "",
) -> SubmittedWine:
    """Create (or find) a wine entry in the canonical store.

    Returns the wine_id plus the display fields the UI needs to confirm
    "this is what got added." `was_already_present` distinguishes a
    fresh insert from an idempotent lookup of an existing row.
    """
    producer = (producer or "").strip()
    wine_name = (wine_name or "").strip()
    variety = (variety or "").strip()
    country = (country or "").strip()
    region = (region or "").strip()
    description = (description or "").strip()

    if not producer:
        raise ValueError("producer is required")

    if vintage is not None:
        v = int(vintage)
        if v < 1850 or v > datetime.utcnow().year + 1:
            raise ValueError(f"vintage {v} out of plausible range")
        vintage = v

    producer_canonical = canonicalize.canonicalize_producer(producer)
    wine_canonical = canonicalize.canonicalize_wine(wine_name)
    key = canonicalize.canonical_key(
        producer_canonical, wine_canonical, vintage,
    )
    wine_id = str(uuid.uuid5(_WINE_ID_NAMESPACE, key))

    # If this canonical key already exists, return it as-is.
    eng = db.engine()
    with eng.connect() as conn:
        existing = conn.execute(
            text("""
                SELECT wine_id, producer_display, wine_display, vintage,
                       variety, country, region
                FROM wines WHERE wine_id = :w
            """),
            {"w": wine_id},
        ).fetchone()
    if existing:
        log.info("submit_wine: existing wine %s for key=%r", wine_id, key)
        return SubmittedWine(
            wine_id=str(existing[0]),
            producer_display=str(existing[1] or ""),
            wine_display=str(existing[2] or ""),
            vintage=int(existing[3]) if existing[3] is not None else None,
            variety=str(existing[4] or ""),
            country=str(existing[5] or ""),
            region=str(existing[6] or ""),
            was_already_present=True,
        )

    # Fresh wine — write to all three tables in one transaction.
    n_reviews = 1 if description else 0
    sources_seen = "user"
    tsv_text = lexical.build_tsv_expression(
        producer=producer, wine_name=wine_name, variety=variety,
        region=region, country=country, description=description,
    )
    with eng.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO wines
                    (wine_id, producer_canonical, wine_canonical, vintage,
                     producer_display, wine_display, variety, country,
                     region, n_source_records, sources_seen, tsv)
                VALUES (:w, :pc, :wc, :v, :pd, :wd, :var, :ctry, :reg,
                        :nsr, :srcs,
                        to_tsvector('english', :tsv_text))
            """),
            {
                "w": wine_id,
                "pc": producer_canonical,
                "wc": wine_canonical,
                "v": vintage,
                "pd": producer,
                "wd": wine_name,
                "var": variety,
                "ctry": country,
                "reg": region,
                "nsr": 1,
                "srcs": sources_seen,
                "tsv_text": tsv_text,
            },
        )
        # NB: review_text_all carries the user's own description. The bulk
        # pipeline uses this column when it rebuilds wines.tsv later, so
        # writing it here keeps user-submitted wines in sync with a future
        # full rebuild rather than getting silently dropped.
        conn.execute(
            text("""
                INSERT INTO wine_features
                    (wine_id, producer_canonical, wine_canonical, vintage,
                     producer_display, wine_display, variety, country,
                     region, n_source_records, sources_seen, n_reviews,
                     median_points, max_points, median_price,
                     review_text_all)
                VALUES (:w, :pc, :wc, :v, :pd, :wd, :var, :ctry, :reg,
                        :nsr, :srcs, :nr, NULL, NULL, NULL, :rta)
            """),
            {
                "w": wine_id,
                "pc": producer_canonical,
                "wc": wine_canonical,
                "v": vintage,
                "pd": producer,
                "wd": wine_name,
                "var": variety,
                "ctry": country,
                "reg": region,
                "nsr": 1,
                "srcs": sources_seen,
                "nr": n_reviews,
                "rta": description or None,
            },
        )

    # Encode + insert the embedding. Build the same text shape as the
    # bulk pipeline: structured prefix + (here) the submitter's
    # description as the only "review."
    parts: list[str] = []
    if variety:
        parts.append(f"variety: {variety}.")
    if region:
        parts.append(f"region: {region}.")
    if country:
        parts.append(f"country: {country}.")
    if vintage is not None:
        parts.append(f"vintage: {vintage}.")
    if description:
        parts.append(description)
    embed_text = " ".join(parts) or producer  # never empty
    embed_text = embed_text[:2000]

    vec = embed.encode_query(embed_text)
    vec_str = "[" + ",".join(f"{x:.6f}" for x in vec.tolist()) + "]"
    with eng.begin() as conn:
        conn.execute(
            text(f"""
                INSERT INTO wine_embeddings
                    (wine_id, embedding, embedding_model)
                VALUES (:w, CAST(:e AS vector({embed.EMBEDDING_DIM})), :m)
            """),
            {"w": wine_id, "e": vec_str, "m": embed.MODEL_NAME},
        )

    log.info(
        "submit_wine: created %s — %s %s (%s) by user %s",
        wine_id, producer, wine_name or "(no wine name)",
        vintage or "NV", submitted_by or "?",
    )
    return SubmittedWine(
        wine_id=wine_id,
        producer_display=producer,
        wine_display=wine_name,
        vintage=vintage,
        variety=variety,
        country=country,
        region=region,
        was_already_present=False,
    )
