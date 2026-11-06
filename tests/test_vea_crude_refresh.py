"""Targeted VEA/Brent refresh coverage without external network access."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from asx_financials import vea_crude_refresh as refresh


def test_refresh_limits_prices_and_only_fetches_selected_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable migrated PostgreSQL database")
    engine = create_engine(url)
    day = date(2026, 1, 9)
    future = date(2026, 1, 12)
    for stamp in (day, future):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM daily_prices WHERE exchange_code='ASX' AND ticker='VEA' "
                    "AND trading_date=:day"
                ),
                {"day": stamp},
            )
            conn.execute(
                text(
                    "DELETE FROM commodity_prices WHERE source='Yahoo Finance' "
                    "AND series='BZ=F' AND observation_date=:day"
                ),
                {"day": stamp},
            )

    def bar(stamp: date) -> SimpleNamespace:
        return SimpleNamespace(
            trading_date=stamp,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
            adjusted_close=None,
            volume=100,
            source="yfinance",
            fetched_at_utc=datetime.now(UTC),
        )

    provider = Mock()
    provider._fetch_price_history.return_value = [bar(day), bar(future)]
    monkeypatch.setattr(refresh, "YFinanceProvider", lambda: provider)
    frame = pd.DataFrame(
        {"Close": [80, 81, 82]},
        index=pd.to_datetime(["2026-01-09", "2026-01-12", "2026-01-13"]).tz_localize(
            "America/New_York"
        ),
    )
    ticker = Mock()
    ticker.history.return_value = frame
    yahoo = Mock(return_value=ticker)
    monkeypatch.setattr(refresh.yf, "Ticker", yahoo)
    try:
        refresh.refresh_prices(engine, day)
        with engine.connect() as conn:
            assert (
                conn.scalar(
                    text(
                        "SELECT close FROM daily_prices WHERE exchange_code='ASX' "
                        "AND ticker='VEA' AND trading_date=:day"
                    ),
                    {"day": day},
                )
                == 10
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM daily_prices WHERE exchange_code='ASX' "
                        "AND ticker='VEA' AND trading_date=:day"
                    ),
                    {"day": future},
                )
                == 0
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT price FROM commodity_prices WHERE source='Yahoo Finance' "
                        "AND series='BZ=F' AND observation_date=:day"
                    ),
                    {"day": day},
                )
                == 80
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM commodity_prices WHERE source='Yahoo Finance' "
                        "AND series='BZ=F' AND observation_date=:day"
                    ),
                    {"day": future},
                )
                == 0
            )
        assert provider._fetch_price_history.call_count == 1
        yahoo.assert_any_call("BZ=F")
        ticker.history.assert_called_once_with(period="max", auto_adjust=False)
    finally:
        with engine.begin() as conn:
            for stamp in (day, future):
                conn.execute(
                    text(
                        "DELETE FROM daily_prices WHERE exchange_code='ASX' "
                        "AND ticker='VEA' AND trading_date=:day"
                    ),
                    {"day": stamp},
                )
                conn.execute(
                    text(
                        "DELETE FROM commodity_prices WHERE source='Yahoo Finance' "
                        "AND series='BZ=F' AND observation_date=:day"
                    ),
                    {"day": stamp},
                )
        engine.dispose()
