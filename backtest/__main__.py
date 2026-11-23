"""Run a daily ASX trend backtest and print summary metrics."""

import argparse
import csv
from datetime import date, datetime, timedelta
from pathlib import Path

from asx_financials.domain.value_objects import AsxTicker
from asx_financials.infrastructure.providers.yfinance_provider import ASX_TIMEZONE
from backtest.archive import save_run_set
from backtest.charts import trading_chart
from backtest.data import load_bars
from backtest.engine import Settings, run
from backtest.strategies import STRATEGIES


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("postgres", "yahoo"), default="postgres")
    parser.add_argument("--strategy", choices=STRATEGIES, default="moving-average")
    parser.add_argument("--tickers", nargs="+", required=True, help="ASX ticker codes")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--equity-csv", type=Path)
    parser.add_argument("--trades-csv", type=Path)
    parser.add_argument("--chart-html", type=Path, help="Save an interactive trade/equity chart")
    parser.add_argument(
        "--record-all",
        action="store_true",
        help="Save a comparable snapshot of every named strategy in backtest/records",
    )
    args = parser.parse_args()
    symbols = [AsxTicker.parse(symbol).value for symbol in args.tickers]
    if len(set(symbols)) != len(symbols):
        parser.error("Duplicate tickers")
    if args.start and args.end and args.end < args.start:
        parser.error("End date precedes start date")
    end = args.end or datetime.now(ASX_TIMEZONE).date() - timedelta(days=1)
    bars = load_bars(args.source, symbols, end)
    version = STRATEGIES[args.strategy]
    result = run(bars, version.strategy, Settings(), args.start)
    peak = result.initial_cash
    drawdown = 0.0
    for _, value in result.equity:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    print(f"Period: {result.equity[0][0]} to {result.equity[-1][0]}")
    print(f"Strategy: {args.strategy}")
    print(f"Tickers: {', '.join(symbols)}")
    print(f"Final equity: AUD {result.equity[-1][1]:,.2f}")
    print(f"Total return: {result.total_return:.2%}")
    print(f"Max drawdown: {drawdown:.2%}")
    print(f"Completed trades: {len(result.trades)}; open positions: {len(result.open_positions)}")
    if args.equity_csv:
        with args.equity_csv.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(("date", "equity_aud"))
            writer.writerows(result.equity)
    if args.trades_csv:
        with args.trades_csv.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(
                (
                    "ticker",
                    "entry_date",
                    "exit_date",
                    "shares",
                    "entry_price",
                    "exit_price",
                    "net_pnl_aud",
                )
            )
            writer.writerows(
                (
                    trade.ticker,
                    trade.entry_day,
                    trade.exit_day,
                    trade.shares,
                    trade.entry_price,
                    trade.exit_price,
                    trade.net_pnl,
                )
                for trade in result.trades
            )
    if args.chart_html:
        trading_chart(bars, result, moving_average_windows=version.averages).write_html(
            args.chart_html, include_plotlyjs=True
        )
        print(f"Chart: {args.chart_html}")
    if args.record_all:
        saved = save_run_set(Path("backtest") / "records", bars, args.start, end, args.source)
        print(f"Recorded strategies: {saved}")


if __name__ == "__main__":
    main()
