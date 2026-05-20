"""Package / publish trained-model artifacts as a GitHub release.

The full training pipeline takes 3+ hours on a CPU. Most people who
want to try WineTone shouldn't have to redo it. Instead we package
the trained artifacts once and publish them as a downloadable
tarball attached to a GitHub release.

What goes into a release tarball
--------------------------------

  manifest.json                  schema version, counts, build info
  parquet/
    wines.parquet                canonical (producer, wine, vintage)
    wine_features.parquet        flat ML table
    wine_embeddings.parquet      dense vectors, 384-dim
    wine_clusters.parquet        cluster assignments
    wine_cluster_centroids.parquet
    wine_sparse_index.parquet    wine_id ↔ row in sparse matrix
  sparse/
    tfidf_matrix.joblib          scipy.sparse.csr_matrix
    tfidf_vectorizer.joblib      sklearn TfidfVectorizer

We do NOT include source_records — that's raw scraped review text
that doesn't need to ship. The encoder model itself is fetched on
first use by fastembed (it's a public HuggingFace asset).

Workflow
--------

  $ make build-all                        # 3+ hours on CPU
  $ winetone export-release               # → release/winetone-data-YYYY-MM-DD.tar.gz
  $ gh release create vYYYY.MM.DD release/winetone-data-YYYY-MM-DD.tar.gz

  # On a friend's machine:
  $ git clone github.com/archisgore/WineTone
  $ cd WineTone
  $ make dev          # or dev-mac
  $ make db-up-bg
  $ gh release download vYYYY.MM.DD --pattern '*.tar.gz'
  $ winetone import-release winetone-data-*.tar.gz
  $ make serve                            # done — minutes, not hours
"""

from __future__ import annotations

import json
import logging
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from winetone import db
from winetone.paths import DATA_ROOT

log = logging.getLogger(__name__)

RELEASE_DIR = DATA_ROOT.parent / "release"

# Tables we export. Order matters for import (foreign-key-ish
# logical order even though CedarDB doesn't enforce them).
EXPORT_TABLES = (
    "wines",
    "wine_features",
    "wine_embeddings",
    "wine_clusters",
    "wine_cluster_centroids",
    "wine_sparse_index",
)

# Schema version. Bump when the manifest layout or table set changes
# so import-release can validate compatibility.
SCHEMA_VERSION = 1


def export(out_dir: Path | None = None) -> Path:
    """Dump tables + sparse artifacts to a versioned tarball.

    Returns the path to the produced tarball. Idempotent: calling
    twice on the same day overwrites the previous output.
    """
    if not db.ping():
        raise RuntimeError("CedarDB unreachable — run `make db-up-bg`")

    out_dir = out_dir or RELEASE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    tarball_name = f"winetone-data-{today}.tar.gz"
    tarball_path = out_dir / tarball_name

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "parquet").mkdir()
        (tmp_path / "sparse").mkdir()

        manifest: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "tables": {},
            "sparse": {},
        }

        eng = db.engine()
        for table in EXPORT_TABLES:
            log.info("exporting table: %s", table)
            try:
                df = pd.read_sql(f"SELECT * FROM {table}", eng)
            except Exception as e:  # noqa: BLE001
                log.warning("skipping %s (%s)", table, e)
                continue
            target = tmp_path / "parquet" / f"{table}.parquet"
            df.to_parquet(target, index=False)
            manifest["tables"][table] = {
                "rows": len(df),
                "bytes": target.stat().st_size,
                "columns": list(df.columns),
            }
            log.info("  %s: %d rows, %s",
                     table, len(df), _human(target.stat().st_size))

        # Sparse artifacts.
        from winetone import embed_sparse
        if embed_sparse.MATRIX_PATH.exists():
            shutil.copy2(
                embed_sparse.MATRIX_PATH,
                tmp_path / "sparse" / "tfidf_matrix.joblib",
            )
            shutil.copy2(
                embed_sparse.VECTORIZER_PATH,
                tmp_path / "sparse" / "tfidf_vectorizer.joblib",
            )
            manifest["sparse"] = {
                "tfidf_matrix.joblib": (tmp_path / "sparse" / "tfidf_matrix.joblib").stat().st_size,
                "tfidf_vectorizer.joblib": (tmp_path / "sparse" / "tfidf_vectorizer.joblib").stat().st_size,
            }
            log.info("  sparse: %s + %s",
                     _human((tmp_path / "sparse" / "tfidf_matrix.joblib").stat().st_size),
                     _human((tmp_path / "sparse" / "tfidf_vectorizer.joblib").stat().st_size))
        else:
            log.warning("sparse artifacts not present — skipping")

        # Manifest.
        (tmp_path / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str)
        )

        # Tarball.
        log.info("writing tarball: %s", tarball_path)
        with tarfile.open(tarball_path, "w:gz", compresslevel=6) as tar:
            tar.add(tmp_path, arcname="winetone-data")

    log.info(
        "release written: %s (%s)", tarball_path, _human(tarball_path.stat().st_size)
    )
    return tarball_path


def import_release(tarball_path: Path) -> dict[str, object]:
    """Load a release tarball into CedarDB.

    Drops and recreates the target tables — this is meant for a
    fresh-clone-of-the-repo bootstrap, not for incremental updates.
    Returns the manifest dict.
    """
    if not db.ping():
        raise RuntimeError("CedarDB unreachable — run `make db-up-bg`")

    tarball_path = Path(tarball_path).resolve()
    if not tarball_path.exists():
        raise FileNotFoundError(tarball_path)

    log.info("extracting %s", tarball_path)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(tarball_path, "r:gz") as tar:
            tar.extractall(tmp_path, filter="data")
        root = tmp_path / "winetone-data"
        if not root.exists():
            # Some tarballs may not have the top-level dir; fall back.
            root = tmp_path

        manifest = json.loads((root / "manifest.json").read_text())
        if manifest["schema_version"] != SCHEMA_VERSION:
            raise RuntimeError(
                f"manifest schema_version={manifest['schema_version']} "
                f"!= expected {SCHEMA_VERSION}; upgrade winetone"
            )

        # Load Parquets via pandas → CedarDB.
        eng = db.engine()
        from sqlalchemy import text as _text
        autocommit = eng.execution_options(isolation_level="AUTOCOMMIT")

        for table, meta in manifest["tables"].items():
            log.info("importing %s (%d rows)", table, meta["rows"])
            df = pd.read_parquet(root / "parquet" / f"{table}.parquet")
            # Drop in autocommit (CedarDB quirk — same as canonicalize.py)
            with autocommit.connect() as conn:
                conn.execute(_text(f"DROP TABLE IF EXISTS {table}"))
            df.to_sql(
                table, eng, index=False, if_exists="replace",
                chunksize=10000,
            )

        # Sparse artifacts → data/canonical/sparse/.
        from winetone import embed_sparse
        embed_sparse.SPARSE_DIR.mkdir(parents=True, exist_ok=True)
        if (root / "sparse" / "tfidf_matrix.joblib").exists():
            shutil.copy2(
                root / "sparse" / "tfidf_matrix.joblib",
                embed_sparse.MATRIX_PATH,
            )
            shutil.copy2(
                root / "sparse" / "tfidf_vectorizer.joblib",
                embed_sparse.VECTORIZER_PATH,
            )
            log.info("imported sparse artifacts")

    log.info("import complete: %s", manifest["exported_at"])
    return manifest


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n //= 1024
    return f"{n:.1f} TB"
