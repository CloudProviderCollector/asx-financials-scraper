"""Backfill available Yahoo daily OHLCV for the fixed ASX 200 roster."""

import argparse
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.dialects.postgresql import insert
from workflows.asx_200_profiles_workflow import ROSTER, PacedSession

from asx_financials.config import get_settings
from asx_financials.domain.models import DailyPriceBar
from asx_financials.domain.value_objects import AsxTicker
from asx_financials.infrastructure.persistence.schema import daily_prices, tickers
from asx_financials.infrastructure.providers.yfinance_provider import YFinanceProvider
from asx_financials.ingest_ticker_csv import load_tickers, read_progress, record

BATCH_SIZE = 500


def save_bars(engine: Engine, ticker: AsxTicker, bars: list[DailyPriceBar]) -> int:
    """Insert missing bars atomically for one ticker; never replace existing history."""
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(tickers)
            .values(
                exchange_code="ASX",
                ticker=ticker.value,
                yahoo_symbol=ticker.yahoo_symbol,
                source_name="yfinance",
                status="active",
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[tickers.c.exchange_code, tickers.c.ticker])
        )
        inserted = 0
        for offset in range(0, len(bars), BATCH_SIZE):
            batch = bars[offset : offset + BATCH_SIZE]
            statement = insert(daily_prices).values(
                [
                    {
                        "exchange_code": "ASX",
                        "ticker": ticker.value,
                        "trading_date": bar.trading_date,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "adjusted_close": bar.adjusted_close,
                        "volume": bar.volume,
                        "source": bar.source,
                        "fetched_at": bar.fetched_at_utc,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for bar in batch
                ]
            )
            inserted += len(
                connection.scalars(
                    statement.on_conflict_do_nothing(
                        index_elements=[
                            daily_prices.c.exchange_code,
                            daily_prices.c.ticker,
                            daily_prices.c.trading_date,
                        ]
                    ).returning(daily_prices.c.trading_date)
                ).all()
            )
    return inserted


def backfill(engine: Engine, progress: Path, interval: int = 60) -> None:
    if interval < 60:
        raise ValueError("The minimum interval between ticker starts is 60 seconds")
    symbols = load_tickers(ROSTER)
    if len(symbols) != 200:
        raise ValueError("ASX 200 roster must contain exactly 200 tickers")
    completed, last_start = read_progress(progress)
    remaining = [symbol for symbol in symbols if symbol not in completed]
    print(f"Backfilling {len(remaining)} of {len(symbols)} tickers", flush=True)
    provider = YFinanceProvider()
    errors: list[str] = []
    with PacedSession() as session:
        for index, symbol in enumerate(remaining, 1):
            if last_start is not None:
                time.sleep(max(0, interval - (datetime.now(UTC) - last_start).total_seconds()))
            last_start = datetime.now(UTC)
            record(progress, {"event": "start", "ticker": symbol, "time": last_start.isoformat()})
            try:
                ticker = AsxTicker.parse(symbol)
                bars, malformed = provider.fetch_all_daily_prices(ticker, session=session)
                inserted = save_bars(engine, ticker, bars)
            except Exception as exc:
                record(
                    progress,
                    {
                        "event": "finish",
                        "ticker": symbol,
                        "time": datetime.now(UTC).isoformat(),
                        "status": "error",
                        "error": str(exc),
                    },
                )
                print(f"[{index}/{len(remaining)}] {symbol}: error: {exc}", flush=True)
                errors.append(symbol)
                if any(
                    marker in str(exc).lower()
                    for marker in ("rate limit", "too many requests", "429", "yfratelimiterror")
                ):
                    raise RuntimeError("Yahoo rate limit detected; stopped backfill") from exc
                continue
            record(
                progress,
                {
                    "event": "finish",
                    "ticker": symbol,
                    "time": datetime.now(UTC).isoformat(),
                    "status": "succeeded",
                    "prices": len(bars),
                    "inserted": inserted,
                    "earliest": min(bar.trading_date for bar in bars).isoformat(),
                    "skipped_rows": malformed,
                },
            )
            print(
                f"[{index}/{len(remaining)}] {symbol}: {len(bars)} bars, "
                f"{inserted} inserted, earliest {min(bar.trading_date for bar in bars)}, "
                f"{len(malformed)} malformed rows skipped",
                flush=True,
            )
    if errors:
        raise RuntimeError(f"Backfill failed for {len(errors)} tickers: {', '.join(errors)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--progress", required=True, type=Path)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        backfill(engine, args.progress, args.interval)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
