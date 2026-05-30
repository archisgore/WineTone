"""Per-user MLP projection.

Drop-in replacement for the linear `A·L + b` from
`winetone.calibrate`. Same input/output dimensionality (384 ↔ 384,
the bge-small embedding space), but with a hidden non-linearity so
it can capture user vocabulary patterns the linear projection
can't — e.g. "popcorn-butter only means buttery *for Chardonnay*",
"shallow means thin *for reds* but means crisp *for whites*".

Architecture
------------
    L (384)
      │
      ├──────────────────── residual ───┐
      │                                  │
      Linear(384, HIDDEN)               │
      GELU                              │
      Dropout(p)                        │
      Linear(HIDDEN, 384)               │
      │                                  │
      └─────────── + ───────────────────┘
                  ▼
              MLP_out (384)

The residual path is critical: at init, the final linear's weights
are scaled near zero, so `MLP_out ≈ L` for a cold user. This matches
the linear projection's identity prior — a user with no labels gets
generic search behavior. As they label, the deltas accumulate in
the MLP branch and the projection drifts.

Tiny per-user data
------------------
We typically have 5-30 labels per user. That's ~100K input scalars
against ~100K parameters in the MLP — a classic over-parameterization
risk. Defenses:

1. Heavy weight decay (`WD`) on the MLP weights.
2. Dropout in the hidden layer.
3. Conservative epoch count (~50) and a relatively low LR (~0.01).
4. Identity-residual structure means the model can "do nothing" and
   still produce a valid projection.

In tests with 5 labels, this configuration learns small, sensible
drifts without exploding to memorize each label exactly.

Storage
-------
Serialized `state_dict` (via `torch.save` into a BytesIO) lives in
`user_projections_mlp.weights BYTEA`. Per-user rows. The runtime
loads the state_dict, instantiates a fresh MLP, and runs forward on
the encoded query.

Backward compatibility
----------------------
This module does NOT touch `user_projections` (the linear table).
The MLP is wired in alongside the linear projection. The recommender
will check `user_projections_mlp` first; if absent, it falls back to
the linear `A · L + b`. That gives us a clean cutover with a
rollback at runtime if the MLP misbehaves for any user.
"""
from __future__ import annotations

import hashlib
import io
import logging
import uuid
from dataclasses import dataclass

import numpy as np
from sqlalchemy import text

from winetone import db, embed
from winetone.calibrate import (
    LR,
    EPOCHS,
    NEG_MARGIN_SQ,
    _load_user_pairs,
    detect_backend,
)

log = logging.getLogger(__name__)

# Architecture / training hyperparams. Tuned conservatively for the
# tiny-data regime — explicitly NOT tuned for a 10K-label-per-user
# future. Re-tune when corpora grow.
HIDDEN_DIM = 128
DROPOUT_P = 0.3
WD = 1e-2            # AdamW weight decay
MLP_EPOCHS = 80
MLP_LR = 1e-2

# When initializing the second linear, scale its weights down so the
# residual term dominates at start. With this near-zero init, the
# MLP branch contributes < 1% of the output magnitude until trained.
INIT_OUT_SCALE = 1e-3


# --- model definition --------------------------------------------------


def _build_mlp(dim: int) -> "torch.nn.Module":  # noqa: UP037
    """Construct a fresh MLP with the standard architecture."""
    import torch
    import torch.nn as nn

    class ProjectionMLP(nn.Module):
        def __init__(self, dim: int) -> None:
            super().__init__()
            self.fc1 = nn.Linear(dim, HIDDEN_DIM)
            self.act = nn.GELU()
            self.drop = nn.Dropout(DROPOUT_P)
            self.fc2 = nn.Linear(HIDDEN_DIM, dim)
            # Near-zero init on fc2 so residual dominates at start.
            with torch.no_grad():
                self.fc2.weight.mul_(INIT_OUT_SCALE)
                self.fc2.bias.zero_()

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":  # noqa: UP037
            h = self.fc1(x)
            h = self.act(h)
            h = self.drop(h)
            h = self.fc2(h)
            return x + h  # residual

    return ProjectionMLP(dim)


# --- schema ------------------------------------------------------------


def init_mlp_schema() -> None:
    """Create user_projections_mlp if it doesn't exist. Idempotent.

    Follows the CedarDB-safe pattern (no `IF NOT EXISTS`, no DEFAULT
    NOW()) for backward compatibility with the rest of the schema —
    Neon accepts both styles but matching the existing code keeps
    one less footgun.
    """
    if not db.ping():
        raise RuntimeError("DB unreachable")
    with db.engine().connect() as conn:
        existing = {
            r[0] for r in conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )).fetchall()
        }
        if "user_projections_mlp" not in existing:
            # user_id is UUID to match users / user_labels /
            # user_projections elsewhere in the schema. Storing as TEXT
            # would cause JOIN type-mismatch errors at retrain time.
            conn.execute(text("""
                CREATE TABLE user_projections_mlp (
                    user_id    UUID PRIMARY KEY,
                    n_labels   INTEGER NOT NULL,
                    weights    BYTEA NOT NULL,
                    fit_at     TIMESTAMP NOT NULL,
                    loss       DOUBLE PRECISION,
                    arch_id    TEXT NOT NULL,
                    labels_sig TEXT NOT NULL
                )
            """))
            conn.commit()
            log.info("created table user_projections_mlp")
        else:
            # Idempotent migration: older table may lack labels_sig.
            cols = {
                r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'user_projections_mlp'"
                )).fetchall()
            }
            if "labels_sig" not in cols:
                conn.execute(text(
                    "ALTER TABLE user_projections_mlp "
                    "ADD COLUMN labels_sig TEXT NOT NULL DEFAULT ''"
                ))
                conn.commit()
                log.info("added column user_projections_mlp.labels_sig")


# --- training ----------------------------------------------------------


def _fit_torch_mlp(
    L: np.ndarray, W: np.ndarray, sign: np.ndarray, weight: np.ndarray,
    device: str,
) -> tuple[bytes, float]:
    """Train the MLP and return (serialized state_dict, final_loss).

    Sign-aware loss, mirroring the linear `_fit_torch` in
    calibrate.py:
        sign=+1 → minimize weighted ||MLP(L) - W||²
        sign=-1 → maximize squared distance up to NEG_MARGIN_SQ
                  (i.e., hinge loss in distance space)

    AdamW with weight_decay on the MLP weights penalizes the *delta*
    from the residual identity. With near-zero fc2 init, this
    effectively shrinks toward "do nothing", which is what we want.
    """
    import torch

    dim = L.shape[1]
    dev = torch.device(device)
    L_t = torch.from_numpy(L).to(dev)
    W_t = torch.from_numpy(W).to(dev)
    sign_t = torch.from_numpy(sign).to(dev)
    weight_t = torch.from_numpy(weight).to(dev)
    pos_mask = sign_t > 0
    neg_mask = sign_t < 0
    pos_w = weight_t * pos_mask.float()
    neg_w = weight_t * neg_mask.float()
    pos_w_sum = pos_w.sum().clamp(min=1e-6)
    neg_w_sum = neg_w.sum().clamp(min=1e-6)

    model = _build_mlp(dim).to(dev)
    model.train()
    # AdamW applies weight_decay correctly (decoupled from LR).
    optim = torch.optim.AdamW(
        model.parameters(), lr=MLP_LR, weight_decay=WD
    )

    final_loss = float("nan")
    for epoch in range(MLP_EPOCHS):
        optim.zero_grad()
        pred = model(L_t)
        sq_dist = torch.sum((pred - W_t) ** 2, dim=1)
        mse_pos = (sq_dist * pos_w).sum() / pos_w_sum
        hinge = torch.clamp(NEG_MARGIN_SQ - sq_dist, min=0.0)
        mse_neg = (hinge * neg_w).sum() / neg_w_sum
        loss = mse_pos + mse_neg
        loss.backward()
        optim.step()
        if epoch == 0 or (epoch + 1) % 20 == 0:
            log.info(
                "  mlp epoch %3d · loss=%.5f (pos=%.5f, neg=%.5f)",
                epoch + 1, loss.item(), mse_pos.item(), mse_neg.item(),
            )
        final_loss = loss.item()

    # Serialize state_dict to bytes for DB storage.
    buf = io.BytesIO()
    torch.save(model.state_dict(), buf)
    return buf.getvalue(), final_loss


def fit(user_id: str, backend: str | None = None) -> dict[str, object]:
    """Train and persist an MLP projection for one user.

    Args:
        user_id: canonical user_id.
        backend: 'torch-cpu' | 'torch-cuda' | 'torch-mps' | None
                 (auto). MLX path is intentionally NOT supported for
                 the MLP yet — the dense linear-algebra advantage
                 isn't material at this model size, and PyTorch keeps
                 the path consistent with the GHA runner (no Apple
                 Silicon in cloud CI).

    Side effects:
        - Inserts/replaces a row in `user_projections_mlp`.
    """
    if not db.ping():
        raise RuntimeError("DB unreachable")
    init_mlp_schema()

    if backend is None:
        b = detect_backend()
        # MLX → torch-cpu fallback in GHA-compatible mode.
        backend = b if b.startswith("torch-") else "torch-cpu"
    if not backend.startswith("torch-"):
        raise ValueError(f"MLP fit requires a torch backend, got {backend}")
    device = backend.split("-", 1)[1]

    L, W, sign, weight = _load_user_pairs(user_id)
    n = len(L)
    if n == 0:
        raise RuntimeError(f"user {user_id} has no usable labels")

    log.info(
        "calibrate_mlp.fit user=%s backend=%s n=%d (dim=%d, hidden=%d, "
        "dropout=%.2f, wd=%g, epochs=%d, lr=%g)",
        user_id, backend, n, embed.EMBEDDING_DIM, HIDDEN_DIM,
        DROPOUT_P, WD, MLP_EPOCHS, MLP_LR,
    )

    state_bytes, final_loss = _fit_torch_mlp(L, W, sign, weight, device=device)

    arch_id = f"mlp-residual-h{HIDDEN_DIM}-d{int(DROPOUT_P*100)}"
    labels_sig = _compute_user_labels_sig(user_id)
    from winetone.recommend import _masked_id_in_conn
    with db.engine().connect() as conn:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        masked_uid = _masked_id_in_conn(conn, user_id)
        # PRIMARY KEY (user_id) means upsert via DELETE+INSERT — the
        # explicit-no-IF-EXISTS pattern matches the rest of the
        # codebase's CedarDB-safe writes.
        conn.execute(text(
            "DELETE FROM user_projections_mlp WHERE user_id = :uid"
        ), {"uid": user_id})
        conn.execute(text("""
            INSERT INTO user_projections_mlp
                (user_id, masked_user_id, n_labels, weights, fit_at,
                 loss, arch_id, labels_sig)
            VALUES (:uid, :mid, :n, :w, :ts, :loss, :arch, :sig)
        """), {
            "uid": user_id, "mid": masked_uid, "n": n, "w": state_bytes,
            "ts": now, "loss": final_loss, "arch": arch_id,
            "sig": labels_sig,
        })
        conn.commit()

    log.info(
        "mlp fit complete · user=%s · loss=%.5f · bytes=%d · arch=%s",
        user_id, final_loss, len(state_bytes), arch_id,
    )
    return {
        "user_id": user_id,
        "n_labels": n,
        "loss_final": final_loss,
        "backend": backend,
        "arch_id": arch_id,
        "weights_bytes": len(state_bytes),
    }


# --- query-time application -------------------------------------------


@dataclass
class LoadedMLP:
    """Result of `load_for_user` — ready to apply to a query embedding."""
    model: object  # torch.nn.Module
    n_labels: int
    arch_id: str


def load_for_user(user_id: str) -> LoadedMLP | None:
    """Load a user's persisted MLP. Returns None if no row exists."""
    import torch
    with db.engine().connect() as conn:
        row = conn.execute(text(
            "SELECT weights, n_labels, arch_id "
            "FROM user_projections_mlp WHERE user_id = :uid"
        ), {"uid": user_id}).first()
    if row is None:
        return None
    state = torch.load(io.BytesIO(row.weights), weights_only=True)
    model = _build_mlp(embed.EMBEDDING_DIM)
    model.load_state_dict(state)
    model.eval()
    return LoadedMLP(model=model, n_labels=row.n_labels, arch_id=row.arch_id)


def apply_projection(query_emb: np.ndarray, loaded: LoadedMLP) -> np.ndarray:
    """Apply a loaded MLP to a query embedding (numpy, 384)."""
    import torch
    with torch.no_grad():
        x = torch.from_numpy(query_emb.astype(np.float32)).unsqueeze(0)
        y = loaded.model(x).squeeze(0).numpy()
    return y


# --- batch refit (for cron orchestration) -----------------------------


def _compute_user_labels_sig(user_id: str) -> str:
    """Hash of a single user's label set.

    Captures the content of every (wine_id, description) pair *plus*
    the original created_at as a sentinel. Any add / edit / delete on
    that user's labels flips the hash — covering the three mutation
    types the auto-retrain pipeline needs to detect for per-user
    refits.
    """
    with db.engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT wine_id, description, created_at
            FROM user_labels
            WHERE user_id = :uid
            ORDER BY wine_id, created_at
        """), {"uid": user_id}).fetchall()
    h = hashlib.sha256()
    for r in rows:
        h.update(r.wine_id.encode())
        h.update(b"\x1f")
        h.update(r.description.encode())
        h.update(b"\x1f")
        h.update(r.created_at.isoformat().encode())
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


def refit_users_with_changes(min_labels: int = 5) -> dict[str, object]:
    """Refit every user whose label set has changed since their last MLP fit.

    Used by `scripts/retrain.py`. **This is the "auto-refit forgetful
    users" mechanism** — users who add or edit labels via the app
    and never run `winetone calibrate fit` manually get their
    projection refit on the next cron tick. They don't have to
    remember.

    The change check uses each user's labels-signature (the
    `_compute_user_labels_sig` hash) compared against the
    `labels_sig` column saved on the last successful MLP fit. The
    sig flips on add / edit / delete equally — so all three mutation
    types are caught, not just adds.

    Returns a summary dict suitable for the GHA logs / Slack ping.
    """
    init_mlp_schema()
    refit_count = 0
    skipped_count = 0
    failed: list[tuple[str, str]] = []
    user_ids: list[str] = []

    with db.engine().connect() as conn:
        rows = conn.execute(text(f"""
            SELECT u.user_id,
                   COUNT(l.wine_id)        AS n_labels,
                   p.labels_sig            AS last_sig
            FROM users u
            JOIN user_labels l ON l.user_id = u.user_id
            LEFT JOIN user_projections_mlp p ON p.user_id = u.user_id
            GROUP BY u.user_id, p.labels_sig
            HAVING COUNT(l.wine_id) >= {int(min_labels)}
        """)).fetchall()

    for r in rows:
        current_sig = _compute_user_labels_sig(r.user_id)
        needs_refit = (r.last_sig or "") != current_sig
        if not needs_refit:
            skipped_count += 1
            continue
        try:
            fit(r.user_id, backend="torch-cpu")
            refit_count += 1
            user_ids.append(r.user_id)
        except Exception as e:  # noqa: BLE001
            log.exception("MLP fit failed for user=%s", r.user_id)
            failed.append((r.user_id, str(e)))

    return {
        "refit": refit_count,
        "skipped": skipped_count,
        "failed": failed,
        "user_ids": user_ids,
    }


if __name__ == "__main__":  # manual smoke test
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    user = sys.argv[1] if len(sys.argv) > 1 else "archis"
    out = fit(user)
    print(out)
