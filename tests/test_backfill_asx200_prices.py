from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, Mock

import pandas as pd
import pytest

from asx_financials import backfill_asx200_prices as backfill
from asx_financials.domain.models import DailyPriceBar
from asx_financials.domain.value_objects import AsxTicker
from asx_financials.infrastructure.providers import yfinance_provider
from asx_financials.infrastructure.providers.yfinance_provider import YFinanceProvider


def test_fetch_all_prices_requests_max_history_without_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yahoo_ticker = Mock()
    yahoo_ticker.history.return_value = pd.DataFrame(
        {
            "Open": [1.0],
            "High": [1.2],
            "Low": [0.9],
            "Close": [1.1],
            "Volume": [100],
        },
        index=pd.DatetimeIndex(["1995-07-31"], tz="Australia/Sydney"),
    )
    factory = Mock(return_value=yahoo_ticker)
    monkeypatch.setattr(yfinance_provider.yf, "Ticker", factory)
    session = Mock()

    bars, malformed = YFinanceProvider().fetch_all_daily_prices(
        AsxTicker.parse("QAN"), session=session
    )

    factory.assert_called_once_with("QAN.AX", session=session)
    yahoo_ticker.history.assert_called_once_with(interval="1d", auto_adjust=False, period="max")
    yahoo_ticker.get_info.assert_not_called()
    assert bars[0].trading_date == date(1995, 7, 31)
    assert bars[0].volume == 100
    assert malformed == []


def test_fetch_all_prices_reports_malformed_rows_and_keeps_valid_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticker = Mock()
    ticker.history.return_value = pd.DataFrame(
        {
            "Open": [1.0, None],
            "High": [1.2, 1.2],
            "Low": [0.9, 0.9],
            "Close": [1.1, 1.1],
            "Volume": [100, 100],
        },
        index=pd.DatetimeIndex(["1995-07-31", "1995-08-01"], tz="Australia/Sydney"),
    )
    monkeypatch.setattr(yfinance_provider.yf, "Ticker", lambda *args, **kwargs: ticker)

    bars, malformed = YFinanceProvider().fetch_all_daily_prices(AsxTicker.parse("QAN"))

    assert [bar.trading_date for bar in bars] == [date(1995, 7, 31)]
    assert len(malformed) == 1
    assert "Missing OHLC price" in malformed[0]


def test_fetch_all_prices_rejects_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    ticker = Mock()
    ticker.history.return_value = pd.DataFrame()
    monkeypatch.setattr(yfinance_provider.yf, "Ticker", lambda *args, **kwargs: ticker)
    with pytest.raises(ValueError, match="Price history was empty"):
        YFinanceProvider().fetch_all_daily_prices(AsxTicker.parse("QAN"))


def test_fetch_all_prices_rejects_wholly_malformed_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticker = Mock()
    ticker.history.return_value = pd.DataFrame(
        {"Open": [None], "High": [1.2], "Low": [0.9], "Close": [1.1], "Volume": [100]},
        index=pd.DatetimeIndex(["1995-07-31"], tz="Australia/Sydney"),
    )
    monkeypatch.setattr(yfinance_provider.yf, "Ticker", lambda *args, **kwargs: ticker)

    with pytest.raises(ValueError, match="Missing OHLC price"):
        YFinanceProvider().fetch_all_daily_prices(AsxTicker.parse("QAN"))


def test_save_bars_inserts_missing_rows_in_batches() -> None:
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    connection.scalars.return_value.all.return_value = [date(1995, 7, 31)]
    bar = DailyPriceBar(
        trading_date=date(1995, 7, 31),
        open=Decimal("1"),
        high=Decimal("1.2"),
        low=Decimal("0.9"),
        close=Decimal("1.1"),
        adjusted_close=None,
        volume=100,
        source="yfinance",
        fetched_at_utc=datetime(2026, 11, 22, tzinfo=UTC),
    )

    assert backfill.save_bars(engine, AsxTicker.parse("QAN"), [bar] * 501) == 2
    assert connection.scalars.call_count == 2
    statement = connection.scalars.call_args_list[0].args[0]
    assert "ON CONFLICT (exchange_code, ticker, trading_date) DO NOTHING" in str(statement)


def test_backfill_resumes_only_incomplete_tickers(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    symbols = ["QAN"] + [f"T{i:03}" for i in range(199)]
    monkeypatch.setattr(backfill, "load_tickers", lambda path: symbols)
    monkeypatch.setattr(backfill, "read_progress", lambda path: (set(symbols[1:]), None))
    monkeypatch.setattr(backfill, "PacedSession", MagicMock())
    provider = Mock()
    provider.fetch_all_daily_prices.return_value = (
        [Mock(trading_date=date(1995, 7, 31))],
        ["Skipped malformed price row: Missing OHLC price"],
    )
    monkeypatch.setattr(backfill, "YFinanceProvider", lambda: provider)
    monkeypatch.setattr(backfill, "save_bars", lambda engine, ticker, bars: 1)
    progress = tmp_path / "progress.jsonl"

    backfill.backfill(Mock(), progress)

    provider.fetch_all_daily_prices.assert_called_once()
    assert provider.fetch_all_daily_prices.call_args.args[0].value == "QAN"
    assert '"status": "succeeded"' in progress.read_text()
    assert (
        '"skipped_rows": ["Skipped malformed price row: Missing OHLC price"]'
        in progress.read_text()
    )


def test_rate_limit_stops_backfill_without_marking_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    symbols = ["QAN"] + [f"T{i:03}" for i in range(199)]
    monkeypatch.setattr(backfill, "load_tickers", lambda path: symbols)
    monkeypatch.setattr(backfill, "read_progress", lambda path: (set(symbols[1:]), None))
    monkeypatch.setattr(backfill, "PacedSession", MagicMock())
    provider = Mock()
    provider.fetch_all_daily_prices.side_effect = ValueError("Yahoo rate limit 429")
    monkeypatch.setattr(backfill, "YFinanceProvider", lambda: provider)
    progress = tmp_path / "progress.jsonl"

    with pytest.raises(RuntimeError, match="rate limit detected"):
        backfill.backfill(Mock(), progress)

    assert '"status": "error"' in progress.read_text()
    assert '"status": "succeeded"' not in progress.read_text()
