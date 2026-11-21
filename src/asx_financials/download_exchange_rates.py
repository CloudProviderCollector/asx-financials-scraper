"""Download Yahoo's daily AUD/USD closes into PostgreSQL."""

from datetime import UTC, date, datetime
from decimal import Decimal

import yfinance as yf
from sqlalchemy import Engine, create_engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url

from asx_financials.config import get_settings
from asx_financials.download_commodity_prices import price
from asx_financials.infrastructure.persistence.schema import exchange_rates

SOURCE = "Yahoo Finance"
SERIES = "AUDUSD=X"


def read_aud_usd() -> list[dict[str, str]]:
    frame = yf.Ticker(SERIES).history(period="max", auto_adjust=False)
    if frame.empty or "Close" not in frame:
        raise ValueError(f"Yahoo returned no {SERIES} daily closes")
    rows = []
    for stamp, value in frame["Close"].items():
        rate = price(value)
        if rate is not None:
            rows.append(
                {
                    "date": stamp.date().isoformat(),
                    "rate": rate,
                    "base_currency": "AUD",
                    "quote_currency": "USD",
                    "source": SOURCE,
                    "series": SERIES,
                }
            )
    if not rows:
        raise ValueError(f"Yahoo returned no valid {SERIES} daily closes")
    return rows


def save_rates(engine: Engine, rows: list[dict[str, str]]) -> None:
    fetched_at = datetime.now(UTC)
    with engine.begin() as connection:
        for offset in range(0, len(rows), 1000):
            values = [
                {
                    "source": row["source"],
                    "series": row["series"],
                    "observation_date": date.fromisoformat(row["date"]),
                    "base_currency": row["base_currency"],
                    "quote_currency": row["quote_currency"],
                    "rate": Decimal(row["rate"]),
                    "fetched_at": fetched_at,
                }
                for row in rows[offset : offset + 1000]
            ]
            statement = insert(exchange_rates).values(values)
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=["source", "series", "observation_date"],
                    set_={
                        "base_currency": statement.excluded.base_currency,
                        "quote_currency": statement.excluded.quote_currency,
                        "rate": statement.excluded.rate,
                        "fetched_at": statement.excluded.fetched_at,
                    },
                )
            )


def main() -> None:
    url = make_url(get_settings().database_url)
    if url.host == "localhost":
        url = url.set(host="127.0.0.1")
    engine = create_engine(url)
    try:
        rows = read_aud_usd()
        save_rates(engine, rows)
        print(f"{SERIES}: {len(rows)} daily closes, {rows[0]['date']} to {rows[-1]['date']}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
