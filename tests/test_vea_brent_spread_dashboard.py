"""Out-of-sample VEA/Brent spread signals."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from dashboards.vea_brent_spread_dashboard import (
    Opportunity,
    SpreadPoint,
    aligned_closes,
    closing_prices_chart,
    find_opportunities,
    fit_model,
    fund_utilization,
    load_prices,
    pair_signals,
    price_chart,
    score_closes,
)


def test_load_prices_uses_matching_stored_sources_from_training_start() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.side_effect = [
        MagicMock(all=lambda: [(date(2018, 1, 2), Decimal("1.2"))]),
        MagicMock(all=lambda: [(date(2018, 1, 2), Decimal("60"))]),
    ]
    shares, oil = load_prices(engine)
    assert shares == [(date(2018, 1, 2), Decimal("1.2"))]
    assert oil == [(date(2018, 1, 2), Decimal("60"))]
    queries = [
        str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
        for call in connection.execute.call_args_list
    ]
    assert "daily_prices.exchange_code = 'ASX'" in queries[0]
    assert "daily_prices.ticker = 'VEA'" in queries[0]
    assert "daily_prices.trading_date >= '2018-01-01'" in queries[0]
    assert "commodity_prices.source = 'Yahoo Finance'" in queries[1]
    assert "commodity_prices.series = 'BZ=F'" in queries[1]
    assert "commodity_prices.frequency = 'daily'" in queries[1]


def test_training_model_does_not_leak_future_prices() -> None:
    shares = [
        (date(2018, 1, 2), Decimal("2")),
        (date(2023, 6, 30), Decimal("4")),
        (date(2023, 7, 3), Decimal("100")),
        (date(2023, 7, 4), Decimal("5")),
    ]
    oil = [
        (date(2018, 1, 2), Decimal("10")),
        (date(2023, 6, 30), Decimal("20")),
        (date(2023, 7, 3), Decimal("30")),
        (date(2023, 7, 4), Decimal("40")),
    ]
    pairs = aligned_closes(shares, oil)
    # Two exactly collinear training pairs cannot produce a Z-score.
    assert fit_model(pairs) is None
    shares.insert(1, (date(2020, 1, 2), Decimal("2")))
    oil.insert(1, (date(2020, 1, 2), Decimal("15")))
    pairs = aligned_closes(shares, oil)
    model = fit_model(pairs)
    assert model is not None
    assert model.observations == 3
    assert model.beta == pytest.approx(8 / 45)
    assert model.mean == pytest.approx(0, abs=1e-12)
    scored = score_closes(pairs, model)
    assert sum(point.spread for point in scored[:3]) == pytest.approx(0, abs=1e-12)
    assert min(point.spread for point in scored[:3]) < 0 < max(point.spread for point in scored[:3])
    assert scored[-2].z_score > 1.5
    assert fit_model(pairs[:-2]) == model


def test_ratio_of_means_is_not_ols_slope_and_guards_constant_spread() -> None:
    pairs = [
        (date(2018, 1, 2), 3, 10),
        (date(2018, 1, 3), 5, 20),
        (date(2023, 6, 30), 4, 30),
    ]
    model = fit_model(pairs)
    assert model is not None
    assert model.beta == pytest.approx(0.2)
    assert model.mean == pytest.approx(0, abs=1e-12)
    assert [point.spread for point in score_closes(pairs, model)] == pytest.approx([1, 1, -2])
    assert fit_model([(day, 0.2 * brent, brent) for day, _, brent in pairs]) is None


def test_only_matched_positive_finite_dates_are_scored() -> None:
    shares = [(date(2018, 1, 2), Decimal("1")), (date(2018, 1, 3), Decimal("NaN"))]
    oil = [(date(2018, 1, 2), Decimal("10")), (date(2018, 1, 3), Decimal("12"))]
    assert aligned_closes(shares, oil) == [(date(2018, 1, 2), 1.0, 10.0)]


def test_strict_long_only_signals_start_after_training_and_never_reverse_same_day() -> None:
    scores = [
        (date(2023, 6, 30), -2),
        (date(2023, 7, 3), -1.5),
        (date(2023, 7, 4), -2),
        (date(2023, 7, 5), -0.5),
        (date(2023, 7, 6), 2),
        (date(2023, 7, 7), 2),
        (date(2023, 7, 10), 0.5),
        (date(2023, 7, 11), 0),
        (date(2023, 7, 12), -2),
        (date(2023, 7, 13), 2),
    ]
    points = [SpreadPoint(day, 1, 10, 0, score) for day, score in scores]
    events, position = find_opportunities(points)
    assert [(event.day, event.action, event.direction) for event in events] == [
        (date(2023, 7, 4), "Entry", "Long VEA"),
        (date(2023, 7, 6), "Exit", "Long VEA"),
        (date(2023, 7, 12), "Entry", "Long VEA"),
        (date(2023, 7, 13), "Exit", "Long VEA"),
    ]
    assert position is None


def test_paired_long_signals_calculate_vea_only_results_and_calendar_days() -> None:
    points = [
        SpreadPoint(date(2023, 7, 3), 10, 20, 0, -2),
        SpreadPoint(date(2023, 7, 6), 13, 18, 0, 0),
        SpreadPoint(date(2023, 7, 7), 12, 19, 0, 2),
        SpreadPoint(date(2023, 7, 10), 9, 21, 0, -2),
        SpreadPoint(date(2023, 7, 13), 11, 22, 0, 0),
    ]
    events, position = find_opportunities(points)
    trades = pair_signals(points, events)
    assert position is None
    assert len(trades) == 2
    assert [trade.holding_days for trade in trades] == [3, 3]
    assert [trade.vea_pnl for trade in trades] == [3, 2]
    assert trades[0].vea_return_pct == pytest.approx(30)
    assert trades[1].vea_return_pct == pytest.approx(200 / 9)
    assert sum(trade.vea_pnl for trade in trades) == 5
    assert fund_utilization(trades, events, points) == pytest.approx(6 / 13 * 100)


def test_open_signal_is_unrealized_but_counts_through_last_matched_close() -> None:
    points = [
        SpreadPoint(date(2023, 7, 3), 10, 20, 0, -2),
        SpreadPoint(date(2023, 7, 4), 11, 20, 0, 0),
        SpreadPoint(date(2023, 7, 7), 9, 21, 0, -2),
        SpreadPoint(date(2023, 7, 10), 8, 22, 0, -1),
    ]
    events, position = find_opportunities(points)
    trades = pair_signals(points, events)
    assert position is not None
    assert len(trades) == 1
    assert trades[0].vea_pnl == 1
    assert fund_utilization(trades, events, points) == pytest.approx(50)
    assert fund_utilization([], [], points) == 0
    assert fund_utilization([], [], []) == 0


def test_first_chart_shows_matched_vea_and_brent_closes_on_separate_axes() -> None:
    points = [
        SpreadPoint(date(2023, 7, 3), 3, 20, 1, -2),
        SpreadPoint(date(2023, 7, 4), 5, 25, 3, 2),
    ]
    figure = closing_prices_chart(points)
    assert [trace.name for trace in figure.data] == [
        "VEA close (AUD/share)",
        "BZ=F close (USD/barrel)",
    ]
    assert (
        list(figure.data[0].x)
        == list(figure.data[1].x)
        == [
            date(2023, 7, 3),
            date(2023, 7, 4),
        ]
    )
    assert list(figure.data[0].y) == [3, 5]
    assert list(figure.data[1].y) == [20, 25]
    assert figure.data[0].yaxis == "y"
    assert figure.data[1].yaxis == "y2"
    assert figure.layout.yaxis.title.text == "VEA close (AUD/share)"
    assert figure.layout.yaxis2.title.text == "BZ=F close (USD/barrel)"


def test_spread_chart_shows_vea_close_and_spread_scores() -> None:
    points = [
        SpreadPoint(date(2023, 7, 3), 3, 20, 1, -2),
        SpreadPoint(date(2023, 7, 4), 5, 20, 3, 2),
    ]
    figure = price_chart(points, [])
    assert list(figure.data[0].y) == [3, 5]
    assert figure.data[0].name == "VEA close (AUD/share)"
    assert list(figure.data[1].y) == [-2, 2]
    assert [trace.name for trace in figure.data[2:]] == [
        "Buy VEA: Z < -1.5",
        "Sell VEA: Z > -0.5",
    ]


def test_histogram_shows_only_long_thresholds() -> None:
    from dashboards.vea_brent_spread_dashboard import SpreadModel, histogram

    points = [
        SpreadPoint(date(2018, 1, 2), 3, 20, -1, -1),
        SpreadPoint(date(2018, 1, 3), 5, 20, 1, 1),
    ]
    figure = histogram(points, SpreadModel(0.1, 0, 1, 2))
    assert len(figure.layout.shapes) == 3


def test_pairing_ignores_events_without_matched_close() -> None:
    points = [SpreadPoint(date(2023, 7, 3), 10, 20, 0, -2)]
    events = [
        Opportunity(date(2023, 7, 3), "Entry", "Long VEA", -2),
        Opportunity(date(2023, 7, 4), "Exit", "Long VEA", 0),
    ]
    assert pair_signals(points, events) == []
