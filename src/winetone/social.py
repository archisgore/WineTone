"""Follow graph + secondhand-calibration helpers.

WineTone is mostly an individual-calibration tool, but most people
joining a new platform don't have 5+ wine labels at the ready. The
follow graph is the cold-start path: pick a user whose vocabulary
resembles yours, follow them, and your fit picks up their labels at a
reduced weight. With a couple of follows + a couple of your own
labels, the projection has enough signal to start placing your queries
intelligently.

Design choices:

  - **One level deep.** A's projection sees A's own labels (weight 1.0)
    + the labels of users A explicitly follows (weight 0.3 each). A's
    projection does NOT cascade into A→B→C territory; that creates
    fanout problems (a label travels through arbitrary depth, and
    rank-1 popular accounts would dominate every fit).

  - **Default weight = 0.3.** Tuned so a user with N follows can still
    have their own labels dominate the fit. With weight 0.3 per follow,
    a user with 3 follows has ~0.9 worth of secondhand labels vs. 1.0
    worth of their own; the regularization keeps things sensible.

  - **No self-follow.** Enforced by a CHECK constraint at the DB level.

  - **Symmetric reads, asymmetric writes.** Anyone can see who follows
    whom (the privacy banner is up — this is research-demo posture).
    Only the follower themselves can add/remove their own follows.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
from sqlalchemy import text

from winetone import db

log = logging.getLogger(__name__)

DEFAULT_FOLLOW_WEIGHT = 0.3


def follow(follower_id: str, followee_id: str,
           weight: float = DEFAULT_FOLLOW_WEIGHT) -> None:
    """Have `follower_id` follow `followee_id`. Idempotent — re-following
    refreshes the weight without erroring."""
    if follower_id == followee_id:
        raise ValueError("can't follow yourself")
    with db.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO follows (follower_id, followee_id, weight, created_at)
                VALUES (:f, :t, :w, :ts)
                ON CONFLICT (follower_id, followee_id) DO UPDATE SET
                    weight = EXCLUDED.weight
            """),
            {"f": follower_id, "t": followee_id,
             "w": float(weight), "ts": datetime.utcnow()},
        )


def unfollow(follower_id: str, followee_id: str) -> None:
    with db.connect() as conn:
        conn.execute(
            text("DELETE FROM follows WHERE follower_id = :f AND followee_id = :t"),
            {"f": follower_id, "t": followee_id},
        )


def is_following(follower_id: str, followee_id: str) -> bool:
    with db.engine().connect() as conn:
        return conn.execute(
            text("SELECT 1 FROM follows "
                 "WHERE follower_id = :f AND followee_id = :t LIMIT 1"),
            {"f": follower_id, "t": followee_id},
        ).first() is not None


def list_following(user_id: str) -> pd.DataFrame:
    """The users `user_id` follows. Returns user_id, display_name, weight,
    n_labels (their public-corpus label count)."""
    return pd.read_sql(
        text("""
            SELECT u.user_id, u.display_name, f.weight, f.created_at,
                   (SELECT COUNT(*) FROM user_labels l WHERE l.user_id = u.user_id) AS n_labels
            FROM follows f JOIN users u ON u.user_id = f.followee_id
            WHERE f.follower_id = :u
            ORDER BY f.created_at DESC
        """),
        db.engine(), params={"u": user_id},
    )


def list_followers(user_id: str) -> pd.DataFrame:
    """The users who follow `user_id`."""
    return pd.read_sql(
        text("""
            SELECT u.user_id, u.display_name, f.weight, f.created_at
            FROM follows f JOIN users u ON u.user_id = f.follower_id
            WHERE f.followee_id = :u
            ORDER BY f.created_at DESC
        """),
        db.engine(), params={"u": user_id},
    )


def count_following(user_id: str) -> int:
    with db.engine().connect() as conn:
        return int(conn.execute(
            text("SELECT COUNT(*) FROM follows WHERE follower_id = :u"),
            {"u": user_id},
        ).scalar() or 0)


def count_followers(user_id: str) -> int:
    with db.engine().connect() as conn:
        return int(conn.execute(
            text("SELECT COUNT(*) FROM follows WHERE followee_id = :u"),
            {"u": user_id},
        ).scalar() or 0)


def labels_with_follow_weights(user_id: str) -> pd.DataFrame:
    """Return a DataFrame of all labels relevant to fitting `user_id`'s
    projection — the user's own labels plus their direct follows'
    labels, each row tagged with the weight it should carry in the loss.

    Schema: user_id, wine_id, description, sentiment, weight.

    The user's own labels get weight 1.0. Followed-user labels get the
    weight stored in follows.weight (default 0.3). One level only —
    follows-of-follows don't propagate.
    """
    return pd.read_sql(
        text("""
            -- Own labels at full weight.
            SELECT user_id, wine_id, description, sentiment, 1.0 AS weight
            FROM user_labels
            WHERE user_id = :u
            UNION ALL
            -- Followed-user labels at the relationship's weight.
            SELECT l.user_id, l.wine_id, l.description, l.sentiment, f.weight
            FROM user_labels l
            JOIN follows f ON f.followee_id = l.user_id
            WHERE f.follower_id = :u
        """),
        db.engine(), params={"u": user_id},
    )
