"""Validate price-series parsing without making network requests."""

import io
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from openpyxl import Workbook
from sqlalchemy.dialects import postgresql

from asx_financials.download_commodity_prices import (
    DAILY_SERIES,
    download,
    price,
    read_fred,
    read_world_bank,
    read_yahoo_futures,
    save_prices,
)


def test_fred_skips_missing_days_and_rejects_wrong_series() -> None:
    content = b"observation_date,DJFUELUSGULF\n1990-04-02,0.550\n1990-04-03,\n1990-04-04,0.560\n"
    rows = read_fred(content, "DJFUELUSGULF", "jet_fuel", "USD/gallon")
    assert [(row["date"], row["price"]) for row in rows] == [
        ("1990-04-02", "0.550"),
        ("1990-04-04", "0.560"),
    ]
    with pytest.raises(ValueError, match="Unexpected FRED columns"):
        read_fred(content, "DCOILWTICO", "crude_wti", "USD/barrel")
    assert price(".") is None
    assert price("NaN") is None
    assert price("0") is None


def test_yahoo_brent_futures_rows_preserve_source_and_skip_invalid_closes() -> None:
    frame = pd.DataFrame(
        {"Close": [94.62, None, 0]},
        index=pd.to_datetime(["2026-11-04", "2026-11-05", "2026-11-06"]),
    )
    with patch("asx_financials.download_commodity_prices.yf.Ticker") as ticker:
        ticker.return_value.history.return_value = frame
        rows = read_yahoo_futures("BZ=F", "crude_brent_futures", "USD/barrel")
    ticker.assert_called_once_with("BZ=F")
    ticker.return_value.history.assert_called_once_with(period="max", auto_adjust=False)
    assert rows == [
        {
            "date": "2026-11-04",
            "commodity": "crude_brent_futures",
            "frequency": "daily",
            "price": "94.62",
            "unit": "USD/barrel",
            "source": "Yahoo Finance",
            "series": "BZ=F",
        }
    ]
    assert "crude_brent_daily" not in DAILY_SERIES


def test_download_saves_brent_futures_and_gold_without_fred_brent() -> None:
    with (
        patch("asx_financials.download_commodity_prices.fetch", return_value=b"unused"),
        patch(
            "asx_financials.download_commodity_prices.read_fred",
            return_value=[{"date": "2026-11-04"}],
        ) as fred,
        patch(
            "asx_financials.download_commodity_prices.read_yahoo_futures",
            return_value=[{"date": "2026-11-04"}],
        ) as yahoo,
        patch("asx_financials.download_commodity_prices.read_world_bank", return_value={}),
        patch("asx_financials.download_commodity_prices.save_prices") as save,
    ):
        download(MagicMock())
    assert [call.args[1] for call in fred.call_args_list] == ["DCOILWTICO", "DJFUELUSGULF"]
    assert [call.args[:3] for call in yahoo.call_args_list] == [
        ("BZ=F", "crude_brent_futures", "USD/barrel"),
        ("GC=F", "gold_comex_futures", "USD/troy_oz"),
    ]
    assert save.call_count == 4


def test_world_bank_monthly_gold_and_crude_parsing() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Monthly Prices"
    sheet.append([None, "Gold", "Crude oil, WTI", "Crude oil, Brent"])
    sheet.append(["1960M01", 35, None, 1.63])
    sheet.append(["1960M02", 36, 2.5, 1.64])
    data = io.BytesIO()
    workbook.save(data)
    result = read_world_bank(data.getvalue())
    assert [(row["date"], row["price"]) for row in result["gold_monthly"]] == [
        ("1960-01-01", "35"),
        ("1960-02-01", "36"),
    ]
    assert len(result["crude_wti_monthly"]) == 1
    assert result["crude_brent_monthly"][0]["unit"] == "USD/barrel"


def test_save_prices_upserts_rows_with_provenance_and_dates() -> None:
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    rows = [
        {
            "date": "1960-01-01",
            "commodity": "gold",
            "frequency": "monthly",
            "price": "35.25",
            "unit": "USD/troy_oz",
            "source": "World Bank",
            "series": "Gold",
        }
    ]
    save_prices(engine, rows)
    assert connection.execute.call_count == 1
    statement = connection.execute.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (source, series, observation_date) DO UPDATE" in sql
    params = statement.compile(dialect=postgresql.dialect()).params
    assert params["observation_date_m0"] == date(1960, 1, 1)
    assert params["price_m0"] == Decimal("35.25")
    assert params["source_m0"] == "World Bank"
