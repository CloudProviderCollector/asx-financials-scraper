"""Persist exchange-scoped daily OHLCV history.

Revision ID: 0003_daily_prices
Revises: 0002_exchange_scoped
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_daily_prices"
down_revision = "0002_exchange_scoped"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_prices",
        sa.Column("exchange_code", sa.String(length=20), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(precision=24, scale=8)),
        sa.Column("high", sa.Numeric(precision=24, scale=8)),
        sa.Column("low", sa.Numeric(precision=24, scale=8)),
        sa.Column("close", sa.Numeric(precision=24, scale=8)),
        sa.Column("adjusted_close", sa.Numeric(precision=24, scale=8)),
        sa.Column("volume", sa.BigInteger()),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("exchange_code", "ticker", "trading_date"),
    )
    op.create_index(
        "ix_daily_prices_latest",
        "daily_prices",
        ["exchange_code", "ticker", sa.text("trading_date DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_daily_prices_latest", table_name="daily_prices")
    op.drop_table("daily_prices")
