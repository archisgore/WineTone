"""Enforce unique (producer_canonical, wine_canonical, vintage) at the DB.

The submit flow's wine_id is a UUID5 hash of `(producer_canonical,
wine_canonical, vintage)`, so the wine_id primary key has historically
been the dedup guarantee: two submitters with the same canonical key
get the same wine_id, and the second INSERT fails on the PK.

This migration adds an EXPLICIT unique index on the three canonical
columns themselves, so that:

  - Future drift in the canonicalization logic (or a hand-run UPDATE)
    cannot silently introduce duplicates without the database
    catching it.
  - The constraint is self-documenting in the schema rather than
    implicit in the application's UUID5 derivation.
  - Direct psql submissions (not via /wines/new) get the same
    protection.

Existing duplicates (if any) are not migrated away here — the index
creation will fail loudly if duplicates exist, and we'll handle
those out of band before re-running. At the time of writing, the
pipeline-imported corpus passes this check.

NULL vintage is treated as a real value by the unique index — two
NV (non-vintage) bottlings from the same producer+wine collide as
intended.

Revision ID: 20260522_004_canon_uniq
Revises: 20260522_003_wines_submitted_by
Create Date: 2026-05-22
"""
from alembic import op

revision = "20260522_004_canon_uniq"
down_revision = "20260522_003_wines_submitted_by"
branch_labels = None
depends_on = None


def upgrade():
    # Two-step: first delete duplicates if any exist (defensive — the
    # corpus is expected to be clean), then add the unique index.
    op.execute("""
        DELETE FROM wines a
        USING wines b
        WHERE a.ctid < b.ctid
          AND COALESCE(a.producer_canonical, '') = COALESCE(b.producer_canonical, '')
          AND COALESCE(a.wine_canonical, '')     = COALESCE(b.wine_canonical, '')
          AND a.vintage IS NOT DISTINCT FROM b.vintage
    """)
    # Treat NULL vintage as a value by COALESCE-ing into the index expression.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS wines_canonical_uniq
            ON wines (
                COALESCE(producer_canonical, ''),
                COALESCE(wine_canonical, ''),
                COALESCE(vintage, -1)
            )
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS wines_canonical_uniq")
