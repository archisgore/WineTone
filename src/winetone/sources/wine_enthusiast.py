"""WineEnthusiast 130k reviews (Kaggle's `zynicide/wine-reviews`).

The canonical UGC corpus for English-language wine descriptions. ~130k
reviews scraped from winemag.com in Nov 2017, ten columns: country,
description (the free-text review), designation, points, price,
province, region_1, region_2, taster_name, taster_twitter_handle,
title, variety, winery.

The dataset is hosted on Kaggle and requires Kaggle API credentials
to pull from the official source. Multiple public github mirrors
exist; we fetch from one of them with a documented fallback list. If
all mirrors are unreachable, we surface a clear error pointing to the
official source.

Acknowledgment: dataset compiled by Zackary Thoutt, public on Kaggle
under CC BY-NC-SA 4.0.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pandas as pd

from winetone.sources.base import FetchResult, Source, http_get

log = logging.getLogger(__name__)

# Try these mirrors in order. All return the same CSV; we pick the
# first one that responds with HTTP 200.
MIRROR_URLS_130K = [
    "https://raw.githubusercontent.com/rubyhsing/kaggle_files/"
    "main/winemag-data-130k-v2.csv",
    "https://raw.githubusercontent.com/morisasy/kaggle/"
    "master/data/winemag-data-130k-v2.csv",
    "https://raw.githubusercontent.com/boedybios/kaggle_explorations/"
    "master/belajar_python_pandas/dataset/winemag-data-130k-v2.csv",
]


def _try_mirrors(urls: list[str]) -> bytes:
    last_exc: Exception | None = None
    for url in urls:
        try:
            log.info("trying mirror: %s", url)
            return http_get(url)
        except httpx.HTTPError as e:
            log.warning("mirror failed: %s (%s)", url, e)
            last_exc = e
    raise RuntimeError(
        "all WineEnthusiast mirrors failed; the official source is "
        "https://www.kaggle.com/datasets/zynicide/wine-reviews "
        "(requires Kaggle API credentials)"
    ) from last_exc


class WineEnthusiast130k(Source):
    name = "wine_enthusiast_130k"
    description = "WineEnthusiast 130k sommelier reviews (Kaggle / Thoutt)"
    homepage = "https://www.kaggle.com/datasets/zynicide/wine-reviews"

    def fetch(self) -> list[FetchResult]:
        content = _try_mirrors(MIRROR_URLS_130K)
        return [FetchResult("winemag-data-130k-v2.csv", content)]

    def parse(self, raw_files: dict[str, Path]) -> pd.DataFrame:
        df = pd.read_csv(raw_files["winemag-data-130k-v2.csv"], index_col=0)
        # Column normalization.
        df.columns = [c.replace(" ", "_").lower() for c in df.columns]
        # Typed columns where it matters for downstream Parquet
        # compression + filter pushdown.
        if "points" in df.columns:
            df["points"] = df["points"].astype("Int16")
        if "price" in df.columns:
            df["price"] = df["price"].astype("Float32")
        string_cols = [
            "country",
            "description",
            "designation",
            "province",
            "region_1",
            "region_2",
            "taster_name",
            "taster_twitter_handle",
            "title",
            "variety",
            "winery",
        ]
        for c in string_cols:
            if c in df.columns:
                df[c] = df[c].astype("string")
        return df
