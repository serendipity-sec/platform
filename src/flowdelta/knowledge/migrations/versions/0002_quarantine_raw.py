"""Retain source material needed to retry quarantined records."""

from alembic import op

revision = "kb_0002"
down_revision = "kb_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE knowledge.sync_failure ADD COLUMN raw_path text")
    op.execute("ALTER TABLE knowledge.sync_failure ADD COLUMN raw_external_id bytea")
    op.execute("ALTER TABLE knowledge.sync_failure ADD COLUMN modified_at text")


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; destructive downgrade is not automated")
