import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from backtest.archive import (
    list_run_sets,
    load_bars_snapshot,
    load_manifest,
    load_result,
    save_run_set,
)
from backtest.charts import trading_chart
from backtest.data import _bar
from backtest.engine import Bar, Position, Result, Settings, Signal, Trade, run
from backtest.strategies import STRATEGIES
from backtest.trend import BreakoutTrend, MovingAverageTrend
from dashboards import backtest_dashboard
from streamlit.testing.v1 import AppTest

from asx_financials.domain.models import DailyPriceBar


def bars(closes: list[float], opens: list[float] | None = None) -> list[Bar]:
    return [
        Bar(
            "BHP",
            date(2020, 1, 1) + timedelta(days=index),
            (opens or closes)[index],
            max(close, (opens or closes)[index]) + 0.1,
            min(close, (opens or closes)[index]) - 0.1,
            close,
            100_000,
            "Materials",
        )
        for index, close in enumerate(closes)
    ]


def strategy() -> BreakoutTrend:
    return BreakoutTrend(
        entry_window=2,
        trend_window=3,
        exit_window=2,
        atr_window=2,
        atr_multiplier=3,
        liquidity_window=2,
        min_dollar_volume=0,
    )


def test_breakout_executes_at_next_open_and_marks_open_position() -> None:
    prices = bars([10, 10.1, 11, 12], [10, 10.1, 11, 14])
    result = run(prices, strategy(), Settings(commission=0, slippage_bps=0))
    assert result.open_positions["BHP"].entry_day == prices[3].day
    assert result.open_positions["BHP"].entry_price == 14
    assert result.trades == []
    assert result.equity[-1][1] == pytest.approx(100_000 - 2 * result.open_positions["BHP"].shares)
    assert result.total_return < 0


def test_exit_signal_waits_until_next_open_and_includes_costs() -> None:
    prices = bars(
        [10, 10.1, 11, 11.5, 9, 8],
        [10, 10.1, 11, 12, 9, 7],
    )
    result = run(prices, strategy())
    trade = result.trades[0]
    assert (trade.entry_day, trade.exit_day) == (prices[3].day, prices[5].day)
    assert trade.entry_price == pytest.approx(12 * 1.0005)
    assert trade.exit_price == pytest.approx(7 * 0.9995)
    assert trade.net_pnl == pytest.approx(
        trade.shares * (trade.exit_price - trade.entry_price) - 20
    )
    assert not result.open_positions


def test_warmup_and_terminal_orders_do_not_use_future_bars() -> None:
    prices = bars([10, 10.1, 11])
    result = run(prices, strategy(), start=prices[-1].day)
    assert result.equity == [(prices[-1].day, 100_000)]
    assert not result.open_positions
    assert not result.trades


def test_liquidity_and_prior_window_prevent_false_breakouts() -> None:
    illiquid = BreakoutTrend(
        entry_window=2,
        trend_window=3,
        exit_window=2,
        atr_window=2,
        liquidity_window=2,
        min_dollar_volume=2_000_000,
    )
    assert run(bars([10, 10.1, 11, 12]), illiquid).open_positions == {}
    assert strategy().signal(bars([10, 10.1, 10.05]), False, None) is None


def test_adjusted_ohlc_and_incomplete_data() -> None:
    day = date(2020, 1, 1)
    row = DailyPriceBar(
        day,
        Decimal("10"),
        Decimal("12"),
        Decimal("9"),
        Decimal("11"),
        Decimal("5.5"),
        100,
        "test",
        datetime.now(UTC),
    )
    adjusted = _bar("BHP", "Materials", row)
    assert (adjusted.open, adjusted.high, adjusted.close) == (5, 6, 5.5)
    with pytest.raises(ValueError, match="incomplete"):
        _bar(
            "BHP",
            "Materials",
            DailyPriceBar(
                day,
                row.open,
                row.high,
                row.low,
                row.close,
                None,
                row.volume,
                row.source,
                row.fetched_at_utc,
            ),
        )


def test_invalid_signal_and_duplicate_date_fail() -> None:
    class BadStrategy:
        def signal(self, history: list[Bar], holding: bool, trailing_stop: float | None) -> Signal:
            return Signal("buy", 0)

        def trailing_distance(self, history: list[Bar]) -> None:
            return None

    with pytest.raises(ValueError, match="stop distance"):
        run(bars([10]), BadStrategy())
    with pytest.raises(ValueError, match="Duplicate"):
        run(bars([10, 11]) * 2, strategy())


def test_shared_sector_cap_and_missing_session_defer_fills() -> None:
    first = bars([10, 10.1, 11, 12])
    second = [
        Bar("RIO", bar.day, bar.open, bar.high, bar.low, bar.close, bar.volume, "Materials")
        for bar in first
        if bar != first[-1]
    ]
    later = Bar("RIO", first[-1].day + timedelta(days=1), 14, 14.1, 11.9, 12, 100_000, "Materials")
    result = run(
        first + second + [later],
        strategy(),
        Settings(
            commission=0, slippage_bps=0, max_position_fraction=0.20, max_sector_fraction=0.25
        ),
    )
    assert result.open_positions["BHP"].entry_day == first[-1].day
    assert result.open_positions["RIO"].entry_day == later.day
    assert (
        sum(position.shares * position.entry_price for position in result.open_positions.values())
        <= 25_000
    )


def test_risk_budget_limits_shares_before_position_cap() -> None:
    class FixedRisk:
        def signal(
            self, history: list[Bar], holding: bool, trailing_stop: float | None
        ) -> Signal | None:
            return Signal("buy", 20) if len(history) == 1 else None

        def trailing_distance(self, history: list[Bar]) -> None:
            return None

    prices = [
        Bar("BHP", date(2020, 1, 1) + timedelta(days=index), 100, 101, 99, 100, 100_000)
        for index in range(2)
    ]
    result = run(prices, FixedRisk(), Settings(slippage_bps=0))
    assert result.open_positions["BHP"].shares == 25
    assert result.cash == 100_000 - 25 * 100 - 10


def test_chart_marks_completed_trades_and_open_positions_on_own_price_panels() -> None:
    bhp = bars([10, 11, 12])
    cba = [Bar("CBA", bar.day, 100, 101, 99, 100, bar.volume, "Banks") for bar in bhp]
    trade = Trade("BHP", bhp[0].day, bhp[2].day, 10, 10.1, 11.9, 18)
    open_position = Position(5, 100.1, cba[1].day, 95, 100, "Banks")
    result = Result(
        [(bar.day, 100_000 + index) for index, bar in enumerate(bhp)],
        [trade],
        {"CBA": open_position},
        99_500,
        100_000,
    )
    figure = trading_chart(bhp + cba, result)
    traces = {trace.name: trace for trace in figure.data}
    assert list(traces["BHP buy"].x) == [trade.entry_day]
    assert list(traces["BHP buy"].y) == [trade.entry_price]
    assert list(traces["BHP sell"].x) == [trade.exit_day]
    assert list(traces["CBA buy"].x) == [open_position.entry_day]
    assert list(traces["CBA sell"].x) == []
    assert traces["BHP buy"].yaxis == "y"
    assert traces["CBA buy"].yaxis == "y2"
    assert traces["Portfolio equity"].yaxis == "y3"
    assert list(traces["Portfolio equity"].y) == [100_000, 100_001, 100_002]


def test_chart_moving_averages_use_warmup_and_each_tickers_own_closes() -> None:
    bhp = bars([float(value) for value in range(1, 69)])
    cba = [
        Bar(
            "CBA",
            bar.day,
            bar.open * 10,
            bar.high * 10,
            bar.low * 10,
            bar.close * 10,
            bar.volume,
            "Banks",
        )
        for bar in bhp
    ]
    visible = bhp[-3:]
    result = Result([(bar.day, 100_000) for bar in visible], [], {}, 100_000, 100_000)
    figure = trading_chart(bhp + cba, result, moving_average_windows=(5, 20, 65))
    traces = {trace.name: trace for trace in figure.data}
    for window in (5, 20, 65):
        average = traces[f"BHP {window}-day MA"]
        assert list(average.x) == [bar.day for bar in visible]
        assert list(average.y) == pytest.approx(
            [
                sum(bar.close for bar in bhp[index - window + 1 : index + 1]) / window
                for index in range(65, 68)
            ]
        )
        assert average.yaxis == "y"
        assert list(traces[f"CBA {window}-day MA"].y) == pytest.approx(
            [value * 10 for value in average.y]
        )
        assert traces[f"CBA {window}-day MA"].yaxis == "y2"
    early = Result([(bar.day, 100_000) for bar in bhp[:3]], [], {}, 100_000, 100_000)
    short_chart = trading_chart(bhp[:3], early, moving_average_windows=(5, 20, 65))
    assert list(next(trace for trace in short_chart.data if trace.name == "BHP 65-day MA").y) == [
        None,
        None,
        None,
    ]


def test_moving_average_entry_exit_are_close_signals_with_next_open_fills() -> None:
    strategy = MovingAverageTrend(fast_window=2, entry_window=3, slow_window=4, atr_window=2)
    prices = bars([12, 12, 8, 8, 21, 22, 6, 6], [12, 12, 8, 8, 21, 23, 6, 5])
    assert strategy.signal(prices[:4], False, None) is None
    buy = strategy.signal(prices[:5], False, None)
    assert buy is not None and buy.side == "buy"
    assert buy.stop_distance == pytest.approx(19.95)
    assert strategy.signal(prices[:6], False, None) is None
    assert strategy.signal(prices[:6], True, None) is None
    assert strategy.signal(prices[:7], True, None) == Signal("sell")
    assert strategy.signal(prices[:8], True, None) is None
    result = run(prices, strategy, Settings(commission=0, slippage_bps=0))
    assert len(result.trades) == 1
    assert result.trades[0].entry_day == prices[5].day
    assert result.trades[0].entry_price == 23
    assert result.trades[0].exit_day == prices[7].day
    assert result.trades[0].exit_price == 5
    assert result.open_positions == {}


def test_moving_average_requires_strict_ordering_and_does_not_apply_atr_stop() -> None:
    strategy = MovingAverageTrend(fast_window=1, entry_window=2, slow_window=4, atr_window=2)
    assert strategy.signal(bars([10.0] * 4 + [12.0]), False, None) is None
    assert strategy.signal(bars([12, 12, 8, 8, 12, 18]), False, None) is None
    assert strategy.signal(bars([10.0] * 5), True, 100) is None
    assert strategy.trailing_distance(bars([10.0] * 5)) is None
    default = MovingAverageTrend()
    initial = bars([12.0] * 45 + [8.0] * 20)
    assert default.signal(initial, False, None) is None
    assert default.signal(bars([12.0] * 45 + [8.0] * 20 + [100.0]), False, None) is not None
    assert default.signal(bars([10.0] * 5), True, None) is None
    assert default.signal(bars([10.0] * 5 + [9.0]), True, None) == Signal("sell")
    assert default.signal(bars([10.0] * 5 + [9.0, 8.0]), True, None) == Signal("sell")
    with pytest.raises(ValueError, match="moving-average"):
        MovingAverageTrend(fast_window=20)


def test_prior_strategy_versions_preserve_state_and_crossover_exits() -> None:
    history = bars([12, 12, 8, 8, 21, 22, 6, 6])
    state = MovingAverageTrend(
        fast_window=2,
        entry_window=3,
        slow_window=4,
        atr_window=2,
        entry_mode="state",
        exit_mode="average_state",
    )
    crossover = MovingAverageTrend(
        fast_window=2, entry_window=3, slow_window=4, atr_window=2, exit_mode="average_cross"
    )
    assert state.signal(history[:6], False, None) is not None
    assert crossover.signal(history[:6], False, None) is None
    assert state.signal(history[:8], True, None) == Signal("sell")
    assert crossover.signal(history[:8], True, None) is None
    assert set(STRATEGIES) == {
        "breakout",
        "ma-state",
        "ma-cross-10",
        "ma-cross-5",
        "moving-average",
    }


def test_run_archive_retains_input_results_and_version_details(tmp_path: Path) -> None:
    prices = bars([12.0] * 45 + [8.0] * 20 + [100.0, 100.0])
    first = save_run_set(tmp_path, prices, prices[65].day, prices[-1].day, "yahoo")
    second = save_run_set(tmp_path, prices, prices[65].day, prices[-1].day, "yahoo")
    assert first != second
    assert list_run_sets(tmp_path) == sorted([first, second], reverse=True)
    assert load_bars_snapshot(first) == sorted(prices, key=lambda bar: (bar.day, bar.ticker))
    manifest = load_manifest(first)
    assert manifest["symbols"] == ["BHP"]
    assert manifest["requested_start"] == prices[65].day.isoformat()
    assert set(manifest["versions"]) == set(STRATEGIES)
    assert manifest["versions"]["moving-average"]["entry"] == STRATEGIES["moving-average"].entry
    result = load_result(first, "moving-average")
    assert result.open_positions["BHP"].entry_day == prices[66].day
    assert result.equity[-1][1] == pytest.approx(
        manifest["versions"]["moving-average"]["final_equity"]
    )


def test_dashboard_shows_price_and_strategy_averages_for_every_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prices = bars([10.0] * 201)
    save_run_set(tmp_path, prices, prices[0].day, prices[-1].day, "yahoo")
    monkeypatch.setattr(backtest_dashboard, "RECORDS", tmp_path)
    page = AppTest.from_string(
        "from dashboards import backtest_dashboard\nbacktest_dashboard.main()",
        default_timeout=60,
    ).run()
    assert not page.exception
    charts = page.get("plotly_chart")
    assert len(charts) == len(STRATEGIES)
    for chart, version in zip(charts, STRATEGIES.values(), strict=True):
        names = {trace["name"] for trace in json.loads(chart.proto.spec)["data"]}
        assert "BHP adjusted close" in names
        assert "Portfolio equity" in names
        assert {f"BHP {window}-day MA" for window in version.averages} <= names
