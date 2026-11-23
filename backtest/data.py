"""Load daily bars from persisted ASX history or explicit Yahoo downloads."""

from datetime import date
from sys import stderr

from sqlalchemy import Engine, create_engine, select

from asx_financials.config import get_settings
from asx_financials.domain.models import DailyPriceBar
from asx_financials.domain.value_objects import AsxTicker
from asx_financials.infrastructure.persistence.schema import daily_prices, tickers
from asx_financials.infrastructure.providers.yfinance_provider import YFinanceProvider
from backtest.engine import Bar


def _bar(ticker: str, sector: str, row: DailyPriceBar) -> Bar:
    if any(
        value is None
        for value in (row.open, row.high, row.low, row.close, row.adjusted_close, row.volume)
    ):
        raise ValueError(f"{ticker} {row.trading_date}: incomplete OHLCV/adjusted close")
    assert row.close is not None and row.adjusted_close is not None
    assert row.open is not None and row.high is not None and row.low is not None
    assert row.volume is not None
    if row.close <= 0 or row.adjusted_close <= 0:
        raise ValueError(f"{ticker} {row.trading_date}: invalid adjusted close")
    factor = row.adjusted_close / row.close
    return Bar(
        ticker,
        row.trading_date,
        float(row.open * factor),
        float(row.high * factor),
        float(row.low * factor),
        float(row.adjusted_close),
        row.volume,
        sector,
    )


def from_postgres(engine: Engine, symbols: list[str], end: date | None = None) -> list[Bar]:
    result: list[Bar] = []
    with engine.connect() as connection:
        for symbol in symbols:
            stmt = (
                select(daily_prices, tickers.c.sector)
                .join(
                    tickers,
                    (tickers.c.exchange_code == daily_prices.c.exchange_code)
                    & (tickers.c.ticker == daily_prices.c.ticker),
                )
                .where(daily_prices.c.exchange_code == "ASX", daily_prices.c.ticker == symbol)
                .order_by(daily_prices.c.trading_date)
            )
            if end is not None:
                stmt = stmt.where(daily_prices.c.trading_date <= end)
            for row in connection.execute(stmt).mappings():
                result.append(
                    _bar(
                        symbol,
                        row["sector"] or symbol,
                        DailyPriceBar(
                            row["trading_date"],
                            row["open"],
                            row["high"],
                            row["low"],
                            row["close"],
                            row["adjusted_close"],
                            row["volume"],
                            row["source"],
                            row["fetched_at"],
                        ),
                    )
                )
    return result


def from_yahoo(symbols: list[str], end: date | None = None) -> list[Bar]:
    provider = YFinanceProvider()
    result: list[Bar] = []
    for symbol in symbols:
        prices, malformed = provider.fetch_all_daily_prices(AsxTicker.parse(symbol))
        if malformed:
            raise ValueError(f"{symbol}: {len(malformed)} malformed Yahoo price rows")
        for row in prices:
            if end is not None and row.trading_date > end:
                continue
            try:
                result.append(_bar(symbol, symbol, row))
            except ValueError as exc:
                print(f"Skipping Yahoo bar: {exc}", file=stderr)
    return result


def load_bars(source: str, symbols: list[str], end: date) -> list[Bar]:
    if source == "postgres":
        engine = create_engine(get_settings().database_url, pool_pre_ping=True)
        try:
            bars = from_postgres(engine, symbols, end)
        finally:
            engine.dispose()
    elif source == "yahoo":
        bars = from_yahoo(symbols, end)
    else:
        raise ValueError(f"Unknown price source: {source}")
    missing = set(symbols) - {bar.ticker for bar in bars}
    if missing:
        raise ValueError(f"No history for: {', '.join(sorted(missing))}")
    return bars
