"""Refresh only the price series consumed by the VEA/Brent strategies."""

from datetime import UTC, date, datetime

import yfinance as yf
from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert

from asx_financials.domain.models import SegmentFailure
from asx_financials.download_commodity_prices import price, save_prices
from asx_financials.infrastructure.persistence.schema import commodity_prices, daily_prices, tickers
from asx_financials.infrastructure.providers.yfinance_provider import YFinanceProvider

BRENT_SYMBOL = "BZ=F"
BRENT_SOURCE = "Yahoo Finance"


def latest_dates(engine: Engine) -> tuple[date | None, date | None]:
    with engine.connect() as connection:
        vea = connection.scalar(
            select(func.max(daily_prices.c.trading_date)).where(
                daily_prices.c.exchange_code == "ASX", daily_prices.c.ticker == "VEA"
            )
        )
        brent = connection.scalar(
            select(func.max(commodity_prices.c.observation_date)).where(
                commodity_prices.c.source == BRENT_SOURCE,
                commodity_prices.c.series == BRENT_SYMBOL,
                commodity_prices.c.frequency == "daily",
            )
        )
    return vea, brent


def refresh_vea(engine: Engine, through: date) -> None:
    latest_vea, _ = latest_dates(engine)
    failures: list[SegmentFailure] = []
    bars = YFinanceProvider()._fetch_price_history(
        yf.Ticker("VEA.AX"), latest_vea, datetime.now(UTC), failures
    )
    if failures:
        raise RuntimeError("VEA price refresh failed: " + "; ".join(f.reason for f in failures))
    with engine.begin() as connection:
        if bars:
            now = datetime.now(UTC)
            connection.execute(
                insert(tickers)
                .values(
                    exchange_code="ASX",
                    ticker="VEA",
                    yahoo_symbol="VEA.AX",
                    source_name="yfinance",
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=[tickers.c.exchange_code, tickers.c.ticker])
            )
        for bar in bars:
            if bar.trading_date > through:
                continue
            values = dict(
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                adjusted_close=bar.adjusted_close,
                volume=bar.volume,
                source=bar.source,
                fetched_at=bar.fetched_at_utc,
                updated_at=datetime.now(UTC),
            )
            statement = insert(daily_prices).values(
                exchange_code="ASX",
                ticker="VEA",
                trading_date=bar.trading_date,
                created_at=datetime.now(UTC),
                **values,
            )
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=["exchange_code", "ticker", "trading_date"], set_=values
                )
            )


def refresh_brent(engine: Engine, through: date) -> None:
    frame = yf.Ticker(BRENT_SYMBOL).history(period="max", auto_adjust=False)
    if frame.empty or "Close" not in frame:
        raise ValueError(f"Yahoo returned no {BRENT_SYMBOL} daily futures closes")
    rows = []
    for stamp, value in frame["Close"].items():
        day = stamp.date()
        if day > through:
            continue
        amount = price(value)
        if amount is not None:
            rows.append(
                {
                    "date": day.isoformat(),
                    "commodity": "crude_brent_futures",
                    "frequency": "daily",
                    "price": amount,
                    "unit": "USD/barrel",
                    "source": BRENT_SOURCE,
                    "series": BRENT_SYMBOL,
                }
            )
    if not rows:
        raise ValueError(f"Yahoo returned no valid {BRENT_SYMBOL} closes through {through}")
    save_prices(engine, rows)


def refresh_prices(engine: Engine, through: date) -> None:
    """Attempt both feeds, then report any failures without losing successful updates."""
    errors = []
    for name, refresh in (("VEA", refresh_vea), ("Brent", refresh_brent)):
        try:
            refresh(engine, through)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise RuntimeError("Price refresh failed: " + "; ".join(errors))
