"""Phase 4 — personalized projection via gradient descent.

A small linear model `A·L + b` regularized toward identity (so a
new user with few labels behaves like the cold-start baseline; each
new label perturbs the projection only where the data demands).

We support **three backend choices**, auto-detected at runtime:

  1. **MLX** — Apple-Silicon-native, fastest on M-series Macs.
     Used when `import mlx.core` succeeds AND we're on `Darwin/arm64`.
  2. **PyTorch CUDA** — used when `torch.cuda.is_available()`.
  3. **PyTorch MPS** — Apple Silicon via PyTorch (fallback if MLX
     isn't installed). Slower than MLX, but the install is one apt-get.
  4. **PyTorch CPU** — universal fallback.

The CLI's `winetone calibrate fit --backend X` overrides auto-detect.
We log which backend ran so the choice is visible.

Both backends are mathematically identical: same objective,
same hyperparameters, same closed-form-equivalent solution at
convergence. The only difference is the inner loop. Switching from
PyTorch to MLX (or vice versa) doesn't change the user's
calibration — it changes the wall-clock cost of computing it.

Tables this writes to:

  user_calibration_history
    (user_id, version, n_labels, backend, A_serialized,
     b_serialized, loss_final, lambda_a, lambda_b, fit_at)

  Append-only: each `fit()` inserts a new version. user_projections
  (in recommend.py) holds the *current* projection used at recommend
  time.
"""

from __future__ import annotations

import logging
import platform
from typing import Literal

import numpy as np
import pandas as pd
from sqlalchemy import text

from winetone import db, embed, recommend

log = logging.getLogger(__name__)

Backend = Literal["mlx", "torch-cuda", "torch-mps", "torch-cpu"]

# Hyperparameters — identical across backends.
LAMBDA_A = 50.0
LAMBDA_B = 5.0
LR = 0.05
EPOCHS = 300

# Negative-label hinge margin. We use squared distance as the loss, so
# this is a *squared* distance. Wine embeddings are L2-normalized
# (max pairwise squared distance = 4 for opposite poles), so margin=2.0
# means "at least the equivalent of a cosine of 0.0 — orthogonal." Past
# that point, the negative label contributes zero loss; before it,
# gradient pushes the projection away from W.
NEG_MARGIN_SQ = 2.0


# --- backend detection --------------------------------------------------


def detect_backend() -> Backend:
    """Return the best available training backend.

    Order of preference:
      1. MLX on Apple-Silicon Mac (native Metal / unified memory)
      2. PyTorch with CUDA (NVIDIA GPU)
      3. PyTorch with MPS (Apple Silicon via PyTorch, fallback if MLX
         isn't installed)
      4. PyTorch CPU (universal fallback)

    If both PyTorch and MLX are missing entirely, raises RuntimeError.
    """
    # Prefer MLX on Apple Silicon when available.
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            import mlx.core as mx
            # Smoke-test that the runtime is functional.
            _ = mx.array([1.0])
            return "mlx"
        except Exception as e:  # noqa: BLE001
            log.debug("mlx not usable: %s", e)

    try:
        import torch
    except ImportError as e:
        raise RuntimeError(
            "No usable ML backend. Install one of:\n"
            "  - mlx        (Apple Silicon: pip install mlx)\n"
            "  - torch      (universal: pip install torch)"
        ) from e

    if torch.cuda.is_available():
        return "torch-cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "torch-mps"
    return "torch-cpu"


def describe_backend(backend: Backend) -> str:
    """Human-readable description of a backend choice."""
    info = {
        "mlx": "Apple MLX (Metal, unified memory)",
        "torch-cuda": "PyTorch CUDA",
        "torch-mps": "PyTorch MPS (Apple Silicon via PyTorch)",
        "torch-cpu": "PyTorch CPU",
    }
    return info.get(backend, str(backend))


# --- data loading ------------------------------------------------------


def _load_user_pairs(
    user_id: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Pull (L, W, sign, weight) for a user's calibration set.

    The training set is `user_id`'s own labels at weight 1.0 PLUS the
    labels of users they follow, each at the relationship's weight
    (default 0.3 — see winetone.social). One level only.

    `sign` is +1.0 for positive labels and -1.0 for negative ones.
    `weight` is a per-sample multiplier on the loss term — own labels
    dominate, follows-labels nudge.
    Neutral labels are filtered out (vocab-only, no preference signal).
    """
    from winetone import social

    labels = social.labels_with_follow_weights(user_id)
    if labels.empty:
        raise RuntimeError(
            f"user {user_id} has no labels (own or via follows) to fit"
        )

    # Drop neutrals from both own and followed labels.
    labels = labels[labels["sentiment"].fillna("positive") != "neutral"]
    if labels.empty:
        raise RuntimeError(
            f"user {user_id} has no positive/negative labels to fit"
        )

    wine_ids = labels["wine_id"].tolist()
    placeholders = ",".join(f"'{w}'" for w in wine_ids)
    wine_emb = pd.read_sql(
        f"SELECT wine_id, embedding FROM wine_embeddings "
        f"WHERE wine_id IN ({placeholders})",
        db.engine(),
    )

    def _parse(v: object) -> np.ndarray:
        if isinstance(v, list):
            return np.asarray(v, dtype=np.float32)
        return np.fromstring(str(v).strip("[]"), sep=",", dtype=np.float32)

    wine_emb["vec"] = wine_emb["embedding"].map(_parse)
    wine_emb_map = dict(zip(wine_emb["wine_id"], wine_emb["vec"], strict=False))

    L_rows = []
    W_rows = []
    sign_rows: list[float] = []
    weight_rows: list[float] = []
    for _, row in labels.iterrows():
        if row["wine_id"] not in wine_emb_map:
            log.warning(
                "skipping label for wine_id=%s (no embedding)", row["wine_id"]
            )
            continue
        L_rows.append(embed.encode_query(row["description"]))
        W_rows.append(wine_emb_map[row["wine_id"]])
        s = (row.get("sentiment") or "positive").lower()
        sign_rows.append(-1.0 if s == "negative" else 1.0)
        weight_rows.append(float(row.get("weight") or 1.0))

    L = (
        np.vstack(L_rows).astype(np.float32)
        if L_rows else np.empty((0, embed.EMBEDDING_DIM), dtype=np.float32)
    )
    W = (
        np.vstack(W_rows).astype(np.float32)
        if W_rows else np.empty((0, embed.EMBEDDING_DIM), dtype=np.float32)
    )
    sign = np.asarray(sign_rows, dtype=np.float32)
    weight = np.asarray(weight_rows, dtype=np.float32)
    return L, W, sign, weight


# --- schema ------------------------------------------------------------


def init_calibration_schema() -> None:
    """Create user_calibration_history if it doesn't exist.

    Same defensive pattern as recommend.init_user_schema — check
    information_schema first (avoid the IF NOT EXISTS + DEFAULT NOW()
    combo that crashed CedarDB in earlier runs).
    """
    existing = set(
        pd.read_sql(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            ),
            db.engine(),
        )["table_name"].tolist()
    )
    if "user_calibration_history" in existing:
        return

    autocommit = db.engine().execution_options(isolation_level="AUTOCOMMIT")
    stmt = """
    CREATE TABLE user_calibration_history (
        user_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        n_labels INTEGER NOT NULL,
        backend TEXT NOT NULL,
        A_serialized BYTEA NOT NULL,
        b_serialized BYTEA NOT NULL,
        loss_final REAL NOT NULL,
        lambda_a REAL NOT NULL,
        lambda_b REAL NOT NULL,
        fit_at TIMESTAMP NOT NULL,
        PRIMARY KEY (user_id, version)
    )
    """
    try:
        with autocommit.connect() as conn:
            conn.execute(text(stmt))
        log.info("created table user_calibration_history")
    except Exception as e:  # noqa: BLE001
        log.warning("could not create user_calibration_history: %s", e)


def _next_version(user_id: str) -> int:
    df = pd.read_sql(
        text(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next "
            "FROM user_calibration_history WHERE user_id = :u"
        ),
        db.engine(),
        params={"u": user_id},
    )
    return int(df.iloc[0]["next"])


def _persist_history(
    user_id: str,
    A: np.ndarray,
    b: np.ndarray,
    n_labels: int,
    loss_final: float,
    backend: Backend,
) -> int:
    from datetime import datetime
    init_calibration_schema()
    version = _next_version(user_id)
    with db.connect() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_calibration_history (
                    user_id, version, n_labels, backend,
                    A_serialized, b_serialized,
                    loss_final, lambda_a, lambda_b, fit_at
                ) VALUES (
                    :u, :v, :n, :be, :A, :b, :loss, :la, :lb, :t
                )
                """
            ),
            {
                "u": user_id, "v": version, "n": n_labels, "be": backend,
                "A": A.tobytes(), "b": b.tobytes(),
                "loss": loss_final, "la": LAMBDA_A, "lb": LAMBDA_B,
                "t": datetime.utcnow(),
            },
        )
    return version


# --- backend-specific training -----------------------------------------


def _fit_torch(
    L: np.ndarray, W: np.ndarray, sign: np.ndarray, weight: np.ndarray,
    device: str,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Train via PyTorch on the given device (`cuda`, `mps`, or `cpu`).

    Sign-aware loss with per-sample weights:
      - sign=+1 (positive label): mean weighted squared distance.
      - sign=-1 (negative label): mean weighted hinge.
      - weight: own labels = 1.0, follows-labels = follow weight (~0.3).
        Both positives and negatives are weighted-mean-normalized.
    """
    import torch
    import torch.nn as nn

    dim = L.shape[1]
    dev = torch.device(device)
    L_t = torch.from_numpy(L).to(dev)
    W_t = torch.from_numpy(W).to(dev)
    sign_t = torch.from_numpy(sign).to(dev)
    weight_t = torch.from_numpy(weight).to(dev)
    pos_mask = sign_t > 0
    neg_mask = sign_t < 0

    linear = nn.Linear(dim, dim, bias=True).to(dev)
    with torch.no_grad():
        linear.weight.copy_(torch.eye(dim, device=dev))
        linear.bias.zero_()

    optim = torch.optim.Adam(linear.parameters(), lr=LR)
    I_d = torch.eye(dim, device=dev)
    final_loss = float("nan")

    pos_w = weight_t * pos_mask.float()
    neg_w = weight_t * neg_mask.float()
    pos_w_sum = pos_w.sum().clamp(min=1e-6)
    neg_w_sum = neg_w.sum().clamp(min=1e-6)

    for epoch in range(EPOCHS):
        optim.zero_grad()
        pred = linear(L_t)
        sq_dist = torch.sum((pred - W_t) ** 2, dim=1)  # (n,)
        mse_pos = (sq_dist * pos_w).sum() / pos_w_sum
        hinge = torch.clamp(NEG_MARGIN_SQ - sq_dist, min=0.0)
        mse_neg = (hinge * neg_w).sum() / neg_w_sum
        mse = mse_pos + mse_neg
        reg_A = LAMBDA_A * torch.sum((linear.weight - I_d) ** 2) / (dim * dim)
        reg_b = LAMBDA_B * torch.sum(linear.bias ** 2) / dim
        loss = mse + reg_A + reg_b
        loss.backward()
        optim.step()
        if epoch == 0 or (epoch + 1) % 50 == 0:
            log.info(
                "  epoch %3d · loss=%.5f (pos=%.5f, neg=%.5f, regA=%.5f, regB=%.5f)",
                epoch + 1, loss.item(), mse_pos.item(), mse_neg.item(),
                reg_A.item(), reg_b.item()
            )
        final_loss = loss.item()

    A = linear.weight.detach().cpu().numpy().astype(np.float32)
    b = linear.bias.detach().cpu().numpy().astype(np.float32)
    return A, b, final_loss


def _fit_mlx(
    L: np.ndarray, W: np.ndarray, sign: np.ndarray, weight: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Train via Apple's MLX framework on Apple-Silicon Metal.

    Sign-aware loss with per-sample weights — see _fit_torch docstring.
    """
    import mlx.core as mx
    import mlx.nn as nn
    import mlx.optimizers as opt

    dim = L.shape[1]
    L_a = mx.array(L)
    W_a = mx.array(W)
    pos_w_np = weight * (sign > 0).astype(np.float32)
    neg_w_np = weight * (sign < 0).astype(np.float32)
    pos_w = mx.array(pos_w_np)
    neg_w = mx.array(neg_w_np)
    pos_w_sum = float(max(pos_w_np.sum(), 1e-6))
    neg_w_sum = float(max(neg_w_np.sum(), 1e-6))

    linear = nn.Linear(dim, dim, bias=True)
    linear.weight = mx.eye(dim)
    linear.bias = mx.zeros((dim,))
    I_d = mx.eye(dim)

    def loss_fn(model, x, y):
        pred = model(x)
        sq_dist = mx.sum((pred - y) ** 2, axis=1)
        mse_pos = mx.sum(sq_dist * pos_w) / pos_w_sum
        hinge = mx.maximum(NEG_MARGIN_SQ - sq_dist, 0.0)
        mse_neg = mx.sum(hinge * neg_w) / neg_w_sum
        mse = mse_pos + mse_neg
        reg_a = LAMBDA_A * mx.sum((model.weight - I_d) ** 2) / (dim * dim)
        reg_b = LAMBDA_B * mx.sum(model.bias ** 2) / dim
        return mse + reg_a + reg_b, mse_pos, mse_neg, reg_a, reg_b

    def loss_only(model, x, y):
        return loss_fn(model, x, y)[0]

    loss_and_grad = nn.value_and_grad(linear, loss_only)
    optimizer = opt.Adam(learning_rate=LR)
    final_loss = float("nan")

    for epoch in range(EPOCHS):
        loss_val, grads = loss_and_grad(linear, L_a, W_a)
        optimizer.update(linear, grads)
        mx.eval(linear.parameters(), optimizer.state)
        if epoch == 0 or (epoch + 1) % 50 == 0:
            _, mp, mn, ra, rb = loss_fn(linear, L_a, W_a)
            log.info(
                "  epoch %3d · loss=%.5f (pos=%.5f, neg=%.5f, regA=%.5f, regB=%.5f)",
                epoch + 1, float(loss_val),
                float(mp), float(mn), float(ra), float(rb),
            )
        final_loss = float(loss_val)

    A = np.asarray(linear.weight, dtype=np.float32)
    b = np.asarray(linear.bias, dtype=np.float32)
    return A, b, final_loss


# --- main entry --------------------------------------------------------


def fit(user_id: str, backend: Backend | None = None) -> dict[str, object]:
    """Train and persist the personal projection for one user.

    Args:
        user_id: the canonical user_id (from `recommend.get_or_create_user`).
        backend: override the auto-detected backend ("mlx",
                 "torch-cuda", "torch-mps", "torch-cpu"). If None,
                 calls `detect_backend()`.

    Side effects:
        - Inserts a new row in `user_calibration_history` (append-only
          version log).
        - Replaces the user's row in `user_projections` (the live
          projection used by `recommend()`).
    """
    if not db.ping():
        raise RuntimeError("CedarDB unreachable")

    if backend is None:
        backend = detect_backend()
    log.info(
        "calibrate.fit user=%s backend=%s (%s)",
        user_id, backend, describe_backend(backend),
    )

    L, W, sign, weight = _load_user_pairs(user_id)
    n = len(L)
    if n == 0:
        raise RuntimeError(f"user {user_id} has no usable labels")

    n_pos = int((sign > 0).sum())
    n_neg = int((sign < 0).sum())
    n_own = int((weight >= 0.999).sum())
    n_followed = n - n_own
    dim = embed.EMBEDDING_DIM
    log.info(
        "fitting PersonalProjection (n=%d [%d own, %d via follows; "
        "%d pos, %d neg], dim=%d, lr=%g, epochs=%d, "
        "λ_A=%g, λ_B=%g, margin²=%g) for user=%s",
        n, n_own, n_followed, n_pos, n_neg, dim, LR, EPOCHS,
        LAMBDA_A, LAMBDA_B, NEG_MARGIN_SQ, user_id,
    )

    if backend == "mlx":
        A, b, final_loss = _fit_mlx(L, W, sign, weight)
    elif backend in ("torch-cuda", "torch-mps", "torch-cpu"):
        device = backend.split("-", 1)[1]
        A, b, final_loss = _fit_torch(L, W, sign, weight, device=device)
    else:
        raise ValueError(f"unknown backend: {backend}")

    # Persist into user_projections (the live table the recommender
    # reads) AND the append-only history log.
    proj = recommend.UserProjection(user_id=user_id, A=A, b=b, n_labels=n)
    recommend._persist_projection(proj)  # noqa: SLF001
    version = _persist_history(user_id, A, b, n, final_loss, backend)

    drift_a = float(np.linalg.norm(A - np.eye(dim, dtype=np.float32)))
    drift_b = float(np.linalg.norm(b))
    log.info(
        "fit complete · version=%d · ||A-I||=%.3f · ||b||=%.3f · loss=%.5f",
        version, drift_a, drift_b, final_loss,
    )
    return {
        "user_id": user_id,
        "version": version,
        "n_labels": n,
        "loss_final": final_loss,
        "drift_a": drift_a,
        "drift_b": drift_b,
        "backend": backend,
    }


def history(user_id: str) -> pd.DataFrame:
    """Return a user's calibration history (one row per fit)."""
    init_calibration_schema()
    return pd.read_sql(
        text(
            """
            SELECT version, n_labels, backend, loss_final,
                   lambda_a, lambda_b, fit_at
            FROM user_calibration_history
            WHERE user_id = :u
            ORDER BY version ASC
            """
        ),
        db.engine(),
        params={"u": user_id},
    )
