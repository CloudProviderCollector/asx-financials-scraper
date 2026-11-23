"""Named strategy versions retained for comparable backtests."""

from dataclasses import dataclass

from backtest.trend import BreakoutTrend, MovingAverageTrend


@dataclass(frozen=True)
class StrategyVersion:
    title: str
    entry: str
    exit: str
    strategy: BreakoutTrend | MovingAverageTrend
    averages: tuple[int, ...] = ()


STRATEGIES: dict[str, StrategyVersion] = {
    "breakout": StrategyVersion(
        "Breakout trend",
        "Close above prior 100-session highs and 200-session average; minimum "
        "AUD 500,000 20-session average daily traded value.",
        "Close below prior 50-session lows or 3 x 20-session ATR trailing stop.",
        BreakoutTrend(),
        (200,),
    ),
    "ma-state": StrategyVersion(
        "MA state, 5/20 exit",
        "20-session MA above 65-session MA while flat (no crossing required).",
        "5-session MA below 20-session MA while holding (no crossing required).",
        MovingAverageTrend(entry_mode="state", exit_mode="average_state"),
        (5, 20, 65),
    ),
    "ma-cross-10": StrategyVersion(
        "MA crossover, 10/20 exit",
        "20-session MA crosses from strictly below to strictly above 65-session MA.",
        "10-session MA crosses from strictly above to strictly below 20-session MA.",
        MovingAverageTrend(fast_window=10, exit_mode="average_cross"),
        (10, 20, 65),
    ),
    "ma-cross-5": StrategyVersion(
        "MA crossover, 5/20 exit",
        "20-session MA crosses from strictly below to strictly above 65-session MA.",
        "5-session MA crosses from strictly above to strictly below 20-session MA.",
        MovingAverageTrend(exit_mode="average_cross"),
        (5, 20, 65),
    ),
    "moving-average": StrategyVersion(
        "MA crossover, price/5 exit (current)",
        "20-session MA crosses from strictly below to strictly above 65-session MA.",
        "Close below 5-session MA while holding (no exit crossing required).",
        MovingAverageTrend(),
        (5, 20, 65),
    ),
}
