"""Domain-adaptive fine-tune of the wine encoder on user labels.

Continue-trains `BAAI/bge-small-en-v1.5` with a LoRA adapter using
the corpus of `(user_description, wine_id)` pairs from `user_labels`.
The goal is to bend the encoder toward WineTone users' actual
vocabulary — including, critically, the wines users have *added*
themselves (`wines.submitted_by_user_id IS NOT NULL`), which carry
zero professional-reviewer prose and are pure HyperLanguage_users
signal.

Pipeline
--------
1. `gather_training_pairs()` → dataset of (description, wine_id),
   with `is_user_added` flags so we can upweight that subset.
2. `train_lora(dataset)` → LoRA adapter weights (a few MB).
3. `validate_against_baseline(adapter, holdout)` → top-k accuracy
   delta vs. currently deployed encoder. **Hard gate.**
4. `regenerate_wine_embeddings(adapter)` → recompute the
   `wine_embeddings` pgvector table. The expensive step. Done in a
   sidecar column, then atomically swapped.
5. `push_to_hf_hub(adapter)` → `archisgore/winetone-encoder` repo.

Why this is mostly a scaffold today
-----------------------------------
We have 2 user labels in the DB as of this writing. Training a LoRA
adapter on 2 pairs would memorize them and produce a worse encoder
than vanilla bge-small. The hard data-readiness gate (`MIN_PAIRS`)
prevents `train_lora()` from running until we have enough signal.
Concretely:

  MIN_PAIRS = 500
  MIN_USERS = 3       # privacy / generalization
  MIN_USER_ADDED = 20 # there must be *some* user-added-wine signal

When all three are met, the scaffold becomes a real training run.
Until then the orchestration calls `is_ready_to_train()` and that
function returns False with a clear "deferred for X reason" message.

What's NOT in this module
-------------------------
- **Re-fitting all per-user MLPs** after an encoder swap. That's
  done by `scripts/retrain.py` calling `calibrate_mlp.refit_*`.
- **Schema migrations.** Done in the orchestration script.
- **HF model-repo push.** Implemented here in `push_to_hf_hub` but
  only called by the orchestrator after validation passes.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import numpy as np
from sqlalchemy import text

from winetone import db, embed

log = logging.getLogger(__name__)

# --- thresholds for gating the encoder fine-tune ----------------------
MIN_PAIRS = 500           # below this, LoRA overfits the small corpus
MIN_USERS = 3             # below this, encoder memorizes one user's voice
MIN_USER_ADDED = 20       # need a nontrivial user-added-wine signal

# --- training hyperparams (used when gate opens) ---------------------
LORA_RANK = 8
LORA_ALPHA = 16
LORA_DROPOUT = 0.1
TRAIN_EPOCHS = 3
TRAIN_BATCH = 16
TRAIN_LR = 2e-4
USER_ADDED_UPWEIGHT = 8.0  # how much to upweight user-added-wine pairs
HOLDOUT_FRAC = 0.10
HOLDOUT_TOP_K = 10
MIN_ACCURACY_DELTA = -0.02  # new must be >= old - 2pp


@dataclass
class TrainingReadiness:
    """Result of `is_ready_to_train()` — answer + diagnostics."""
    ready: bool
    reason: str
    total_pairs: int
    distinct_users: int
    user_added_pairs: int


def is_ready_to_train() -> TrainingReadiness:
    """Check whether we have enough data to actually run a fine-tune.

    Returns a structured result so the orchestrator can log *why* the
    run was deferred (and the user can act on it — usually, just
    "keep adding labels").
    """
    with db.engine().connect() as conn:
        total = conn.execute(text(
            "SELECT COUNT(*) FROM user_labels"
        )).scalar()
        users = conn.execute(text(
            "SELECT COUNT(DISTINCT user_id) FROM user_labels"
        )).scalar()
        user_added = conn.execute(text("""
            SELECT COUNT(*) FROM user_labels l
            JOIN wines w ON w.wine_id = l.wine_id
            WHERE w.submitted_by_user_id IS NOT NULL
        """)).scalar()

    if total < MIN_PAIRS:
        return TrainingReadiness(
            ready=False,
            reason=f"only {total} pairs ( < MIN_PAIRS={MIN_PAIRS} )",
            total_pairs=total, distinct_users=users,
            user_added_pairs=user_added,
        )
    if users < MIN_USERS:
        return TrainingReadiness(
            ready=False,
            reason=f"only {users} distinct users "
                   f"( < MIN_USERS={MIN_USERS} ) — privacy/generalization risk",
            total_pairs=total, distinct_users=users,
            user_added_pairs=user_added,
        )
    if user_added < MIN_USER_ADDED:
        return TrainingReadiness(
            ready=False,
            reason=f"only {user_added} user-added-wine pairs "
                   f"( < MIN_USER_ADDED={MIN_USER_ADDED} )",
            total_pairs=total, distinct_users=users,
            user_added_pairs=user_added,
        )

    return TrainingReadiness(
        ready=True,
        reason=f"ready: {total} pairs, {users} users, {user_added} user-added",
        total_pairs=total, distinct_users=users,
        user_added_pairs=user_added,
    )


# --- pair gathering ---------------------------------------------------


@dataclass
class TrainingPair:
    description: str
    wine_id: str
    is_user_added: bool
    user_id: str


def gather_training_pairs() -> list[TrainingPair]:
    """Pull every (description, wine_id) from `user_labels`, joined
    with `wines.submitted_by_user_id` for the is-user-added flag.
    """
    with db.engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT l.description, l.wine_id, l.user_id,
                   (w.submitted_by_user_id IS NOT NULL) AS is_user_added
            FROM user_labels l
            JOIN wines w ON w.wine_id = l.wine_id
        """)).fetchall()
    return [
        TrainingPair(
            description=r.description, wine_id=r.wine_id,
            is_user_added=bool(r.is_user_added), user_id=r.user_id,
        ) for r in rows
    ]


def stratified_holdout(
    pairs: list[TrainingPair], frac: float = HOLDOUT_FRAC, seed: int = 42,
) -> tuple[list[TrainingPair], list[TrainingPair]]:
    """Hold out `frac` of each user's pairs (stratified by user_id).

    Stratification matters because a non-stratified holdout could
    leave a user entirely out, and that user's MLP fit will then be
    based on training data the encoder never saw — interpretation of
    the validation metric breaks down.
    """
    import random
    rng = random.Random(seed)
    by_user: dict[str, list[TrainingPair]] = {}
    for p in pairs:
        by_user.setdefault(p.user_id, []).append(p)
    train, holdout = [], []
    for uid, ps in by_user.items():
        rng.shuffle(ps)
        n_hold = max(1, int(round(len(ps) * frac))) if len(ps) >= 2 else 0
        holdout.extend(ps[:n_hold])
        train.extend(ps[n_hold:])
    return train, holdout


# --- training (gated; raises if not ready) ----------------------------


def train_lora(
    train_pairs: list[TrainingPair],
    output_dir: str,
) -> str:
    """Train a LoRA adapter on the (description, wine_id) pairs.

    Implemented when the data-readiness gate opens (MIN_PAIRS).
    Stays a thoughtful stub until then so we don't ship an
    unvalidated training loop. The shape it'll have:

    1. Load `BAAI/bge-small-en-v1.5` via sentence-transformers.
    2. Wrap query and document encoders in `peft.LoraConfig` (rank
       LORA_RANK, alpha LORA_ALPHA, dropout LORA_DROPOUT).
    3. Build a training dataset of (description, wine_text) positives
       — where `wine_text` is the concatenated review text for that
       wine (same prep as `embed._build_embedding_text`).
    4. Apply `USER_ADDED_UPWEIGHT` via sample weight repetition.
    5. Loss: MultipleNegativesRankingLoss (treats other batch items
       as negatives — the standard contrastive recipe for SBERT).
    6. Save the LoRA adapter to `output_dir`. Caller pushes to HF.

    Returns the path to the saved adapter.
    """
    ready = is_ready_to_train()
    if not ready.ready:
        raise RuntimeError(
            f"encoder fine-tune skipped — {ready.reason}. "
            f"(total={ready.total_pairs} users={ready.distinct_users} "
            f"user_added={ready.user_added_pairs})"
        )
    # TODO: actually implement when the gate opens. The structure
    # above is what gets filled in — sentence-transformers' SBERT +
    # peft.LoraConfig + MNR loss is the standard recipe. Estimated
    # ~100 LOC for the real training loop.
    raise NotImplementedError(
        "Data-readiness gate is open, but the training loop hasn't "
        "been built yet. See docstring for the shape it'll take."
    )


# --- validation (gated; the regression guard) -------------------------


def validate_against_baseline(
    adapter_path: str,
    holdout: list[TrainingPair],
    top_k: int = HOLDOUT_TOP_K,
) -> dict[str, float]:
    """Top-k accuracy of the new encoder vs. the currently deployed one.

    For each held-out (description, wine_id), encode the description
    with both encoders and check whether wine_id is in the top-k
    nearest wines.

    Returns:
        { "acc_new": ..., "acc_old": ..., "delta": acc_new - acc_old,
          "passes_gate": delta >= MIN_ACCURACY_DELTA }

    Logged to `encoder_retrain_history` whether it passes or not.
    """
    raise NotImplementedError(
        "Validation requires the training loop to produce an adapter. "
        "Same data-readiness condition gates this."
    )


# --- corpus regeneration (gated; expensive) ---------------------------


def regenerate_wine_embeddings(adapter_path: str, full: bool = False) -> int:
    """Recompute `wine_embeddings` with the new encoder.

    Two-phase swap to keep the live runtime safe:
      1. Write new vectors to `wine_embeddings_v2` (sidecar table).
      2. After all rows succeed, rename: live → _old, _v2 → live.
      3. Drop _old.

    `full=False` re-embeds the current 20k stratified sample.
    `full=True` does all 164k. The latter is a multi-hour CPU job —
    only run when we've decided we're committed to full-corpus
    coverage at the new encoder.
    """
    raise NotImplementedError(
        "Schema two-phase swap + actual encoder call to be implemented. "
        "Pattern: `embed.build()` already knows how to write batched "
        "embeddings; the change is just to target a sidecar table and "
        "use the LoRA-adapted encoder."
    )


# --- HF push (small enough to build now) ------------------------------


def push_to_hf_hub(
    adapter_path: str,
    repo_id: str = "archisgore/winetone-encoder",
    commit_message: str | None = None,
) -> str:
    """Upload the saved LoRA adapter to a HF model repo.

    The repo is private by default. Re-deploys to the production
    Space don't auto-pull a new encoder — that's a separate step in
    the retrain orchestration once validation has passed.

    Token comes from env `HF_TOKEN_WRITE` so that the read-only token
    used for the Space deploy can't accidentally trigger a model push.
    """
    from huggingface_hub import HfApi
    token = os.environ.get("HF_TOKEN_WRITE")
    if not token:
        raise RuntimeError(
            "HF_TOKEN_WRITE env not set — refusing to push. "
            "Set it as a repo secret in GitHub Actions."
        )
    api = HfApi(token=token)
    api.create_repo(repo_id=repo_id, repo_type="model", private=True,
                    exist_ok=True)
    commit = api.upload_folder(
        repo_id=repo_id,
        folder_path=adapter_path,
        commit_message=commit_message or "winetone encoder fine-tune",
        repo_type="model",
    )
    log.info("pushed LoRA adapter to %s (commit %s)", repo_id, commit)
    return str(commit)
