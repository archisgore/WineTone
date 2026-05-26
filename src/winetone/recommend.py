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
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

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
        "user_audit_log": """
            CREATE TABLE user_audit_log (
                event_id      UUID PRIMARY KEY,
                user_id       UUID,
                clerk_user_id TEXT,
                event_at      TIMESTAMP NOT NULL,
                event_type    TEXT NOT NULL,
                field         TEXT,
                old_value     TEXT,
                new_value     TEXT,
                source        TEXT NOT NULL,
                request_id    TEXT
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


# --- display-name helpers + audit log -----------------------------------

# Pattern for "synthetic" display_names — what _resolve_user emits when
# Clerk hands us no username and the Backend API call falls back. We
# refuse to let a synthetic name clobber a real one.
_SYNTHETIC_NAME_RE = re.compile(r"^user_[a-z0-9]{8}$")


def is_synthetic_display_name(name: str | None) -> bool:
    """True if `name` looks like a fallback-generated placeholder."""
    return bool(_SYNTHETIC_NAME_RE.fullmatch(name or ""))


def synthesize_display_name(clerk_user_id: str, email: str = "") -> str:
    """Pick the best display_name we can when Clerk gave us nothing.

    Priority:
      1. The local-part of the email, stripped of plus-addressing and
         sanitized to a reasonable charset.
      2. Last-resort synthetic `user_<8-hex>` derived from the
         clerk_user_id. Recognizable as a fallback so the
         no-clobber rule can detect it.
    """
    if email and "@" in email:
        local = email.split("@", 1)[0].split("+", 1)[0]
        local = re.sub(r"[^A-Za-z0-9_.-]", "", local)[:32]
        if local and not is_synthetic_display_name(local):
            return local
    suffix = clerk_user_id.removeprefix("user_")[:8].lower()
    return f"user_{suffix}"


def log_user_event(
    *,
    user_id: str | None,
    clerk_user_id: str | None,
    event_type: str,
    field: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    source: str,
    request_id: str | None = None,
) -> None:
    """Append a row to user_audit_log. Best-effort: never raises.

    Audit writes must not block sign-in or break account flows. If the
    table is missing or the insert fails for any reason, we log a
    warning and return — the call site continues. This is the
    audit-log discipline: visible-on-success, soundless-on-failure.
    """
    try:
        with db.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO user_audit_log "
                    "(event_id, user_id, clerk_user_id, event_at, event_type, "
                    " field, old_value, new_value, source, request_id) "
                    "VALUES (:eid, :uid, :cid, :ts, :et, :f, :ov, :nv, :s, :rid)"
                ),
                {
                    "eid": str(uuid.uuid4()),
                    "uid": user_id,
                    "cid": clerk_user_id,
                    "ts": datetime.utcnow(),
                    "et": event_type,
                    "f": field,
                    "ov": old_value,
                    "nv": new_value,
                    "s": source,
                    "rid": request_id,
                },
            )
    except Exception as e:  # noqa: BLE001
        log.warning("audit log write failed (%s): %s", event_type, e)


def get_or_create_user_for_clerk(
    clerk_user_id: str,
    display_name: str,
    email: str = "",
    request_id: str | None = None,
) -> str:
    """Find the internal user_id for a Clerk identity, creating the row
    if this is the first time we've seen them.

    Resolution order:

    1. Look up by clerk_user_id (the stable JWT `sub`). Hit ⟹ that's
       the user; refresh display_name if Clerk changed it. Return.
    2. Miss. Look up by email. Hit ⟹ same person via a different
       Clerk instance (typical when test→prod is promoted). Merge:
       update that row's clerk_user_id to the new one, refresh
       display_name. Return existing user_id.
    3. Still miss. Try to create a new row. If display_name collides
       (somebody else owns the name), suffix it with `-2`, `-3`, ...
       until it's unique. Doing this here keeps sign-in unblocked
       even when Clerk happens to assign a colliding username.

    The merge behavior is what makes the test→prod Clerk migration
    transparent: signing in with the production instance for the
    first time after a test-instance sign-in preserves your labels,
    follow graph, and projection.
    """
    # Ensure the schema (including user_audit_log) exists before any
    # write happens. `init_user_schema` is idempotent and cheap on
    # warm DBs (single information_schema lookup per missing table).
    init_user_schema()
    with db.connect() as conn:
        row = conn.execute(
            text("SELECT user_id, display_name FROM users WHERE clerk_user_id = :c"),
            {"c": clerk_user_id},
        ).fetchone()
        if row:
            existing_uid, existing_name = row
            if existing_name != display_name:
                # No-clobber rule: a real display_name (anything that
                # doesn't match the synthetic fallback pattern) must
                # never be overwritten by a synthetic one. Today's
                # incident — archisgore → user_3e5etskk — was exactly
                # this clobber happening unguarded.
                if (is_synthetic_display_name(display_name)
                        and not is_synthetic_display_name(existing_name)):
                    log.warning(
                        "REFUSING to clobber %r with synthetic %r for user=%s",
                        existing_name, display_name, existing_uid,
                    )
                    log_user_event(
                        user_id=str(existing_uid),
                        clerk_user_id=clerk_user_id,
                        event_type="display_name_clobber_blocked",
                        field="display_name",
                        old_value=existing_name,
                        new_value=display_name,
                        source="auth_flow",
                        request_id=request_id,
                    )
                else:
                    try:
                        conn.execute(
                            text("UPDATE users SET display_name = :n WHERE user_id = :u"),
                            {"n": display_name, "u": existing_uid},
                        )
                        log_user_event(
                            user_id=str(existing_uid),
                            clerk_user_id=clerk_user_id,
                            event_type="display_name_changed",
                            field="display_name",
                            old_value=existing_name,
                            new_value=display_name,
                            source="auth_flow",
                            request_id=request_id,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "could not rename user %s to %r: %s",
                            existing_uid, display_name, e,
                        )
            return str(existing_uid)

        # No clerk_user_id match. Maybe this person already exists
        # under a different clerk_user_id (e.g., promoted from test
        # Clerk → prod Clerk, which uses a fresh user ID namespace).
        # Match on email if we have one.
        if email:
            row = conn.execute(
                text("SELECT user_id, display_name FROM users WHERE email = :e"),
                {"e": email},
            ).fetchone()
            if row:
                existing_uid, existing_name = row
                # No-clobber for the merge path too — preserve a real
                # display_name when relinking by email.
                merged_name = (
                    existing_name
                    if (is_synthetic_display_name(display_name)
                        and not is_synthetic_display_name(existing_name))
                    else display_name
                )
                conn.execute(
                    text(
                        "UPDATE users SET clerk_user_id = :c, "
                        "                 display_name = :n "
                        "          WHERE user_id = :u"
                    ),
                    {"c": clerk_user_id, "n": merged_name, "u": existing_uid},
                )
                log.info(
                    "merged Clerk identity: user %s (email=%s) now linked to clerk=%s "
                    "(was: %s)", existing_uid, email, clerk_user_id, existing_name,
                )
                log_user_event(
                    user_id=str(existing_uid),
                    clerk_user_id=clerk_user_id,
                    event_type="clerk_id_relinked",
                    field="clerk_user_id",
                    old_value=None,  # we don't store the old one separately
                    new_value=clerk_user_id,
                    source="auth_flow",
                    request_id=request_id,
                )
                if merged_name != existing_name:
                    log_user_event(
                        user_id=str(existing_uid),
                        clerk_user_id=clerk_user_id,
                        event_type="display_name_changed",
                        field="display_name",
                        old_value=existing_name,
                        new_value=merged_name,
                        source="auth_flow",
                        request_id=request_id,
                    )
                return str(existing_uid)

        # Genuinely new person. Try to insert; suffix display_name on
        # collision until it lands. The retry caps at 50 to guard
        # against pathological loops.
        user_id = str(uuid.uuid4())
        chosen_name = display_name
        for attempt in range(1, 50):
            try:
                conn.execute(
                    text(
                        "INSERT INTO users (user_id, clerk_user_id, display_name, "
                        "email, created_at) "
                        "VALUES (:u, :c, :n, :e, :t)"
                    ),
                    {
                        "u": user_id, "c": clerk_user_id,
                        "n": chosen_name, "e": email,
                        "t": datetime.utcnow(),
                    },
                )
                log.info("created user %s (clerk=%s, name=%s)",
                         user_id, clerk_user_id, chosen_name)
                log_user_event(
                    user_id=user_id,
                    clerk_user_id=clerk_user_id,
                    event_type="created",
                    field=None,
                    old_value=None,
                    new_value=chosen_name,
                    source="auth_flow",
                    request_id=request_id,
                )
                return user_id
            except IntegrityError as e:
                # Collision on display_name — try the next suffix. Any
                # OTHER unique-violation (e.g. clerk_user_id) is fatal.
                msg = str(e.orig if hasattr(e, "orig") else e)
                if "display_name" not in msg:
                    raise
                attempt_n = attempt + 1
                chosen_name = f"{display_name}-{attempt_n}"
                log.info(
                    "display_name=%r taken; retrying as %r",
                    display_name if attempt == 1 else f"{display_name}-{attempt}",
                    chosen_name,
                )
        # If we somehow exhaust 50 suffixes, raise rather than infinite-loop.
        raise RuntimeError(
            f"could not find an unused suffix for display_name={display_name!r} "
            f"after 50 attempts"
        )


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


def add_label(
    user_id: str,
    wine_id: str,
    description: str,
    sentiment: str = "positive",
) -> None:
    """Record (or update) a (user, wine, description, sentiment) tuple.

    Exactly one label per (user_id, wine_id) — re-labelling the same
    wine overwrites the previous description and sentiment instead of
    creating a duplicate row. ON CONFLICT requires the unique index
    `user_labels_user_wine_uniq` (added in migration 20260522_002).

    `sentiment`: 'positive' (default — "this wine tastes like my description"),
    'negative' ("I described this wine — and I don't want more of it"), or
    'neutral' (no preference signal — just vocabulary calibration).
    """
    from datetime import datetime

    from winetone import moderation
    s = (sentiment or "positive").lower()
    if s not in ("positive", "negative", "neutral"):
        raise ValueError(f"invalid sentiment {sentiment!r}")
    moderation.screen(description, kind="label")
    with db.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO user_labels "
                "(user_id, wine_id, description, sentiment, created_at) "
                "VALUES (:u, :w, :d, :s, :t) "
                "ON CONFLICT (user_id, wine_id) DO UPDATE SET "
                "    description = EXCLUDED.description, "
                "    sentiment   = EXCLUDED.sentiment, "
                "    created_at  = EXCLUDED.created_at"
            ),
            {"u": user_id, "w": wine_id, "d": description,
             "s": s, "t": datetime.utcnow()},
        )
    # Refresh the vocab-search index for this label. encode_and_store
    # also prunes stale embedding rows for the same (user, wine) that
    # no longer match the current description.
    from winetone import embed_user_labels
    embed_user_labels.encode_and_store(user_id, wine_id, description)


def delete_label(user_id: str, wine_id: str) -> int:
    """Remove a (user_id, wine_id) row from user_labels along with any
    associated embedding rows. Returns the number of label rows
    deleted (0 if the label didn't exist).

    Idempotent — deleting a non-existent label is a no-op, not an
    error. The follow-up projection won't be re-fit automatically;
    callers should re-call `calibrate.fit()` if they want the
    projection updated to reflect the deletion.
    """
    with db.connect() as conn:
        # Drop any embedding rows first so the FK-less side stays
        # consistent (we don't have a real FK on user_label_embeddings
        # to user_labels, but the join semantics require pruning).
        conn.execute(
            text(
                "DELETE FROM user_label_embeddings "
                "WHERE user_id = :u AND wine_id = :w"
            ),
            {"u": user_id, "w": wine_id},
        )
        result = conn.execute(
            text(
                "DELETE FROM user_labels "
                "WHERE user_id = :u AND wine_id = :w"
            ),
            {"u": user_id, "w": wine_id},
        )
    return int(getattr(result, "rowcount", 0) or 0)


def get_labels(user_id: str) -> pd.DataFrame:
    """Return all labels this user has provided (incl. sentiment)."""
    return pd.read_sql(
        text(
            "SELECT user_id, wine_id, description, sentiment "
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


# --- self-serve username rename ----------------------------------------


# Reserved names — can't be claimed by ordinary users.
_RESERVED_DISPLAY_NAMES = frozenset({
    "admin", "administrator", "root", "winetone", "wine-tone",
    "support", "help", "system", "moderator", "mod", "staff",
    "anonymous", "guest", "user", "me", "self", "you",
    "api", "www", "mail", "ftp", "smtp",
})

# Reserved substrings — any name containing one of these (case-
# insensitively) is rejected unless the requester is in the
# corresponding owner-allowlist. Today's only entry: "archis" is
# reserved for the founder.
_RESERVED_SUBSTRINGS: dict[str, str] = {
    # substring → env-var name carrying the owner's clerk_user_id
    "archis": "WINETONE_NAME_OWNER_ARCHIS_CLERK_ID",
}


def validate_display_name(
    name: str,
    *,
    requester_clerk_user_id: str | None = None,
) -> str:
    """Normalize and validate a candidate display_name.

    Returns the normalized name on success, raises ValueError on
    failure (with a user-readable message — these surface directly
    in the rename UI).

    `requester_clerk_user_id` lets the founder bypass the
    "archis"-substring reservation when set to his own clerk_id via
    `WINETONE_NAME_OWNER_ARCHIS_CLERK_ID`. Anyone else gets the
    name rejected.
    """
    n = (name or "").strip()
    if not n:
        raise ValueError("Username can't be empty.")
    if len(n) < 2:
        raise ValueError("Username must be at least 2 characters.")
    if len(n) > 32:
        raise ValueError("Username must be at most 32 characters.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", n):
        raise ValueError(
            "Username can use letters, numbers, and . _ - only."
        )
    if is_synthetic_display_name(n):
        raise ValueError(
            "That looks like an auto-generated placeholder — pick a real name."
        )
    lower = n.lower()
    if lower in _RESERVED_DISPLAY_NAMES:
        raise ValueError(f"Username {n!r} is reserved.")
    for substr, env_var in _RESERVED_SUBSTRINGS.items():
        if substr in lower:
            allowed_owner = os.environ.get(env_var, "").strip()
            if allowed_owner and allowed_owner == requester_clerk_user_id:
                continue  # owner is allowed to use their own substring
            raise ValueError(
                f"Username containing {substr!r} is reserved."
            )
    return n


def rename_user(
    user_id: str,
    new_display_name: str,
    *,
    requester_clerk_user_id: str | None = None,
    source: str = "self_serve",
    request_id: str | None = None,
) -> str:
    """Change a user's display_name. Self-serve from the profile page.

    Validates the new name, checks for collisions (case-insensitive),
    performs the UPDATE, and writes an audit-log entry. Raises
    ValueError on validation failure or collision; the route surfaces
    the message back to the user verbatim.

    `requester_clerk_user_id` is passed through to
    `validate_display_name` so reserved-substring exemptions (e.g.,
    the founder's "archis" reservation) can be honored.

    Returns the actually-applied display_name (so the caller can
    redirect to /u/<new_name>).
    """
    with db.connect() as conn:
        cur = conn.execute(
            text("SELECT user_id, display_name, clerk_user_id "
                 "  FROM users WHERE user_id = :u"),
            {"u": user_id},
        ).fetchone()
        if cur is None:
            raise ValueError("User not found.")
        old_name = cur.display_name
        clerk_uid = cur.clerk_user_id
    new_name = validate_display_name(
        new_display_name,
        requester_clerk_user_id=requester_clerk_user_id or clerk_uid,
    )
    with db.connect() as conn:
        if old_name == new_name:
            return old_name  # no-op
        # Collision check — case-insensitive, excludes self.
        clash = conn.execute(
            text("SELECT 1 FROM users "
                 " WHERE LOWER(display_name) = LOWER(:n) "
                 "   AND user_id != :u LIMIT 1"),
            {"n": new_name, "u": user_id},
        ).first()
        if clash:
            raise ValueError(f"Username {new_name!r} is already taken.")
        conn.execute(
            text("UPDATE users SET display_name = :n WHERE user_id = :u"),
            {"n": new_name, "u": user_id},
        )
    log_user_event(
        user_id=str(user_id),
        clerk_user_id=clerk_uid,
        event_type="display_name_changed",
        field="display_name",
        old_value=old_name,
        new_value=new_name,
        source=source,
        request_id=request_id,
    )
    log.info("rename: user=%s %r → %r (source=%s)",
             user_id, old_name, new_name, source)
    return new_name


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


# --- query-time projection helper -----------------------------------------


def _project_query(user_id: str, L_q: np.ndarray) -> np.ndarray:
    """Apply the per-user projection to a query embedding.

    With env flag ``WINETONE_USE_MLP=1`` AND a row in
    `user_projections_mlp` for this user, uses the MLP from
    `winetone.calibrate_mlp`. Otherwise — flag unset, MLP load fails,
    or no MLP row — falls through to the existing linear `A · L + b`.

    The env-flag indirection lets us A/B the MLP against the linear
    projection in production without redeploying: flip the flag in
    HF Spaces secrets and the next request picks the new path.
    """
    if os.environ.get("WINETONE_USE_MLP") == "1":
        try:
            from winetone import calibrate_mlp
            loaded = calibrate_mlp.load_for_user(user_id)
            if loaded is not None:
                log.info(
                    "recommend: MLP projection user=%s n=%d arch=%s",
                    user_id, loaded.n_labels, loaded.arch_id,
                )
                return calibrate_mlp.apply_projection(L_q, loaded)
        except Exception:  # noqa: BLE001
            log.exception(
                "MLP projection failed for user=%s — falling back to linear",
                user_id,
            )

    proj = load_projection(user_id)
    return L_q if proj is None else proj.apply(L_q)


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
    # Dense side (with optional user personalization).
    L_q = embed.encode_query(query)
    target = _project_query(user_id, L_q) if user_id else L_q

    dense_ids, dense_vecs = embed.load_embeddings()
    if len(dense_ids) == 0:
        raise RuntimeError("No embeddings — run `winetone build embeddings`")

    dense_sims_arr = dense_vecs @ target
    dense_score = dict(zip(dense_ids, dense_sims_arr.tolist(), strict=False))

    # Sparse side — Postgres FTS via `wines.tsv` and `ts_rank`.
    # Returns up to 200 lexical hits, scores normalized to [0, 1].
    # Non-matching wines aren't in the dict; the hybrid merge treats
    # those as 0 — which is exactly the right behavior.
    from winetone import lexical
    sparse_score = lexical.score_candidates(query, limit=200)

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


def explain_recommendations(
    user_id: str,
    wine_ids: list[str],
) -> dict[str, str]:
    """For each recommended wine_id, compose a one-sentence reason
    grounded in the user's own labels.

    Strategy: for each recommended wine, find the user's positive
    label whose wine embedding is most similar. Compose a sentence
    quoting that label's description and producer. If the user has
    no positive labels with embeddings, returns an empty dict (the
    caller falls back to no explanation rather than an awkward one).
    """
    if not wine_ids:
        return {}
    # Pull the user's positive labels along with the wines' embeddings.
    labels_df = pd.read_sql(
        text("""
            SELECT l.wine_id AS label_wine_id, l.description,
                   w.producer_display, w.wine_display, w.vintage,
                   we.embedding
              FROM user_labels l
              JOIN wines w ON w.wine_id = l.wine_id
              JOIN wine_embeddings we ON we.wine_id = l.wine_id
             WHERE l.user_id = :u
               AND l.sentiment = 'positive'
        """),
        db.engine(), params={"u": user_id},
    )
    if labels_df.empty:
        return {}
    # Parse pgvector strings into numpy arrays.
    label_vecs = []
    keep_rows = []
    for i, raw in enumerate(labels_df["embedding"]):
        if isinstance(raw, str):
            v = np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32)
        else:
            v = np.asarray(raw, dtype=np.float32)
        if v.shape[0] != embed.EMBEDDING_DIM:
            continue
        v = v / (np.linalg.norm(v) + 1e-9)
        label_vecs.append(v)
        keep_rows.append(i)
    if not label_vecs:
        return {}
    labels_df = labels_df.iloc[keep_rows].reset_index(drop=True)
    L = np.stack(label_vecs)
    # Now pull the recommended wines' embeddings.
    placeholders = ",".join(f"'{w}'" for w in wine_ids)
    rec_df = pd.read_sql(
        f"SELECT wine_id, embedding FROM wine_embeddings "
        f"WHERE wine_id IN ({placeholders})",
        db.engine(),
    )
    rec_vec_by_id: dict[str, np.ndarray] = {}
    for _, row in rec_df.iterrows():
        raw = row["embedding"]
        if isinstance(raw, str):
            v = np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32)
        else:
            v = np.asarray(raw, dtype=np.float32)
        if v.shape[0] != embed.EMBEDDING_DIM:
            continue
        rec_vec_by_id[row["wine_id"]] = v / (np.linalg.norm(v) + 1e-9)

    out: dict[str, str] = {}
    for rec_id in wine_ids:
        rv = rec_vec_by_id.get(rec_id)
        if rv is None:
            continue
        sims = L @ rv
        best = int(np.argmax(sims))
        row = labels_df.iloc[best]
        desc = (row["description"] or "").strip()
        # Trim long descriptions to keep the explanation legible.
        if len(desc) > 80:
            desc = desc[:77].rstrip() + "…"
        producer = row["producer_display"] or "a wine you labelled"
        vintage = row["vintage"]
        anchor = producer
        if vintage and not pd.isna(vintage):
            anchor = f"{producer} ({int(vintage)})"
        out[rec_id] = (
            f"Recommended because you described {anchor} "
            f"as “{desc}” — this wine sits in the same "
            "neighbourhood of your palate space."
        )
    return out


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
