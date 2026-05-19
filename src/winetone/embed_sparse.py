"""Phase 3b — sparse embeddings via TF-IDF for free-form text.

Dense embeddings (bge-small) capture semantic similarity ("earthy"
matches "barnyard") but blur lexical specificity — a search for
"petrol" can drift to wines that share semantic neighborhoods
without ever mentioning petrol. Sparse embeddings give us back
that lexical precision: each non-zero element is a specific token,
weighted by how distinctive it is to that wine.

The recommendation pipeline (recommend.py) combines both:

    score(wine, query) = α · cosine(dense(wine), dense(query))
                       + (1-α) · cosine(sparse(wine), sparse(query))

with α ∈ [0,1] tunable per query.

We use TF-IDF rather than SPLADE/learned-sparse because:

* TF-IDF is fast enough to embed the full 164k corpus in seconds
  on CPU — SPLADE on CPU is hours.
* Sparse matrices stored cleanly in scipy.sparse + serialized to
  disk via joblib for fast load.
* For PoC the lexical-precision wins are what matter; learned
  sparse is a future upgrade once the dense ↔ sparse hybrid policy
  is dialed in.

Storage: rather than fight CedarDB to store sparse vectors (it
doesn't have a native sparse type), we persist the entire sparse
matrix as a single joblib file under `data/canonical/sparse/`.
The wine_id ↔ row-index map is in CedarDB so we can join.
"""

from __future__ import annotations

import logging
from pathlib import Path

import joblib
import pandas as pd
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer
from sqlalchemy import text

from winetone import db
from winetone.paths import DATA_ROOT

log = logging.getLogger(__name__)

SPARSE_DIR = DATA_ROOT / "canonical" / "sparse"
MATRIX_PATH = SPARSE_DIR / "tfidf_matrix.joblib"
VECTORIZER_PATH = SPARSE_DIR / "tfidf_vectorizer.joblib"

# TF-IDF hyperparameters. min_df=3 drops tokens that appear in fewer
# than 3 documents — kills typos and ultra-rare proper nouns.
# max_features=50000 caps the vocab; wine reviews have ~30k unique
# meaningful words in practice.
MIN_DF = 3
MAX_DF = 0.95
MAX_FEATURES = 50_000


def _load_corpus() -> pd.DataFrame:
    if not db.ping():
        raise RuntimeError("CedarDB unreachable")
    return pd.read_sql(
        """
        SELECT wine_id, review_text_all, variety, country, region
        FROM wine_features
        WHERE review_text_all IS NOT NULL AND length(review_text_all) > 0
        """,
        db.engine(),
    )


def _compose_sparse_text(row: pd.Series) -> str:
    """Mix structured signal in with review text for the TF-IDF input.

    We deliberately include variety/country/region tokens so that a
    query mentioning "Bordeaux" upranks Bordeaux wines through the
    lexical channel even when the review text doesn't explicitly say
    "Bordeaux".
    """
    parts = [str(row["review_text_all"])]
    for col in ("variety", "country", "region"):
        v = row.get(col)
        if isinstance(v, str) and v:
            parts.append(v)
    return " ".join(parts)


def build() -> dict[str, object]:
    """Fit TF-IDF and persist the sparse matrix + vectorizer + index."""
    df = _load_corpus()
    log.info("loaded %d wines for sparse encoding", len(df))

    df["__text__"] = df.apply(_compose_sparse_text, axis=1)

    vectorizer = TfidfVectorizer(
        lowercase=True,
        strip_accents="unicode",
        token_pattern=r"\b[a-zA-Z]{2,}\b",  # alphabetic tokens of length >= 2
        min_df=MIN_DF,
        max_df=MAX_DF,
        max_features=MAX_FEATURES,
        norm="l2",
        sublinear_tf=True,  # log(1+tf) — softens the dominance of repeated words
        ngram_range=(1, 2),  # unigrams + bigrams ("french oak" gets its own term)
    )
    log.info("fitting TF-IDF (n=%d, n-gram=1..2, max_features=%d)", len(df), MAX_FEATURES)
    X = vectorizer.fit_transform(df["__text__"].tolist())
    log.info(
        "sparse matrix shape=%s · density=%.4f%%",
        X.shape, 100 * X.nnz / (X.shape[0] * X.shape[1])
    )

    SPARSE_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(X, MATRIX_PATH)
    joblib.dump(vectorizer, VECTORIZER_PATH)
    log.info("persisted sparse artifacts to %s", SPARSE_DIR)

    # Wine_id ↔ row-index map in CedarDB so recommend can join.
    with db.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS wine_sparse_index"))
        conn.execute(
            text(
                """
                CREATE TABLE wine_sparse_index (
                    wine_id TEXT PRIMARY KEY,
                    row_index INTEGER NOT NULL,
                    n_terms INTEGER NOT NULL
                )
                """
            )
        )
    nnz_per_row = X.getnnz(axis=1)
    rows = [
        {"wine_id": wid, "row_index": int(i), "n_terms": int(nnz_per_row[i])}
        for i, wid in enumerate(df["wine_id"])
    ]
    with db.connect() as conn:
        # Insert in chunks; pandas.to_sql would also work.
        chunk = 10000
        for s in range(0, len(rows), chunk):
            conn.execute(
                text(
                    "INSERT INTO wine_sparse_index (wine_id, row_index, n_terms) "
                    "VALUES (:wine_id, :row_index, :n_terms)"
                ),
                rows[s:s + chunk],
            )

    return {
        "n_wines": int(X.shape[0]),
        "vocab_size": int(X.shape[1]),
        "avg_terms_per_wine": float(X.nnz / X.shape[0]),
    }


def load_matrix() -> tuple[sparse.csr_matrix, TfidfVectorizer, dict[str, int]]:
    """Read back the sparse matrix + vectorizer + wine_id ↔ row map."""
    if not (MATRIX_PATH.exists() and VECTORIZER_PATH.exists()):
        raise FileNotFoundError(
            "sparse artifacts missing — run `winetone build sparse`"
        )
    X = joblib.load(MATRIX_PATH)
    vec = joblib.load(VECTORIZER_PATH)
    idx = pd.read_sql(
        "SELECT wine_id, row_index FROM wine_sparse_index", db.engine()
    )
    wine_to_row = dict(zip(idx["wine_id"], idx["row_index"], strict=False))
    return X, vec, wine_to_row


def encode_query(query: str, vec: TfidfVectorizer) -> sparse.csr_matrix:
    """Sparse-encode an arbitrary query string."""
    return vec.transform([query])


def top_k(
    X: sparse.csr_matrix,
    query_vec: sparse.csr_matrix,
    k: int,
) -> list[tuple[int, float]]:
    """Return (row_index, score) pairs for the top-k matches.

    Uses sparse-matrix dot product — fast even for the full 164k corpus.
    """
    # Both X and query_vec are L2-normalized by the vectorizer, so
    # X @ query_vec.T is cosine similarity directly.
    sims = X @ query_vec.T  # (n_wines, 1) sparse
    dense_sims = sims.toarray().ravel()
    if k >= len(dense_sims):
        order = dense_sims.argsort()[::-1]
    else:
        # argpartition then sort the top-k for speed.
        idx = dense_sims.argpartition(-k)[-k:]
        order = idx[dense_sims[idx].argsort()[::-1]]
    return [(int(i), float(dense_sims[i])) for i in order[:k]]


# --- convenience: paths for the demo / scripts -------------------------


def matrix_path() -> Path:
    return MATRIX_PATH


def vectorizer_path() -> Path:
    return VECTORIZER_PATH
