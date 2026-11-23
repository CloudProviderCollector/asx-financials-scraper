"""Daily trend strategies evaluated on completed closes."""

from dataclasses import dataclass
from statistics import mean
from typing import Literal

from backtest.engine import Bar, Signal


def _atr_distance(history: list[Bar], window: int, multiplier: float) -> float | None:
    if len(history) <= window:
        return None
    recent = history[-window:]
    previous = history[-window - 1 : -1]
    true_ranges = [
        max(bar.high - bar.low, abs(bar.high - prior.close), abs(bar.low - prior.close))
        for bar, prior in zip(recent, previous, strict=True)
    ]
    return mean(true_ranges) * multiplier


@dataclass(frozen=True)
class MovingAverageTrend:
    fast_window: int = 5
    entry_window: int = 20
    slow_window: int = 65
    atr_window: int = 20
    atr_multiplier: float = 3.0
    entry_mode: Literal["cross", "state"] = "cross"
    exit_mode: Literal["price", "average_state", "average_cross"] = "price"

    def __post_init__(self) -> None:
        if (
            not 0 < self.fast_window < self.entry_window < self.slow_window
            or self.atr_window < 1
            or self.atr_multiplier <= 0
            or self.entry_mode not in ("cross", "state")
            or self.exit_mode not in ("price", "average_state", "average_cross")
        ):
            raise ValueError("Invalid moving-average strategy parameters")

    def trailing_distance(self, history: list[Bar]) -> float | None:
        return None

    def signal(
        self, history: list[Bar], holding: bool, trailing_stop: float | None
    ) -> Signal | None:
        if holding:
            if self.exit_mode == "price":
                if len(history) >= self.fast_window and history[-1].close < mean(
                    bar.close for bar in history[-self.fast_window :]
                ):
                    return Signal("sell")
                return None
            required = self.entry_window + (self.exit_mode == "average_cross")
            if len(history) < required:
                return None
            current_fast = mean(bar.close for bar in history[-self.fast_window :])
            current_entry = mean(bar.close for bar in history[-self.entry_window :])
            if current_fast >= current_entry:
                return None
            if self.exit_mode == "average_state":
                return Signal("sell")
            previous_fast = mean(bar.close for bar in history[-self.fast_window - 1 : -1])
            previous_entry = mean(bar.close for bar in history[-self.entry_window - 1 : -1])
            return Signal("sell") if previous_fast > previous_entry else None
        required = max(self.slow_window + (self.entry_mode == "cross"), self.atr_window + 1)
        if len(history) < required:
            return None
        current_entry = mean(bar.close for bar in history[-self.entry_window :])
        current_slow = mean(bar.close for bar in history[-self.slow_window :])
        if current_entry <= current_slow:
            return None
        if self.entry_mode == "cross":
            previous_entry = mean(bar.close for bar in history[-self.entry_window - 1 : -1])
            previous_slow = mean(bar.close for bar in history[-self.slow_window - 1 : -1])
            if previous_entry >= previous_slow:
                return None
        distance = _atr_distance(history, self.atr_window, self.atr_multiplier)
        return Signal("buy", distance) if distance and distance > 0 else None


@dataclass(frozen=True)
class BreakoutTrend:
    entry_window: int = 100
    trend_window: int = 200
    exit_window: int = 50
    atr_window: int = 20
    atr_multiplier: float = 3.0
    liquidity_window: int = 20
    min_dollar_volume: float = 500_000.0

    def __post_init__(self) -> None:
        if (
            min(
                self.entry_window,
                self.trend_window,
                self.exit_window,
                self.atr_window,
                self.liquidity_window,
            )
            < 1
            or self.atr_multiplier <= 0
            or self.min_dollar_volume < 0
        ):
            raise ValueError("Invalid trend strategy parameters")

    def trailing_distance(self, history: list[Bar]) -> float | None:
        return _atr_distance(history, self.atr_window, self.atr_multiplier)

    def signal(
        self, history: list[Bar], holding: bool, trailing_stop: float | None
    ) -> Signal | None:
        current = history[-1]
        if holding:
            if trailing_stop is not None and current.close < trailing_stop:
                return Signal("sell")
            if len(history) > self.exit_window and current.close < min(
                bar.low for bar in history[-self.exit_window - 1 : -1]
            ):
                return Signal("sell")
            return None
        required = max(
            self.entry_window + 1, self.trend_window, self.atr_window + 1, self.liquidity_window
        )
        if len(history) < required:
            return None
        if current.close <= max(bar.high for bar in history[-self.entry_window - 1 : -1]):
            return None
        if current.close <= mean(bar.close for bar in history[-self.trend_window :]):
            return None
        if mean(bar.close * bar.volume for bar in history[-self.liquidity_window :]) < (
            self.min_dollar_volume
        ):
            return None
        distance = self.trailing_distance(history)
        return Signal("buy", distance) if distance and distance > 0 else None
