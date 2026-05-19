"""WineEnthusiast 150k (the v1 corpus, ~150k reviews).

Companion to `wine_enthusiast_130k`. The original scrape of
winemag.com — different rows, partial overlap with the 130k v2
dataset. Useful complement because (a) it has rows the 130k set
doesn't (the v2 was a recompile, not a strict superset), and (b)
the additional rows feed the entity-resolution layer in Phase 2.

Schema: country, description, designation, points, price,
province, region_1, region_2, variety, winery. Same as 130k minus
the taster_name + taster_twitter_handle + title columns.

Acknowledgment: dataset compiled by Zackary Thoutt, public on
Kaggle under CC BY-NC-SA 4.0.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pandas as pd

from winetone.sources.base import FetchResult, Source, http_get

log = logging.getLogger(__name__)

MIRROR_URLS_150K = [
    "https://raw.githubusercontent.com/stoltzmaniac/wine-reviews-kaggle/"
    "master/winemag-data_first150k.csv",
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
        "all WineEnthusiast 150k mirrors failed; the official source is "
        "https://www.kaggle.com/datasets/zynicide/wine-reviews"
    ) from last_exc


class WineEnthusiast150k(Source):
    name = "wine_enthusiast_150k"
    description = (
        "WineEnthusiast 150k sommelier reviews (Kaggle / Thoutt, v1 corpus)"
    )
    homepage = "https://www.kaggle.com/datasets/zynicide/wine-reviews"

    def fetch(self) -> list[FetchResult]:
        content = _try_mirrors(MIRROR_URLS_150K)
        return [FetchResult("winemag-data_first150k.csv", content)]

    def parse(self, raw_files: dict[str, Path]) -> pd.DataFrame:
        df = pd.read_csv(raw_files["winemag-data_first150k.csv"], index_col=0)
        df.columns = [c.replace(" ", "_").lower() for c in df.columns]
        if "points" in df.columns:
            df["points"] = df["points"].astype("Int16")
        if "price" in df.columns:
            df["price"] = df["price"].astype("Float32")
        for c in (
            "country",
            "description",
            "designation",
            "province",
            "region_1",
            "region_2",
            "variety",
            "winery",
        ):
            if c in df.columns:
                df[c] = df[c].astype("string")
        return df
