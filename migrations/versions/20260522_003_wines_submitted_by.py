"""Add wines.submitted_by_user_id so user-submitted wines can be
attributed back to their submitter.

The wines table is a mix of pipeline-imported entries (no submitter)
and user-submitted entries (a real user). Today the submitter is
only mentioned in a log line; nothing persists. This migration adds
a nullable FK so the dashboard can render "wines you've added" and
the wine-detail page can credit the submitter.

`ON DELETE SET NULL` matches the privacy policy: deleting a user
removes their labels and follow graph, but their wine submissions
remain in the public catalog as attribution-less entries
indistinguishable from pipeline-imported wines.

Partial index on the non-null subset — most rows (the 164K from the
pipeline) never get a value here, so a full index would be mostly
wasted space.

Revision ID: 20260522_003_wines_submitted_by
Revises: 20260522_002_user_labels_unique
Create Date: 2026-05-22
"""
from alembic import op

revision = "20260522_003_wines_submitted_by"
down_revision = "20260522_002_user_labels_unique"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE wines
            ADD COLUMN IF NOT EXISTS submitted_by_user_id UUID
            REFERENCES users(user_id) ON DELETE SET NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS wines_submitted_by_idx
            ON wines (submitted_by_user_id)
         WHERE submitted_by_user_id IS NOT NULL
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS wines_submitted_by_idx")
    op.execute("ALTER TABLE wines DROP COLUMN IF EXISTS submitted_by_user_id")
