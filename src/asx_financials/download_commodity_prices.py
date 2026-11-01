"""Download long-run oil, gold and jet-fuel benchmark prices into separate CSV files."""

import argparse
import csv
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import chain
from pathlib import Path

import requests
import yfinance as yf
from openpyxl import load_workbook

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
WORLD_BANK_URL = (
    "https://thedocs.worldbank.org/en/doc/"
    "74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/related/"
    "CMO-Historical-Data-Monthly.xlsx"
)
# The World Bank's October 2026 workbook is its latest published monthly history
# as of 2026-11-01. Update this URL when it publishes a newer historical workbook.
DAILY_SERIES = {
    "crude_wti_daily.csv": ("DCOILWTICO", "crude_wti", "USD/barrel"),
    "crude_brent_daily.csv": ("DCOILBRENTEU", "crude_brent", "USD/barrel"),
    "jet_fuel_us_gulf_coast_daily.csv": (
        "DJFUELUSGULF",
        "jet_fuel_us_gulf_coast",
        "USD/gallon",
    ),
}
MONTHLY_SERIES = {
    "gold_monthly.csv": ("Gold", "gold", "USD/troy_oz"),
    "crude_wti_monthly.csv": ("Crude oil, WTI", "crude_wti", "USD/barrel"),
    "crude_brent_monthly.csv": ("Crude oil, Brent", "crude_brent", "USD/barrel"),
}
FIELDS = ("date", "commodity", "frequency", "price", "unit", "source", "series")


def fetch(url: str) -> bytes:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.content


def price(value: object) -> str | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return format(amount, "f")


def read_fred(content: bytes, series: str, commodity: str, unit: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    if reader.fieldnames != ["observation_date", series]:
        raise ValueError(f"Unexpected FRED columns for {series}: {reader.fieldnames}")
    rows = []
    for row in reader:
        stamp = date.fromisoformat(row["observation_date"])
        amount = price(row[series])
        if amount is not None:
            rows.append(
                dict(
                    zip(
                        FIELDS,
                        (stamp.isoformat(), commodity, "daily", amount, unit, "FRED/EIA", series),
                        strict=True,
                    )
                )
            )
    if not rows:
        raise ValueError(f"No observations for {series}")
    return rows


def read_world_bank(content: bytes) -> dict[str, list[dict[str, str]]]:
    book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        sheet = book["Monthly Prices"]
        lines = sheet.iter_rows(values_only=True)
        headers = None
        for line in lines:
            if line[0] == "1960M01":
                break
            if "Gold" in line:
                headers = line
        else:
            raise ValueError("World Bank monthly data does not start in January 1960")
        if headers is None:
            raise ValueError("World Bank monthly price headers not found")
        columns = {name: headers.index(name) for name, _, _ in MONTHLY_SERIES.values()}
        output: dict[str, list[dict[str, str]]] = {name: [] for name in MONTHLY_SERIES}
        for row in chain((line,), lines):
            stamp = row[0]
            if not isinstance(stamp, str) or not re.fullmatch(r"\d{4}M(?:0[1-9]|1[0-2])", stamp):
                continue
            month = date(int(stamp[:4]), int(stamp[5:]), 1).isoformat()
            for filename, (column, commodity, unit) in MONTHLY_SERIES.items():
                amount = price(row[columns[column]])
                if amount is not None:
                    output[filename].append(
                        dict(
                            zip(
                                FIELDS,
                                (month, commodity, "monthly", amount, unit, "World Bank", column),
                                strict=True,
                            )
                        )
                    )
        if any(not rows for rows in output.values()):
            raise ValueError("World Bank workbook has an empty price series")
        return output
    finally:
        book.close()


def read_gold_futures() -> list[dict[str, str]]:
    """Yahoo's GC=F is a COMEX futures close, not a physical gold spot fixing."""
    frame = yf.Ticker("GC=F").history(period="max", auto_adjust=False)
    if frame.empty or "Close" not in frame:
        raise ValueError("Yahoo returned no GC=F daily futures closes")
    rows = []
    for stamp, value in frame["Close"].items():
        amount = price(value)
        if amount is not None:
            rows.append(
                dict(
                    zip(
                        FIELDS,
                        (
                            stamp.date().isoformat(),
                            "gold_comex_futures",
                            "daily",
                            amount,
                            "USD/troy_oz",
                            "Yahoo Finance",
                            "GC=F",
                        ),
                        strict=True,
                    )
                )
            )
    if not rows:
        raise ValueError("Yahoo returned no valid GC=F daily futures closes")
    return rows


def save_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def download(output: Path) -> None:
    errors = []
    for filename, (series, commodity, unit) in DAILY_SERIES.items():
        try:
            rows = read_fred(fetch(FRED_URL.format(series)), series, commodity, unit)
            save_csv(output / filename, rows)
            print(f"{filename}: {len(rows)} observations, {rows[0]['date']} to {rows[-1]['date']}")
        except (requests.RequestException, OSError, ValueError) as exc:
            errors.append(f"{filename}: {exc}")
    try:
        rows = read_gold_futures()
        save_csv(output / "gold_comex_futures_daily.csv", rows)
        print(
            f"gold_comex_futures_daily.csv: {len(rows)} observations, "
            f"{rows[0]['date']} to {rows[-1]['date']}"
        )
    except (OSError, ValueError, KeyError) as exc:
        errors.append(f"Yahoo GC=F futures: {exc}")
    try:
        for filename, rows in read_world_bank(fetch(WORLD_BANK_URL)).items():
            save_csv(output / filename, rows)
            print(f"{filename}: {len(rows)} observations, {rows[0]['date']} to {rows[-1]['date']}")
    except (requests.RequestException, OSError, ValueError, KeyError) as exc:
        errors.append(f"World Bank workbook: {exc}")
    if errors:
        raise RuntimeError("Some downloads failed:\n" + "\n".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/commodity_prices"))
    args = parser.parse_args()
    download(args.output)


if __name__ == "__main__":
    main()
