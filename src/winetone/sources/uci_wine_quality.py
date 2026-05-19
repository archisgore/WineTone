"""UCI Wine Quality dataset — red + white Vinho Verde.

  https://archive.ics.uci.edu/dataset/186/wine+quality

Cortez et al. (2009). Two CSVs, ~6500 rows total, semicolon-separated.
11 physicochemical features + a 0–10 quality score panel-averaged from
sensory data. All-numeric, no missing values.

Schema (both red and white):
    fixed acidity, volatile acidity, citric acid, residual sugar,
    chlorides, free sulfur dioxide, total sulfur dioxide, density,
    pH, sulphates, alcohol, quality
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from winetone.sources.base import FetchResult, Source, http_get

RED_URL = (
    "https://archive.ics.uci.edu/ml/machine-learning-databases/"
    "wine-quality/winequality-red.csv"
)
WHITE_URL = (
    "https://archive.ics.uci.edu/ml/machine-learning-databases/"
    "wine-quality/winequality-white.csv"
)


class UciWineQuality(Source):
    name = "uci_wine_quality"
    description = "UCI Wine Quality — red + white Vinho Verde, ~6500 rows"
    homepage = "https://archive.ics.uci.edu/dataset/186/wine+quality"

    def fetch(self) -> list[FetchResult]:
        return [
            FetchResult("winequality-red.csv", http_get(RED_URL)),
            FetchResult("winequality-white.csv", http_get(WHITE_URL)),
        ]

    def parse(self, raw_files: dict[str, Path]) -> pd.DataFrame:
        red = pd.read_csv(raw_files["winequality-red.csv"], sep=";")
        red["wine_color"] = "red"
        white = pd.read_csv(raw_files["winequality-white.csv"], sep=";")
        white["wine_color"] = "white"

        df = pd.concat([red, white], ignore_index=True)
        # Normalize column names: spaces → underscores, lowercase.
        df.columns = [c.replace(" ", "_").lower() for c in df.columns]
        # Make types explicit + memory-tight.
        for c in df.columns:
            if c in ("wine_color",):
                df[c] = df[c].astype("string")
            elif c == "quality":
                df[c] = df[c].astype("int8")
            else:
                df[c] = df[c].astype("float32")
        return df
