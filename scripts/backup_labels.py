"""Daily backup of user-contributed data to a HF Dataset repo.

Designed to survive a Neon outage: the labels — the irreplaceable
training signal in this system — live in a second store on a
different vendor (Hugging Face), in versioned git history.

What's snapshotted
------------------
- `user_labels`               — the primary signal
- `user_label_embeddings`     — cached label embeddings
- `user_projections`          — current per-user linear projections
- `user_projections_mlp`      — current per-user MLP projections
- `users`                     — user metadata for join purposes
- `wines_user_added`          — `wines` rows where
                                 `submitted_by_user_id IS NOT NULL`
- `manifest.json`             — schema info, row counts, timestamp

We deliberately *don't* back up the full 164k canonical `wines`
table — those come from public sources we can re-pull. Only the
irreplaceable bits get snapshotted.

Output format
-------------
Each table → one Parquet file. Compact, columnar, fast restore.
Files are committed to `archisgore/winetone-labels-backup` (private
dataset repo); the git history retains every commit, so we have
free point-in-time recovery for as long as we keep the repo.

Cron cadence
------------
Daily at 03:00 UTC via `.github/workflows/backup.yml`.
Backups always run — no change gating. They're cheap (a few MB at
most) and the value of "we have yesterday's data" justifies the
trivial cost.

Runnable locally too:
    python scripts/backup_labels.py --dry     # print what would happen
    python scripts/backup_labels.py --apply   # full live backup
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger("backup")


# --- table snapshots --------------------------------------------------


# Output filename → (SQL, underlying_table_to_check_for_existence).
# The output name and the source table are decoupled because
# `wines_user_added` is a virtual subset of `wines`, not its own table.
TABLES: dict[str, tuple[str, str]] = {
    "user_labels": (
        "SELECT * FROM user_labels ORDER BY user_id, wine_id, created_at",
        "user_labels",
    ),
    "user_label_embeddings": (
        # BYTEA serialized embeddings — Parquet handles binary fine.
        "SELECT * FROM user_label_embeddings ORDER BY user_id, wine_id",
        "user_label_embeddings",
    ),
    "user_projections": (
        "SELECT user_id, n_labels, A_serialized, b_serialized, fit_at "
        "FROM user_projections ORDER BY user_id",
        "user_projections",
    ),
    "user_projections_mlp": (
        # Only present after the first MLP fit lands; the
        # information_schema check below handles its absence.
        "SELECT user_id, n_labels, weights, fit_at, loss, arch_id, labels_sig "
        "FROM user_projections_mlp ORDER BY user_id",
        "user_projections_mlp",
    ),
    "users": (
        "SELECT * FROM users ORDER BY user_id",
        "users",
    ),
    "wines_user_added": (
        "SELECT * FROM wines "
        "WHERE submitted_by_user_id IS NOT NULL "
        "ORDER BY wine_id",
        "wines",
    ),
}


def _table_exists(conn, name: str) -> bool:
    from sqlalchemy import text
    return conn.execute(text(
        "SELECT EXISTS ("
        "  SELECT 1 FROM information_schema.tables "
        "  WHERE table_schema='public' AND table_name=:n"
        ")"
    ), {"n": name}).scalar()


def dump_to_parquet(out_dir: Path) -> dict[str, int]:
    """Dump each table to a Parquet file under `out_dir`.

    Returns a dict of table → row count.
    """
    from sqlalchemy import text  # noqa: F401  (used via pandas read_sql)
    from winetone import db
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    with db.engine().connect() as conn:
        for name, (sql, src_table) in TABLES.items():
            if not _table_exists(conn, src_table):
                # Underlying table missing (e.g., user_projections_mlp
                # before first MLP fit). Record -1 so absence is visible
                # in the manifest.
                counts[name] = -1
                continue
            df = pd.read_sql(sql, conn)
            counts[name] = len(df)
            df.to_parquet(out_dir / f"{name}.parquet", index=False)
            log.info("  dumped %s: %d rows", name, len(df))
    return counts


def write_manifest(out_dir: Path, counts: dict[str, int]) -> None:
    """Write a JSON manifest describing this snapshot."""
    db_url = os.environ.get("WINETONE_DB_URL", "<unset>")
    # Strip secrets from the host before recording — we want host but
    # not credentials in the manifest.
    safe_host = "<redacted>"
    try:
        from urllib.parse import urlparse
        p = urlparse(db_url)
        safe_host = p.hostname or "<unknown>"
    except Exception:  # noqa: BLE001
        pass
    manifest = {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "source_db_host": safe_host,
        "schema_version": "v1",
        "row_counts": counts,
        "tables_missing": [k for k, v in counts.items() if v == -1],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log.info("manifest: %s", manifest)


# --- HF push ----------------------------------------------------------


def push_to_hf(
    src_dir: Path,
    repo_id: str = "archisgore/winetone-labels-backup",
    commit_message: str | None = None,
) -> str:
    """Upload the snapshot directory as a commit to a private dataset repo.

    Token: `HF_TOKEN_WRITE` env var. Refuses to push without it — we
    intentionally split read-only and write tokens.
    """
    from huggingface_hub import HfApi
    token = os.environ.get("HF_TOKEN_WRITE")
    if not token:
        raise RuntimeError(
            "HF_TOKEN_WRITE env not set. Set it as a repo secret."
        )
    api = HfApi(token=token)
    api.create_repo(repo_id=repo_id, repo_type="dataset",
                    private=True, exist_ok=True)
    msg = commit_message or (
        f"snapshot {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    )
    commit = api.upload_folder(
        repo_id=repo_id,
        folder_path=str(src_dir),
        repo_type="dataset",
        commit_message=msg,
    )
    log.info("pushed snapshot to %s (commit %s)", repo_id, commit)
    return str(commit)


# --- main -------------------------------------------------------------


def run(apply: bool = False) -> int:
    from winetone import db
    if not db.ping():
        log.error("DB unreachable — aborting")
        return 2
    with tempfile.TemporaryDirectory(prefix="winetone-backup-") as tmp:
        out_dir = Path(tmp) / "snapshot"
        counts = dump_to_parquet(out_dir)
        write_manifest(out_dir, counts)
        if not apply:
            log.info(
                "[dry] dumped %d Parquet files under %s; not pushing.",
                len([v for v in counts.values() if v >= 0]), out_dir,
            )
            return 0
        push_to_hf(out_dir)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually push to HF. Without this, runs dry.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
