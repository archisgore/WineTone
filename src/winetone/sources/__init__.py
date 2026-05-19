"""Registry of all WineTone data sources.

The CLI uses `SOURCES` as the authoritative registry. Adding a new
source is a two-step diff: implement a `Source` subclass under
`winetone.sources.<name>`, then add it to this dict.
"""

from __future__ import annotations

from winetone.sources.base import Source
from winetone.sources.uci_wine import UciWine
from winetone.sources.uci_wine_quality import UciWineQuality
from winetone.sources.wikidata import Wikidata
from winetone.sources.wine_enthusiast import WineEnthusiast130k
from winetone.sources.wine_enthusiast_150k import WineEnthusiast150k

SOURCES: dict[str, type[Source]] = {
    UciWineQuality.name: UciWineQuality,
    UciWine.name: UciWine,
    WineEnthusiast130k.name: WineEnthusiast130k,
    WineEnthusiast150k.name: WineEnthusiast150k,
    Wikidata.name: Wikidata,
}


def get(name: str) -> Source:
    """Return an instance of the named source, or raise KeyError."""
    cls = SOURCES[name]
    return cls()
