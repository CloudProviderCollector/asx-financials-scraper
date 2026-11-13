"""Store daily S&P/ASX 200 Yahoo Finance snapshots.

Revision ID: 0007_asx_200_snapshots
Revises: 0006_vea_crude_workflow
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_asx_200_snapshots"
down_revision = "0006_vea_crude_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asx_200_snapshots",
        sa.Column("session_date", sa.Date(), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(8)),
        sa.Column("open", sa.Numeric(24, 8)),
        sa.Column("high", sa.Numeric(24, 8)),
        sa.Column("low", sa.Numeric(24, 8)),
        sa.Column("close", sa.Numeric(24, 8)),
        sa.Column("volume", sa.BigInteger()),
        sa.Column("regular_market_price", sa.Numeric(24, 8)),
        sa.Column("previous_close", sa.Numeric(24, 8)),
        sa.Column("average_volume", sa.BigInteger()),
        sa.Column("average_volume_10days", sa.BigInteger()),
        sa.Column("trailing_pe", sa.Numeric(24, 8)),
        sa.Column("forward_pe", sa.Numeric(24, 8)),
        sa.Column("quote_time", sa.DateTime(timezone=True)),
        sa.Column("info", JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("asx_200_snapshots")
