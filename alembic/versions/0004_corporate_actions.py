"""Persist exchange-scoped dividend and split actions.

Revision ID: 0004_corporate_actions
Revises: 0003_daily_prices
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_corporate_actions"
down_revision = "0003_daily_prices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corporate_actions",
        sa.Column("exchange_code", sa.String(length=20), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("value", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action_type IN ('dividend', 'split')", name="ck_corporate_actions_type"
        ),
        sa.PrimaryKeyConstraint(
            "exchange_code", "ticker", "action_type", "effective_date", "source"
        ),
    )
    op.create_index(
        "ix_corporate_actions_latest",
        "corporate_actions",
        ["exchange_code", "ticker", sa.text("effective_date DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_corporate_actions_latest", table_name="corporate_actions")
    op.drop_table("corporate_actions")
