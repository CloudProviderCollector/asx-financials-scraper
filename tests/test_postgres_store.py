"""PostgreSQL integration tests; TEST_DATABASE_URL must point at a disposable database."""

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError, IntegrityError

from asx_financials.dashboard import load_ticker_data
from asx_financials.domain.enums import IngestionRunStatus, StatementFrequency, StatementType
from asx_financials.domain.models import (
    AsxListedCompany,
    CompanyProfileSnapshot,
    CorporateAction,
    DailyPriceBar,
    FinancialStatementSnapshot,
    RawSourcePayload,
    SegmentFailure,
)
from asx_financials.domain.value_objects import AsxTicker
from asx_financials.infrastructure.persistence.schema import (
    corporate_actions,
    daily_prices,
    financial_facts,
    ingestion_runs,
    statement_snapshots,
    tickers,
)
from asx_financials.infrastructure.persistence.store import PostgresFinancialDataStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL persistence integration tests")
    schema = "test_financials_" + uuid4().hex
    admin = create_engine(url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    except Exception as exc:
        admin.dispose()
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")
    isolated_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    old_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = isolated_url.render_as_string(hide_password=False)
    engine = create_engine(isolated_url)
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield engine
    finally:
        engine.dispose()
        if old_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_url
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def store(database):
    return PostgresFinancialDataStore(database)


def _snapshot(*, value=100, period=None, ticker="AAA"):
    return FinancialStatementSnapshot(
        source_name="yfinance",
        ticker=ticker,
        yahoo_symbol=f"{ticker}.AX",
        statement_type=StatementType.INCOME_STATEMENT,
        frequency=StatementFrequency.ANNUAL,
        source_period_key="year",
        source_period_end=period,
        payload={"values": {"Total Revenue": value}},
        fetched_at_utc=NOW,
    )


def _complete(
    store, run_id, ticker="AAA", snapshots=None, profile=None, payloads=None, bars=(), actions=()
):
    symbol = AsxTicker(ticker)
    store.start_ingestion_run(run_id, symbol, symbol.yahoo_symbol, NOW)
    return store.complete_ingestion_run(
        ingestion_run_id=run_id,
        ticker=symbol,
        yahoo_symbol=symbol.yahoo_symbol,
        status=IngestionRunStatus.PARTIALLY_SUCCEEDED.value,
        completed_at_utc=NOW,
        message="done",
        company_profile=profile,
        statement_snapshots=snapshots or [],
        raw_payloads=payloads or [],
        segment_failures=[SegmentFailure("balance-sheet", "not available")],
        daily_price_bars=bars,
        corporate_actions=actions,
    )


def _bar(day, close, *, fetched_at=NOW):
    return DailyPriceBar(
        trading_date=day,
        open=Decimal("10.25"),
        high=Decimal("11.50"),
        low=Decimal("9.50"),
        close=Decimal(close),
        adjusted_close=None,
        volume=3_000_000_000,
        source="yfinance",
        fetched_at_utc=fetched_at,
    )


def _action(day, value, *, action_type="split", source="yfinance", fetched_at=NOW):
    return CorporateAction(
        action_type=action_type,
        effective_date=day,
        value=Decimal(value),
        source=source,
        fetched_at_utc=fetched_at,
    )


def test_corporate_actions_upsert_order_and_exchange_isolation(store, database):
    ticker = AsxTicker("ACTION")
    first_day = date(2025, 6, 1)
    last_day = date(2025, 12, 31)
    assert store.get_latest_action_date(ticker) is None
    assert store.get_corporate_actions(ticker) == []
    _complete(
        store,
        "action-initial",
        ticker="ACTION",
        actions=[
            _action(last_day, "2"),
            _action(first_day, "0.25", action_type="dividend"),
            _action(last_day, "0.5", action_type="dividend"),
            _action(last_day, "3", source="alternate"),
        ],
    )
    with database.begin() as connection:
        connection.execute(
            corporate_actions.insert().values(
                exchange_code="NYSE",
                ticker="ACTION",
                action_type="split",
                effective_date=date(2026, 1, 2),
                source="other",
                value=Decimal("9"),
                fetched_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    assert store.get_latest_action_date(ticker) == last_day
    assert store.get_latest_action_date(ticker, "NYSE") == date(2026, 1, 2)
    _complete(
        store,
        "action-revision",
        ticker="ACTION",
        actions=[_action(last_day, "4", fetched_at=NOW + timedelta(hours=1))],
    )
    _complete(
        store,
        "action-stale",
        ticker="ACTION",
        actions=[_action(last_day, "99", fetched_at=NOW - timedelta(days=1))],
    )
    actions = store.get_corporate_actions(ticker)
    assert [(a.effective_date, a.action_type, a.source, a.value) for a in actions] == [
        (first_day, "dividend", "yfinance", Decimal("0.25000000")),
        (last_day, "dividend", "yfinance", Decimal("0.50000000")),
        (last_day, "split", "alternate", Decimal("3.00000000")),
        (last_day, "split", "yfinance", Decimal("4.00000000")),
    ]
    assert actions[-1].fetched_at_utc == NOW + timedelta(hours=1)
    assert len(store.get_corporate_actions(ticker, "NYSE")) == 1
    with database.connect() as connection:
        row = (
            connection.execute(
                select(corporate_actions).where(
                    corporate_actions.c.exchange_code == "ASX",
                    corporate_actions.c.ticker == "ACTION",
                    corporate_actions.c.action_type == "split",
                    corporate_actions.c.source == "yfinance",
                )
            )
            .mappings()
            .one()
        )
    assert row["created_at"] <= row["updated_at"]
    assert row["value"] == Decimal("4.00000000")


def test_corporate_actions_atomic_with_ingestion_run(store):
    ticker = AsxTicker("ACTERR")
    with pytest.raises(RuntimeError, match="not found"):
        store.complete_ingestion_run(
            ingestion_run_id="missing-action",
            ticker=ticker,
            yahoo_symbol=ticker.yahoo_symbol,
            status="succeeded",
            completed_at_utc=NOW,
            message="done",
            company_profile=None,
            statement_snapshots=[],
            raw_payloads=[],
            segment_failures=[],
            corporate_actions=[_action(date(2025, 1, 1), "2")],
        )
    assert store.get_corporate_actions(ticker) == []
    with pytest.raises(DataError):
        _complete(
            store,
            "invalid-action",
            ticker="ACTERR",
            actions=[
                _action(date(2025, 1, 1), "2"),
                _action(date(2025, 1, 2), "100000000000000000000"),
            ],
        )
    assert store.get_corporate_actions(ticker) == []


def test_daily_prices_incremental_upsert_and_exchange_isolation(store, database):
    ticker = AsxTicker("PRICE")
    first_day = date(2025, 12, 30)
    last_day = date(2025, 12, 31)
    assert store.get_latest_price_date(ticker) is None
    _complete(store, "price-initial", ticker="PRICE", bars=[_bar(first_day, "10.00")])
    with database.begin() as connection:
        connection.execute(
            daily_prices.insert().values(
                exchange_code="NYSE",
                ticker="PRICE",
                trading_date=date(2026, 1, 2),
                open=None,
                high=None,
                low=None,
                close=Decimal("90"),
                adjusted_close=None,
                volume=None,
                source="other",
                fetched_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    assert store.get_latest_price_date(ticker) == first_day
    assert store.get_latest_price_date(ticker, "NYSE") == date(2026, 1, 2)
    _complete(
        store,
        "price-incremental",
        ticker="PRICE",
        bars=[
            _bar(first_day, "10.50", fetched_at=NOW + timedelta(hours=1)),
            _bar(last_day, "11.00"),
        ],
    )
    _complete(
        store,
        "price-old-result",
        ticker="PRICE",
        bars=[_bar(first_day, "1.00", fetched_at=NOW - timedelta(days=1))],
    )
    assert store.get_latest_price_date(ticker) == last_day
    with database.connect() as connection:
        rows = (
            connection.execute(
                select(daily_prices)
                .where(daily_prices.c.ticker == "PRICE")
                .order_by(daily_prices.c.exchange_code, daily_prices.c.trading_date)
            )
            .mappings()
            .all()
        )
    assert len(rows) == 3
    asx = [row for row in rows if row["exchange_code"] == "ASX"]
    assert [row["close"] for row in asx] == [Decimal("10.50000000"), Decimal("11.00000000")]
    assert asx[0]["adjusted_close"] is None
    assert asx[0]["volume"] == 3_000_000_000
    assert asx[0]["source"] == "yfinance"
    assert asx[0]["fetched_at"] == NOW + timedelta(hours=1)
    assert rows[-1]["close"] == Decimal("90.00000000")


def test_daily_prices_atomic_with_ingestion_run(store, database):
    with pytest.raises(RuntimeError, match="not found"):
        store.complete_ingestion_run(
            ingestion_run_id="missing-price",
            ticker=AsxTicker("ATOMIC"),
            yahoo_symbol="ATOMIC.AX",
            status="succeeded",
            completed_at_utc=NOW,
            message="done",
            company_profile=None,
            statement_snapshots=[],
            raw_payloads=[],
            segment_failures=[],
            daily_price_bars=[_bar(date(2025, 12, 31), "5")],
        )
    assert store.get_latest_price_date(AsxTicker("ATOMIC")) is None
    with pytest.raises(DataError):
        _complete(
            store,
            "invalid-price",
            ticker="ATOMIC",
            bars=[
                _bar(date(2025, 12, 30), "5"),
                _bar(date(2025, 12, 31), "100000000000000000000"),
            ],
        )
    assert store.get_latest_price_date(AsxTicker("ATOMIC")) is None


def test_sync_counts_deactivation_and_profile_preservation(store, database):
    first = store.sync_ticker_universe(
        companies=[
            AsxListedCompany("AAA", "Alpha", "Energy"),
            AsxListedCompany("BBB", "Beta", None),
        ],
        source_url="https://example.test/one",
        fetched_at_utc=NOW,
        invalid_count=2,
    )
    assert (first.inserted_count, first.invalid_count) == (2, 2)
    again = store.sync_ticker_universe(
        companies=[AsxListedCompany("AAA", "Alpha renamed", "Energy")],
        source_url="https://example.test/one",
        fetched_at_utc=NOW,
    )
    assert (again.updated_count, again.deactivated_count) == (1, 1)
    assert (
        store.sync_ticker_universe(
            companies=[AsxListedCompany("BBB", "Beta", None)],
            source_url="https://example.test/one",
            fetched_at_utc=NOW,
        ).reactivated_count
        == 1
    )
    with database.connect() as connection:
        rows = {row.ticker: row for row in connection.execute(select(tickers)).all()}
    assert rows["AAA"].listed_in_universe is False
    assert rows["BBB"].listed_in_universe is True


def test_dashboard_loads_saved_ticker_and_ignores_other_exchange(store, database):
    with database.begin() as connection:
        connection.execute(
            tickers.insert().values(
                exchange_code="NYSE",
                ticker="DASH",
                yahoo_symbol="DASH",
                source_name="other_exchange",
                company_name="Other listing",
                status="active",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    assert load_ticker_data(database, AsxTicker("DASH")) is None
    assert load_ticker_data(database, AsxTicker("NOPE")) is None

    profile = CompanyProfileSnapshot(
        source_name="yfinance",
        ticker="DASH",
        yahoo_symbol="DASH.AX",
        company_name="ASX listing",
        exchange_name="ASX",
        currency="AUD",
        sector="Financials",
        industry="Banks",
        country="Australia",
        website=None,
        profile_payload={"trailingPE": 25.1, "forwardPE": 24.0},
        fetched_at_utc=NOW,
    )
    _complete(
        store,
        "asx-dashboard",
        ticker="DASH",
        snapshots=[_snapshot(ticker="DASH")],
        profile=profile,
    )
    result = load_ticker_data(database, AsxTicker("DASH"))
    assert result is not None
    assert result["listing"]["company_name"] == "ASX listing"
    assert result["listing"]["profile_payload"]["trailingPE"] == 25.1
    assert [row["run_id"] for row in result["runs"]] == ["asx-dashboard"]
    assert len(result["snapshots"]) == len(result["facts"]) == 1
    assert all(row["exchange_code"] == "ASX" for row in result["snapshots"])


def test_same_symbol_on_other_exchange_is_isolated(store, database):
    with database.begin() as connection:
        connection.execute(
            tickers.insert().values(
                exchange_code="NYSE",
                ticker="CCC",
                yahoo_symbol="CCC",
                source_name="other_exchange",
                status="active",
                listed_in_universe=True,
                first_seen_in_universe=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        connection.execute(
            ingestion_runs.insert().values(
                run_id="nyse-ccc",
                exchange_code="NYSE",
                run_type="ticker_fundamentals",
                ticker="CCC",
                yahoo_symbol="CCC",
                source_name="other_exchange",
                status="succeeded",
                started_at=NOW,
                message="NYSE ingestion",
            )
        )
    store.sync_ticker_universe(
        companies=[AsxListedCompany("CCC", "ASX Company", None)],
        source_url="https://example.test/asx",
        fetched_at_utc=NOW,
    )
    _complete(store, "asx-ccc", ticker="CCC", snapshots=[_snapshot(value=42, ticker="CCC")])
    assert store.get_latest_ingestion(AsxTicker("CCC")).ingestion_run_id == "asx-ccc"
    assert store.get_company(AsxTicker("CCC")).company_name == "ASX Company"
    assert store.get_latest_statement_previews(AsxTicker("CCC"))[0].values == {"Total Revenue": 42}
    with database.begin() as connection:
        asx_snapshot = dict(
            connection.execute(
                select(statement_snapshots).where(
                    statement_snapshots.c.exchange_code == "ASX",
                    statement_snapshots.c.ticker == "CCC",
                )
            )
            .mappings()
            .one()
        )
        connection.execute(
            statement_snapshots.insert().values(
                **{
                    **asx_snapshot,
                    "snapshot_id": str(uuid4()),
                    "run_id": "nyse-ccc",
                    "exchange_code": "NYSE",
                    "raw_payload_id": None,
                }
            )
        )
    _complete(
        store, "asx-ccc-revision", ticker="CCC", snapshots=[_snapshot(value=84, ticker="CCC")]
    )
    assert store.get_latest_statement_previews(AsxTicker("CCC"))[0].values == {"Total Revenue": 84}
    with database.connect() as connection:
        nyse_snapshot = connection.execute(
            select(statement_snapshots.c.is_current).where(
                statement_snapshots.c.exchange_code == "NYSE",
                statement_snapshots.c.ticker == "CCC",
            )
        ).scalar_one()
    assert nyse_snapshot is True
    result = store.sync_ticker_universe(
        companies=[], source_url="https://example.test/empty", fetched_at_utc=NOW
    )
    with database.connect() as connection:
        nyse = (
            connection.execute(
                select(tickers).where(tickers.c.exchange_code == "NYSE", tickers.c.ticker == "CCC")
            )
            .mappings()
            .one()
        )
        asx = (
            connection.execute(
                select(tickers).where(tickers.c.exchange_code == "ASX", tickers.c.ticker == "CCC")
            )
            .mappings()
            .one()
        )
    assert result.deactivated_count >= 1
    assert nyse["listed_in_universe"] is True
    assert nyse["status"] == "active"
    assert asx["listed_in_universe"] is False


def test_revisions_undated_uniqueness_and_reads(store, database):
    profile = CompanyProfileSnapshot(
        source_name="yfinance",
        ticker="AAA",
        yahoo_symbol="AAA.AX",
        company_name="Profile name",
        exchange_name="ASX",
        currency="AUD",
        sector="Energy",
        industry="Oil",
        country="Australia",
        website=None,
        profile_payload={"nested": [1, 2]},
        fetched_at_utc=NOW,
    )
    raw = RawSourcePayload(
        "yfinance", "AAA", "AAA.AX", "income-statement-annual", {"rows": [1]}, NOW
    )
    first = _complete(store, "rev-1", snapshots=[_snapshot()], profile=profile, payloads=[raw])
    assert (first.inserted_snapshot_count, first.stored_raw_payload_count) == (1, 1)
    assert _complete(store, "rev-2", snapshots=[_snapshot()]).inserted_snapshot_count == 0
    assert _complete(store, "rev-3", snapshots=[_snapshot(value=200)]).inserted_snapshot_count == 1
    _complete(store, "rev-4", snapshots=[_snapshot(value=500, period=date(2025, 12, 31))])
    company = store.get_company(AsxTicker("AAA"))
    assert company.company_name == "Profile name"
    assert company.profile_payload == {"nested": [1, 2]}
    assert company.statement_availability[0].snapshot_count == 2
    assert store.get_latest_statement_previews(AsxTicker("AAA"))[0].values == {"Total Revenue": 500}
    latest = store.get_latest_ingestion(AsxTicker("AAA"))
    assert latest.status == IngestionRunStatus.PARTIALLY_SUCCEEDED
    assert latest.segment_failures == [SegmentFailure("balance-sheet", "not available")]
    with database.connect() as connection:
        snapshots = (
            connection.execute(
                select(statement_snapshots).where(
                    statement_snapshots.c.ticker == "AAA",
                    statement_snapshots.c.period_end.is_(None),
                )
            )
            .mappings()
            .all()
        )
        facts = (
            connection.execute(
                select(financial_facts).where(
                    financial_facts.c.ticker == "AAA", financial_facts.c.period_end.is_(None)
                )
            )
            .mappings()
            .all()
        )
    assert len(snapshots) == len(facts) == 2
    assert sum(item["is_current"] for item in snapshots) == 1
    assert [item["value"] for item in facts if item["is_current"]] == [200]
    # The database, not only application logic, rejects duplicate NULL-period revisions.
    duplicate = dict(snapshots[0])
    duplicate["snapshot_id"] = str(uuid4())
    duplicate["is_current"] = False
    with pytest.raises(IntegrityError), database.begin() as connection:
        connection.execute(statement_snapshots.insert().values(**duplicate))


def test_exchange_migration_preserves_existing_asx_rows(database, monkeypatch):
    schema = "test_upgrade_" + uuid4().hex
    url = database.url.set(query={})
    admin = create_engine(url)
    isolated_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    isolated = create_engine(isolated_url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        monkeypatch.setenv("DATABASE_URL", isolated_url.render_as_string(hide_password=False))
        command.upgrade(Config("alembic.ini"), "0001_initial_postgres")
        with isolated.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tickers (ticker, yahoo_symbol, source_name, status, "
                    "listed_on_asx, asx_list_source_url, created_at, updated_at) "
                    "VALUES ('AAA', 'AAA.AX', 'asx_listed_companies_csv', 'active', "
                    "true, 'https://example.test/asx', :now, :now)"
                ),
                {"now": NOW},
            )
            connection.execute(
                text(
                    "INSERT INTO ingestion_runs (run_id, run_type, ticker, yahoo_symbol, "
                    "source_name, status, started_at, message) VALUES "
                    "('existing-run', 'ticker_fundamentals', 'AAA', 'AAA.AX', "
                    "'yfinance', 'succeeded', :now, 'done')"
                ),
                {"now": NOW},
            )
        command.upgrade(Config("alembic.ini"), "head")
        with isolated.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM daily_prices")).scalar_one() == 0
        with isolated.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO daily_prices (exchange_code, ticker, trading_date, source, "
                    "fetched_at, created_at, updated_at) VALUES "
                    "('ASX', 'AAA', '2025-12-31', 'yfinance', :now, :now, :now)"
                ),
                {"now": NOW},
            )
        command.downgrade(Config("alembic.ini"), "0002_exchange_scoped")
        with isolated.connect() as connection:
            assert (
                connection.execute(text("SELECT to_regclass('daily_prices')")).scalar_one() is None
            )
        command.upgrade(Config("alembic.ini"), "head")
        with isolated.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT exchange_code, listed_in_universe, universe_source_url "
                    "FROM tickers WHERE ticker = 'AAA'"
                )
            ).one()
            run = connection.execute(
                text("SELECT exchange_code FROM ingestion_runs WHERE run_id = 'existing-run'")
            ).scalar_one()
        assert row == ("ASX", True, "https://example.test/asx")
        assert run == "ASX"
        command.downgrade(Config("alembic.ini"), "0001_initial_postgres")
        with isolated.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT listed_on_asx FROM tickers WHERE ticker = 'AAA'")
                ).scalar_one()
                is True
            )
    finally:
        isolated.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_missing_run_rolls_back_and_failure_transition(store, database):
    with pytest.raises(RuntimeError, match="not found"):
        store.complete_ingestion_run(
            ingestion_run_id="missing",
            ticker=AsxTicker("ZZZ"),
            yahoo_symbol="ZZZ.AX",
            status="succeeded",
            completed_at_utc=NOW,
            message="missing",
            company_profile=None,
            statement_snapshots=[_snapshot(ticker="ZZZ")],
            raw_payloads=[],
            segment_failures=[],
        )
    assert store.get_company(AsxTicker("ZZZ")) is None
    store.start_ingestion_run("failure", AsxTicker("ZZZ"), "ZZZ.AX", NOW)
    store.mark_ingestion_run_failed("failure", NOW, "network error")
    latest = store.get_latest_ingestion(AsxTicker("ZZZ"))
    assert latest.status == IngestionRunStatus.FAILED
    assert latest.message == "network error"
    assert latest.completed_at_utc == NOW
    with database.connect() as connection:
        assert (
            connection.execute(
                select(ingestion_runs.c.run_id).where(ingestion_runs.c.run_id == "missing")
            ).first()
            is None
        )
