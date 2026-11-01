"""Relational persistence schema; changes are applied exclusively with Alembic."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

commodity_prices = Table(
    "commodity_prices",
    metadata,
    Column("source", String(80), primary_key=True),
    Column("series", String(80), primary_key=True),
    Column("observation_date", Date, primary_key=True),
    Column("commodity", String(80), nullable=False),
    Column("frequency", String(12), nullable=False),
    Column("price", Numeric(24, 8), nullable=False),
    Column("unit", String(32), nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("price > 0", name="ck_commodity_prices_positive"),
    CheckConstraint("frequency IN ('daily', 'monthly')", name="ck_commodity_prices_frequency"),
)
Index(
    "ix_commodity_prices_lookup",
    commodity_prices.c.commodity,
    commodity_prices.c.frequency,
    commodity_prices.c.observation_date.desc(),
)

tickers = Table(
    "tickers",
    metadata,
    Column("exchange_code", String(20), primary_key=True),
    Column("ticker", String(20), primary_key=True),
    Column("yahoo_symbol", String(32), nullable=False),
    Column("source_name", String(80), nullable=False),
    Column("company_name", Text),
    Column("exchange_name", String(80)),
    Column("industry_group", Text),
    Column("status", String(20), nullable=False),
    Column("listed_in_universe", Boolean, nullable=False, server_default=text("false")),
    Column("first_seen_in_universe", DateTime(timezone=True)),
    Column("last_seen_in_universe", DateTime(timezone=True)),
    Column("last_universe_sync_at", DateTime(timezone=True)),
    Column("universe_source_url", Text),
    Column("delisted_detected_at", DateTime(timezone=True)),
    Column("currency", String(20)),
    Column("sector", Text),
    Column("industry", Text),
    Column("country", Text),
    Column("website", Text),
    Column("profile_payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("last_fundamental_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ix_tickers_universe", tickers.c.exchange_code, tickers.c.listed_in_universe, tickers.c.ticker
)
Index("ix_tickers_sector_industry", tickers.c.exchange_code, tickers.c.sector, tickers.c.industry)

ticker_universe_sync_runs = Table(
    "ticker_universe_sync_runs",
    metadata,
    Column("sync_run_id", String(36), primary_key=True),
    Column("exchange_code", String(20), nullable=False),
    Column("source_url", Text, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    Column("status", String(24), nullable=False),
    Column("seen_count", Integer, nullable=False, server_default=text("0")),
    Column("inserted_count", Integer, nullable=False, server_default=text("0")),
    Column("updated_count", Integer, nullable=False, server_default=text("0")),
    Column("reactivated_count", Integer, nullable=False, server_default=text("0")),
    Column("deactivated_count", Integer, nullable=False, server_default=text("0")),
    Column("invalid_count", Integer, nullable=False, server_default=text("0")),
    Column("unchanged_count", Integer, nullable=False, server_default=text("0")),
)
Index(
    "ix_sync_runs_started",
    ticker_universe_sync_runs.c.exchange_code,
    ticker_universe_sync_runs.c.started_at.desc(),
)

ingestion_runs = Table(
    "ingestion_runs",
    metadata,
    Column("run_id", String(128), primary_key=True),
    Column("exchange_code", String(20), nullable=False),
    Column("run_type", String(40), nullable=False),
    Column("ticker", String(20), nullable=False),
    Column("yahoo_symbol", String(32), nullable=False),
    Column("source_name", String(80), nullable=False),
    Column("status", String(24), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    Column("message", Text, nullable=False),
    Column("segment_failures", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("stored_snapshot_count", Integer, nullable=False, server_default=text("0")),
    Column("stored_raw_payload_count", Integer, nullable=False, server_default=text("0")),
    Column("stored_fact_count", Integer, nullable=False, server_default=text("0")),
)
Index(
    "ix_ingestion_runs_latest",
    ingestion_runs.c.exchange_code,
    ingestion_runs.c.ticker,
    ingestion_runs.c.started_at.desc(),
)

raw_source_payloads = Table(
    "raw_source_payloads",
    metadata,
    Column("raw_payload_id", String(36), primary_key=True),
    Column("run_id", String(128), ForeignKey("ingestion_runs.run_id"), nullable=False),
    Column("exchange_code", String(20), nullable=False),
    Column("ticker", String(20), nullable=False),
    Column("yahoo_symbol", String(32), nullable=False),
    Column("source_name", String(80), nullable=False),
    Column("segment_name", String(100), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_raw_payloads_run", raw_source_payloads.c.run_id)
Index(
    "ix_raw_payloads_ticker_segment",
    raw_source_payloads.c.exchange_code,
    raw_source_payloads.c.ticker,
    raw_source_payloads.c.segment_name,
    raw_source_payloads.c.fetched_at.desc(),
)

statement_snapshots = Table(
    "statement_snapshots",
    metadata,
    Column("snapshot_id", String(36), primary_key=True),
    Column("run_id", String(128), ForeignKey("ingestion_runs.run_id"), nullable=False),
    Column("exchange_code", String(20), nullable=False),
    Column("ticker", String(20), nullable=False),
    Column("source_name", String(80), nullable=False),
    Column("statement_type", String(40), nullable=False),
    Column("frequency", String(24), nullable=False),
    Column("period_end", Date),
    Column("yahoo_symbol", String(32), nullable=False),
    Column("source_period_key", Text, nullable=False),
    Column("fiscal_year", Integer),
    Column("fiscal_quarter", Integer),
    Column("revision_hash", String(64), nullable=False),
    Column("is_current", Boolean, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("raw_payload_id", String(36), ForeignKey("raw_source_payloads.raw_payload_id")),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
_snapshot_key = (
    statement_snapshots.c.exchange_code,
    statement_snapshots.c.ticker,
    statement_snapshots.c.source_name,
    statement_snapshots.c.statement_type,
    statement_snapshots.c.frequency,
)
Index(
    "uq_snapshots_revision_dated",
    *_snapshot_key,
    statement_snapshots.c.period_end,
    statement_snapshots.c.revision_hash,
    unique=True,
    postgresql_where=statement_snapshots.c.period_end.is_not(None),
)
Index(
    "uq_snapshots_revision_undated",
    *_snapshot_key,
    statement_snapshots.c.revision_hash,
    unique=True,
    postgresql_where=statement_snapshots.c.period_end.is_(None),
)
Index(
    "uq_snapshots_current_dated",
    *_snapshot_key,
    statement_snapshots.c.period_end,
    unique=True,
    postgresql_where=statement_snapshots.c.is_current.is_(True)
    & statement_snapshots.c.period_end.is_not(None),
)
Index(
    "uq_snapshots_current_undated",
    *_snapshot_key,
    unique=True,
    postgresql_where=statement_snapshots.c.is_current.is_(True)
    & statement_snapshots.c.period_end.is_(None),
)
Index(
    "ix_snapshots_preview",
    statement_snapshots.c.exchange_code,
    statement_snapshots.c.ticker,
    statement_snapshots.c.statement_type,
    statement_snapshots.c.frequency,
    statement_snapshots.c.period_end.desc(),
    statement_snapshots.c.fetched_at.desc(),
)

daily_prices = Table(
    "daily_prices",
    metadata,
    Column("exchange_code", String(20), primary_key=True),
    Column("ticker", String(20), primary_key=True),
    Column("trading_date", Date, primary_key=True),
    Column("open", Numeric(24, 8)),
    Column("high", Numeric(24, 8)),
    Column("low", Numeric(24, 8)),
    Column("close", Numeric(24, 8)),
    Column("adjusted_close", Numeric(24, 8)),
    Column("volume", BigInteger),
    Column("source", String(80), nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ix_daily_prices_latest",
    daily_prices.c.exchange_code,
    daily_prices.c.ticker,
    daily_prices.c.trading_date.desc(),
)

corporate_actions = Table(
    "corporate_actions",
    metadata,
    Column("exchange_code", String(20), primary_key=True),
    Column("ticker", String(20), primary_key=True),
    Column("action_type", String(20), primary_key=True),
    Column("effective_date", Date, primary_key=True),
    Column("source", String(80), primary_key=True),
    Column("value", Numeric(24, 8), nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("action_type IN ('dividend', 'split')", name="ck_corporate_actions_type"),
)
Index(
    "ix_corporate_actions_latest",
    corporate_actions.c.exchange_code,
    corporate_actions.c.ticker,
    corporate_actions.c.effective_date.desc(),
)

financial_facts = Table(
    "financial_facts",
    metadata,
    Column("fact_id", String(36), primary_key=True),
    Column("exchange_code", String(20), nullable=False),
    Column("ticker", String(20), nullable=False),
    Column("source_name", String(80), nullable=False),
    Column(
        "statement_snapshot_id",
        String(36),
        ForeignKey("statement_snapshots.snapshot_id"),
        nullable=False,
    ),
    Column("statement_type", String(40), nullable=False),
    Column("frequency", String(24), nullable=False),
    Column("period_end", Date),
    Column("metric_key", Text, nullable=False),
    Column("metric_label", Text, nullable=False),
    Column("value", Numeric, nullable=False),
    Column("currency", String(20)),
    Column("unit", String(40)),
    Column("is_current", Boolean, nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ix_facts_metric",
    financial_facts.c.exchange_code,
    financial_facts.c.metric_key,
    financial_facts.c.frequency,
    financial_facts.c.period_end.desc(),
    financial_facts.c.is_current,
    financial_facts.c.value,
)
Index(
    "ix_facts_ticker_metric",
    financial_facts.c.exchange_code,
    financial_facts.c.ticker,
    financial_facts.c.exchange_code,
    financial_facts.c.metric_key,
    financial_facts.c.frequency,
    financial_facts.c.period_end.desc(),
)
