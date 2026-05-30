"""Daily backup of user-contributed data to a private GitHub repo.

Pseudonymized by design: every row references the user by their
`masked_user_id` (a random UUID minted at user creation), not by
`user_id`. The `users` table itself — the only place that maps
masked_id → display_name/email/clerk_user_id — is NEVER included in
the backup. When a user is deleted, their `users` row is removed,
and every existing backup snapshot becomes a permanent orphan: no
way to map any `masked_user_id` back to a real identity, ever.

What's snapshotted (all with `user_id` and PII columns dropped):
  - `user_labels`            — primary signal
  - `user_projections`       — linear A·L+b per user
  - `user_projections_mlp`   — MLP weights per user
  - `follows`                — social graph (both endpoints masked)
  - `wines_user_added`       — wines added via /wines/new
  - `user_audit_log`         — event metadata only (no old/new values
                                because those can carry PII like a
                                previous display_name)
  - `manifest.json`          — snapshot timestamp + row counts

What's NOT snapshotted:
  - `users` itself — the PII source. Never backed up.
  - `user_label_embeddings` — regenerable from label text, big binary.
  - The PII fields inside `user_audit_log.old_value/new_value` — a
    user's old display_name lives there, so it's redacted from the
    snapshot. The live Neon audit log keeps them for ops debugging.

Target: GitHub repo `archisgore/winetone-labels-backup` (private).
Workflow checks out the repo, calls this script with `--target
<path>`, then commits + pushes.

Usage:
    python scripts/backup_labels.py --target /path/to/backup-repo
    python scripts/backup_labels.py --target . --dry         # no writes
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger("backup")


# Each entry: output filename → (SELECT query, table to check for existence).
#
# The queries deliberately exclude every column that links back to a
# real user identity:
#   - No `user_id` columns (only `masked_user_id` / `*_masked_id`)
#   - No `clerk_user_id`, `email`, `display_name`
#   - For `user_audit_log`: no `old_value` / `new_value` (those can
#     carry a previous display_name, which is PII)
TABLES: dict[str, tuple[str, str]] = {
    "user_labels": (
        "SELECT masked_user_id, wine_id, description, sentiment, created_at "
        "FROM user_labels "
        "WHERE masked_user_id IS NOT NULL "
        "ORDER BY masked_user_id, wine_id, created_at",
        "user_labels",
    ),
    "user_projections": (
        "SELECT masked_user_id, n_labels, A_serialized, b_serialized, fit_at "
        "FROM user_projections "
        "WHERE masked_user_id IS NOT NULL "
        "ORDER BY masked_user_id",
        "user_projections",
    ),
    "user_projections_mlp": (
        "SELECT masked_user_id, n_labels, weights, fit_at, loss, "
        "       arch_id, labels_sig "
        "FROM user_projections_mlp "
        "WHERE masked_user_id IS NOT NULL "
        "ORDER BY masked_user_id",
        "user_projections_mlp",
    ),
    "follows": (
        "SELECT follower_masked_id, followee_masked_id, weight, created_at "
        "FROM follows "
        "WHERE follower_masked_id IS NOT NULL "
        "  AND followee_masked_id IS NOT NULL "
        "ORDER BY follower_masked_id, followee_masked_id",
        "follows",
    ),
    "wines_user_added": (
        # The wine catalog row, with the submitter pseudonymized.
        # Catalog fields (wine_id, producer/wine names, vintage, etc.)
        # are not PII — they describe wines, not people.
        "SELECT wine_id, producer_canonical, wine_canonical, vintage, "
        "       producer_display, wine_display, variety, country, region, "
        "       n_source_records, sources_seen, submitted_by_masked_id "
        "FROM wines "
        "WHERE submitted_by_user_id IS NOT NULL "
        "ORDER BY wine_id",
        "wines",
    ),
    "user_audit_log": (
        # Event metadata only. `old_value` and `new_value` are excluded
        # because they can contain a user's previous display_name —
        # which is PII. Live Neon audit log keeps them for ops use.
        "SELECT event_id, masked_user_id, event_at, event_type, "
        "       field, source, request_id "
        "FROM user_audit_log "
        "ORDER BY event_at",
        "user_audit_log",
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
    """Dump each table to a Parquet file under `out_dir`. Returns
    `{name: row_count}` (or -1 for tables that don't exist yet).
    """
    from sqlalchemy import text  # noqa: F401 (read_sql uses it implicitly)
    from winetone import db
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with db.engine().connect() as conn:
        for name, (sql, src_table) in TABLES.items():
            if not _table_exists(conn, src_table):
                counts[name] = -1
                continue
            df = pd.read_sql(sql, conn)
            counts[name] = len(df)
            df.to_parquet(out_dir / f"{name}.parquet", index=False)
            log.info("  dumped %s: %d rows", name, len(df))
    return counts


def write_manifest(out_dir: Path, counts: dict[str, int]) -> None:
    """Write a JSON manifest with snapshot metadata."""
    db_url = os.environ.get("WINETONE_DB_URL", "<unset>")
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
        "schema_version": "v2-pseudonymized",
        "row_counts": counts,
        "tables_missing": [k for k, v in counts.items() if v == -1],
        # Reminder for whoever opens this snapshot in five years:
        "schema_note": (
            "Every row references the user via masked_user_id only. "
            "The users table — the only PII source — is not included. "
            "Deleted users have no way back to identity in this snapshot."
        ),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log.info("manifest: %s", manifest)


def write_readme(out_dir: Path) -> None:
    """Write a stable README.md so the GH repo isn't a wall of binary."""
    readme = out_dir / "README.md"
    readme.write_text(
        "# winetone-labels-backup\n\n"
        "Daily snapshots of WineTone user-contributed data.\n\n"
        "**Pseudonymized by design.** Every row references the user "
        "only via `masked_user_id` (a random UUID generated at user "
        "creation). The `users` table — the only place that maps "
        "masked_user_id → display_name / email / clerk_user_id — is "
        "**not** in this backup. Once a user is deleted (their `users` "
        "row removed in the live DB), every snapshot here becomes a "
        "permanent orphan: no way to map any `masked_user_id` back to "
        "a real identity.\n\n"
        "Each commit is a full snapshot. See `manifest.json` for the "
        "row counts and the source DB host.\n\n"
        "Backup pipeline: `archisgore/WineTone` "
        "→ `.github/workflows/backup.yml` → daily cron 03:00 UTC.\n"
    )


# --- main -------------------------------------------------------------


def run(target: Path, apply: bool) -> int:
    from winetone import db
    if not db.ping():
        log.error("DB unreachable — aborting")
        return 2
    counts = dump_to_parquet(target)
    write_manifest(target, counts)
    write_readme(target)
    if not apply:
        log.info(
            "[dry] wrote %d parquet files under %s; not committing.",
            len([v for v in counts.values() if v >= 0]),
            target,
        )
    else:
        log.info("wrote snapshot to %s (commit + push handled by workflow)",
                 target)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help="Local directory to write the parquet files + manifest into.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Pair with `--target`; without it, the run is dry "
             "(files written, but the log says 'not committing').",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(args.target, args.apply)


if __name__ == "__main__":
    sys.exit(main())
