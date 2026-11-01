"""Persist commodity benchmarks independently of exchange-listed tickers.

Revision ID: 0005_commodity_prices
Revises: 0004_corporate_actions
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_commodity_prices"
down_revision = "0004_corporate_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commodity_prices",
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("series", sa.String(80), nullable=False),
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("commodity", sa.String(80), nullable=False),
        sa.Column("frequency", sa.String(12), nullable=False),
        sa.Column("price", sa.Numeric(24, 8), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("price > 0", name="ck_commodity_prices_positive"),
        sa.CheckConstraint(
            "frequency IN ('daily', 'monthly')", name="ck_commodity_prices_frequency"
        ),
        sa.PrimaryKeyConstraint("source", "series", "observation_date"),
    )
    op.create_index(
        "ix_commodity_prices_lookup",
        "commodity_prices",
        ["commodity", "frequency", sa.text("observation_date DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_commodity_prices_lookup", table_name="commodity_prices")
    op.drop_table("commodity_prices")
