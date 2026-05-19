"""Filesystem layout for the data pipeline.

The convention follows the architecture from docs/DATA-AND-ML-PIPELINE-PLAN.md:

    data/
    ├── raw/<source>/<fetch_date>/    append-only verbatim payloads
    │                                  hash-keyed filenames, never overwrite
    ├── staging/<source>/             cleaned + typed Parquet files
    │                                  produced from raw by the parser
    └── (later)                       canonical wines, features, embeddings
                                       — live in Postgres + pgvector, not the FS.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("WINETONE_DATA_ROOT", REPO_ROOT / "data"))
RAW_ROOT = DATA_ROOT / "raw"
STAGING_ROOT = DATA_ROOT / "staging"


def today_utc() -> str:
    """Return today's date in UTC as YYYY-MM-DD. Stable per process run."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def raw_dir(source: str, fetch_date: str | None = None) -> Path:
    """Return data/raw/<source>/<fetch_date>/ — creating it on demand."""
    d = RAW_ROOT / source / (fetch_date or today_utc())
    d.mkdir(parents=True, exist_ok=True)
    return d


def staging_dir(source: str) -> Path:
    """Return data/staging/<source>/ — creating it on demand."""
    d = STAGING_ROOT / source
    d.mkdir(parents=True, exist_ok=True)
    return d
