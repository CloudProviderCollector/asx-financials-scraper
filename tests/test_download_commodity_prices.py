"""Validate price-series parsing without making network requests."""

import io
from pathlib import Path

import pytest
from openpyxl import Workbook

from asx_financials.download_commodity_prices import price, read_fred, read_world_bank, save_csv


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


def test_world_bank_monthly_gold_and_crude_parsing(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Monthly Prices"
    sheet.append([None, "Gold", "Crude oil, WTI", "Crude oil, Brent"])
    sheet.append(["1960M01", 35, None, 1.63])
    sheet.append(["1960M02", 36, 2.5, 1.64])
    data = io.BytesIO()
    workbook.save(data)
    result = read_world_bank(data.getvalue())
    assert [(row["date"], row["price"]) for row in result["gold_monthly.csv"]] == [
        ("1960-01-01", "35"),
        ("1960-02-01", "36"),
    ]
    assert len(result["crude_wti_monthly.csv"]) == 1
    assert result["crude_brent_monthly.csv"][0]["unit"] == "USD/barrel"
    destination = tmp_path / "gold.csv"
    save_csv(destination, result["gold_monthly.csv"])
    assert destination.read_text(encoding="utf-8").splitlines()[0].startswith("date,commodity,")
    assert not destination.with_suffix(".tmp").exists()
