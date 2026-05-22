"""Make user_labels unique per (user_id, wine_id).

Before this migration, labelling the same wine twice created two rows
in user_labels. That broke the "my note about this wine" mental model
and gave duplicated training pairs to the calibrator.

The fix has three parts:
1. Dedupe existing rows: keep the most-recent label per (user, wine).
2. Prune the orphaned user_label_embeddings rows whose user_labels
   parent we just deleted.
3. Add a UNIQUE INDEX on user_labels(user_id, wine_id) so future
   INSERTs that should be UPDATEs trigger ON CONFLICT instead of
   creating a duplicate.

Revision ID: 20260522_002_user_labels_unique
Revises: 20260521_001_age_confirmation
Create Date: 2026-05-22
"""
from alembic import op

revision = "20260522_002_user_labels_unique"
down_revision = "20260521_001_age_confirmation"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Dedupe user_labels: keep the most-recent row per
    #    (user_id, wine_id), delete the older copies. Tie-breaks by
    #    physical row identity (ctid) so the deletion is deterministic
    #    even when created_at ties.
    op.execute("""
        DELETE FROM user_labels a
        USING user_labels b
        WHERE a.user_id = b.user_id
          AND a.wine_id = b.wine_id
          AND (a.created_at, a.ctid) < (b.created_at, b.ctid)
    """)

    # 2. Prune orphaned user_label_embeddings whose description no
    #    longer matches the (now-deduped) user_labels row. This keeps
    #    the embedding table consistent with what calibrate.fit() will
    #    see when it joins them.
    op.execute("""
        DELETE FROM user_label_embeddings e
        WHERE NOT EXISTS (
            SELECT 1 FROM user_labels l
             WHERE l.user_id = e.user_id
               AND l.wine_id = e.wine_id
               AND l.description = e.description
        )
    """)

    # 3. The unique constraint. After dedupe this is safe to add.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS user_labels_user_wine_uniq
            ON user_labels (user_id, wine_id)
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS user_labels_user_wine_uniq")
    # Dedupe is not reversible — we can't recover the discarded rows.
