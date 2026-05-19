"""UCI Wine dataset (the 178-sample 3-cultivar set).

  https://archive.ics.uci.edu/dataset/109/wine

Aeberhard et al. Three Italian cultivars from the same region, 13
chemical attributes each (alcohol, malic acid, ash, alcalinity of ash,
magnesium, total phenols, flavanoids, nonflavanoid phenols,
proanthocyanins, color intensity, hue, OD280/OD315, proline). No
header in the raw file; the column meanings come from wine.names.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from winetone.sources.base import FetchResult, Source, http_get

DATA_URL = (
    "https://archive.ics.uci.edu/ml/machine-learning-databases/"
    "wine/wine.data"
)
NAMES_URL = (
    "https://archive.ics.uci.edu/ml/machine-learning-databases/"
    "wine/wine.names"
)

# Column order is fixed by the wine.names documentation.
COLUMNS = [
    "cultivar_id",      # 1, 2, or 3
    "alcohol",
    "malic_acid",
    "ash",
    "alcalinity_of_ash",
    "magnesium",
    "total_phenols",
    "flavanoids",
    "nonflavanoid_phenols",
    "proanthocyanins",
    "color_intensity",
    "hue",
    "od280_od315",
    "proline",
]


class UciWine(Source):
    name = "uci_wine"
    description = "UCI Wine — 178 samples, 3 Italian cultivars, 13 attrs"
    homepage = "https://archive.ics.uci.edu/dataset/109/wine"

    def fetch(self) -> list[FetchResult]:
        return [
            FetchResult("wine.data", http_get(DATA_URL)),
            FetchResult("wine.names", http_get(NAMES_URL)),
        ]

    def parse(self, raw_files: dict[str, Path]) -> pd.DataFrame:
        df = pd.read_csv(raw_files["wine.data"], header=None, names=COLUMNS)
        df["cultivar_id"] = df["cultivar_id"].astype("int8")
        df["magnesium"] = df["magnesium"].astype("int16")
        df["proline"] = df["proline"].astype("int32")
        for c in df.columns:
            if df[c].dtype == "float64":
                df[c] = df[c].astype("float32")
        return df
