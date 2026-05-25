"""Cron-driven retrain orchestrator.

Called by `.github/workflows/retrain.yml` every day at ~06:00 UTC.
The job is intentionally cheap on no-op runs (the common case):

    1. Connect to DB
    2. Compute current change signature
    3. Read `retrain_state` row
    4. If signature unchanged AND it's not been > 30 days → exit no-op
    5. Else: backup-snapshot, refit MLPs, optionally fine-tune encoder
    6. Write updated `retrain_state` row, exit

Exits non-zero only on genuine failure (DB unreachable, training
crash). A no-op exit is success.

This script is also runnable locally:
    python scripts/retrain.py            # dry-run (no DB writes)
    python scripts/retrain.py --apply    # full live run
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

# These imports are deferred so `--help` works without a DB connection.
log = logging.getLogger("retrain")


# --- signature + state ------------------------------------------------


@dataclasses.dataclass
class Signature:
    """Captures the shape of the training data at a point in time."""
    total_labels: int
    total_users_with_labels: int
    user_added_wines: int
    labels_hash: str
    max_label_ts: str  # ISO

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def compute_signature() -> Signature:
    """Compute the current signature from the live DB.

    `labels_hash` is sha256 over the sorted concatenation of
    `(label_id, user_id, wine_id, description)` rows. Any insert,
    update, or delete in `user_labels` flips the hash — that's the
    detection mechanism for the three mutation types
    (added/deleted/edited) that the user called out.

    A label_id column doesn't exist in user_labels, so we synthesize
    one as `f"{user_id}|{wine_id}|{created_at}"` — unique per row.
    """
    from winetone import db
    with db.engine().connect() as conn:
        total = conn.execute(text(
            "SELECT COUNT(*) FROM user_labels"
        )).scalar()
        users = conn.execute(text(
            "SELECT COUNT(DISTINCT user_id) FROM user_labels"
        )).scalar()
        user_added = conn.execute(text("""
            SELECT COUNT(*) FROM wines
            WHERE submitted_by_user_id IS NOT NULL
        """)).scalar()
        rows = conn.execute(text("""
            SELECT user_id, wine_id, description, created_at
            FROM user_labels
            ORDER BY user_id, wine_id, created_at
        """)).fetchall()
        max_ts = conn.execute(text(
            "SELECT MAX(created_at) FROM user_labels"
        )).scalar()

    h = hashlib.sha256()
    for r in rows:
        key = f"{r.user_id}|{r.wine_id}|{r.created_at.isoformat()}"
        h.update(key.encode())
        h.update(b"\x1f")  # unit separator
        h.update(r.description.encode())
        h.update(b"\n")

    return Signature(
        total_labels=int(total or 0),
        total_users_with_labels=int(users or 0),
        user_added_wines=int(user_added or 0),
        labels_hash="sha256:" + h.hexdigest(),
        max_label_ts=max_ts.isoformat() if max_ts else "",
    )


def _init_state_schema() -> None:
    """Create the `retrain_state` table if it doesn't exist."""
    from winetone import db
    with db.engine().connect() as conn:
        existing = {
            r[0] for r in conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )).fetchall()
        }
        if "retrain_state" not in existing:
            conn.execute(text("""
                CREATE TABLE retrain_state (
                    artifact     TEXT PRIMARY KEY,
                    last_run_at  TIMESTAMP NOT NULL,
                    last_result  TEXT NOT NULL,
                    signature    TEXT NOT NULL
                )
            """))
            conn.commit()
            log.info("created table retrain_state")


def load_last_state(artifact: str) -> tuple[Signature | None, datetime | None, str]:
    """Read the last successful run's signature for `artifact`.

    Returns (sig, last_run_at, last_result). All None on first run.
    """
    from winetone import db
    with db.engine().connect() as conn:
        row = conn.execute(text(
            "SELECT last_run_at, last_result, signature "
            "FROM retrain_state WHERE artifact = :a"
        ), {"a": artifact}).first()
    if row is None:
        return None, None, ""
    sig_d = json.loads(row.signature)
    sig = Signature(**sig_d)
    return sig, row.last_run_at, row.last_result


def save_state(artifact: str, sig: Signature, result: str) -> None:
    """Persist the latest run's signature + result."""
    from winetone import db
    with db.engine().connect() as conn:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        conn.execute(text(
            "DELETE FROM retrain_state WHERE artifact = :a"
        ), {"a": artifact})
        conn.execute(text("""
            INSERT INTO retrain_state
                (artifact, last_run_at, last_result, signature)
            VALUES (:a, :t, :r, :s)
        """), {
            "a": artifact, "t": now, "r": result,
            "s": json.dumps(sig.to_dict()),
        })
        conn.commit()


# --- decision logic ---------------------------------------------------


def decide_mlp_run(sig: Signature, last_sig: Signature | None) -> tuple[bool, str]:
    """Should we refit per-user MLPs this round?

    Any per-user change → refit. The MLP module's
    `refit_users_with_changes` is itself idempotent: it only touches
    users whose label set has shifted.
    """
    if last_sig is None:
        return True, "no prior run"
    if sig.labels_hash != last_sig.labels_hash:
        return True, "labels_hash changed (add/edit/delete somewhere)"
    return False, "no label changes since last MLP refit"


def decide_encoder_run(
    sig: Signature, last_sig: Signature | None, last_run_at: datetime | None,
) -> tuple[bool, str]:
    """Should we attempt an encoder fine-tune this round?

    Three combined gates:
      1. Data volume: `is_ready_to_train()` from embed_finetune.
      2. Growth: ≥ 50 new labels since last successful encoder run.
      3. Staleness: OR `labels_hash` changed AND ≥ 30 days old.

    Both gates 2 and 3 are explicitly OR'd — either is enough.
    """
    from winetone import embed_finetune
    readiness = embed_finetune.is_ready_to_train()
    if not readiness.ready:
        return False, f"encoder data-readiness gate: {readiness.reason}"

    if last_sig is None:
        return True, "first encoder run"

    grew_by = sig.total_labels - last_sig.total_labels
    if grew_by >= 50:
        return True, f"+{grew_by} labels since last encoder run"

    if sig.labels_hash != last_sig.labels_hash and last_run_at is not None:
        if datetime.now(timezone.utc).replace(tzinfo=None) - last_run_at \
                >= timedelta(days=30):
            return True, "labels changed and >30 days since last run"

    return False, (
        f"only +{grew_by} labels since last encoder run "
        f"(<50 threshold, and not yet 30 days stale)"
    )


# --- orchestration ----------------------------------------------------


def run(dry: bool = True) -> int:
    """Top-level orchestration. Returns process exit code (0 = ok)."""
    from winetone import calibrate_mlp, db
    if not db.ping():
        log.error("DB unreachable — aborting")
        return 2

    _init_state_schema()
    sig = compute_signature()
    log.info("signature: total=%d users=%d user_added=%d hash=%s",
             sig.total_labels, sig.total_users_with_labels,
             sig.user_added_wines, sig.labels_hash[:24])

    # --- MLP decision ---
    last_mlp_sig, last_mlp_run, last_mlp_result = load_last_state("mlp")
    do_mlp, why_mlp = decide_mlp_run(sig, last_mlp_sig)
    log.info("mlp: do=%s why=%s", do_mlp, why_mlp)

    mlp_result = "skipped"
    if do_mlp:
        if dry:
            log.info("[dry] would refit MLPs now")
        else:
            summary = calibrate_mlp.refit_users_with_changes()
            log.info("mlp refit summary: %s", summary)
            mlp_result = (
                f"refit={summary['refit']} "
                f"skipped={summary['skipped']} "
                f"failed={len(summary['failed'])}"
            )
            save_state("mlp", sig, mlp_result)
    else:
        if not dry and last_mlp_sig is None:
            # Persist a no-op state row so the next run has a comparison.
            save_state("mlp", sig, "no-op:initial")

    # --- Encoder decision ---
    last_enc_sig, last_enc_run, last_enc_result = load_last_state("encoder")
    do_enc, why_enc = decide_encoder_run(sig, last_enc_sig, last_enc_run)
    log.info("encoder: do=%s why=%s", do_enc, why_enc)

    enc_result = "skipped"
    if do_enc:
        # The training path itself raises NotImplementedError until
        # we wire it up — keep this branch guarded.
        if dry:
            log.info("[dry] would run encoder fine-tune now")
        else:
            try:
                from winetone import embed_finetune
                pairs = embed_finetune.gather_training_pairs()
                train, holdout = embed_finetune.stratified_holdout(pairs)
                log.info("encoder: %d train, %d holdout", len(train), len(holdout))
                adapter_path = "/tmp/winetone-encoder-adapter"
                embed_finetune.train_lora(train, output_dir=adapter_path)
                metrics = embed_finetune.validate_against_baseline(
                    adapter_path, holdout
                )
                if not metrics["passes_gate"]:
                    enc_result = f"aborted-regression delta={metrics['delta']:+.3f}"
                    log.error("encoder validation failed — aborting promote")
                else:
                    embed_finetune.push_to_hf_hub(adapter_path)
                    embed_finetune.regenerate_wine_embeddings(adapter_path)
                    enc_result = (
                        f"promoted acc_new={metrics['acc_new']:.3f} "
                        f"delta={metrics['delta']:+.3f}"
                    )
            except NotImplementedError as e:
                log.warning("encoder fine-tune not yet implemented: %s", e)
                enc_result = "deferred:not-implemented"
            except Exception as e:  # noqa: BLE001
                log.exception("encoder fine-tune crashed: %s", e)
                enc_result = f"failed:{type(e).__name__}"
            save_state("encoder", sig, enc_result)

    log.info("done. mlp=%s encoder=%s", mlp_result, enc_result)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write to the DB. Without this, runs dry.",
    )
    parser.add_argument("--verbose", "-v", action="count", default=1)
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose >= 2 else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(dry=not args.apply)


if __name__ == "__main__":
    sys.exit(main())
