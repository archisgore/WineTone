"""Phase 2 — entity resolution and canonical wine identity.

The plan (docs/DATA-AND-ML-PIPELINE-PLAN.md §2) calls for a
four-stage entity-resolution cascade. For the PoC we ship Stage 1
(deterministic canonicalization) only — the deterministic canonical
key (producer × wine_name × vintage) gets us roughly 70% of records
resolved without ML, and that's enough to validate the rest of the
pipeline. Stages 2–4 (fuzzy / ML / manual) become Phase 2.5 work.

Inputs: every Parquet under data/staging/<source>/<source>.parquet
that has columns we can map to (producer, wine_name, vintage).
Currently consumed:

* `wine_enthusiast_130k` — the canonical UGC corpus, 130k rows.
* `wine_enthusiast_150k` — the v1 scrape, 151k rows. Significant
  overlap with 130k after canonicalization.

Output: three tables in CedarDB:

* `wines`          — one row per canonical (producer, wine, vintage)
* `source_records` — every source-record's mapping into a wine_id
* `wine_features`  — denormalized flat table for downstream ML
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from collections.abc import Iterable

import pandas as pd
from sqlalchemy import text

from winetone import db
from winetone.paths import staging_dir

log = logging.getLogger(__name__)


# --- canonicalization primitives ------------------------------------------

# These get stripped from producer names so "Château Margaux" and
# "Margaux, Ch." collapse to the same canonical form.
_PRODUCER_TITLE_TOKENS = {
    "chateau", "château", "ch.", "ch",
    "domaine", "domain",
    "weingut", "winzerverein",
    "bodega", "bodegas",
    "tenuta", "azienda", "az.",
    "the",
}

# These get stripped from wine names — they're classification /
# quality markers, not the wine identity. Keep them in a separate
# field of the canonical record instead.
_WINE_CLASSIFICATION_TOKENS = {
    "premier", "grand", "cru", "classé", "classe", "classifie",
    "riserva", "reserva", "reserve",
    "gran", "selección", "seleccion", "selection",
    "vendemmia", "vendange",
    "doc", "docg", "igt", "aoc", "dop", "pdo", "pgi",
    "vqa", "ava",
    "estate", "vineyard", "vineyards",
}

_VINTAGE_RE = re.compile(r"\b(18\d{2}|19\d{2}|20\d{2})\b")


def _strip_accents(s: str) -> str:
    """Strip diacritics so "rosé" → "rose" and "château" → "chateau"."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _clean(s: str) -> str:
    """Lowercase, strip accents, collapse whitespace, drop punctuation."""
    s = _strip_accents(s).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def canonicalize_producer(raw: str | None) -> str:
    """Reduce a producer string to a canonical form for matching."""
    if not raw:
        return ""
    cleaned = _clean(raw)
    tokens = [t for t in cleaned.split() if t not in _PRODUCER_TITLE_TOKENS]
    return " ".join(tokens)


def canonicalize_wine(raw: str | None) -> str:
    """Reduce a wine-name string to a canonical form for matching."""
    if not raw:
        return ""
    cleaned = _clean(raw)
    tokens = [
        t for t in cleaned.split() if t not in _WINE_CLASSIFICATION_TOKENS
    ]
    return " ".join(tokens)


def extract_vintage(title: str | None) -> int | None:
    """Pull a 4-digit vintage year out of a free-text title."""
    if not title:
        return None
    m = _VINTAGE_RE.search(title)
    if not m:
        return None
    y = int(m.group(1))
    if 1850 <= y <= 2030:
        return y
    return None


def canonical_key(producer: str, wine_name: str, vintage: int | None) -> str:
    """The string that defines wine identity for exact-match dedup."""
    return f"{producer}||{wine_name}||{vintage or 'NV'}"


# --- the pipeline --------------------------------------------------------


def _normalize_review_source(df: pd.DataFrame, source_name: str) -> pd.DataFrame:
    """Project a review source into the common review schema."""
    out = pd.DataFrame()
    out["source"] = pd.Series([source_name] * len(df), dtype="string")
    out["source_row_id"] = pd.Series(df.index, dtype="string")
    out["producer_raw"] = df["winery"].astype("string")
    out["wine_title_raw"] = df.get("title", df["winery"]).astype("string")
    out["designation"] = df.get(
        "designation", pd.Series([None] * len(df))
    ).astype("string")
    out["variety"] = df["variety"].astype("string")
    out["country"] = df["country"].astype("string")
    out["region"] = df.get("region_1", df["province"]).astype("string")
    out["province"] = df["province"].astype("string")
    out["points"] = df.get("points", pd.Series([None] * len(df))).astype("Int16")
    out["price"] = df.get("price", pd.Series([None] * len(df))).astype("Float32")
    out["description"] = df["description"].astype("string")

    out["vintage"] = (
        out["wine_title_raw"].map(extract_vintage).astype("Int16")
    )
    out["wine_name_raw"] = out["wine_title_raw"].fillna("").map(
        lambda s: _VINTAGE_RE.sub("", s).strip(" -–—,()").strip()
    ).astype("string")

    return out


def _load_review_sources() -> pd.DataFrame:
    """Concatenate every review source we have into one tidy frame."""
    frames: list[pd.DataFrame] = []
    review_sources = ["wine_enthusiast_130k", "wine_enthusiast_150k"]
    for source_name in review_sources:
        path = staging_dir(source_name) / f"{source_name}.parquet"
        if not path.exists():
            log.warning("missing staged source: %s", source_name)
            continue
        df = pd.read_parquet(path)
        n = len(df)
        # The 150k has no `title` field; the description carries vintage too.
        if "title" not in df.columns:
            df["title"] = (
                df["winery"].fillna("") + " "
                + df.get("designation", pd.Series([""] * n)).fillna("")
            )
        frames.append(_normalize_review_source(df, source_name))
        log.info("loaded %d rows from %s", n, source_name)
    if not frames:
        raise RuntimeError(
            "no review sources staged — run `winetone pull --tier a` first"
        )
    return pd.concat(frames, ignore_index=True)


def _resolve_canonical(reviews: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stage 1 entity resolution: deterministic canonical key dedup."""
    log.info("canonicalizing %d review rows", len(reviews))
    reviews = reviews.copy()
    reviews["producer_canonical"] = reviews["producer_raw"].map(
        canonicalize_producer
    ).astype("string")
    reviews["wine_canonical"] = reviews["wine_name_raw"].map(
        canonicalize_wine
    ).astype("string")
    reviews["canonical_key"] = [
        canonical_key(p, w, int(v) if pd.notna(v) else None)
        for p, w, v in zip(
            reviews["producer_canonical"].fillna(""),
            reviews["wine_canonical"].fillna(""),
            reviews["vintage"],
            strict=False,
        )
    ]

    namespace = uuid.UUID("a1f9e8c5-3a4e-4d12-9c33-2b5fcf1b9b51")

    def _wine_id(key: str) -> str:
        return str(uuid.uuid5(namespace, key))

    reviews["wine_id"] = reviews["canonical_key"].map(_wine_id).astype("string")

    wines = (
        reviews.groupby("canonical_key", as_index=False)
        .agg(
            wine_id=("wine_id", "first"),
            producer_canonical=("producer_canonical", "first"),
            wine_canonical=("wine_canonical", "first"),
            vintage=("vintage", "first"),
            producer_display=("producer_raw", "first"),
            wine_display=("wine_name_raw", "first"),
            variety=("variety", "first"),
            country=("country", "first"),
            region=("region", "first"),
            n_source_records=("source", "count"),
            sources_seen=("source", lambda s: ",".join(sorted(set(s)))),
        )
    )
    wines = wines.drop(columns=["canonical_key"])
    log.info(
        "resolved %d distinct wines from %d records (dedup ratio %.2f)",
        len(wines),
        len(reviews),
        len(reviews) / max(len(wines), 1),
    )

    source_records = reviews[
        [
            "source",
            "source_row_id",
            "wine_id",
            "producer_raw",
            "wine_title_raw",
            "designation",
            "variety",
            "country",
            "region",
            "province",
            "vintage",
            "points",
            "price",
            "description",
        ]
    ].copy()
    return wines, source_records


def _load_user_descriptions() -> pd.DataFrame:
    """Pull accumulated user-provided descriptions from user_labels.

    This is the feedback path requested by the user: every description
    a user has added via `winetone calibrate add` flows back into
    review_text_all on the next canonical rebuild. As the per-user
    label table grows, the global embedding corpus grows with it —
    user vocabulary becomes signal the encoder sees on the next train.

    If the user_labels table doesn't exist yet (first build before any
    user has calibrated) we return an empty frame.
    """
    try:
        df = pd.read_sql(
            """
            SELECT wine_id, description
            FROM user_labels
            WHERE description IS NOT NULL AND length(description) > 0
            """,
            db.engine(),
        )
        log.info("loaded %d user-contributed descriptions", len(df))
        return df
    except Exception as e:  # noqa: BLE001
        log.info("no user_labels table yet (%s)", e)
        return pd.DataFrame(columns=["wine_id", "description"])


def _build_wine_features(
    wines: pd.DataFrame, source_records: pd.DataFrame
) -> pd.DataFrame:
    """Roll up per-wine signals into one wide flat table for ML."""

    def _agg_reviews(s: pd.Series) -> str:
        return " || ".join([str(x) for x in s.dropna().tolist()])

    grouped = source_records.groupby("wine_id", as_index=False).agg(
        n_reviews=("description", "count"),
        review_text_all=("description", _agg_reviews),
        median_points=("points", "median"),
        max_points=("points", "max"),
        median_price=("price", "median"),
    )
    grouped["n_reviews"] = grouped["n_reviews"].astype("Int32")
    grouped["median_points"] = grouped["median_points"].astype("Float32")
    grouped["max_points"] = grouped["max_points"].astype("Float32")
    grouped["median_price"] = grouped["median_price"].astype("Float32")

    # Feedback loop: pull user-contributed descriptions into review_text_all.
    user_desc = _load_user_descriptions()
    if not user_desc.empty:
        user_rollup = (
            user_desc.groupby("wine_id", as_index=False)
            .agg(
                user_n=("description", "count"),
                user_text=("description", _agg_reviews),
            )
        )
        grouped = grouped.merge(user_rollup, on="wine_id", how="left")
        # Concatenate user text after the canonical review text.
        grouped["review_text_all"] = grouped.apply(
            lambda r: " || ".join(
                [t for t in (r["review_text_all"], r.get("user_text")) if isinstance(t, str) and t]
            ),
            axis=1,
        )
        grouped["n_reviews_with_user"] = (
            grouped["n_reviews"].fillna(0).astype("Int32")
            + grouped["user_n"].fillna(0).astype("Int32")
        )
        n_extended = grouped["user_n"].fillna(0).astype(int).gt(0).sum()
        log.info(
            "merged user descriptions into review_text_all for %d wines",
            n_extended,
        )

    features = wines.merge(grouped, on="wine_id", how="left")
    log.info(
        "built wine_features: rows=%d cols=%d", len(features), len(features.columns)
    )
    return features


def _persist_cedardb(
    wines: pd.DataFrame,
    source_records: pd.DataFrame,
    wine_features: pd.DataFrame,
) -> None:
    """Write the three canonical tables into CedarDB.

    Uses pandas.to_sql for the bulk load — fine at our PoC scale
    (~280k records). For Sprint 3+ we may switch to CedarDB's COPY
    for higher throughput.
    """
    db.init_schema()
    eng = db.engine()
    log.info("writing %d wines to CedarDB", len(wines))
    wines.to_sql("wines", eng, index=False, if_exists="replace", chunksize=10000)
    log.info("writing %d source_records to CedarDB", len(source_records))
    source_records.to_sql(
        "source_records", eng, index=False, if_exists="replace", chunksize=10000
    )
    log.info("writing %d wine_features to CedarDB", len(wine_features))
    wine_features.to_sql(
        "wine_features", eng, index=False, if_exists="replace", chunksize=10000
    )

    # Indexes for downstream lookups. CedarDB requires CREATE INDEX to
    # run in autocommit mode (not inside an explicit transaction); it
    # also doesn't accept `IF NOT EXISTS`. We open each index in its
    # own autocommitting connection so a duplicate-name error on one
    # doesn't poison the others.
    autocommit = db.engine().execution_options(isolation_level="AUTOCOMMIT")
    for stmt in (
        "CREATE INDEX idx_sr_wine_id ON source_records (wine_id)",
        "CREATE INDEX idx_w_producer ON wines (producer_canonical)",
        "CREATE INDEX idx_w_variety ON wines (variety)",
    ):
        try:
            with autocommit.connect() as conn:
                conn.execute(text(stmt))
        except Exception as e:  # noqa: BLE001
            log.warning("index create skipped: %s (%s)", stmt, e)

    # Populate the full-text-search column over the just-loaded wines.
    # The bulk pipeline has review text available (wine_features.review_text_all)
    # so the tsv is richer than what user-submitted wines can build.
    log.info("populating wines.tsv (FTS column) + GIN index")
    with autocommit.connect() as conn:
        conn.execute(text("ALTER TABLE wines ADD COLUMN IF NOT EXISTS tsv tsvector"))
        # Fold in review_text_all from wine_features for the richest possible
        # lexical signal.
        conn.execute(text("""
            UPDATE wines w SET tsv = to_tsvector('english',
                COALESCE(w.producer_display, '') || ' ' ||
                COALESCE(w.wine_display, '')     || ' ' ||
                COALESCE(w.variety, '')          || ' ' ||
                COALESCE(w.region, '')           || ' ' ||
                COALESCE(w.country, '')          || ' ' ||
                COALESCE((SELECT review_text_all FROM wine_features f
                          WHERE f.wine_id = w.wine_id), '')
            )
        """))
        try:
            conn.execute(text("CREATE INDEX wines_tsv_gin ON wines USING GIN (tsv)"))
        except Exception as e:  # noqa: BLE001
            log.warning("GIN index create skipped: %s", e)

    log.info("canonical store written")


def build() -> dict[str, int]:
    """Run the full Phase 2 pipeline end-to-end."""
    if not db.ping():
        raise RuntimeError(
            "CedarDB is not reachable — run `make db-up-bg` first"
        )
    reviews = _load_review_sources()
    wines, source_records = _resolve_canonical(reviews)
    wine_features = _build_wine_features(wines, source_records)
    _persist_cedardb(wines, source_records, wine_features)
    return {
        "n_wines": len(wines),
        "n_source_records": len(source_records),
        "n_features": len(wine_features),
    }


def load_wines() -> pd.DataFrame:
    """Read the canonical wines table back from CedarDB."""
    if not db.ping():
        raise RuntimeError("CedarDB unreachable — run `make db-up-bg`")
    return pd.read_sql("SELECT * FROM wines", db.engine())


def load_wine_features() -> pd.DataFrame:
    """Read the wine_features flat table back from CedarDB."""
    if not db.ping():
        raise RuntimeError("CedarDB unreachable — run `make db-up-bg`")
    return pd.read_sql("SELECT * FROM wine_features", db.engine())


def load_source_records(wine_ids: Iterable[str] | None = None) -> pd.DataFrame:
    """Read source_records. Optionally filter by wine_ids."""
    if not db.ping():
        raise RuntimeError("CedarDB unreachable — run `make db-up-bg`")
    if wine_ids is None:
        return pd.read_sql("SELECT * FROM source_records", db.engine())
    ids = list(wine_ids)
    placeholders = ",".join(f":id{i}" for i in range(len(ids)))
    params = {f"id{i}": v for i, v in enumerate(ids)}
    return pd.read_sql(
        text(
            f"SELECT * FROM source_records WHERE wine_id IN ({placeholders})"
        ),
        db.engine(),
        params=params,
    )
