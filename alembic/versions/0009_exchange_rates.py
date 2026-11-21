"""Store daily foreign-exchange closes with source and quote direction.

Revision ID: 0009_exchange_rates
Revises: 0008_asx_profile_backfills
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_exchange_rates"
down_revision = "0008_asx_profile_backfills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "exchange_rates",
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("series", sa.String(80), nullable=False),
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("quote_currency", sa.String(3), nullable=False),
        sa.Column("rate", sa.Numeric(24, 10), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rate > 0", name="ck_exchange_rates_positive"),
        sa.CheckConstraint("base_currency <> quote_currency", name="ck_exchange_rates_currencies"),
        sa.PrimaryKeyConstraint("source", "series", "observation_date"),
    )
    op.create_index(
        "ix_exchange_rates_lookup",
        "exchange_rates",
        ["base_currency", "quote_currency", sa.text("observation_date DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_exchange_rates_lookup", table_name="exchange_rates")
    op.drop_table("exchange_rates")
