"""Baseline schema — capture every table currently in production.

Revision ID: 20260521_000_baseline
Revises:
Create Date: 2026-05-21

This is the schema we already have on Neon and on local CedarDB as of
2026-05-21. The upgrade() does nothing on a database that's already
populated (the IF NOT EXISTS guards handle the no-op case); on a
fresh database it creates everything needed to run WineTone end to
end.

To mark an existing live DB as having this baseline applied without
actually running it:
    alembic stamp 20260521_000_baseline

Future migrations should depend on this revision via `down_revision`
and define their own narrow up/down.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260521_000_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ── pgvector extension ──────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── canonical wine tables ───────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS wines (
            wine_id            TEXT PRIMARY KEY,
            producer_canonical TEXT,
            wine_canonical     TEXT,
            vintage            DOUBLE PRECISION,
            producer_display   TEXT,
            wine_display       TEXT,
            variety            TEXT,
            country            TEXT,
            region             TEXT,
            n_source_records   BIGINT,
            sources_seen       TEXT,
            tsv                tsvector
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS wines_tsv_gin ON wines USING GIN (tsv)"
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS wine_features (
            wine_id            TEXT PRIMARY KEY,
            producer_canonical TEXT,
            wine_canonical     TEXT,
            vintage            DOUBLE PRECISION,
            producer_display   TEXT,
            wine_display       TEXT,
            variety            TEXT,
            country            TEXT,
            region             TEXT,
            n_source_records   BIGINT,
            sources_seen       TEXT,
            n_reviews          BIGINT,
            median_points      DOUBLE PRECISION,
            max_points         DOUBLE PRECISION,
            median_price       DOUBLE PRECISION,
            review_text_all    TEXT
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS wine_embeddings (
            wine_id         TEXT PRIMARY KEY,
            embedding       vector(384) NOT NULL,
            embedding_model TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS wine_clusters (
            wine_id    TEXT PRIMARY KEY,
            cluster_id INTEGER NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS wine_cluster_centroids (
            cluster_id INTEGER PRIMARY KEY,
            centroid   vector(384) NOT NULL,
            size       INTEGER NOT NULL
        )
    """)

    # ── user / auth tables ──────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id        UUID PRIMARY KEY,
            clerk_user_id  TEXT NOT NULL UNIQUE,
            display_name   TEXT NOT NULL UNIQUE,
            email          TEXT,
            created_at     TIMESTAMP NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_labels (
            user_id     UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            wine_id     TEXT NOT NULL,
            description TEXT NOT NULL,
            sentiment   TEXT NOT NULL DEFAULT 'positive'
                CHECK (sentiment IN ('positive','negative','neutral')),
            created_at  TIMESTAMP NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_projections (
            user_id      UUID PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            n_labels     INTEGER NOT NULL,
            a_serialized BYTEA NOT NULL,
            b_serialized BYTEA NOT NULL,
            fit_at       TIMESTAMP NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_calibration_history (
            user_id      UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            version      INTEGER NOT NULL,
            n_labels     INTEGER NOT NULL,
            backend      TEXT NOT NULL,
            a_serialized BYTEA NOT NULL,
            b_serialized BYTEA NOT NULL,
            loss_final   REAL NOT NULL,
            lambda_a     REAL NOT NULL,
            lambda_b     REAL NOT NULL,
            fit_at       TIMESTAMP NOT NULL,
            PRIMARY KEY (user_id, version)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_label_embeddings (
            user_id          UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            wine_id          TEXT NOT NULL,
            description      TEXT NOT NULL,
            description_hash TEXT NOT NULL,
            embedding        vector(384) NOT NULL,
            encoded_at       TIMESTAMP NOT NULL,
            PRIMARY KEY (user_id, wine_id, description_hash)
        )
    """)

    # ── social follow graph ─────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS follows (
            follower_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            followee_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            weight      REAL NOT NULL DEFAULT 0.3,
            created_at  TIMESTAMP NOT NULL,
            PRIMARY KEY (follower_id, followee_id),
            CONSTRAINT follows_no_self CHECK (follower_id <> followee_id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS follows_followee_idx ON follows (followee_id)"
    )

    # ── moderation / abuse reports ──────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS abuse_reports (
            report_id        UUID PRIMARY KEY,
            reporter_user_id UUID REFERENCES users(user_id) ON DELETE SET NULL,
            target_kind      TEXT NOT NULL
                CHECK (target_kind IN ('label','wine','profile')),
            target_id        TEXT NOT NULL,
            reason           TEXT NOT NULL,
            note             TEXT,
            status           TEXT NOT NULL DEFAULT 'open',
            created_at       TIMESTAMP NOT NULL
        )
    """)


def downgrade():
    # Order matters — drop dependent tables before parents.
    for table in (
        "abuse_reports",
        "follows",
        "user_label_embeddings",
        "user_calibration_history",
        "user_projections",
        "user_labels",
        "users",
        "wine_cluster_centroids",
        "wine_clusters",
        "wine_embeddings",
        "wine_features",
        "wines",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
