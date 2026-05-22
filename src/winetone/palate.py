"""Palate report — interpretable summary of a user's calibrated palate.

Computes five "where do they sit on this axis?" scores and a list of
distinctive descriptors, all derived from the user's positive labels.

The axes are deliberately phrase-anchored rather than wine-anchored:
- The encoder turns a phrase like "ripe fruit jammy" into a point in
  the same 384-dim space as the wine embeddings, so cosine-similarity
  comparisons are well-defined.
- Hand-curated wine_ids would give marginally better signal but
  require ongoing maintenance as the corpus changes. Phrases are
  declarative and self-documenting.

Each axis is defined by two phrase clusters, one anchoring each end.
A user's palate centroid (the mean of their positive labels' wine
embeddings) is scored against each cluster's mean embedding, and the
relative cosine similarity becomes the axis position in [0, 1].

Distinctive descriptors are extracted from the user's label
description text via a simple TF-relative-frequency measure: words
that appear in the user's labels much more often than they appear
in the global corpus's user_labels.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd
from sqlalchemy import text

from winetone import db, embed

log = logging.getLogger(__name__)


@dataclass
class Axis:
    """A single palate axis with two phrase anchors."""
    key: str                  # short id like "savory_fruity"
    left_label: str           # e.g. "Savory"
    right_label: str          # e.g. "Fruity"
    left_blurb: str           # one-line explanation of the left end
    right_blurb: str          # one-line explanation of the right end
    left_anchors: list[str]
    right_anchors: list[str]


# Five axes that map reasonably to how people actually talk about wine.
# Each end has 3-5 phrase anchors — multiple phrases let the centroid
# absorb stylistic variation without one weird phrase dominating.
AXES: list[Axis] = [
    Axis(
        key="savory_fruity",
        left_label="Savory",
        right_label="Fruity",
        left_blurb="umami, mushroom, forest floor, earth",
        right_blurb="ripe fruit, jammy, juicy, fresh-picked",
        left_anchors=[
            "savory mushroom forest floor",
            "umami soy earthy",
            "leather tobacco dried herbs",
            "truffle wet leaves savory",
        ],
        right_anchors=[
            "ripe strawberry juicy fruit",
            "jammy blackberry cassis",
            "candied red fruit cherry",
            "fresh-picked plum raspberry",
        ],
    ),
    Axis(
        key="structured_soft",
        left_label="Structured",
        right_label="Soft",
        left_blurb="tannic grip, taut acidity, age-worthy",
        right_blurb="round, easy, plush, immediately pleasurable",
        left_anchors=[
            "tannic grip structured age-worthy",
            "taut acidity firm structure",
            "muscular powerful concentrated",
            "tight clenched needs time",
        ],
        right_anchors=[
            "soft round plush easy",
            "supple silky open-knit",
            "smooth approachable immediately pleasurable",
            "velvety creamy generous",
        ],
    ),
    Axis(
        key="old_new_world",
        left_label="Old World",
        right_label="New World",
        left_blurb="terroir, restraint, mineral",
        right_blurb="fruit-forward, oak, vanilla, extraction",
        left_anchors=[
            "earthy mineral terroir-driven restrained",
            "wet stones limestone chalk",
            "classical Burgundy Bordeaux restrained",
            "old-world precision elegance",
        ],
        right_anchors=[
            "bold extracted American oak vanilla",
            "fruit-forward jammy ripe full-throttle",
            "modern Napa Sonoma rich extraction",
            "vanillin toast caramel butter",
        ],
    ),
    Axis(
        key="light_bold",
        left_label="Light",
        right_label="Bold",
        left_blurb="delicate, refreshing, pale, low alcohol",
        right_blurb="full-bodied, powerful, intense, high alcohol",
        left_anchors=[
            "delicate light-bodied refreshing",
            "pale crisp easy-drinking",
            "low alcohol fresh transparent",
            "ethereal subtle quiet",
        ],
        right_anchors=[
            "full-bodied powerful intense concentrated",
            "high alcohol rich opulent",
            "muscular hedonistic big",
            "dense brooding heavyweight",
        ],
    ),
    Axis(
        key="dry_sweet",
        left_label="Dry",
        right_label="Off-dry",
        left_blurb="bone dry, austere, no residual sugar",
        right_blurb="off-dry, honey, residual sweetness",
        left_anchors=[
            "bone dry austere no residual sugar",
            "lean linear unadorned",
            "racy acidity zero sweetness",
            "uncompromising dry brut",
        ],
        right_anchors=[
            "off-dry honey residual sweetness",
            "demi-sec sweet ripe perfumed",
            "luscious botrytis dessert",
            "kabinett spätlese hint of sugar",
        ],
    ),
]


@dataclass
class PalatePoint:
    axis: Axis
    position: float           # 0 → left anchor, 1 → right anchor
    confidence: float         # 0 → no signal, 1 → strong signal


@dataclass
class PalateReport:
    user_id: str
    display_name: str
    n_labels: int
    points: list[PalatePoint]
    descriptors: list[str]      # top distinctive words
    representative_labels: list[dict]


# --- Encoder helpers (cached so repeated calls don't re-embed) ----------

@lru_cache(maxsize=1)
def _axis_anchors() -> dict[str, dict[str, np.ndarray]]:
    """Embed each axis's anchor phrases once at module-load and cache."""
    out: dict[str, dict[str, np.ndarray]] = {}
    for axis in AXES:
        left_embs = np.stack([embed.encode_query(a) for a in axis.left_anchors])
        right_embs = np.stack([embed.encode_query(a) for a in axis.right_anchors])
        # L2-normalize the centroids since we'll use cosine sim.
        left_centroid = left_embs.mean(axis=0)
        left_centroid = left_centroid / (np.linalg.norm(left_centroid) + 1e-9)
        right_centroid = right_embs.mean(axis=0)
        right_centroid = right_centroid / (np.linalg.norm(right_centroid) + 1e-9)
        out[axis.key] = {"left": left_centroid, "right": right_centroid}
    return out


def _user_palate_centroid(user_id: str) -> tuple[np.ndarray | None, int]:
    """Mean wine-embedding of the user's POSITIVE labels.

    Returns (centroid, n_used). Centroid is None and n_used is 0 if
    the user has no positive labels (or the wines have no embeddings).
    """
    df = pd.read_sql(
        text("""
            SELECT we.wine_id, we.embedding
              FROM user_labels ul
              JOIN wine_embeddings we ON we.wine_id = ul.wine_id
             WHERE ul.user_id = :u
               AND ul.sentiment = 'positive'
        """),
        db.engine(),
        params={"u": user_id},
    )
    if df.empty:
        return None, 0
    # pgvector returns strings like "[0.1,0.2,...]" — parse them.
    vecs = []
    for raw in df["embedding"]:
        if isinstance(raw, str):
            v = np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32)
        else:
            v = np.asarray(raw, dtype=np.float32)
        if v.shape[0] == embed.EMBEDDING_DIM:
            vecs.append(v)
    if not vecs:
        return None, 0
    centroid = np.mean(np.stack(vecs), axis=0)
    centroid /= (np.linalg.norm(centroid) + 1e-9)
    return centroid, len(vecs)


_WORD_RE = re.compile(r"[a-z]{3,}")
_STOPWORDS = {
    "the", "and", "but", "for", "with", "are", "was", "this", "that",
    "have", "has", "had", "very", "much", "lot", "wine", "wines",
    "like", "likes", "liked", "really", "good", "bad", "nice", "great",
    "some", "any", "you", "your", "they", "them", "their", "ours",
    "from", "into", "onto", "than", "more", "most", "less", "all",
    "one", "two", "three", "four", "five", "ten",
    "tastes", "taste", "tasting", "tasted",
    "smells", "smell", "smelled", "smelling",
    "feels", "feel", "felt", "feeling",
}


def _distinctive_words(user_id: str, n: int = 5) -> list[str]:
    """Tokens used by THIS user disproportionately vs. the global corpus."""
    # User's tokens.
    user_descs = pd.read_sql(
        text("SELECT description FROM user_labels WHERE user_id = :u"),
        db.engine(), params={"u": user_id},
    )["description"].tolist()
    user_tokens: Counter[str] = Counter()
    for d in user_descs:
        for w in _WORD_RE.findall((d or "").lower()):
            if w not in _STOPWORDS:
                user_tokens[w] += 1
    if not user_tokens:
        return []
    # Global tokens (across all users' labels).
    global_descs = pd.read_sql(
        text("SELECT description FROM user_labels"),
        db.engine(),
    )["description"].tolist()
    global_tokens: Counter[str] = Counter()
    for d in global_descs:
        for w in _WORD_RE.findall((d or "").lower()):
            if w not in _STOPWORDS:
                global_tokens[w] += 1
    total_global = max(sum(global_tokens.values()), 1)
    total_user = max(sum(user_tokens.values()), 1)
    # Score each user-token by relative frequency (Laplace-smoothed).
    scored = []
    for w, cu in user_tokens.items():
        # Skip tokens that only appear once in user — probably noise.
        if cu < 1:
            continue
        global_freq = (global_tokens.get(w, 0) + 1) / (total_global + 1)
        user_freq = cu / total_user
        ratio = user_freq / global_freq
        scored.append((ratio, cu, w))
    scored.sort(reverse=True)
    return [w for _, _, w in scored[:n]]


def _representative_labels(user_id: str, n: int = 5) -> list[dict]:
    """Pick a handful of the user's labels to display on the report."""
    df = pd.read_sql(
        text("""
            SELECT l.wine_id, l.description, l.sentiment,
                   w.producer_display, w.wine_display, w.vintage,
                   w.variety, w.country
              FROM user_labels l
              LEFT JOIN wines w ON w.wine_id = l.wine_id
             WHERE l.user_id = :u
             ORDER BY l.created_at DESC
             LIMIT :n
        """),
        db.engine(),
        params={"u": user_id, "n": n},
    )
    return df.to_dict("records")


def build_report(user_id: str, display_name: str) -> PalateReport:
    """The whole-report computation for one user.

    If the user has zero positive labels, axes return position 0.5
    with confidence 0 (so the UI shows the sliders all at midpoint
    with a "needs more labels" message).
    """
    centroid, n_used = _user_palate_centroid(user_id)
    points: list[PalatePoint] = []
    if centroid is None:
        for axis in AXES:
            points.append(PalatePoint(axis=axis, position=0.5, confidence=0.0))
    else:
        anchors = _axis_anchors()
        # Confidence rises with label count and saturates around 5.
        confidence = min(1.0, n_used / 5.0)
        for axis in AXES:
            left = anchors[axis.key]["left"]
            right = anchors[axis.key]["right"]
            sim_left = float(np.dot(centroid, left))
            sim_right = float(np.dot(centroid, right))
            # Linear interpolation: position from 0 (entirely left) to
            # 1 (entirely right). Scale by the gap so similar wines map
            # to similar positions across users.
            diff = sim_right - sim_left
            # Empirically the cosine differences live in roughly
            # [-0.25, 0.25] given our axes; sigmoid'd so the slider
            # spans nicely without saturating at the extremes.
            position = 1.0 / (1.0 + np.exp(-diff * 8.0))
            points.append(PalatePoint(
                axis=axis,
                position=float(position),
                confidence=confidence,
            ))
    return PalateReport(
        user_id=user_id,
        display_name=display_name,
        n_labels=n_used,
        points=points,
        descriptors=_distinctive_words(user_id),
        representative_labels=_representative_labels(user_id),
    )
