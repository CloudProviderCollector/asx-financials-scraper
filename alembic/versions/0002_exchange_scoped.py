"""Scope financial persistence by exchange and generalize universe fields.

Revision ID: 0002_exchange_scoped
Revises: 0001_initial_postgres
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_exchange_scoped"
down_revision = "0001_initial_postgres"
branch_labels = None
depends_on = None


_EXCHANGE_TABLES = (
    "tickers",
    "ticker_universe_sync_runs",
    "ingestion_runs",
    "raw_source_payloads",
    "statement_snapshots",
    "financial_facts",
)

_UNIVERSE_COLUMNS = (
    ("listed_on_asx", "listed_in_universe"),
    ("first_seen_in_asx_list", "first_seen_in_universe"),
    ("last_seen_in_asx_list", "last_seen_in_universe"),
    ("last_asx_list_sync_at", "last_universe_sync_at"),
    ("asx_list_source_url", "universe_source_url"),
)


def _create_scoped_indexes() -> None:
    op.create_index(
        "ix_tickers_universe", "tickers", ["exchange_code", "listed_in_universe", "ticker"]
    )
    op.create_index(
        "ix_tickers_sector_industry", "tickers", ["exchange_code", "sector", "industry"]
    )
    op.create_index(
        "ix_sync_runs_started",
        "ticker_universe_sync_runs",
        ["exchange_code", sa.text("started_at DESC")],
    )
    op.create_index(
        "ix_ingestion_runs_latest",
        "ingestion_runs",
        ["exchange_code", "ticker", sa.text("started_at DESC")],
    )
    op.create_index(
        "ix_raw_payloads_ticker_segment",
        "raw_source_payloads",
        ["exchange_code", "ticker", "segment_name", sa.text("fetched_at DESC")],
    )
    key = ["exchange_code", "ticker", "source_name", "statement_type", "frequency"]
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
            "exchange_code",
            "ticker",
            "statement_type",
            "frequency",
            sa.text("period_end DESC"),
            sa.text("fetched_at DESC"),
        ],
    )
    op.create_index(
        "ix_facts_metric",
        "financial_facts",
        [
            "exchange_code",
            "metric_key",
            "frequency",
            sa.text("period_end DESC"),
            "is_current",
            "value",
        ],
    )
    op.create_index(
        "ix_facts_ticker_metric",
        "financial_facts",
        ["exchange_code", "ticker", "metric_key", "frequency", sa.text("period_end DESC")],
    )


def _drop_scoped_indexes() -> None:
    for name, table in (
        ("ix_facts_ticker_metric", "financial_facts"),
        ("ix_facts_metric", "financial_facts"),
        ("ix_snapshots_preview", "statement_snapshots"),
        ("uq_snapshots_current_undated", "statement_snapshots"),
        ("uq_snapshots_current_dated", "statement_snapshots"),
        ("uq_snapshots_revision_undated", "statement_snapshots"),
        ("uq_snapshots_revision_dated", "statement_snapshots"),
        ("ix_raw_payloads_ticker_segment", "raw_source_payloads"),
        ("ix_ingestion_runs_latest", "ingestion_runs"),
        ("ix_sync_runs_started", "ticker_universe_sync_runs"),
        ("ix_tickers_sector_industry", "tickers"),
        ("ix_tickers_universe", "tickers"),
    ):
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    op.drop_index("ix_tickers_listed", table_name="tickers")
    for name, table in (
        ("ix_tickers_sector_industry", "tickers"),
        ("ix_sync_runs_started", "ticker_universe_sync_runs"),
        ("ix_ingestion_runs_latest", "ingestion_runs"),
        ("ix_raw_payloads_ticker_segment", "raw_source_payloads"),
        ("uq_snapshots_revision_dated", "statement_snapshots"),
        ("uq_snapshots_revision_undated", "statement_snapshots"),
        ("uq_snapshots_current_dated", "statement_snapshots"),
        ("uq_snapshots_current_undated", "statement_snapshots"),
        ("ix_snapshots_preview", "statement_snapshots"),
        ("ix_facts_metric", "financial_facts"),
        ("ix_facts_ticker_metric", "financial_facts"),
    ):
        op.drop_index(name, table_name=table)

    for table in _EXCHANGE_TABLES:
        op.add_column(
            table,
            sa.Column(
                "exchange_code", sa.String(20), nullable=False, server_default=sa.text("'ASX'")
            ),
        )
        op.alter_column(table, "exchange_code", server_default=None)
    for old, new in _UNIVERSE_COLUMNS:
        op.alter_column("tickers", old, new_column_name=new)
    op.drop_constraint("tickers_pkey", "tickers", type_="primary")
    op.create_primary_key("tickers_pkey", "tickers", ["exchange_code", "ticker"])
    _create_scoped_indexes()


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text("SELECT ticker FROM tickers GROUP BY ticker HAVING count(*) > 1 LIMIT 1")
    ).scalar()
    if duplicate is not None:
        raise RuntimeError(
            f"Cannot downgrade exchange-scoped tickers: ticker {duplicate!r} exists "
            "in multiple exchanges; the ASX-only primary key would lose data."
        )
    for table in _EXCHANGE_TABLES:
        non_asx = connection.execute(
            sa.text(f"SELECT 1 FROM {table} WHERE exchange_code <> 'ASX' LIMIT 1")
        ).scalar()
        if non_asx is not None:
            raise RuntimeError(
                f"Cannot downgrade exchange-scoped {table}: non-ASX rows would lose "
                "their exchange identity."
            )

    _drop_scoped_indexes()
    op.drop_constraint("tickers_pkey", "tickers", type_="primary")
    op.create_primary_key("tickers_pkey", "tickers", ["ticker"])
    for old, new in _UNIVERSE_COLUMNS:
        op.alter_column("tickers", new, new_column_name=old)
    for table in reversed(_EXCHANGE_TABLES):
        op.drop_column(table, "exchange_code")

    op.create_index("ix_tickers_listed", "tickers", ["listed_on_asx", "ticker"])
    op.create_index("ix_tickers_sector_industry", "tickers", ["sector", "industry"])
    op.create_index(
        "ix_sync_runs_started", "ticker_universe_sync_runs", [sa.text("started_at DESC")]
    )
    op.create_index(
        "ix_ingestion_runs_latest", "ingestion_runs", ["ticker", sa.text("started_at DESC")]
    )
    op.create_index(
        "ix_raw_payloads_ticker_segment",
        "raw_source_payloads",
        ["ticker", "segment_name", sa.text("fetched_at DESC")],
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
