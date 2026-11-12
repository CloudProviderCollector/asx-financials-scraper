"""VEA/Brent linear regression dashboard tests."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from dashboards.vea_brent_regression_dashboard import (
    TRAIN_END,
    TRAIN_START,
    aligned_closes,
    fit_regression,
    load_prices,
    main,
    price_chart,
    regression_chart,
    residual_histogram,
    score_residuals,
    trade_chart,
)


def test_load_prices_reads_complete_stored_vea_and_daily_brent_history() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.side_effect = [
        MagicMock(all=lambda: [(date(2017, 1, 2), Decimal("1.2"))]),
        MagicMock(all=lambda: [(date(2017, 1, 2), Decimal("60"))]),
    ]
    assert load_prices(engine) == (
        [(date(2017, 1, 2), Decimal("1.2"))],
        [(date(2017, 1, 2), Decimal("60"))],
    )
    queries = [
        str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
        for call in connection.execute.call_args_list
    ]
    assert "daily_prices.exchange_code = 'ASX'" in queries[0]
    assert "daily_prices.ticker = 'VEA'" in queries[0]
    assert "daily_prices.trading_date >=" not in queries[0]
    assert "commodity_prices.source = 'Yahoo Finance'" in queries[1]
    assert "commodity_prices.series = 'BZ=F'" in queries[1]
    assert "commodity_prices.frequency = 'daily'" in queries[1]
    assert "commodity_prices.observation_date >=" not in queries[1]


def test_aligned_closes_excludes_unmatched_nonpositive_and_nonfinite_data() -> None:
    shares = [
        (date(2018, 7, 2), Decimal("2")),
        (date(2018, 7, 3), Decimal("NaN")),
        (date(2018, 7, 4), Decimal("0")),
        (date(2018, 7, 5), Decimal("4")),
    ]
    oil = [
        (date(2018, 7, 2), Decimal("10")),
        (date(2018, 7, 3), Decimal("20")),
        (date(2018, 7, 4), Decimal("30")),
        (date(2018, 7, 6), Decimal("40")),
    ]
    assert aligned_closes(shares, oil) == [(date(2018, 7, 2), 2.0, 10.0)]


def test_period_and_all_history_use_distinct_ols_fits_with_intercepts() -> None:
    pairs = [
        (date(2018, 6, 29), 100.0, 5.0),
        (TRAIN_START, 3.0, 10.0),
        (TRAIN_END, 5.0, 20.0),
        (date(2023, 7, 1), 9.0, 30.0),
    ]
    period = fit_regression(pairs, TRAIN_START, TRAIN_END)
    all_history = fit_regression(pairs)
    assert period is not None and all_history is not None
    assert (period.a, period.b, period.observations) == pytest.approx((0.2, 1, 2))
    assert all_history.observations == 4
    assert all_history.a != pytest.approx(period.a)
    chart = regression_chart(pairs, period)
    assert [trace.name for trace in chart.data] == ["VEA close", "Estimated VEA"]
    assert list(chart.data[0].x) == list(chart.data[1].x) == [day for day, _, _ in pairs]
    assert list(chart.data[0].y) == [100, 3, 5, 9]
    assert list(chart.data[1].y) == pytest.approx([2, 3, 5, 7])
    assert chart.data[0].yaxis == chart.data[1].yaxis


@pytest.mark.parametrize(
    ("latest", "year_start"),
    [
        (date(2024, 3, 31), date(2023, 3, 31)),
        (date(2024, 2, 29), date(2023, 2, 28)),
    ],
)
def test_price_chart_shows_matched_vea_and_brent_on_separate_axes(
    latest: date, year_start: date
) -> None:
    pairs = [(date(2023, 2, 1), 3.0, 10.0), (latest, 5.0, 20.0)]
    chart = price_chart(pairs)
    assert [trace.name for trace in chart.data] == ["VEA close", "Brent close"]
    assert [list(trace.x) for trace in chart.data] == [[day for day, _, _ in pairs]] * 2
    assert list(chart.data[0].y) == [3.0, 5.0]
    assert list(chart.data[1].y) == [10.0, 20.0]
    assert (chart.data[0].yaxis, chart.data[1].yaxis) == ("y", "y2")
    assert chart.layout.yaxis.title.text == "VEA close (AUD/share)"
    assert chart.layout.yaxis2.title.text == "Brent close (USD/barrel)"
    assert chart.layout.yaxis2.side == "right"
    assert list(chart.layout.xaxis.range) == [year_start, latest]
    selector = chart.layout.xaxis.rangeselector
    assert [
        (button.label, button.count, button.step, button.stepmode) for button in selector.buttons
    ] == [
        ("1d", 1, "day", "backward"),
        ("5d", 5, "day", "backward"),
        ("1m", 1, "month", "backward"),
        ("3m", 3, "month", "backward"),
        ("1y", 1, "year", "backward"),
        ("5y", 5, "year", "backward"),
        ("max", None, "all", None),
    ]


def test_regression_rejects_insufficient_or_constant_brent() -> None:
    assert fit_regression([]) is None
    assert fit_regression([(TRAIN_START, 3, 10)]) is None
    assert fit_regression([(TRAIN_START, 3, 10), (TRAIN_END, 4, 10)]) is None
    assert fit_regression([(TRAIN_START, 3, 10), (TRAIN_END, 4, float("nan"))]) is None


def test_histograms_use_fitting_dates_and_mark_mean_and_both_sigma_bands() -> None:
    pairs = [
        (TRAIN_START, 3.0, 10.0),
        (date(2019, 1, 2), 5.0, 20.0),
        (TRAIN_END, 4.0, 30.0),
        (date(2023, 7, 1), 100.0, 40.0),
    ]
    model = fit_regression(pairs, TRAIN_START, TRAIN_END)
    assert model is not None
    period = residual_histogram(pairs, model, TRAIN_START, TRAIN_END)
    all_history = residual_histogram(pairs, model)
    assert list(period.data[0].x) == pytest.approx([-0.5, 1.0, -0.5])
    assert list(all_history.data[0].x) == pytest.approx([-0.5, 1.0, -0.5, 95.0])
    for figure in (period, all_history):
        bins = figure.data[0].xbins
        assert (bins.end - bins.start) / bins.size == pytest.approx(20)
    assert period.data[0].xbins.start == pytest.approx(-0.5)
    assert period.data[0].xbins.end == pytest.approx(1.0)
    sigma = (0.25 / 3 + 1 / 3 + 0.25 / 3) ** 0.5
    assert [shape.x0 for shape in period.layout.shapes] == pytest.approx(
        [0, -0.5 * sigma, 0.5 * sigma, -1.5 * sigma, 1.5 * sigma]
    )
    assert len(all_history.layout.shapes) == 5
    for figure in (period, all_history):
        assert [annotation.text for annotation in figure.layout.annotations] == [
            f"{label}: {shape.x0:+.4f}"
            for label, shape in zip(
                ("Mean", "-0.5 sigma", "+0.5 sigma", "-1.5 sigma", "+1.5 sigma"),
                figure.layout.shapes,
                strict=True,
            )
        ]
    assert period.layout.xaxis.title.text == "VEA - estimated VEA (AUD/share)"
    assert period.layout.yaxis.title.text == "Matched trading dates"


def test_trading_scores_use_only_training_residuals_and_fixed_coefficients() -> None:
    pairs = [
        (date(2018, 6, 29), 1.0, 10.0),
        (TRAIN_START, 3.0, 10.0),
        (date(2019, 1, 2), 5.0, 20.0),
        (TRAIN_END, 4.0, 30.0),
        (date(2023, 7, 3), 2.0, 40.0),
        (date(2023, 7, 4), 8.0, 40.0),
    ]
    model = fit_regression(pairs, TRAIN_START, TRAIN_END)
    assert model is not None
    scored = score_residuals(pairs, model)
    assert scored is not None
    points, sigma = scored
    assert sigma == pytest.approx(((0.5**2 + 1**2 + 0.5**2) / 3) ** 0.5)
    assert [point.spread for point in points] == pytest.approx([-2.5, -0.5, 1, -0.5, -3, 3])
    assert [point.z_score for point in points] == pytest.approx(
        [point.spread / sigma for point in points]
    )
    earlier = score_residuals(pairs[:4], model)
    assert earlier is not None and earlier[1] == pytest.approx(sigma)
    exact_pairs = [(TRAIN_START, 3, 10), (TRAIN_END, 5, 20)]
    exact_model = fit_regression(exact_pairs)
    assert exact_model is not None
    assert score_residuals(exact_pairs, exact_model) is None
    all_model = fit_regression(pairs)
    assert all_model is not None
    all_scored = score_residuals(pairs, all_model, start=None, end=None)
    assert all_scored is not None
    all_points, all_sigma = all_scored
    all_residuals = [vea - (all_model.a * brent + all_model.b) for _, vea, brent in pairs]
    assert all_sigma == pytest.approx(sum(x * x for x in all_residuals) ** 0.5 / len(pairs) ** 0.5)
    assert [point.spread for point in all_points] == pytest.approx(all_residuals)
    assert all_scored[1] != pytest.approx(sigma)

    from dashboards.vea_brent_spread_dashboard import (
        find_opportunities,
        fund_utilization,
        pair_signals,
    )

    events, position = find_opportunities(points, start=min(point.day for point in points))
    assert [(event.day, event.action) for event in events] == [
        (date(2018, 6, 29), "Entry"),
        (date(2019, 1, 2), "Exit"),
        (date(2023, 7, 3), "Entry"),
        (date(2023, 7, 4), "Exit"),
    ]
    assert position is None
    trades = pair_signals(points, events)
    assert [trade.vea_pnl for trade in trades] == [4, 6]
    expected_days = (date(2019, 1, 2) - date(2018, 6, 29)).days + 1
    total_days = (date(2023, 7, 4) - date(2018, 6, 29)).days + 1
    assert fund_utilization(trades, events, points, start=points[0].day) == pytest.approx(
        expected_days / total_days * 100
    )
    figure = trade_chart(points, events)
    assert [trace.name for trace in figure.data] == [
        "VEA close (AUD/share)",
        "Fixed-reference residual Z-score",
        "Buy VEA: Z < -1.5",
        "Sell VEA: Z > -0.5",
    ]
    assert list(figure.data[1].y) == pytest.approx([point.z_score for point in points])
    assert figure.data[0].yaxis == "y"
    assert figure.data[1].yaxis == "y2"
    assert len(figure.layout.shapes) == 5
    assert [shape.line.color for shape in figure.layout.shapes[1:]] == [
        "red",
        "green",
        "red",
        "green",
    ]


def test_main_shows_separate_trades_for_both_fits() -> None:
    shares = [
        (date(2018, 6, 29), Decimal("1")),
        (TRAIN_START, Decimal("3")),
        (date(2019, 1, 2), Decimal("5")),
        (TRAIN_END, Decimal("4")),
        (date(2023, 7, 3), Decimal("2")),
        (date(2023, 7, 4), Decimal("8")),
    ]
    oil = [
        (day, Decimal(brent))
        for (day, _), brent in zip(shares, (10, 10, 20, 30, 40, 40), strict=True)
    ]
    with (
        patch("dashboards.vea_brent_regression_dashboard._engine"),
        patch("dashboards.vea_brent_regression_dashboard.load_prices", return_value=(shares, oil)),
        patch("dashboards.vea_brent_regression_dashboard.st") as st,
    ):
        main()
    assert st.plotly_chart.call_count == 7
    assert st.dataframe.call_count == 2
    trade_rows = st.dataframe.call_args_list[0].args[0]
    assert len(trade_rows) == 2
    assert trade_rows[0]["Buy signal date"] == date(2018, 6, 29)
    assert trade_rows[0]["Sell signal date"] == date(2019, 1, 2)
    assert trade_rows[0]["VEA P/L (AUD/share)"] == 4
    assert trade_rows[1]["Buy signal date"] == date(2023, 7, 3)
    assert trade_rows[1]["Sell signal date"] == date(2023, 7, 4)
    assert trade_rows[1]["VEA P/L (AUD/share)"] == 6
    utilization = (
        ((date(2019, 1, 2) - date(2018, 6, 29)).days + 1)
        / ((date(2023, 7, 4) - date(2018, 6, 29)).days + 1)
        * 100
    )
    assert any(
        f"Fund utilization (calendar days): {utilization:.2f}%" in call.args[0]
        for call in st.write.call_args_list
    )
    all_data_rows = st.dataframe.call_args_list[1].args[0]
    assert all_data_rows != trade_rows
    assert all_data_rows[0]["Buy signal date"] == date(2023, 7, 3)
    assert all_data_rows[0]["Sell signal date"] == date(2023, 7, 4)
    assert st.subheader.call_args_list[4].args[0] == "Fit: all available data"
    st.subheader.assert_any_call(
        "July 2018-June 2023 fit: signals across all available matched dates"
    )
    st.subheader.assert_any_call("All-data fit: signals across all available matched dates")
    figures = [call.args[0] for call in st.plotly_chart.call_args_list]
    assert len(figures[3].layout.shapes) == 5
    assert len(figures[6].layout.shapes) == 2
    assert list(figures[3].data[1].y) != list(figures[6].data[1].y)


def test_main_shows_latest_valid_dates_for_each_source_independently() -> None:
    shares = [
        (date(2023, 1, 2), Decimal("3")),
        (date(2023, 1, 3), Decimal("4")),
        (date(2023, 1, 5), Decimal("5")),
        (date(2023, 1, 6), Decimal("NaN")),
    ]
    oil = [
        (date(2023, 1, 2), Decimal("10")),
        (date(2023, 1, 3), Decimal("20")),
        (date(2023, 1, 4), Decimal("30")),
    ]
    with (
        patch("dashboards.vea_brent_regression_dashboard._engine"),
        patch("dashboards.vea_brent_regression_dashboard.load_prices", return_value=(shares, oil)),
        patch("dashboards.vea_brent_regression_dashboard.st") as st,
    ):
        main()
    st.markdown.assert_called_once_with(
        ":red[**Latest VEA close: 2023-01-05 | Latest Brent close: 2023-01-04**]"
    )


def test_main_displays_two_comparisons_with_separate_fits() -> None:
    shares = [
        (TRAIN_START, Decimal("3")),
        (TRAIN_END, Decimal("5")),
        (date(2023, 7, 1), Decimal("9")),
        (date(2023, 7, 2), Decimal("7")),
    ]
    oil = [
        (TRAIN_START, Decimal("10")),
        (TRAIN_END, Decimal("20")),
        (date(2023, 7, 1), Decimal("30")),
        (date(2023, 7, 2), Decimal("40")),
    ]
    with (
        patch("dashboards.vea_brent_regression_dashboard._engine"),
        patch("dashboards.vea_brent_regression_dashboard.load_prices", return_value=(shares, oil)),
        patch("dashboards.vea_brent_regression_dashboard.st") as st,
    ):
        main()
    assert st.subheader.call_count == 5
    assert st.subheader.call_args_list[0].args[0] == "VEA and Brent closing prices"
    assert st.plotly_chart.call_count == 6
    st.warning.assert_any_call("Trading signals require a nonconstant training residual.")
    figures = [call.args[0] for call in st.plotly_chart.call_args_list]
    assert list(figures[0].data[0].y) == pytest.approx([3, 5, 9, 7])
    assert list(figures[0].data[1].y) == pytest.approx([10, 20, 30, 40])
    assert list(figures[1].data[1].y) == pytest.approx([3, 5, 7, 9])
    assert list(figures[2].data[0].x) == pytest.approx([0, 0])
    assert list(figures[3].data[1].y) != list(figures[1].data[1].y)
    assert len(figures[5].data) == 4
    assert st.dataframe.call_count <= 1
