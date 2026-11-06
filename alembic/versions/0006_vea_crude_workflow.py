"""Persist VEA/Brent workflow completion and alert deliveries.

Revision ID: 0006_vea_crude_workflow
Revises: 0005_commodity_prices
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_vea_crude_workflow"
down_revision = "0005_commodity_prices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vea_crude_runs",
        sa.Column("target_date", sa.Date(), primary_key=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "vea_crude_alerts",
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("alert_key", sa.String(120), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("target_date", "alert_key"),
    )


def downgrade() -> None:
    op.drop_table("vea_crude_alerts")
    op.drop_table("vea_crude_runs")
