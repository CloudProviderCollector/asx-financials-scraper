"""Initial PostgreSQL financial persistence schema.

Revision ID: 0001_initial_postgres
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial_postgres"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tickers",
        sa.Column("ticker", sa.String(20), primary_key=True),
        sa.Column("yahoo_symbol", sa.String(32), nullable=False),
        sa.Column("source_name", sa.String(80), nullable=False),
        sa.Column("company_name", sa.Text()),
        sa.Column("exchange_name", sa.String(80)),
        sa.Column("industry_group", sa.Text()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("listed_on_asx", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("first_seen_in_asx_list", sa.DateTime(timezone=True)),
        sa.Column("last_seen_in_asx_list", sa.DateTime(timezone=True)),
        sa.Column("last_asx_list_sync_at", sa.DateTime(timezone=True)),
        sa.Column("asx_list_source_url", sa.Text()),
        sa.Column("delisted_detected_at", sa.DateTime(timezone=True)),
        sa.Column("currency", sa.String(20)),
        sa.Column("sector", sa.Text()),
        sa.Column("industry", sa.Text()),
        sa.Column("country", sa.Text()),
        sa.Column("website", sa.Text()),
        sa.Column(
            "profile_payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("last_fundamental_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tickers_listed", "tickers", ["listed_on_asx", "ticker"])
    op.create_index("ix_tickers_sector_industry", "tickers", ["sector", "industry"])

    op.create_table(
        "ticker_universe_sync_runs",
        sa.Column("sync_run_id", sa.String(36), primary_key=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("seen_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("inserted_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("reactivated_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("deactivated_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("invalid_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("unchanged_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index(
        "ix_sync_runs_started", "ticker_universe_sync_runs", [sa.text("started_at DESC")]
    )

    op.create_table(
        "ingestion_runs",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("run_type", sa.String(40), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("yahoo_symbol", sa.String(32), nullable=False),
        sa.Column("source_name", sa.String(80), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "segment_failures",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "stored_snapshot_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "stored_raw_payload_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("stored_fact_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index(
        "ix_ingestion_runs_latest", "ingestion_runs", ["ticker", sa.text("started_at DESC")]
    )

    op.create_table(
        "raw_source_payloads",
        sa.Column("raw_payload_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("ingestion_runs.run_id"), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("yahoo_symbol", sa.String(32), nullable=False),
        sa.Column("source_name", sa.String(80), nullable=False),
        sa.Column("segment_name", sa.String(100), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_raw_payloads_run", "raw_source_payloads", ["run_id"])
    op.create_index(
        "ix_raw_payloads_ticker_segment",
        "raw_source_payloads",
        ["ticker", "segment_name", sa.text("fetched_at DESC")],
    )

    op.create_table(
        "statement_snapshots",
        sa.Column("snapshot_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("ingestion_runs.run_id"), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("source_name", sa.String(80), nullable=False),
        sa.Column("statement_type", sa.String(40), nullable=False),
        sa.Column("frequency", sa.String(24), nullable=False),
        sa.Column("period_end", sa.Date()),
        sa.Column("yahoo_symbol", sa.String(32), nullable=False),
        sa.Column("source_period_key", sa.Text(), nullable=False),
        sa.Column("fiscal_year", sa.Integer()),
        sa.Column("fiscal_quarter", sa.Integer()),
        sa.Column("revision_hash", sa.String(64), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "raw_payload_id", sa.String(36), sa.ForeignKey("raw_source_payloads.raw_payload_id")
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    key = ["ticker", "source_name", "statement_type", "frequency"]
    op.create_index(
        "uq_snapshots_revision_dated",
        "statement_snapshots",
        [*key, "period_end", "revision_hash"],
        unique=True,
        postgresql_where=sa.text("period_end IS NOT NULL"),
    )
    op.create_index(
        "uq_snapshots_revision_undated",
        "statement_snapshots",
        [*key, "revision_hash"],
        unique=True,
        postgresql_where=sa.text("period_end IS NULL"),
    )
    op.create_index(
        "uq_snapshots_current_dated",
        "statement_snapshots",
        [*key, "period_end"],
        unique=True,
        postgresql_where=sa.text("is_current IS TRUE AND period_end IS NOT NULL"),
    )
    op.create_index(
        "uq_snapshots_current_undated",
        "statement_snapshots",
        key,
        unique=True,
        postgresql_where=sa.text("is_current IS TRUE AND period_end IS NULL"),
    )
    op.create_index(
        "ix_snapshots_preview",
        "statement_snapshots",
        [
            "ticker",
            "statement_type",
            "frequency",
            sa.text("period_end DESC"),
            sa.text("fetched_at DESC"),
        ],
    )

    op.create_table(
        "financial_facts",
        sa.Column("fact_id", sa.String(36), primary_key=True),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("source_name", sa.String(80), nullable=False),
        sa.Column(
            "statement_snapshot_id",
            sa.String(36),
            sa.ForeignKey("statement_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("statement_type", sa.String(40), nullable=False),
        sa.Column("frequency", sa.String(24), nullable=False),
        sa.Column("period_end", sa.Date()),
        sa.Column("metric_key", sa.Text(), nullable=False),
        sa.Column("metric_label", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.String(20)),
        sa.Column("unit", sa.String(40)),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_facts_metric",
        "financial_facts",
        ["metric_key", "frequency", sa.text("period_end DESC"), "is_current", "value"],
    )
    op.create_index(
        "ix_facts_ticker_metric",
        "financial_facts",
        ["ticker", "metric_key", "frequency", sa.text("period_end DESC")],
    )


def downgrade() -> None:
    op.drop_table("financial_facts")
    op.drop_table("statement_snapshots")
    op.drop_table("raw_source_payloads")
    op.drop_table("ingestion_runs")
    op.drop_table("ticker_universe_sync_runs")
    op.drop_table("tickers")
