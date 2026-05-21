"""Phase 4 — personalized recommendations from few-shot user labels.

The user provides ≥5 wines from our catalog along with their *own*
free-text descriptions of those wines. From this we fit a personal
linear projection that maps their language into our wine
embedding space, regularized toward a global prior so 5 samples
don't overfit.

Math (see DATA-AND-ML-PIPELINE-PLAN.md §4.3):

  For each (description_i, wine_i) the user provides:

    L_i = sentence_encoder(description_i)        # 384-dim
    W_i = wine_embedding(wine_i)                  # 384-dim

  Fit a per-user A_user, b_user via ridge regression:

    minimize  sum_i ||W_i - (A · L_i + b)||^2
            +  λ_A · ||A - A_0||_F^2
            +  λ_b · ||b - b_0||^2

  where (A_0, b_0) is the global identity prior — the closed-form
  closes to W ≈ L when the user data is scarce.

At query time:

    L_q = sentence_encoder(query_text)
    target = A_user · L_q + b_user
    top_k = nearest wines in embedding space to `target`

Storage (CedarDB):

  users           — one row per user (user_id, created_at)
  user_labels     — (user_id, wine_id, description) triples
  user_projections — (user_id, A serialized, b serialized, fit_at)

For the PoC we use an *identity* prior: A_0 = I, b_0 = 0. That
means a cold user gets the generic "encode-the-query, search
embedding-space directly" behavior. As the user adds labels,
their A_user and b_user drift away from identity in directions
that explain their observed labels.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import text

from winetone import db, embed

log = logging.getLogger(__name__)

# Ridge regularization. λ_A is large because A is 384x384 (147k
# parameters) and we have ~5 samples — needs heavy shrinkage toward
# the identity prior.
LAMBDA_A = 100.0
LAMBDA_B = 10.0


@dataclass
class UserProjection:
    """A user's personal projection from language to wine space."""

    user_id: str
    A: np.ndarray  # (dim, dim)
    b: np.ndarray  # (dim,)
    n_labels: int

    def apply(self, language_vec: np.ndarray) -> np.ndarray:
        out = self.A @ language_vec + self.b
        n = np.linalg.norm(out)
        return out / (n if n > 0 else 1.0)


# --- schema -------------------------------------------------------------


def init_user_schema() -> None:
    """Create the user-related tables in CedarDB if absent.

    CedarDB has been observed to crash on `CREATE TABLE IF NOT EXISTS`
    combined with `DEFAULT NOW()`; we sidestep both by:
      1. Checking pg_catalog/information_schema for existence first.
      2. Avoiding DEFAULT NOW() — clients supply timestamps explicitly.
    """
    autocommit = db.engine().execution_options(isolation_level="AUTOCOMMIT")

    schemas = {
        "users": """
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """,
        "user_labels": """
            CREATE TABLE user_labels (
                user_id TEXT NOT NULL,
                wine_id TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
        """,
        "user_projections": """
            CREATE TABLE user_projections (
                user_id TEXT PRIMARY KEY,
                n_labels INTEGER NOT NULL,
                A_serialized BYTEA NOT NULL,
                b_serialized BYTEA NOT NULL,
                fit_at TIMESTAMP NOT NULL
            )
        """,
    }

    # One round-trip to find which tables already exist.
    existing = set(
        pd.read_sql(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            ),
            db.engine(),
        )["table_name"].tolist()
    )

    for name, ddl in schemas.items():
        if name in existing:
            continue
        try:
            with autocommit.connect() as conn:
                conn.execute(text(ddl))
            log.info("created table %s", name)
        except Exception as e:  # noqa: BLE001
            log.warning("could not create table %s: %s", name, e)


# --- user + label management ---------------------------------------------


def get_or_create_user_for_clerk(
    clerk_user_id: str,
    display_name: str,
    email: str = "",
) -> str:
    """Find the internal user_id for a Clerk identity, creating the row
    if this is the first time we've seen them.

    Looks up by clerk_user_id (the stable JWT `sub`). If found, returns
    the existing user_id and optionally refreshes the display name in
    case the user changed their Clerk username. If not found, creates a
    new row.
    """
    from datetime import datetime
    with db.connect() as conn:
        row = conn.execute(
            text("SELECT user_id, display_name FROM users WHERE clerk_user_id = :c"),
            {"c": clerk_user_id},
        ).fetchone()
        if row:
            existing_uid, existing_name = row
            # Refresh display_name if Clerk changed it (rare but allowed).
            if existing_name != display_name:
                try:
                    conn.execute(
                        text("UPDATE users SET display_name = :n WHERE user_id = :u"),
                        {"n": display_name, "u": existing_uid},
                    )
                except Exception as e:  # noqa: BLE001
                    # Likely a UNIQUE violation if the new name is taken.
                    log.warning(
                        "could not rename user %s to %r: %s",
                        existing_uid, display_name, e,
                    )
            return str(existing_uid)
        user_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO users (user_id, clerk_user_id, display_name, "
                "email, created_at) "
                "VALUES (:u, :c, :n, :e, :t)"
            ),
            {
                "u": user_id, "c": clerk_user_id,
                "n": display_name, "e": email,
                "t": datetime.utcnow(),
            },
        )
        log.info("created user %s (clerk=%s, name=%s)",
                 user_id, clerk_user_id, display_name)
        return user_id


def get_user_by_display_name(display_name: str) -> str | None:
    """Read-only lookup — returns user_id or None. No row creation."""
    with db.connect() as conn:
        row = conn.execute(
            text("SELECT user_id FROM users WHERE display_name = :n"),
            {"n": display_name},
        ).fetchone()
        return str(row[0]) if row else None


def get_or_create_user(display_name: str) -> str:
    """Convenience for CLI / local-dev use only.

    Looks up an existing user by display_name; if absent, creates one
    with a synthetic `clerk_user_id` (`cli:<uuid>`) so the NOT NULL +
    UNIQUE constraints are satisfied. Web requests should go through
    get_or_create_user_for_clerk with a real Clerk identity instead.
    """
    from datetime import datetime
    with db.connect() as conn:
        row = conn.execute(
            text("SELECT user_id FROM users WHERE display_name = :n"),
            {"n": display_name},
        ).fetchone()
        if row:
            return str(row[0])
        user_id = str(uuid.uuid4())
        synthetic_clerk_id = f"cli:{uuid.uuid4()}"
        conn.execute(
            text(
                "INSERT INTO users (user_id, clerk_user_id, display_name, "
                "email, created_at) "
                "VALUES (:u, :c, :n, :e, :t)"
            ),
            {
                "u": user_id, "c": synthetic_clerk_id,
                "n": display_name, "e": "",
                "t": datetime.utcnow(),
            },
        )
        log.info("created CLI user %s (display=%s)", user_id, display_name)
        return user_id


def add_label(user_id: str, wine_id: str, description: str) -> None:
    """Record a (user, wine, description) tuple."""
    from datetime import datetime
    with db.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO user_labels (user_id, wine_id, description, created_at) "
                "VALUES (:u, :w, :d, :t)"
            ),
            {"u": user_id, "w": wine_id, "d": description, "t": datetime.utcnow()},
        )
    # Also index the description into the vocabulary-search corpus.
    # Best-effort: never fail label creation if the encoder hiccups.
    from winetone import embed_user_labels
    embed_user_labels.encode_and_store(user_id, wine_id, description)


def get_labels(user_id: str) -> pd.DataFrame:
    """Return all labels this user has provided."""
    return pd.read_sql(
        text(
            "SELECT user_id, wine_id, description "
            "FROM user_labels WHERE user_id = :u"
        ),
        db.engine(),
        params={"u": user_id},
    )


def find_wine_by_text(query: str, limit: int = 5) -> pd.DataFrame:
    """Lookup helper for label-time: find wines whose display string,
    variety, region, or country matches each token in the query.

    AND-semantics across tokens: "Pinot Noir Burgundy" requires
    "Pinot", "Noir", and "Burgundy" to each appear in some field of
    the row. Restricts to wines that have an embedding so labels
    feed cleanly into the calibration step.
    """
    tokens = [t for t in query.lower().split() if t]
    if not tokens:
        return pd.DataFrame()
    # Each token must match producer/wine/variety/region/country.
    where_parts = []
    params: dict[str, object] = {"lim": limit}
    for i, tok in enumerate(tokens):
        key = f"t{i}"
        params[key] = f"%{tok}%"
        where_parts.append(
            f"(LOWER(COALESCE(w.producer_display, '')) LIKE :{key} "
            f"OR LOWER(COALESCE(w.wine_display, '')) LIKE :{key} "
            f"OR LOWER(COALESCE(w.variety, '')) LIKE :{key} "
            f"OR LOWER(COALESCE(w.region, '')) LIKE :{key} "
            f"OR LOWER(COALESCE(w.country, '')) LIKE :{key})"
        )
    where_clause = " AND ".join(where_parts)
    # Rank by median_points DESC, then n_reviews DESC — when "petrus"
    # could match Château Pétrus (97 pts, hundreds of reviews) or
    # Petrussa (a small Italian Pinot Bianco producer, 89 pts), we want
    # the famous one. Falls back to plain substring order if features
    # join is unavailable.
    sql = f"""
        SELECT w.wine_id, w.producer_display, w.wine_display, w.vintage,
               w.variety, w.country, w.region,
               COALESCE(f.median_points, 0) AS _pts,
               COALESCE(f.n_reviews, 0) AS _n
        FROM wines w
        LEFT JOIN wine_features f ON f.wine_id = w.wine_id
        WHERE w.wine_id IN (SELECT wine_id FROM wine_embeddings)
          AND {where_clause}
        ORDER BY _pts DESC, _n DESC
        LIMIT :lim
    """
    df = pd.read_sql(text(sql), db.engine(), params=params)
    # Drop the helper sort columns.
    return df.drop(columns=[c for c in ("_pts", "_n") if c in df.columns])


# --- projection fitting --------------------------------------------------


def _load_user_label_pairs(user_id: str) -> tuple[np.ndarray, np.ndarray, int]:
    """Return (L, W, n) — language embeddings and wine embeddings."""
    labels = get_labels(user_id)
    if len(labels) == 0:
        raise RuntimeError(
            f"User {user_id} has no labels — call `winetone calibrate add` first"
        )
    # Wine embeddings for the labeled wines.
    wine_ids = labels["wine_id"].tolist()
    placeholders = ",".join(f"'{w}'" for w in wine_ids)
    wine_emb = pd.read_sql(
        f"SELECT wine_id, embedding FROM wine_embeddings "
        f"WHERE wine_id IN ({placeholders})",
        db.engine(),
    )
    # Parse pgvector text format.
    def _parse(v: object) -> np.ndarray:
        if isinstance(v, list):
            return np.asarray(v, dtype=np.float32)
        return np.fromstring(str(v).strip("[]"), sep=",", dtype=np.float32)

    wine_emb["vec"] = wine_emb["embedding"].map(_parse)
    wine_emb_map = dict(zip(wine_emb["wine_id"], wine_emb["vec"], strict=False))

    # Encode each user description.
    rows_L = []
    rows_W = []
    for _, row in labels.iterrows():
        if row["wine_id"] not in wine_emb_map:
            log.warning(
                "label for wine_id=%s skipped (no embedding)", row["wine_id"]
            )
            continue
        rows_L.append(embed.encode_query(row["description"]))
        rows_W.append(wine_emb_map[row["wine_id"]])

    L = np.vstack(rows_L) if rows_L else np.empty((0, embed.EMBEDDING_DIM))
    W = np.vstack(rows_W) if rows_W else np.empty((0, embed.EMBEDDING_DIM))
    return L, W, len(rows_L)


def fit_projection(user_id: str) -> UserProjection:
    """Fit and persist a per-user (A, b) via ridge regression with
    identity prior."""
    L, W, n = _load_user_label_pairs(user_id)
    if n < 1:
        raise RuntimeError("Need at least 1 label to fit a projection")
    if n < 5:
        log.warning(
            "only %d labels — calibration will be heavily biased toward "
            "the identity prior; aim for ≥ 5", n
        )

    d = embed.EMBEDDING_DIM
    I_d = np.eye(d, dtype=np.float32)

    # Closed-form ridge regression with identity prior on A and zero prior
    # on b. Treat A and b together as a single ([A | b]) (d, d+1) matrix
    # operating on [L | 1].
    #
    #   M = [A | b]   shape (d, d+1)
    #   target W = M · [L^T ; 1]  shape (d, n)
    #   prior:  A_0 = I, b_0 = 0  →  M_0 = [I | 0]
    #
    # Closed form:  M = (W L_aug^T + λ M_0) (L_aug L_aug^T + λ I_{d+1})^{-1}
    #   with separate λ_A on the I-block and λ_B on the 0-block. We use
    #   diagonal lambdas as a 1D vector.
    L_aug = np.hstack([L, np.ones((n, 1), dtype=np.float32)])  # (n, d+1)
    M0 = np.hstack([I_d, np.zeros((d, 1), dtype=np.float32)])  # (d, d+1)
    lambdas = np.concatenate(
        [np.full(d, LAMBDA_A, dtype=np.float32), np.array([LAMBDA_B], dtype=np.float32)]
    )

    XtX = L_aug.T @ L_aug  # (d+1, d+1)
    XtY = L_aug.T @ W      # (d+1, d)
    reg = np.diag(lambdas)  # (d+1, d+1)
    rhs = XtY.T + reg[:d, :d+1].T @ M0.T  # actually let's just do directly:
    # Closed form re-derivation: we want M minimizing
    #   ||W - M L_aug^T||_F^2 + sum_j lambda_j ||M[:, j] - M0[:, j]||^2
    # Equivalent (per row of M, treating M[i, :]^T = m_i):
    #   m_i = (L_aug^T L_aug + diag(lambdas))^{-1} (L_aug^T W[:, i] + lambdas * M0[i, :])
    #
    # Because the row-axes are independent, we can solve once for all rows:
    G = XtX + np.diag(lambdas)
    # Right-hand side: (d+1, d) = L_aug^T W + lambdas * M0^T  (broadcast)
    rhs = L_aug.T @ W + (lambdas[:, None] * M0.T)  # (d+1, d)
    M_T = np.linalg.solve(G, rhs)  # (d+1, d)
    M = M_T.T  # (d, d+1)

    A = M[:, :d].astype(np.float32)
    b = M[:, d].astype(np.float32)

    proj = UserProjection(user_id=user_id, A=A, b=b, n_labels=n)
    _persist_projection(proj)
    log.info(
        "fit projection for user=%s n=%d, ||A-I||_F=%.3f, ||b||=%.3f",
        user_id, n, float(np.linalg.norm(A - I_d)), float(np.linalg.norm(b))
    )
    return proj


def _persist_projection(proj: UserProjection) -> None:
    """Store (or replace) a user's projection in CedarDB."""
    from datetime import datetime
    A_bytes = proj.A.tobytes()
    b_bytes = proj.b.tobytes()
    with db.connect() as conn:
        conn.execute(
            text(
                "DELETE FROM user_projections WHERE user_id = :u"
            ),
            {"u": proj.user_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO user_projections (user_id, n_labels, A_serialized,
                                              b_serialized, fit_at)
                VALUES (:u, :n, :A, :b, :t)
                """
            ),
            {
                "u": proj.user_id, "n": proj.n_labels,
                "A": A_bytes, "b": b_bytes,
                "t": datetime.utcnow(),
            },
        )


def load_projection(user_id: str) -> UserProjection | None:
    """Read a stored projection. Returns None if not fitted."""
    init_user_schema()
    df = pd.read_sql(
        text(
            "SELECT user_id, n_labels, A_serialized, b_serialized "
            "FROM user_projections WHERE user_id = :u"
        ),
        db.engine(),
        params={"u": user_id},
    )
    if df.empty:
        return None
    row = df.iloc[0]
    d = embed.EMBEDDING_DIM
    A = np.frombuffer(row["a_serialized"], dtype=np.float32).reshape(d, d)
    b = np.frombuffer(row["b_serialized"], dtype=np.float32)
    return UserProjection(
        user_id=row["user_id"], A=A.copy(), b=b.copy(), n_labels=int(row["n_labels"])
    )


# --- recommendation ------------------------------------------------------


def recommend(
    user_id: str | None,
    query: str,
    k: int = 10,
    filters: dict[str, object] | None = None,
    alpha: float = 0.6,
) -> pd.DataFrame:
    """Recommend top-k wines for a user given a free-text query.

    Hybrid score:
        score = alpha · cosine(dense_wine, dense_target)
              + (1 - alpha) · cosine(sparse_wine, sparse_query)

    - If user_id is None or no projection exists, falls back to the
      identity projection on the dense side.
    - If sparse embeddings aren't built, alpha is forced to 1.0 and
      only the dense channel contributes.
    - `filters` can include `country`, `variety`, etc.
    - `alpha`: dense weight in [0, 1]. Default 0.6 leans semantic.
    """
    # Dense side (with optional user personalization)
    L_q = embed.encode_query(query)
    proj = load_projection(user_id) if user_id else None
    target = L_q if proj is None else proj.apply(L_q)

    dense_ids, dense_vecs = embed.load_embeddings()
    if len(dense_ids) == 0:
        raise RuntimeError("No embeddings — run `winetone build embeddings`")

    dense_sims_arr = dense_vecs @ target
    dense_score = dict(zip(dense_ids, dense_sims_arr.tolist(), strict=False))

    # Sparse side (lexical, full-corpus, fast)
    sparse_score: dict[str, float] = {}
    try:
        # Import here to avoid cost when sparse isn't built.
        from winetone import embed_sparse

        X, vec, wine_to_row = embed_sparse.load_matrix()
        q_sparse = embed_sparse.encode_query(query, vec)
        # Score every wine — we'll combine with dense in the union.
        sims_full = (X @ q_sparse.T).toarray().ravel()
        row_to_wine = {r: w for w, r in wine_to_row.items()}
        for r, s in enumerate(sims_full):
            wid = row_to_wine.get(r)
            if wid is not None:
                sparse_score[wid] = float(s)
    except FileNotFoundError:
        log.info(
            "sparse artifacts missing — using dense only "
            "(alpha forced to 1.0)"
        )
        alpha = 1.0

    # Combine. A wine in only one channel still gets ranked (other
    # channel contributes 0).
    all_ids = set(dense_score) | set(sparse_score)
    hybrid_score = {
        w: alpha * dense_score.get(w, 0.0)
            + (1 - alpha) * sparse_score.get(w, 0.0)
        for w in all_ids
    }

    candidate_n = max(k * 10, 200)
    top_ids = sorted(hybrid_score, key=hybrid_score.get, reverse=True)[:candidate_n]

    placeholders = ",".join(f"'{w}'" for w in top_ids)
    df = pd.read_sql(
        f"""
        SELECT w.wine_id, w.producer_display, w.wine_display, w.vintage,
               w.variety, w.country, w.region,
               f.median_price, f.median_points
        FROM wines w
        LEFT JOIN wine_features f ON f.wine_id = w.wine_id
        WHERE w.wine_id IN ({placeholders})
        """,
        db.engine(),
    )
    df["dense_sim"] = df["wine_id"].map(dense_score).fillna(0.0).astype(float)
    df["sparse_sim"] = df["wine_id"].map(sparse_score).fillna(0.0).astype(float)
    df["similarity"] = df["wine_id"].map(hybrid_score).astype(float)
    df = df.sort_values("similarity", ascending=False)

    if filters:
        # Range filters live on median_price (special-cased); other keys
        # remain equality on the wines columns.
        if "max_price" in filters and filters["max_price"] is not None:
            df = df[df["median_price"].notna()
                    & (df["median_price"] <= float(filters["max_price"]))]
        if "min_price" in filters and filters["min_price"] is not None:
            df = df[df["median_price"].notna()
                    & (df["median_price"] >= float(filters["min_price"]))]
        for col, val in filters.items():
            if col in ("max_price", "min_price"):
                continue
            if col in df.columns and val is not None:
                df = df[df[col] == val]
    return df.head(k).reset_index(drop=True)


def find_alternatives(
    reference_wine_id: str,
    k: int = 10,
    max_price: float | None = None,
    min_savings_pct: float | None = None,
) -> pd.DataFrame:
    """Find wines closest in embedding space to a reference wine —
    optionally cheaper than it.

    This is the "find me something like Pétrus but under $100" feature.
    The dense embedding has already done the work of grouping wines by
    flavor / style; we just sort by cosine to the reference and apply
    a price ceiling.

    Args:
        reference_wine_id: the wine to find alternatives to.
        k: how many to return.
        max_price: absolute cap in USD (median_price ≤ max_price).
        min_savings_pct: alternative cap as a fraction of reference price
            (0.5 means "at least 50% cheaper than the reference"). If
            both this and max_price are given, both must hold.

    Returns a DataFrame with similarity, median_price, savings columns.
    """
    dense_ids, dense_vecs = embed.load_embeddings()
    if len(dense_ids) == 0:
        raise RuntimeError("No embeddings — run `winetone build embeddings`")
    id_to_idx = {wid: i for i, wid in enumerate(dense_ids)}
    if reference_wine_id not in id_to_idx:
        raise ValueError(
            f"no embedding for wine_id={reference_wine_id!r} — "
            "it may not be in the embedded sample"
        )

    import numpy as np
    ref_vec = dense_vecs[id_to_idx[reference_wine_id]]
    sims = (dense_vecs @ ref_vec).astype(float)
    # Pull a generous candidate pool so price filters can survive.
    candidate_n = max(k * 30, 500)
    top_indices = np.argsort(-sims)[:candidate_n]
    top_ids = [dense_ids[i] for i in top_indices]

    placeholders = ",".join(f"'{w}'" for w in top_ids)
    df = pd.read_sql(
        f"""
        SELECT w.wine_id, w.producer_display, w.wine_display, w.vintage,
               w.variety, w.country, w.region,
               f.median_price, f.median_points
        FROM wines w
        LEFT JOIN wine_features f ON f.wine_id = w.wine_id
        WHERE w.wine_id IN ({placeholders})
        """,
        db.engine(),
    )
    sim_map = {dense_ids[i]: float(sims[i]) for i in top_indices}
    df["similarity"] = df["wine_id"].map(sim_map).astype(float)

    # Drop the reference itself.
    df = df[df["wine_id"] != reference_wine_id]

    # Reference price (for savings %).
    ref_row = pd.read_sql(
        f"SELECT median_price FROM wine_features WHERE wine_id = '{reference_wine_id}'",
        db.engine(),
    )
    ref_price = float(ref_row["median_price"].iloc[0]) if (
        not ref_row.empty and pd.notna(ref_row["median_price"].iloc[0])
    ) else None

    df["savings"] = (
        df["median_price"].apply(
            lambda p: None if (p is None or pd.isna(p) or ref_price is None)
            else (ref_price - p) / ref_price
        )
    )

    # Price filters.
    if max_price is not None:
        df = df[df["median_price"].notna()
                & (df["median_price"] <= float(max_price))]
    if min_savings_pct is not None and ref_price is not None:
        threshold = ref_price * (1 - float(min_savings_pct))
        df = df[df["median_price"].notna() & (df["median_price"] <= threshold)]

    df = df.sort_values("similarity", ascending=False)
    return df.head(k).reset_index(drop=True)
