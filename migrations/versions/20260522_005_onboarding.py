"""Add users.onboarding_style for the starter-style picker.

A nullable TEXT field that stores which starter style the user
picked during onboarding (one of "old_world", "new_world",
"natural", or NULL meaning they skipped). The dashboard reads this
to render a "your starter wines" affordance for users who have
not yet labelled anything.

Revision ID: 20260522_005_onboarding
Revises: 20260522_004_canon_uniq
Create Date: 2026-05-22
"""
from alembic import op

revision = "20260522_005_onboarding"
down_revision = "20260522_004_canon_uniq"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_style TEXT"
    )


def downgrade():
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS onboarding_style")
