"""Tests for the read-only VEA and Brent crude comparison."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from dashboards.vea_crude_dashboard import (
    Trade,
    _normalized_ratios,
    average_vea_oil_ratio,
    backtest_trades,
    comparison_chart,
    fund_utilization,
    load_prices,
    main,
    normalized_chart,
    ratio_histogram,
    trade_chart,
    trade_summary,
    vea_per_crude_chart,
)


def test_queries_target_vea_and_daily_brent() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.side_effect = [
        MagicMock(all=MagicMock(return_value=[(date(2026, 1, 2), Decimal("2.5"), Decimal("2.4"))])),
        MagicMock(all=MagicMock(return_value=[(date(2026, 1, 2), Decimal("65"))])),
    ]
    shares, oil, opens = load_prices(engine)
    assert len(shares) == len(oil) == len(opens) == 1
    assert opens[date(2026, 1, 2)] == Decimal("2.4")
    queries = [
        str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
        for call in connection.execute.call_args_list
    ]
    assert "FROM daily_prices" in queries[0]
    assert "daily_prices.exchange_code = 'ASX'" in queries[0]
    assert "daily_prices.ticker = 'VEA'" in queries[0]
    assert "daily_prices.close IS NOT NULL OR daily_prices.open IS NOT NULL" in queries[0]
    assert "ORDER BY daily_prices.trading_date" in queries[0]
    assert "FROM commodity_prices" in queries[1]
    assert "commodity_prices.source = 'FRED/EIA'" in queries[1]
    assert "commodity_prices.series = 'DCOILBRENTEU'" in queries[1]
    assert "commodity_prices.frequency = 'daily'" in queries[1]
    assert "ORDER BY commodity_prices.observation_date" in queries[1]


def test_comparison_chart_has_shared_dates_and_separate_units() -> None:
    figure = comparison_chart(
        [(date(2026, 1, 2), Decimal("2.5")), (date(2026, 1, 5), Decimal("2.6"))],
        [(date(2026, 1, 2), Decimal("65")), (date(2026, 1, 5), Decimal("66"))],
    )
    assert [trace.yaxis for trace in figure.data] == ["y", "y2"]
    assert list(figure.data[0].y) == [2.5, 2.6]
    assert list(figure.data[1].y) == [65.0, 66.0]
    assert list(figure.data[0].x) == list(figure.data[1].x)
    assert figure.layout.yaxis.title.text == "VEA close (AUD/share)"
    assert figure.layout.yaxis2.title.text == "Brent crude (USD/barrel)"


def test_normalization_uses_mean_of_same_date_ratios_not_ratio_of_means() -> None:
    shares = [
        (date(2026, 1, 2), Decimal("4")),
        (date(2026, 1, 5), Decimal("9")),
        (date(2026, 1, 6), Decimal("100")),
    ]
    oil = [
        (date(2026, 1, 1), Decimal("1000")),
        (date(2026, 1, 2), Decimal("2")),
        (date(2026, 1, 5), Decimal("3")),
    ]
    assert average_vea_oil_ratio(shares, oil) == (Decimal("2.5"), 2)
    figure = normalized_chart(shares, oil, Decimal("2.5"))
    assert [trace.yaxis for trace in figure.data] == [None, None]
    assert list(figure.data[0].y) == [4.0, 9.0, 100.0]
    assert list(figure.data[1].y) == [2500.0, 5.0, 7.5]
    assert list(figure.data[1].x) == [day for day, _ in oil]
    assert average_vea_oil_ratio(shares, [(date(2026, 1, 7), Decimal("2"))]) is None


def test_vea_per_crude_chart_min_max_scales_matched_prices_before_dividing() -> None:
    shares = [
        (date(2026, 1, 2), Decimal("2")),
        (date(2026, 1, 5), Decimal("4")),
        (date(2026, 1, 6), Decimal("6")),
        (date(2026, 1, 7), Decimal("20")),
    ]
    oil = [
        (date(2026, 1, 1), Decimal("1000")),
        (date(2026, 1, 2), Decimal("60")),
        (date(2026, 1, 5), Decimal("70")),
        (date(2026, 1, 6), Decimal("120")),
    ]
    figure = vea_per_crude_chart(shares, oil)
    assert [trace.yaxis for trace in figure.data] == ["y"] + ["y2"] * 6
    assert figure.data[0].name == "VEA.AX close (AUD/share)"
    assert figure.data[1].name == "Normalized VEA / normalized Brent"
    assert list(figure.data[0].x) == [day for day, _ in shares]
    assert list(figure.data[0].y) == [2.0, 4.0, 6.0, 20.0]
    assert list(figure.data[1].x) == [date(2026, 1, 5), date(2026, 1, 6)]
    assert list(figure.data[1].y) == [3.0, 1.0]
    assert list(figure.data[2].x) == [shares[0][0], shares[-1][0]]
    assert list(figure.data[2].y) == [2.0, 2.0]
    assert figure.data[2].line.dash == "dash"
    assert "Median" in figure.data[2].name
    for trace, expected, color in zip(
        figure.data[3:],
        [1.3173, 2.6827, 1.7, 2.3],
        ["orange", "orange", "green", "green"],
        strict=True,
    ):
        assert list(trace.x) == [shares[0][0], shares[-1][0]]
        assert list(trace.y) == [expected, expected]
        assert trace.line.color == color
        assert trace.line.dash == "dash"
    assert "15.865th percentile" in figure.data[3].name
    assert "84.135th percentile" in figure.data[4].name
    assert "35th percentile" in figure.data[5].name
    assert "65th percentile" in figure.data[6].name
    assert figure.layout.yaxis.title.text == "VEA close (AUD/share)"
    assert figure.layout.yaxis2.title.text == "Normalized VEA / normalized Brent (unitless)"
    assert figure.layout.yaxis2.range is None
    assert figure.data[0].xaxis == figure.data[1].xaxis == "x"
    assert figure.layout.yaxis2.overlaying == "y"
    assert figure.layout.yaxis2.side == "right"


def test_vea_per_crude_chart_autoscales_without_capping_ratio_values() -> None:
    shares = [
        (date(2026, 1, 2), Decimal("2")),
        (date(2026, 1, 3), Decimal("19")),
        (date(2026, 1, 4), Decimal("20")),
    ]
    oil = [
        (date(2026, 1, 2), Decimal("60")),
        (date(2026, 1, 3), Decimal("61")),
        (date(2026, 1, 4), Decimal("120")),
    ]
    figure = vea_per_crude_chart(shares, oil)
    assert list(figure.data[1].y) == [float(Decimal(170) / 3), 1.0]
    expected_median = float((Decimal(170) / 3 + Decimal(1)) / 2)
    assert list(figure.data[2].y) == [expected_median, expected_median]
    lower = float(Decimal(1) + (Decimal(170) / 3 - 1) * Decimal("0.15865"))
    upper = float(Decimal(1) + (Decimal(170) / 3 - 1) * Decimal("0.84135"))
    assert list(figure.data[3].y) == [lower, lower]
    assert list(figure.data[4].y) == [upper, upper]
    for trace, fraction in zip(figure.data[5:], [Decimal("0.35"), Decimal("0.65")], strict=True):
        expected = float(Decimal(1) + (Decimal(170) / 3 - 1) * fraction)
        assert list(trace.y) == [expected, expected]
    assert figure.layout.yaxis2.range is None


def test_vea_per_crude_chart_skips_constant_series_and_unmatched_dates() -> None:
    shares = [(date(2026, 1, 2), Decimal("2")), (date(2026, 1, 3), Decimal("4"))]
    oil = [(date(2026, 1, 2), Decimal("60")), (date(2026, 1, 3), Decimal("60"))]
    assert list(vea_per_crude_chart(shares, oil).data[1].x) == []
    assert len(vea_per_crude_chart(shares, oil).data) == 2
    assert list(vea_per_crude_chart(shares, []).data[1].x) == []
    varied_oil = [(date(2026, 1, 2), Decimal("60")), (date(2026, 1, 3), Decimal("70"))]
    figure = vea_per_crude_chart(shares, varied_oil)
    assert list(figure.data[1].x) == [date(2026, 1, 3)]
    assert list(figure.data[1].y) == [1.0]
    assert list(figure.data[2].y) == [1.0, 1.0]
    assert list(figure.data[3].y) == [1.0, 1.0]
    assert list(figure.data[4].y) == [1.0, 1.0]
    assert list(figure.data[5].y) == [1.0, 1.0]
    assert list(figure.data[6].y) == [1.0, 1.0]


def test_vea_per_crude_chart_median_uses_all_valid_ratio_points() -> None:
    shares = [
        (date(2026, 1, 2), Decimal("2")),
        (date(2026, 1, 3), Decimal("4")),
        (date(2026, 1, 4), Decimal("6")),
        (date(2026, 1, 5), Decimal("8")),
    ]
    oil = [
        (date(2026, 1, 2), Decimal("60")),
        (date(2026, 1, 3), Decimal("80")),
        (date(2026, 1, 4), Decimal("90")),
        (date(2026, 1, 5), Decimal("120")),
    ]
    figure = vea_per_crude_chart(shares, oil)
    assert list(figure.data[1].y) == [1.0, 4 / 3, 1.0]
    assert list(figure.data[2].y) == [1.0, 1.0]
    assert list(figure.data[3].y) == [1.0, 1.0]
    expected_upper = float(Decimal(1) + (Decimal(4) / 3 - 1) * Decimal("0.68270"))
    assert list(figure.data[4].y) == [expected_upper, expected_upper]
    assert list(figure.data[5].y) == [1.0, 1.0]
    expected_65th = float(Decimal(1) + (Decimal(4) / 3 - 1) * Decimal("0.3"))
    assert list(figure.data[6].y) == [expected_65th, expected_65th]


def test_ratio_histogram_uses_twenty_buckets_and_all_valid_ratios() -> None:
    shares = [
        (date(2026, 1, 2), Decimal("2")),
        (date(2026, 1, 3), Decimal("4")),
        (date(2026, 1, 4), Decimal("6")),
        (date(2026, 1, 5), Decimal("8")),
    ]
    oil = [
        (date(2026, 1, 1), Decimal("1000")),
        (date(2026, 1, 2), Decimal("60")),
        (date(2026, 1, 3), Decimal("80")),
        (date(2026, 1, 4), Decimal("90")),
        (date(2026, 1, 5), Decimal("120")),
    ]
    ratios = list(vea_per_crude_chart(shares, oil).data[1].y)
    figure = ratio_histogram(_normalized_ratios(shares, oil))
    histogram = figure.data[0]
    assert list(histogram.x) == ratios == [1.0, 4 / 3, 1.0]
    assert histogram.xbins.start < min(ratios)
    assert histogram.xbins.end > max(ratios)
    assert (histogram.xbins.end - histogram.xbins.start) / histogram.xbins.size == 20
    assert len(figure.layout.shapes) == 5
    high_orange = float(Decimal(1) + (Decimal(4) / 3 - 1) * Decimal("0.68270"))
    high_green = float(Decimal(1) + (Decimal(4) / 3 - 1) * Decimal("0.3"))
    for shape, expected, color in zip(
        figure.layout.shapes,
        [1.0, 1.0, high_orange, 1.0, high_green],
        ["red", "orange", "orange", "green", "green"],
        strict=True,
    ):
        assert shape.x0 == shape.x1 == expected
        assert shape.line.color == color
        assert shape.line.dash == "dash"
    assert figure.layout.xaxis.title.text == "Normalized VEA / normalized Brent (unitless)"
    assert figure.layout.yaxis.title.text == "Number of trading dates"


def test_ratio_histogram_handles_empty_and_constant_ratios() -> None:
    empty = ratio_histogram([])
    assert list(empty.data[0].x) == []
    assert len(empty.layout.shapes) == 0
    figure = ratio_histogram([(date(2026, 1, 3), Decimal("1"))])
    assert list(figure.data[0].x) == [1.0]
    assert figure.data[0].xbins.start == 0.5
    assert figure.data[0].xbins.end == 1.5
    assert figure.data[0].xbins.size == 0.05
    assert [shape.x0 for shape in figure.layout.shapes] == [1.0] * 5
    assert [shape.line.color for shape in figure.layout.shapes] == [
        "red",
        "orange",
        "orange",
        "green",
        "green",
    ]


def test_load_prices_keeps_open_only_rows_for_execution() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.side_effect = [
        MagicMock(
            all=MagicMock(
                return_value=[
                    (date(2026, 1, 2), Decimal("2.5"), Decimal("2.4")),
                    (date(2026, 1, 5), None, Decimal("2.7")),
                ]
            )
        ),
        MagicMock(all=MagicMock(return_value=[])),
    ]
    shares, oil, opens = load_prices(engine)
    assert shares == [(date(2026, 1, 2), Decimal("2.5"))]
    assert oil == []
    assert opens == {date(2026, 1, 2): Decimal("2.4"), date(2026, 1, 5): Decimal("2.7")}


def test_fund_utilization_counts_calendar_days_and_open_positions() -> None:
    start, end = date(2026, 1, 1), date(2026, 1, 10)
    trades = [
        Trade(date(2026, 1, 2), Decimal(10), date(2026, 1, 5), Decimal(11)),
        Trade(date(2026, 1, 7), Decimal(11), date(2026, 1, 9), Decimal(12)),
    ]
    assert fund_utilization(trades, None, start, end) == Decimal(50)
    assert fund_utilization(trades, date(2026, 1, 10), start, end) == Decimal(60)
    assert fund_utilization([], date(2026, 1, 1), start, end) == Decimal(100)
    assert fund_utilization([], None, start, end) == Decimal(0)
    assert fund_utilization([], None, end, start) == Decimal(0)
    assert fund_utilization(
        [Trade(date(2025, 12, 31), Decimal(10), date(2026, 1, 4), Decimal(12))],
        None,
        start,
        end,
    ) == Decimal(30)


def test_trade_chart_marks_execution_dates_with_vertical_lines() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7)]
    shares = [(day, Decimal(price)) for day, price in zip(days[:3], (2, 3, 4), strict=True)]
    trade = Trade(days[0], Decimal("2.1"), days[1], Decimal("3.1"))
    figure = trade_chart(shares, [trade], days[3])
    assert list(figure.data[0].x) == days[:3]
    assert list(figure.data[0].y) == [2.0, 3.0, 4.0]
    assert figure.data[0].name == "VEA.AX close (AUD/share)"
    assert [
        (shape.x0, shape.x1, shape.y0, shape.y1, shape.yref, shape.line.color)
        for shape in figure.layout.shapes
    ] == [
        (day.isoformat(), day.isoformat(), 0, 1, "y domain", color)
        for day, color in zip((days[0], days[1], days[3]), ("red", "green", "red"), strict=True)
    ]
    assert figure.layout.yaxis.title.text == "VEA close (AUD/share)"
    assert len(trade_chart(shares, [], None).layout.shapes) == 0


def test_backtest_uses_next_trading_day_even_without_a_ratio_or_close() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7, 8)]
    shares = [(day, Decimal("10")) for day in (days[0], days[2], days[4])]
    opens = {day: Decimal(price) for day, price in zip(days, (8, 10, 11, 12, 14), strict=True)}
    ratios = [(days[0], Decimal(0)), (days[2], Decimal(2)), (days[4], Decimal(4))]
    trades, open_position = backtest_trades(shares, opens, ratios)
    assert [(trade.buy_date, trade.sell_date) for trade in trades] == [(days[1], days[3])]
    assert [(trade.buy_price, trade.sell_price) for trade in trades] == [(Decimal(10), Decimal(12))]
    assert open_position is None


def test_backtest_does_not_reenter_on_a_failed_buy_execution() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7)]
    shares = [(day, Decimal(10)) for day in days]
    ratios = list(zip(days, map(Decimal, [0, 0, 2, 4]), strict=True))
    opens = {days[2]: Decimal(10), days[3]: Decimal(11)}
    assert backtest_trades(shares, opens, ratios) == ([], None)


def test_backtest_threshold_equality_does_not_trigger_trade() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9)]
    shares = [(day, Decimal(10)) for day in days]
    ratios = list(zip(days, map(Decimal, [0, 0, 0, 4, 4, 4]), strict=True))
    opens = {day: Decimal(10) for day in days}
    assert backtest_trades(shares, opens, ratios) == ([], None)


def test_backtest_executes_next_trading_open_and_compounds_completed_trades() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9, 12, 13, 14)]
    shares = list(zip(days, [Decimal("10")] * len(days), strict=True))
    opens = dict(zip(days, map(Decimal, [10, 10, 12, 13, 9, 10, 8, 7, 10]), strict=True))
    ratios = list(zip(days, map(Decimal, [0, 2, 4, 4, 0, 2, 4, 4, 2]), strict=True))
    trades, open_position = backtest_trades(shares, opens, ratios)
    assert [(trade.buy_date, trade.sell_date) for trade in trades] == [
        (days[1], days[3]),
        (days[5], days[7]),
    ]
    assert [(trade.buy_price, trade.sell_price) for trade in trades] == [
        (Decimal(10), Decimal(13)),
        (Decimal(10), Decimal(7)),
    ]
    assert [trade.return_pct for trade in trades] == [Decimal(30), Decimal(-30)]
    assert [trade.holding_days for trade in trades] == [2, 4]
    assert trade_summary(trades) == (Decimal(50), Decimal(-9))
    assert open_position is None


def test_backtest_skips_unavailable_opens_and_reports_open_position() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9)]
    shares = [(day, Decimal(10)) for day in days]
    ratios = list(zip(days, map(Decimal, [1, 2, 4, 4, 0, 4]), strict=True))
    opens = {days[4]: Decimal(9), days[5]: Decimal(11)}
    trades, open_position = backtest_trades(shares, opens, ratios)
    assert trades == []
    assert open_position == days[5]
    assert backtest_trades(shares, {days[1]: Decimal(10)}, ratios) == ([], None)
    assert backtest_trades(shares, opens, []) == ([], None)
    assert trade_summary([]) == (Decimal(0), Decimal(0))


def test_staged_strategy_sells_on_upper_breakout_or_lower_reversal() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9, 12, 13, 14, 15)]
    shares = [(day, Decimal(10)) for day in days]
    opens = dict(zip(days, map(Decimal, [10, 11, 12, 13, 14, 15, 16, 17, 18, 19]), strict=True))
    ratios = list(zip(days, map(Decimal, [0, 20, 40, 40, 80, 100, 0, 40, 20, 100]), strict=True))
    trades, open_buy_date = backtest_trades(shares, opens, ratios, staged_sell=True)
    assert [(trade.buy_date, trade.sell_date) for trade in trades] == [
        (days[1], days[5]),
        (days[7], days[9]),
    ]
    assert [(trade.buy_price, trade.sell_price) for trade in trades] == [
        (Decimal(11), Decimal(15)),
        (Decimal(17), Decimal(19)),
    ]
    assert open_buy_date is None
    assert backtest_trades(shares, opens, ratios)[0][0].sell_date == days[3]


def test_staged_strategy_waits_for_second_observation_and_keeps_open_position() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7, 8)]
    shares = [(day, Decimal(10)) for day in days]
    opens = {day: Decimal(10) for day in days}
    ratios = list(zip(days, map(Decimal, [0, 10, 40, 10, 10]), strict=True))
    trades, open_buy_date = backtest_trades(shares, opens, ratios, staged_sell=True)
    assert trades == []
    assert open_buy_date == days[1]


def test_confirmed_strategy_requires_two_days_for_each_stage() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9, 12, 13, 14, 15)]
    shares = [(day, Decimal(10)) for day in days]
    opens = {day: Decimal(10 + index) for index, day in enumerate(days)}
    for values, expected_sell in (
        ([0, 0, 20, 40, 40, 40, 80, 80, 20, 100], days[8]),
        ([0, 0, 10, 40, 40, 5, 5, 80, 100, 100], days[7]),
    ):
        ratios = list(zip(days, map(Decimal, values), strict=True))
        trades, open_buy_date = backtest_trades(
            shares, opens, ratios, staged_sell=True, confirm_two_days=True
        )
        assert [(trade.buy_date, trade.sell_date) for trade in trades] == [(days[2], expected_sell)]
        assert trades[0].buy_price == opens[days[2]]
        assert trades[0].sell_price == opens[expected_sell]
        assert open_buy_date is None


def test_confirmed_strategy_requires_consecutive_trading_day_ratios() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19)]
    shares = [(day, Decimal(10)) for day in days]
    opens = {day: Decimal(10) for day in days}
    values: list[int | None] = [0, None, 0, 20, 40, None, 40, 50, 80, None, 80, 20]
    ratios = [
        (day, Decimal(value)) for day, value in zip(days, values, strict=True) if value is not None
    ]
    assert backtest_trades(shares, opens, ratios, staged_sell=True, confirm_two_days=True) == (
        [],
        None,
    )
    values = [20, 0, 0, 20, 40, 40, 30, 80, None, 80, 20, 100]
    ratios = [
        (day, Decimal(value)) for day, value in zip(days, values, strict=True) if value is not None
    ]
    assert backtest_trades(shares, opens, ratios, staged_sell=True, confirm_two_days=True) == (
        [],
        days[3],
    )


def test_confirmed_strategy_open_only_trading_day_breaks_confirmation() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7, 8)]
    shares = [(day, Decimal(10)) for day in (days[0], days[2], days[3], days[4])]
    opens = {day: Decimal(10) for day in days}
    ratios = list(
        zip((days[0], days[2], days[3], days[4]), map(Decimal, [0, 0, 20, 40]), strict=True)
    )
    assert backtest_trades(shares, opens, ratios, staged_sell=True, confirm_two_days=True) == (
        [],
        None,
    )


def test_confirmed_strategy_does_not_reuse_stage_observations() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9, 12, 13)]
    shares = [(day, Decimal(10)) for day in days]
    opens = {day: Decimal(10) for day in days}
    ratios = list(zip(days, map(Decimal, [0, 0, 80, 80, 80, 20, 100, 50]), strict=True))
    trades, open_buy_date = backtest_trades(
        shares, opens, ratios, staged_sell=True, confirm_two_days=True
    )
    assert trades == []
    assert open_buy_date == days[2]


def test_confirmed_strategy_skips_missing_execution_open_and_strict_equality() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9)]
    shares = [(day, Decimal(10)) for day in days]
    ratios = list(zip(days, map(Decimal, [0, 0, 0, 4, 4, 4]), strict=True))
    opens = {day: Decimal(10) for day in days}
    assert backtest_trades(shares, opens, ratios, staged_sell=True, confirm_two_days=True) == (
        [],
        None,
    )
    ratios = list(zip(days, map(Decimal, [0, 0, 10, 40, 40, 80]), strict=True))
    opens.pop(days[2])
    assert backtest_trades(shares, opens, ratios, staged_sell=True, confirm_two_days=True) == (
        [],
        None,
    )


def test_main_limits_oil_history_to_vea_date_range() -> None:
    shares = [(date(2026, 1, 2), Decimal("2.5")), (date(2026, 1, 5), Decimal("2.6"))]
    oil = [
        (date(2026, 1, 1), Decimal("64")),
        (date(2026, 1, 2), Decimal("65")),
        (date(2026, 1, 6), Decimal("67")),
    ]
    with (
        patch("dashboards.vea_crude_dashboard.st.set_page_config"),
        patch("dashboards.vea_crude_dashboard.st.title"),
        patch("dashboards.vea_crude_dashboard.st.caption") as caption,
        patch("dashboards.vea_crude_dashboard.st.subheader"),
        patch("dashboards.vea_crude_dashboard.st.plotly_chart") as plot,
        patch("dashboards.vea_crude_dashboard._engine"),
        patch("dashboards.vea_crude_dashboard.load_prices", return_value=(shares, oil, {})),
    ):
        main()
    assert plot.call_count == 7
    original, normalized, divided, histogram, strategy, staged_strategy, confirmed_strategy = [
        call.args[0] for call in plot.call_args_list
    ]
    assert list(original.data[1].x) == [date(2026, 1, 2)]
    assert list(normalized.data[1].x) == [date(2026, 1, 2)]
    assert list(normalized.data[1].y) == [2.5]
    assert list(divided.data[1].x) == []
    assert list(divided.data[1].y) == []
    assert list(histogram.data[0].x) == []
    assert list(strategy.data[0].x) == [day for day, _ in shares]
    assert len(strategy.layout.shapes) == 0
    assert list(staged_strategy.data[0].x) == [day for day, _ in shares]
    assert len(staged_strategy.layout.shapes) == 0
    assert list(confirmed_strategy.data[0].x) == [day for day, _ in shares]
    assert len(confirmed_strategy.layout.shapes) == 0
    assert any("1 matching trading dates" in call.args[0] for call in caption.call_args_list)
    assert all(call.kwargs["width"] == "stretch" for call in plot.call_args_list)


def test_main_displays_completed_trades_and_summary() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6)]
    shares = [(day, Decimal(price)) for day, price in zip(days, (2, 3, 4), strict=True)]
    oil = [(day, Decimal(price)) for day, price in zip(days, (60, 70, 80), strict=True)]
    trade = Trade(days[0], Decimal(10), days[2], Decimal(12))
    with (
        patch("dashboards.vea_crude_dashboard.st.set_page_config"),
        patch("dashboards.vea_crude_dashboard.st.title"),
        patch("dashboards.vea_crude_dashboard.st.caption"),
        patch("dashboards.vea_crude_dashboard.st.subheader"),
        patch("dashboards.vea_crude_dashboard.st.plotly_chart") as plot,
        patch("dashboards.vea_crude_dashboard.st.dataframe") as dataframe,
        patch("dashboards.vea_crude_dashboard.st.write") as write,
        patch("dashboards.vea_crude_dashboard._engine"),
        patch("dashboards.vea_crude_dashboard.load_prices", return_value=(shares, oil, {})),
        patch(
            "dashboards.vea_crude_dashboard.backtest_trades", return_value=([trade], None)
        ) as backtest,
    ):
        main()
    assert backtest.call_count == 3
    assert backtest.call_args_list[1].kwargs == {"staged_sell": True}
    assert backtest.call_args_list[2].kwargs == {"staged_sell": True, "confirm_two_days": True}
    assert [shape.line.color for shape in plot.call_args.args[0].layout.shapes] == ["red", "green"]
    assert dataframe.call_count == 3
    assert dataframe.call_args.args[0] == [
        {
            "Buy date": days[0],
            "Buy price (AUD)": 10.0,
            "Sell date": days[2],
            "Sell price (AUD)": 12.0,
            "Profit/loss (%)": 20.0,
            "Holding days": 4,
        }
    ]
    assert "Win rate: 100.00%" in write.call_args.args[0]
    assert "Total profit/loss (compounded): +20.00%" in write.call_args.args[0]
    assert write.call_count == 3
    assert all("Fund utilization: 80.00%" in call.args[0] for call in write.call_args_list)


def test_main_counts_open_position_in_fund_utilization() -> None:
    days = [date(2026, 1, day) for day in (2, 5, 6)]
    shares = [(day, Decimal(price)) for day, price in zip(days, (2, 3, 4), strict=True)]
    oil = [(day, Decimal(price)) for day, price in zip(days, (60, 70, 80), strict=True)]
    with (
        patch("dashboards.vea_crude_dashboard.st.set_page_config"),
        patch("dashboards.vea_crude_dashboard.st.title"),
        patch("dashboards.vea_crude_dashboard.st.caption"),
        patch("dashboards.vea_crude_dashboard.st.subheader"),
        patch("dashboards.vea_crude_dashboard.st.plotly_chart"),
        patch("dashboards.vea_crude_dashboard.st.info"),
        patch("dashboards.vea_crude_dashboard.st.write") as write,
        patch("dashboards.vea_crude_dashboard._engine"),
        patch("dashboards.vea_crude_dashboard.load_prices", return_value=(shares, oil, {})),
        patch("dashboards.vea_crude_dashboard.backtest_trades", return_value=([], days[1])),
    ):
        main()
    assert write.call_count == 3
    assert all("Fund utilization: 40.00%" in call.args[0] for call in write.call_args_list)


def test_main_handles_missing_series() -> None:
    with (
        patch("dashboards.vea_crude_dashboard.st.set_page_config"),
        patch("dashboards.vea_crude_dashboard.st.title"),
        patch("dashboards.vea_crude_dashboard.st.caption"),
        patch("dashboards.vea_crude_dashboard.st.warning") as warning,
        patch("dashboards.vea_crude_dashboard.st.plotly_chart") as plot,
        patch("dashboards.vea_crude_dashboard._engine"),
        patch("dashboards.vea_crude_dashboard.load_prices", return_value=([], [], {})),
    ):
        main()
    assert warning.call_count == 2
    plot.assert_not_called()
