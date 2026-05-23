"""Starter-style onboarding — pick a palate archetype and get a
suggested set of wines to label.

The goal is to reduce the "label five wines from a blank slate"
friction. A new user picks one of three archetypes; the dashboard
then surfaces five corpus wines that match that archetype, ready
to be labelled in the user's own words. The user still types the
descriptions themselves — the system only narrows what to label.

Anchor phrases are encoded via the live encoder and matched against
the existing wine_embeddings via cosine similarity. Concrete wines
are picked at runtime rather than hand-curated wine_ids so the
starter set survives corpus updates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd
from sqlalchemy import text

from winetone import db, embed

log = logging.getLogger(__name__)


@dataclass
class Style:
    key: str          # stored in users.onboarding_style
    name: str         # displayed in the picker
    blurb: str        # one paragraph "this is who you are"
    anchor: str       # phrase used to find starter wines


STYLES: list[Style] = [
    Style(
        key="old_world",
        name="The Old-World Structurer",
        blurb=(
            "You reach for wines built on restraint and structure. "
            "Limestone minerality, savoury earth, taut acidity, and a "
            "willingness to let a bottle wait. Burgundy, Barolo, the "
            "Loire, the Mosel — geography that has been making wine "
            "the same way for centuries."
        ),
        anchor=(
            "earthy mineral terroir-driven restrained limestone "
            "savoury old-world Burgundy Bordeaux Piedmont"
        ),
    ),
    Style(
        key="new_world",
        name="The New-World Fruit-Lover",
        blurb=(
            "You want a wine that says what it is on the first sip. "
            "Ripe fruit, oak with vanilla and toast, generous body, "
            "and no apologies. California, Australia, Argentina — "
            "places where the climate gives the grape every chance "
            "to ripen and the winemaker leans into it."
        ),
        anchor=(
            "ripe fruit-forward jammy bold extracted vanilla oak "
            "Napa Sonoma Barossa Mendoza new-world rich"
        ),
    ),
    Style(
        key="natural",
        name="The Natural-Wine Adventurer",
        blurb=(
            "You want a wine that surprises you. Minimal intervention, "
            "wild ferment, sometimes a little funk and sometimes a "
            "skin-contact orange that throws everyone off. Less about "
            "showing the grape's name, more about showing the moment."
        ),
        anchor=(
            "natural wild ferment funky skin contact orange wine "
            "low intervention zero zero living"
        ),
    ),
]


def get_style(key: str) -> Style | None:
    for s in STYLES:
        if s.key == key:
            return s
    return None


@lru_cache(maxsize=8)
def _anchor_target(key: str) -> np.ndarray:
    """Encode the style's anchor phrase once and cache."""
    style = get_style(key)
    if style is None:
        raise ValueError(f"unknown style {key!r}")
    vec = embed.encode_query(style.anchor)
    return vec / (np.linalg.norm(vec) + 1e-9)


def starter_wines(style_key: str, k: int = 5) -> list[dict]:
    """Find k catalog wines that best match the style's anchor phrase.

    Uses the same dense-embedding cosine search the recommender uses,
    minus the user-projection step (since the user has no projection
    yet at onboarding time). Picks wines with at least one source
    record so we avoid edge cases in the corpus.
    """
    target = _anchor_target(style_key)
    ids, vecs = embed.load_embeddings()
    if len(ids) == 0:
        return []
    sims = vecs @ target
    # Take the top 50 and then de-duplicate by producer so the user
    # doesn't see five Burgundies from the same domaine.
    top_n = max(50, k * 10)
    top_idx = np.argpartition(-sims, kth=min(top_n, len(sims) - 1))[:top_n]
    top_ids = [ids[i] for i in top_idx]
    top_scores = {ids[i]: float(sims[i]) for i in top_idx}
    placeholders = ",".join(f"'{w}'" for w in top_ids)
    rows = pd.read_sql(
        text(f"""
            SELECT wine_id, producer_display, wine_display, vintage,
                   variety, country, region
              FROM wines
             WHERE wine_id IN ({placeholders})
               AND producer_display IS NOT NULL
        """),
        db.engine(),
    )
    rows["score"] = rows["wine_id"].map(top_scores).astype(float)
    rows = rows.sort_values("score", ascending=False)
    # De-duplicate by producer.
    seen_producers: set[str] = set()
    picks: list[dict] = []
    for _, row in rows.iterrows():
        p = (row["producer_display"] or "").strip().lower()
        if p in seen_producers:
            continue
        seen_producers.add(p)
        picks.append(row.to_dict())
        if len(picks) >= k:
            break
    return picks


def set_user_style(user_id: str, style_key: str | None) -> None:
    """Persist the user's chosen style. None clears it."""
    with db.connect() as conn:
        conn.execute(
            text("UPDATE users SET onboarding_style = :s WHERE user_id = :u"),
            {"s": style_key, "u": user_id},
        )


def get_user_style(user_id: str) -> str | None:
    """Read the user's chosen style. Tolerates the column being
    missing on databases where the migration hasn't applied yet
    (returns None in that case)."""
    try:
        with db.engine().connect() as conn:
            row = conn.execute(
                text("SELECT onboarding_style FROM users WHERE user_id = :u"),
                {"u": user_id},
            ).fetchone()
        if row is None:
            return None
        return row[0]
    except Exception as e:  # noqa: BLE001
        if "onboarding_style" in str(e):
            return None
        raise
