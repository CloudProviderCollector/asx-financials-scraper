from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from asx_financials.application.services import (
    IngestTickerCommand,
    InitializeTickerUniverseCommand,
    TickerIngestionService,
    TickerUniverseInitializationService,
)
from asx_financials.domain.enums import IngestionRunStatus, StatementFrequency, StatementType
from asx_financials.domain.models import (
    AsxListedCompaniesFetchResult,
    AsxListedCompany,
    CompanyProfileSnapshot,
    CorporateAction,
    DailyPriceBar,
    FinancialStatementSnapshot,
    PersistedIngestionOutcome,
    ProviderFetchResult,
    RawSourcePayload,
    SegmentFailure,
)
from asx_financials.domain.value_objects import AsxTicker

FIXED_NOW = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)


class StubClock:
    def utcnow(self) -> datetime:
        return FIXED_NOW


class StubProvider:
    def __init__(self, result: ProviderFetchResult) -> None:
        self._result = result

    def fetch(
        self,
        ticker: AsxTicker,
        include_annual: bool,
        include_quarterly: bool,
        latest_price_date: date | None = None,
        latest_action_date: date | None = None,
    ) -> ProviderFetchResult:
        self.latest_action_date = latest_action_date
        self.latest_price_date = latest_price_date
        return self._result


class StubTickerUniverseProvider:
    def fetch(self, source_url: str) -> AsxListedCompaniesFetchResult:
        return AsxListedCompaniesFetchResult(
            companies=[
                AsxListedCompany(
                    ticker="BHP",
                    company_name="BHP Group Limited",
                    industry_group="Materials",
                )
            ],
            invalid_count=1,
        )


class RecordingStore:
    def __init__(self) -> None:
        self.completions: list[dict[str, object]] = []
        self.ticker_syncs: list[dict[str, object]] = []
        self.latest_price_date: date | None = None
        self.latest_action_date: date | None = None

    def sync_ticker_universe(self, **kwargs):
        self.ticker_syncs.append(kwargs)
        return kwargs

    def start_ingestion_run(
        self,
        ingestion_run_id: str,
        ticker: AsxTicker,
        yahoo_symbol: str,
        started_at_utc: datetime,
    ) -> None:
        return None

    def complete_ingestion_run(self, **kwargs) -> PersistedIngestionOutcome:
        self.completions.append(kwargs)
        return PersistedIngestionOutcome(
            inserted_snapshot_count=len(kwargs["statement_snapshots"]),
            stored_raw_payload_count=len(kwargs["raw_payloads"]),
            stored_price_bar_count=len(kwargs["daily_price_bars"]),
        )

    def mark_ingestion_run_failed(
        self,
        ingestion_run_id: str,
        completed_at_utc: datetime,
        message: str,
    ) -> None:
        return None

    def get_latest_price_date(self, ticker: AsxTicker, exchange_code: str = "ASX"):
        return self.latest_price_date

    def get_latest_action_date(self, ticker: AsxTicker, exchange_code: str = "ASX"):
        return self.latest_action_date

    def get_latest_ingestion(self, ticker: AsxTicker):
        return None

    def get_company(self, ticker: AsxTicker):
        return None

    def get_latest_statement_previews(self, ticker: AsxTicker):
        return []


def make_provider_result() -> ProviderFetchResult:
    return ProviderFetchResult(
        source_name="yfinance",
        yahoo_symbol="BHP.AX",
        company_profile=CompanyProfileSnapshot(
            source_name="yfinance",
            ticker="BHP",
            yahoo_symbol="BHP.AX",
            company_name="BHP Group Limited",
            exchange_name="ASX",
            currency="AUD",
            sector="Materials",
            industry="Mining",
            country="Australia",
            website="https://www.bhp.com",
            profile_payload={"longName": "BHP Group Limited"},
            fetched_at_utc=FIXED_NOW,
        ),
        statement_snapshots=[
            FinancialStatementSnapshot(
                source_name="yfinance",
                ticker="BHP",
                yahoo_symbol="BHP.AX",
                statement_type=StatementType.INCOME_STATEMENT,
                frequency=StatementFrequency.ANNUAL,
                source_period_key="2024-06-30",
                source_period_end=datetime(2024, 6, 30, tzinfo=UTC).date(),
                payload={"periodEnd": "2024-06-30", "values": {"TotalRevenue": 1}},
                fetched_at_utc=FIXED_NOW,
            )
        ],
        daily_price_bars=[
            DailyPriceBar(
                trading_date=date(2026, 4, 23),
                open=Decimal("42.50"),
                high=Decimal("43"),
                low=Decimal("42"),
                close=Decimal("42.80"),
                adjusted_close=None,
                volume=123456,
                source="yfinance",
                fetched_at_utc=FIXED_NOW,
            )
        ],
        raw_payloads=[
            RawSourcePayload(
                source_name="yfinance",
                ticker="BHP",
                yahoo_symbol="BHP.AX",
                segment_name="company-profile",
                payload={"longName": "BHP Group Limited"},
                fetched_at_utc=FIXED_NOW,
            )
        ],
        segment_failures=[],
    )


def test_ingest_returns_succeeded_for_full_success() -> None:
    store = RecordingStore()
    service = TickerIngestionService(StubProvider(make_provider_result()), store, StubClock())

    result = service.ingest(IngestTickerCommand(ticker="BHP"))

    assert result.status is IngestionRunStatus.SUCCEEDED
    assert result.inserted_snapshot_count == 1
    assert result.stored_raw_payload_count == 1
    assert result.stored_price_bar_count == 1
    assert store.completions[0]["daily_price_bars"] == make_provider_result().daily_price_bars


def test_ingest_uses_latest_saved_day_and_accepts_price_only_data() -> None:
    store = RecordingStore()
    store.latest_price_date = date(2026, 4, 23)
    provider = StubProvider(
        replace(make_provider_result(), company_profile=None, statement_snapshots=[])
    )
    result = TickerIngestionService(provider, store, StubClock()).ingest(
        IngestTickerCommand(ticker="BHP")
    )
    assert provider.latest_price_date == date(2026, 4, 23)
    assert result.status is IngestionRunStatus.SUCCEEDED


def test_ingest_persists_actions_on_incremental_price_fetch() -> None:
    store = RecordingStore()
    store.latest_price_date = date(2026, 4, 23)
    store.latest_action_date = date(2026, 4, 20)
    event = CorporateAction("dividend", date(2026, 4, 21), Decimal("2"), "yfinance", FIXED_NOW)
    provider = StubProvider(replace(make_provider_result(), corporate_actions=[event]))
    TickerIngestionService(provider, store, StubClock()).ingest(IngestTickerCommand(ticker="BHP"))
    assert provider.latest_action_date == date(2026, 4, 20)
    assert store.completions[0]["corporate_actions"] == [event]


def test_ingest_returns_partial_success_when_some_segments_fail() -> None:
    partial = replace(
        make_provider_result(),
        segment_failures=[SegmentFailure("cash-flow-quarterly", "Statement data was empty.")],
    )
    service = TickerIngestionService(StubProvider(partial), RecordingStore(), StubClock())

    result = service.ingest(IngestTickerCommand(ticker="BHP"))

    assert result.status is IngestionRunStatus.PARTIALLY_SUCCEEDED
    assert len(result.segment_failures) == 1


def test_ingest_returns_failed_when_provider_has_no_usable_data() -> None:
    empty = ProviderFetchResult(
        source_name="yfinance",
        yahoo_symbol="WBC.AX",
        company_profile=None,
        statement_snapshots=[],
        raw_payloads=[],
        segment_failures=[SegmentFailure("company-profile", "empty")],
    )
    service = TickerIngestionService(StubProvider(empty), RecordingStore(), StubClock())

    result = service.ingest(IngestTickerCommand(ticker="WBC"))

    assert result.status is IngestionRunStatus.FAILED


def test_ingest_rejects_when_no_periods_selected() -> None:
    service = TickerIngestionService(
        StubProvider(make_provider_result()),
        RecordingStore(),
        StubClock(),
    )

    with pytest.raises(ValueError):
        service.ingest(
            IngestTickerCommand(
                ticker="BHP",
                include_annual=False,
                include_quarterly=False,
            )
        )


def test_initialize_ticker_universe_syncs_provider_rows() -> None:
    store = RecordingStore()
    service = TickerUniverseInitializationService(
        StubTickerUniverseProvider(),
        store,
        StubClock(),
    )

    result = service.initialize(InitializeTickerUniverseCommand(source_url="https://example.test"))

    assert result["source_url"] == "https://example.test"
    assert result["fetched_at_utc"] == FIXED_NOW
    assert result["invalid_count"] == 1
    assert store.ticker_syncs[0]["companies"][0].ticker == "BHP"
