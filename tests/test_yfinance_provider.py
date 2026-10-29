from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd

from asx_financials.domain.enums import StatementFrequency, StatementType
from asx_financials.domain.value_objects import AsxTicker
from asx_financials.infrastructure.providers import yfinance_provider
from asx_financials.infrastructure.providers.yfinance_provider import YFinanceProvider


class StubTicker:
    def __init__(self):
        self.history_options = None

    def history(self, **kwargs):
        self.history_options = kwargs
        return pd.DataFrame(
            {
                "Open": [10.5, 11],
                "High": [12, 12],
                "Low": [10, 10.5],
                "Close": [11, 11.5],
                "Adj Close": [10.8, None],
                "Volume": [100000, 123456],
            },
            index=pd.DatetimeIndex(["2026-04-22", "2026-04-23"], tz="Australia/Sydney"),
        )

    def get_actions(self, period="max"):
        return pd.DataFrame()

    def get_info(self):
        return {
            "longName": "BHP Group Limited",
            "fullExchangeName": "ASX",
            "currency": "AUD",
            "sector": "Materials",
            "industry": "Mining",
            "country": "Australia",
            "website": "https://www.bhp.com",
        }

    def get_income_stmt(self, pretty: bool = False, freq: str = "yearly"):
        return (
            pd.DataFrame(
                {
                    pd.Timestamp("2024-06-30"): {"TotalRevenue": 100, "NetIncome": 50},
                    pd.Timestamp("2025-03-31"): {"TotalRevenue": 25, "NetIncome": 10},
                }
            )
            if freq == "yearly"
            else pd.DataFrame({pd.Timestamp("2025-03-31"): {"TotalRevenue": 25, "NetIncome": 10}})
        )

    def get_balance_sheet(self, pretty: bool = False, freq: str = "yearly"):
        return pd.DataFrame({pd.Timestamp("2024-06-30"): {"TotalAssets": 200}})

    def get_cash_flow(self, pretty: bool = False, freq: str = "yearly"):
        return pd.DataFrame({pd.Timestamp("2024-06-30"): {"OperatingCashFlow": 80}})


def test_provider_extracts_company_and_statement_snapshots(monkeypatch) -> None:
    monkeypatch.setattr(yfinance_provider.yf, "Ticker", lambda _: StubTicker())
    provider = YFinanceProvider()

    result = provider.fetch(AsxTicker.parse("BHP"), include_annual=True, include_quarterly=True)

    assert result.company_profile is not None
    assert result.company_profile.company_name == "BHP Group Limited"
    assert len(result.daily_price_bars) == 2
    assert result.daily_price_bars[0].adjusted_close == Decimal("10.8")
    assert result.daily_price_bars[1].adjusted_close is None
    assert len(result.statement_snapshots) >= 4
    assert any(
        snapshot.statement_type is StatementType.INCOME_STATEMENT
        and snapshot.frequency is StatementFrequency.QUARTERLY
        and snapshot.source_period_end == date(2025, 3, 31)
        for snapshot in result.statement_snapshots
    )


def test_history_initial_fetch_requests_all_available_data(monkeypatch) -> None:
    ticker = StubTicker()
    monkeypatch.setattr(yfinance_provider.yf, "Ticker", lambda _: ticker)
    monkeypatch.setattr(yfinance_provider, "_utcnow", lambda: datetime(2026, 4, 24, tzinfo=UTC))

    result = YFinanceProvider().fetch(AsxTicker.parse("BHP"), True, False)

    assert ticker.history_options == {"interval": "1d", "auto_adjust": False, "period": "max"}
    assert [bar.trading_date for bar in result.daily_price_bars] == [
        date(2026, 4, 22),
        date(2026, 4, 23),
    ]


def test_history_incremental_refetches_last_stored_day(monkeypatch) -> None:
    ticker = StubTicker()
    monkeypatch.setattr(yfinance_provider.yf, "Ticker", lambda _: ticker)
    monkeypatch.setattr(yfinance_provider, "_utcnow", lambda: datetime(2026, 4, 24, tzinfo=UTC))

    result = YFinanceProvider().fetch(
        AsxTicker.parse("BHP"), True, False, latest_price_date=date(2026, 4, 23)
    )

    assert ticker.history_options == {
        "interval": "1d",
        "auto_adjust": False,
        "start": "2026-04-23",
        "end": "2026-04-25",
    }
    assert [bar.trading_date for bar in result.daily_price_bars] == [date(2026, 4, 23)]


def test_history_empty_incremental_is_not_failure(monkeypatch) -> None:
    class EmptyHistoryTicker(StubTicker):
        def history(self, **kwargs):
            return pd.DataFrame()

    monkeypatch.setattr(yfinance_provider.yf, "Ticker", lambda _: EmptyHistoryTicker())
    result = YFinanceProvider().fetch(
        AsxTicker.parse("BHP"), True, False, latest_price_date=date(2026, 4, 23)
    )
    assert not result.daily_price_bars
    assert not any(f.segment_name == "daily-price-history" for f in result.segment_failures)


def test_history_empty_initial_and_error_are_segment_failures(monkeypatch) -> None:
    class BrokenHistoryTicker(StubTicker):
        def history(self, **kwargs):
            raise RuntimeError("network unavailable")

    monkeypatch.setattr(yfinance_provider.yf, "Ticker", lambda _: BrokenHistoryTicker())
    result = YFinanceProvider().fetch(AsxTicker.parse("BHP"), True, False)
    assert result.company_profile is not None
    assert any(f.segment_name == "daily-price-history" for f in result.segment_failures)


def test_history_skips_invalid_rows_and_uses_exchange_local_date(monkeypatch) -> None:
    class InvalidHistoryTicker(StubTicker):
        def history(self, **kwargs):
            return pd.DataFrame(
                {
                    "Open": [10, 10],
                    "High": [11, 11],
                    "Low": [9, 9],
                    "Close": [10, 10],
                    "Volume": [100, -1],
                },
                index=pd.DatetimeIndex(["2026-04-22 14:00:00+00:00", "2026-04-23 14:00:00+00:00"]),
            )

    monkeypatch.setattr(yfinance_provider.yf, "Ticker", lambda _: InvalidHistoryTicker())
    monkeypatch.setattr(yfinance_provider, "_utcnow", lambda: datetime(2026, 4, 25, tzinfo=UTC))
    result = YFinanceProvider().fetch(AsxTicker.parse("BHP"), True, False)
    assert [bar.trading_date for bar in result.daily_price_bars] == [date(2026, 4, 23)]
    assert any(f.segment_name == "daily-price-history" for f in result.segment_failures)


def test_actions_extract_dividends_splits_and_skip_invalid_rows(monkeypatch) -> None:
    class ActionTicker(StubTicker):
        def get_actions(self, period="max"):
            assert period == "max"
            return pd.DataFrame(
                {"Dividends": [Decimal("2.70"), 0, -1], "Stock Splits": [0, 2, 0]},
                index=pd.DatetimeIndex(
                    ["2026-04-20", "2026-04-21", "2026-04-22"], tz="Australia/Sydney"
                ),
            )

    monkeypatch.setattr(yfinance_provider.yf, "Ticker", lambda _: ActionTicker())
    monkeypatch.setattr(yfinance_provider, "_utcnow", lambda: datetime(2026, 4, 24, tzinfo=UTC))
    result = YFinanceProvider().fetch(AsxTicker.parse("BHP"), True, False)
    assert [(a.action_type, a.effective_date, a.value) for a in result.corporate_actions] == [
        ("dividend", date(2026, 4, 20), Decimal("2.70")),
        ("split", date(2026, 4, 21), Decimal("2")),
    ]
    assert any(f.segment_name == "corporate-actions" for f in result.segment_failures)


def test_action_error_does_not_discard_prices(monkeypatch) -> None:
    class ActionTicker(StubTicker):
        def get_actions(self, period="max"):
            raise RuntimeError("network unavailable")

    monkeypatch.setattr(yfinance_provider.yf, "Ticker", lambda _: ActionTicker())
    result = YFinanceProvider().fetch(AsxTicker.parse("BHP"), True, False)
    assert len(result.daily_price_bars) == 2
    assert any(f.segment_name == "corporate-actions" for f in result.segment_failures)


def test_provider_reports_missing_profile(monkeypatch) -> None:
    class EmptyTicker(StubTicker):
        def get_info(self):
            return {}

    monkeypatch.setattr(yfinance_provider.yf, "Ticker", lambda _: EmptyTicker())
    provider = YFinanceProvider()

    result = provider.fetch(AsxTicker.parse("CSL"), include_annual=True, include_quarterly=False)

    assert result.company_profile is None
    assert any(failure.segment_name == "company-profile" for failure in result.segment_failures)
