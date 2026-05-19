"""Wikidata SPARQL source — cross-reference anchor for entity resolution.

One query: every Wikidata entity that's a wine (instance of Q282 or
any subclass), with its producer / country / region / grape variety
where present. This single query produces three useful join surfaces:

* Distinct **wines** with stable Q-IDs.
* Distinct **wineries** (via `wdt:P176 manufacturer`).
* Distinct **grape varieties** (via `wdt:P186 made from material`).

We initially wrote separate top-level queries for wineries and
varieties (using `wdt:P31 wd:Q420684` and `wd:Q1043016` respectively),
but those returned zero rows in practice — Wikidata's winery and
grape entities use various typing patterns and the strict-P31 join
matches very little. The "wine → manufacturer / variety" path inside
this one query reliably surfaces both via real wine entries.

Why this matters for Phase 2 (entity resolution): Wikidata gives us
**stable Q-IDs** keyed by canonical entity. When TTB COLA says
"Robert Mondavi" and WineEnthusiast says "Mondavi", both can resolve
to the same Wikidata Q-ID and we know they're the same producer.

Politeness: Wikidata's Query Service is a shared community
resource. We use a descriptive User-Agent (per their guidelines at
<https://meta.wikimedia.org/wiki/User-Agent_policy>) and accept
JSON via Accept-header negotiation rather than the SPARQL endpoint's
default HTML.

Output: one Parquet file. Each row is one (wine, optional winery,
optional country, optional region, optional variety) tuple; missing
fields are NULL. Downstream consumers can pivot for distinct
wineries / varieties at query time.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pandas as pd

from winetone.sources.base import FetchResult, Source

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

_USER_AGENT = (
    "winetone-data-pipeline/0.1 "
    "(+https://github.com/archisgore/WineTone; me@archisgore.com)"
)

# Each query is bounded by LIMIT so a single fetch is a single HTTP
# round-trip. If we ever need more than these limits, we'll paginate
# via OFFSET — but the practical Wikidata corpus for wine is small.

QUERY_WINES = """
SELECT DISTINCT ?wine ?wineLabel
                ?winery ?wineryLabel
                ?country ?countryLabel
                ?region ?regionLabel
                ?variety ?varietyLabel
WHERE {
  ?wine wdt:P31/wdt:P279* wd:Q282 .
  OPTIONAL { ?wine wdt:P176 ?winery . }
  OPTIONAL { ?wine wdt:P17  ?country . }
  OPTIONAL { ?wine wdt:P131 ?region . }
  OPTIONAL { ?wine wdt:P186 ?variety . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT 20000
"""


def _sparql(query: str) -> bytes:
    with httpx.Client(
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/sparql-results+json",
        },
        follow_redirects=True,
        timeout=120.0,
    ) as client:
        r = client.post(SPARQL_ENDPOINT, data={"query": query})
        r.raise_for_status()
        return r.content


def _bindings_to_rows(payload: bytes) -> list[dict[str, object]]:
    obj = json.loads(payload)
    rows: list[dict[str, object]] = []
    for b in obj["results"]["bindings"]:
        row: dict[str, object] = {}
        for key, cell in b.items():
            row[key] = cell["value"]
        rows.append(row)
    return rows


def _qid(uri: object) -> str | None:
    """Extract the bare Q-id from a Wikidata entity URI.

    Tolerates NaN floats and None — pandas inserts those for SPARQL
    OPTIONAL columns where the binding was absent.
    """
    if not isinstance(uri, str) or not uri:
        return None
    return uri.rsplit("/", 1)[-1]


class Wikidata(Source):
    name = "wikidata"
    description = "Wikidata SPARQL — wines, wineries, grape varieties"
    homepage = "https://query.wikidata.org"

    def fetch(self) -> list[FetchResult]:
        return [
            FetchResult("wines.json", _sparql(QUERY_WINES)),
        ]

    def parse(self, raw_files: dict[str, Path]) -> pd.DataFrame:
        wines = pd.DataFrame(
            _bindings_to_rows(raw_files["wines.json"].read_bytes())
        )
        if wines.empty:
            # Schema-only frame so downstream consumers can still
            # introspect columns.
            return pd.DataFrame(
                columns=[
                    "wd_id",
                    "label",
                    "winery_qid",
                    "winery_label",
                    "country_qid",
                    "country_label",
                    "region_qid",
                    "region_label",
                    "variety_qid",
                    "variety_label",
                ]
            )

        wines["wd_id"] = wines["wine"].map(_qid)
        wines = wines.rename(
            columns={
                "wineLabel": "label",
                "winery": "winery_qid",
                "wineryLabel": "winery_label",
                "country": "country_qid",
                "countryLabel": "country_label",
                "region": "region_qid",
                "regionLabel": "region_label",
                "variety": "variety_qid",
                "varietyLabel": "variety_label",
            }
        )
        for c in ("winery_qid", "country_qid", "region_qid", "variety_qid"):
            if c in wines.columns:
                wines[c] = wines[c].map(_qid)
        wines = wines.drop(columns=["wine"])

        # Stable column order: identity first, then attrs.
        front = ["wd_id", "label"]
        rest = [c for c in wines.columns if c not in front]
        wines = wines[front + rest]
        for c in wines.columns:
            wines[c] = wines[c].astype("string")
        return wines


__all__ = ["Wikidata"]
