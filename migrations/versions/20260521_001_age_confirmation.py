"""Add users.confirmed_age_at for the drinking-age self-attestation gate.

Revision ID: 20260521_001_age_confirmation
Revises: 20260521_000_baseline
Create Date: 2026-05-21
"""
from alembic import op

revision = "20260521_001_age_confirmation"
down_revision = "20260521_000_baseline"
branch_labels = None
depends_on = None


def upgrade():
    # Idempotent — the live Neon DB already has this column from a
    # hand-run ALTER on 2026-05-21 ~19:36 UTC. New databases get the
    # column from this migration.
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS confirmed_age_at TIMESTAMP"
    )


def downgrade():
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS confirmed_age_at")
