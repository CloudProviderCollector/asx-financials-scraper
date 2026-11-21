"""Verify AUD/USD history parsing and persistence without network requests."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy.dialects import postgresql

from asx_financials.download_exchange_rates import read_aud_usd, save_rates


def test_read_aud_usd_preserves_quote_direction_and_skips_invalid_closes() -> None:
    frame = pd.DataFrame(
        {"Close": [0.65, None, 0]},
        index=pd.to_datetime(["2026-11-12", "2026-11-13", "2026-11-16"]),
    )
    with patch("asx_financials.download_exchange_rates.yf.Ticker") as ticker:
        ticker.return_value.history.return_value = frame
        rows = read_aud_usd()

    ticker.assert_called_once_with("AUDUSD=X")
    ticker.return_value.history.assert_called_once_with(period="max", auto_adjust=False)
    assert rows == [
        {
            "date": "2026-11-12",
            "rate": "0.65",
            "base_currency": "AUD",
            "quote_currency": "USD",
            "source": "Yahoo Finance",
            "series": "AUDUSD=X",
        }
    ]


def test_read_aud_usd_rejects_empty_history() -> None:
    with patch("asx_financials.download_exchange_rates.yf.Ticker") as ticker:
        ticker.return_value.history.return_value = pd.DataFrame()
        with pytest.raises(ValueError, match="no AUDUSD=X"):
            read_aud_usd()


def test_save_rates_upserts_rate_and_currency_direction() -> None:
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    save_rates(
        engine,
        [
            {
                "date": "2026-11-12",
                "rate": "0.65",
                "base_currency": "AUD",
                "quote_currency": "USD",
                "source": "Yahoo Finance",
                "series": "AUDUSD=X",
            }
        ],
    )

    statement = connection.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "ON CONFLICT (source, series, observation_date) DO UPDATE" in str(compiled)
    assert compiled.params["observation_date_m0"] == date(2026, 11, 12)
    assert compiled.params["rate_m0"] == Decimal("0.65")
    assert compiled.params["base_currency_m0"] == "AUD"
    assert compiled.params["quote_currency_m0"] == "USD"
