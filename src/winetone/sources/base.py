"""Base protocol for data sources.

Every source implements `Source`: it knows how to fetch its raw payload
(idempotently, with retries) and how to parse that payload into a tidy
pandas DataFrame for the staging layer. The framework writes the raw
bytes verbatim to `data/raw/<name>/<date>/` (append-only) and the
parsed Parquet to `data/staging/<name>/`.
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import httpx
import pandas as pd
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
)

from winetone.paths import raw_dir, staging_dir

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """One artifact produced by a source's fetch step."""

    filename: str  # e.g. "winequality-red.csv"
    content: bytes


class Source(ABC):
    """A pullable data source.

    Subclasses implement `name`, `fetch`, and `parse`. The framework
    handles writing raw bytes, retries, hashing, and Parquet output.
    """

    #: short kebab-case slug, used as the directory name
    name: str = ""

    #: human-readable description for the CLI
    description: str = ""

    #: source homepage / docs URL, for the manifest
    homepage: str = ""

    @abstractmethod
    def fetch(self) -> list[FetchResult]:
        """Download every artifact this source produces. Bytes only.

        Implementations should be idempotent and side-effect-free.
        The framework persists the bytes to disk.
        """

    @abstractmethod
    def parse(self, raw_files: dict[str, Path]) -> pd.DataFrame:
        """Parse the raw artifacts into a tidy DataFrame.

        `raw_files` maps filename → on-disk path of the verbatim bytes.
        Return one DataFrame; the framework writes it to Parquet.
        """

    # --- framework methods (concrete) -----------------------------------

    def run(self) -> dict[str, object]:
        """Execute fetch + parse, persist outputs, return a manifest dict."""
        fetched = self.fetch()
        raw_files: dict[str, Path] = {}
        raw_meta: list[dict[str, object]] = []
        raw_d = raw_dir(self.name)

        for art in fetched:
            sha256 = hashlib.sha256(art.content).hexdigest()
            path = raw_d / art.filename
            path.write_bytes(art.content)
            raw_files[art.filename] = path
            raw_meta.append(
                {
                    "filename": art.filename,
                    "bytes": len(art.content),
                    "sha256": sha256,
                }
            )
            log.info(
                "raw written: source=%s file=%s bytes=%d sha256=%s",
                self.name,
                art.filename,
                len(art.content),
                sha256[:16],
            )

        df = self.parse(raw_files)
        parquet_path = staging_dir(self.name) / f"{self.name}.parquet"
        df.to_parquet(parquet_path, index=False)
        log.info(
            "staged: source=%s rows=%d cols=%d path=%s",
            self.name,
            len(df),
            len(df.columns),
            parquet_path,
        )

        return {
            "source": self.name,
            "rows": len(df),
            "cols": list(df.columns),
            "raw_files": raw_meta,
            "parquet_path": str(parquet_path),
        }


# --- shared HTTP helper ---------------------------------------------------

_USER_AGENT = (
    "winetone-data-pipeline/0.1 "
    "(+https://github.com/archisgore/WineTone)"
)


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def http_get(url: str, *, timeout: float = 30.0) -> bytes:
    """GET with retries + a polite User-Agent."""
    with httpx.Client(
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
        timeout=timeout,
    ) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.content
