"""Associate later profile captures with an explicitly backfilled session.

Revision ID: 0008_asx_profile_backfills
Revises: 0007_asx_200_snapshots
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_asx_profile_backfills"
down_revision = "0007_asx_200_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asx_profile_backfills",
        sa.Column("session_date", sa.Date(), primary_key=True),
        sa.Column("ticker", sa.String(20), primary_key=True),
        sa.Column(
            "raw_payload_id",
            sa.String(36),
            sa.ForeignKey("raw_source_payloads.raw_payload_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("asx_profile_backfills")
