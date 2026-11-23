"""Long-only, multi-symbol daily-bar execution with next-open orders."""

from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Protocol


@dataclass(frozen=True)
class Bar:
    ticker: str
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    sector: str = ""

    def __post_init__(self) -> None:
        if (
            not all(isfinite(x) and x > 0 for x in (self.open, self.high, self.low, self.close))
            or self.low > min(self.open, self.close, self.high)
            or self.high < max(self.open, self.close)
            or self.volume < 0
        ):
            raise ValueError(f"{self.ticker} {self.day}: invalid OHLCV bar")


@dataclass(frozen=True)
class Signal:
    side: str
    stop_distance: float = 0.0


class Strategy(Protocol):
    def signal(
        self, history: list[Bar], holding: bool, trailing_stop: float | None
    ) -> Signal | None:
        """Return a close-of-session decision using only this ticker's history."""

    def trailing_distance(self, history: list[Bar]) -> float | None:
        """Return the distance below the highest close for an existing position."""


@dataclass(frozen=True)
class Settings:
    cash: float = 100_000.0
    risk_fraction: float = 0.005
    max_position_fraction: float = 0.10
    max_sector_fraction: float = 0.25
    commission: float = 10.0
    slippage_bps: float = 5.0

    def __post_init__(self) -> None:
        if (
            self.cash <= 0
            or not 0 < self.risk_fraction <= 1
            or not 0 < self.max_position_fraction <= 1
            or not 0 < self.max_sector_fraction <= 1
            or self.commission < 0
            or self.slippage_bps < 0
        ):
            raise ValueError("Invalid backtest settings")


@dataclass
class Position:
    shares: int
    entry_price: float
    entry_day: date
    stop: float
    peak: float
    sector: str


@dataclass(frozen=True)
class Trade:
    ticker: str
    entry_day: date
    exit_day: date
    shares: int
    entry_price: float
    exit_price: float
    net_pnl: float


@dataclass(frozen=True)
class Result:
    equity: list[tuple[date, float]]
    trades: list[Trade]
    open_positions: dict[str, Position]
    cash: float
    initial_cash: float

    @property
    def total_return(self) -> float:
        return self.equity[-1][1] / self.initial_cash - 1


def run(
    bars: list[Bar], strategy: Strategy, settings: Settings = Settings(), start: date | None = None
) -> Result:
    if not bars:
        raise ValueError("No price bars")
    ordered = sorted(bars, key=lambda bar: (bar.day, bar.ticker))
    if len({(bar.day, bar.ticker) for bar in ordered}) != len(ordered):
        raise ValueError("Duplicate ticker/trading date")
    histories: dict[str, list[Bar]] = {}
    positions: dict[str, Position] = {}
    pending: dict[str, Signal] = {}
    last_close: dict[str, float] = {}
    trades: list[Trade] = []
    equity: list[tuple[date, float]] = []
    cash = settings.cash
    index = 0
    while index < len(ordered):
        day = ordered[index].day
        today: list[Bar] = []
        while index < len(ordered) and ordered[index].day == day:
            today.append(ordered[index])
            index += 1
        if start is not None and day < start:
            for bar in today:
                histories.setdefault(bar.ticker, []).append(bar)
                last_close[bar.ticker] = bar.close
            continue

        # Execute exits first; missing ticker sessions defer their pending order.
        for bar in today:
            position = positions.get(bar.ticker)
            if pending.get(bar.ticker, None) is None or position is None:
                continue
            if pending[bar.ticker].side == "sell":
                fill = bar.open * (1 - settings.slippage_bps / 10_000)
                cash += position.shares * fill - settings.commission
                trades.append(
                    Trade(
                        bar.ticker,
                        position.entry_day,
                        day,
                        position.shares,
                        position.entry_price,
                        fill,
                        position.shares * (fill - position.entry_price) - 2 * settings.commission,
                    )
                )
                del positions[bar.ticker]
                del pending[bar.ticker]

        open_equity = cash + sum(
            position.shares
            * (next((bar.open for bar in today if bar.ticker == ticker), last_close[ticker]))
            for ticker, position in positions.items()
        )
        for bar in today:
            signal = pending.get(bar.ticker)
            if signal is None or signal.side != "buy":
                continue
            del pending[bar.ticker]
            if bar.ticker in positions:
                continue
            fill = bar.open * (1 + settings.slippage_bps / 10_000)
            opening_prices = {current.ticker: current.open for current in today}
            sector_value = sum(
                position.shares * opening_prices.get(ticker, last_close[ticker])
                for ticker, position in positions.items()
                if position.sector == bar.sector
            )
            budget = min(
                cash - settings.commission,
                open_equity * settings.max_position_fraction,
                open_equity * settings.max_sector_fraction - sector_value,
                open_equity * settings.risk_fraction * fill / signal.stop_distance,
            )
            shares = max(0, int(budget // fill))
            if shares:
                cash -= shares * fill + settings.commission
                positions[bar.ticker] = Position(
                    shares, fill, day, fill - signal.stop_distance, fill, bar.sector
                )

        for bar in today:
            history = histories.setdefault(bar.ticker, [])
            history.append(bar)
            last_close[bar.ticker] = bar.close
            position = positions.get(bar.ticker)
            if position is not None:
                position.peak = max(position.peak, bar.close)
                distance = strategy.trailing_distance(history)
                if distance is not None:
                    position.stop = max(position.stop, position.peak - distance)
            if bar.ticker in pending:
                continue
            signal = strategy.signal(
                history, position is not None, position.stop if position else None
            )
            if signal is not None:
                if signal.side == "buy" and position is None:
                    if not isfinite(signal.stop_distance) or signal.stop_distance <= 0:
                        raise ValueError("Buy signal requires a positive finite stop distance")
                    pending[bar.ticker] = signal
                elif signal.side == "sell" and position is not None:
                    pending[bar.ticker] = signal
                else:
                    raise ValueError(f"Invalid signal: {signal.side}")
        equity.append(
            (
                day,
                cash
                + sum(
                    position.shares * last_close[ticker] for ticker, position in positions.items()
                ),
            )
        )
    if not equity:
        raise ValueError("No bars on or after the requested start date")
    return Result(equity, trades, positions, cash, settings.cash)
