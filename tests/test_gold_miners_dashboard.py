"""Tests for the read-only ASX gold miners and gold futures charts."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from dashboards import gold_miners_dashboard as dashboard


def use_fitted_input_defaults(streamlit: MagicMock) -> None:
    streamlit.number_input.side_effect = lambda _label, *, value, **_kwargs: value


def test_load_prices_reads_four_asx_stocks_and_gc_futures() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.side_effect = [
        MagicMock(
            all=MagicMock(
                return_value=[
                    (ticker, date(2026, 11, 16), Decimal("12")) for ticker in dashboard.TICKERS
                ]
            )
        ),
        MagicMock(all=MagicMock(return_value=[(date(2026, 11, 16), Decimal("4000"))])),
        MagicMock(all=MagicMock(return_value=[(date(2026, 11, 16), Decimal("0.65"))])),
    ]

    shares, gold, aud_usd = dashboard.load_prices(engine)

    assert set(shares) == set(dashboard.TICKERS)
    assert all(len(prices) == 1 for prices in shares.values())
    assert gold == [(date(2026, 11, 16), Decimal("4000"))]
    assert aud_usd == [(date(2026, 11, 16), Decimal("0.65"))]
    queries = [
        str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
        for call in connection.execute.call_args_list
    ]
    assert "daily_prices.exchange_code = 'ASX'" in queries[0]
    assert "daily_prices.ticker IN ('NST', 'EVN', 'NEM', 'PRU')" in queries[0]
    assert "daily_prices.close IS NOT NULL" in queries[0]
    assert "commodity_prices.source = 'Yahoo Finance'" in queries[1]
    assert "commodity_prices.series = 'GC=F'" in queries[1]
    assert "commodity_prices.frequency = 'daily'" in queries[1]
    assert "exchange_rates.series = 'AUDUSD=X'" in queries[2]
    assert "exchange_rates.base_currency = 'AUD'" in queries[2]
    assert "exchange_rates.quote_currency = 'USD'" in queries[2]


def test_load_opens_reads_positive_asx_open_prices_including_open_only_days() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.all.return_value = [
        ("NST", date(2026, 11, 16), Decimal("12")),
        ("NST", date(2026, 11, 17), Decimal("0")),
        ("EVN", date(2026, 11, 17), Decimal("8")),
    ]

    opens = dashboard.load_opens(engine)

    assert opens["NST"] == {date(2026, 11, 16): Decimal("12")}
    assert opens["EVN"] == {date(2026, 11, 17): Decimal("8")}
    assert opens["NEM"] == opens["PRU"] == {}
    query = str(
        connection.execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "daily_prices.exchange_code = 'ASX'" in query
    assert "daily_prices.open IS NOT NULL" in query
    assert "daily_prices.close" not in query


def test_gold_in_aud_uses_same_day_usd_per_aud_and_skips_missing_or_invalid_rates() -> None:
    first, second, third = date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16)
    gold_usd = [
        (first, Decimal("2000")),
        (second, Decimal("2100")),
        (third, Decimal("2200")),
    ]
    aud_usd = [(first, Decimal("0.8")), (third, Decimal("0"))]

    assert dashboard.gold_in_aud(gold_usd, aud_usd) == [(first, Decimal("2500"))]
    assert dashboard.gold_in_aud(gold_usd, [(first, Decimal("0.8"))]) == [(first, Decimal("2500"))]


def test_sigma_strategy_uses_next_stock_open_and_excludes_open_position_from_returns() -> None:
    days = [date(2026, 11, day) for day in (2, 3, 4, 5, 6, 9, 10, 11)]
    shares = [(day, Decimal("10")) for day in days]
    deviations = list(
        zip(days[:7], map(Decimal, ("-10", "-2", "-1", "0", "1", "2", "-10")), strict=True)
    )
    opens = {
        day: price
        for day, price in zip(
            days, map(Decimal, ("10", "10", "11", "12", "13", "14", "15", "16")), strict=True
        )
    }

    with patch.object(dashboard, "st") as streamlit:
        dashboard.display_strategy("NST", shares, opens, deviations)

    chart = streamlit.plotly_chart.call_args.args[0]
    assert chart.data[0].name == "NST.AX close (AUD/share)"
    assert [(shape.x0, shape.line.color) for shape in chart.layout.shapes] == [
        (days[1].isoformat(), "red"),
        (days[2].isoformat(), "green"),
        (days[7].isoformat(), "red"),
    ]
    trades = streamlit.dataframe.call_args.args[0]
    assert len(trades) == 1
    assert trades[0]["Buy price (AUD)"] == 10.0
    assert trades[0]["Sell price (AUD)"] == 11.0
    assert "Completed trades: 1" in streamlit.write.call_args.args[0]
    assert "One position remains open" in streamlit.caption.call_args.args[0]


def test_comparison_chart_uses_separate_axes_and_unscaled_prices() -> None:
    figure = dashboard.comparison_chart(
        "NST",
        [(date(2026, 11, 16), Decimal("20.5"))],
        [(date(2026, 11, 16), Decimal("4000"))],
    )

    assert [trace.yaxis for trace in figure.data] == ["y", "y2"]
    assert list(figure.data[0].y) == [20.5]
    assert list(figure.data[1].y) == [4000.0]
    assert figure.layout.yaxis.title.text == "NST.AX close (AUD/share)"
    assert figure.layout.yaxis2.title.text == "GC=F gold futures close (AUD/troy oz)"
    assert figure.layout.yaxis2.side == "right"


def test_main_renders_four_charts_with_each_stocks_overlap() -> None:
    early = date(2023, 10, 26)
    shared = date(2023, 10, 27)
    later = date(2023, 10, 30)
    shares = {
        ticker: [
            (shared if ticker == "NEM" else early, Decimal("10")),
            (later if ticker == "NEM" else shared, Decimal("12")),
        ]
        for ticker in dashboard.TICKERS
    }
    gold = [(early, Decimal("1900")), (shared, Decimal("1910")), (later, Decimal("1920"))]
    rates = [(day, Decimal("0.5")) for day, _ in gold]

    with (
        patch.object(dashboard, "st") as streamlit,
        patch.object(dashboard, "_engine"),
        patch.object(dashboard, "load_prices", return_value=(shares, gold, rates)),
    ):
        use_fitted_input_defaults(streamlit)
        dashboard.main()

    assert streamlit.plotly_chart.call_count == 24
    charts = [call.args[0] for call in streamlit.plotly_chart.call_args_list]
    assert [chart.data[0].name.split(".")[0] for chart in charts[::6]] == list(dashboard.TICKERS)
    assert list(charts[12].data[1].x) == [shared, later]
    assert list(charts[0].data[1].y) == [3800.0, 3820.0]
    assert list(charts[13].data[0].x) == list(charts[13].data[1].x) == [shared, later]
    assert all(len(charts[index].data) == 2 for index in range(0, 24, 6))
    assert "Sum of squared errors (SSE)" in streamlit.caption.call_args_list[1].args[0]
    assert "mean squared error (MSE)" in streamlit.caption.call_args_list[1].args[0]
    deviation_charts = [
        call.args[0]
        for call in streamlit.plotly_chart.call_args_list
        if call.kwargs.get("key") == "NST-deviation"
    ]
    assert len(deviation_charts) == 1
    assert deviation_charts[0].data[1].name == "NST.AX estimated close (AUD/share)"
    assert deviation_charts[0].data[2].name == "Actual minus estimated / estimated (%)"
    assert [call.kwargs.get("key") for call in streamlit.plotly_chart.call_args_list[-5:]] == [
        None,
        "PRU-ten-year-estimate",
        "PRU-deviation",
        "PRU-deviation-histogram",
        "PRU-valuation-trades",
    ]


def test_main_warns_instead_of_plotting_missing_or_disjoint_series() -> None:
    shares = {ticker: [] for ticker in dashboard.TICKERS}
    shares["NST"] = [(date(2023, 10, 26), Decimal("10"))]
    shares["EVN"] = [(date(2023, 10, 28), Decimal("10"))]
    gold = [(date(2023, 10, 27), Decimal("1900"))]
    rates = [(gold[0][0], Decimal("0.5"))]

    with (
        patch.object(dashboard, "st") as streamlit,
        patch.object(dashboard, "_engine"),
        patch.object(dashboard, "load_prices", return_value=(shares, gold, rates)),
    ):
        use_fitted_input_defaults(streamlit)
        dashboard.main()

    streamlit.plotly_chart.assert_not_called()
    assert streamlit.warning.call_count == 4


def test_trading_graphs_and_executions_use_five_year_aud_gold_regression_deviation() -> None:
    days = [
        date(2019, 11, 16),
        date(2021, 11, 15),
        date(2021, 11, 16),
        date(2023, 11, 16),
        date(2025, 11, 16),
        date(2026, 11, 16),
    ]
    stocks = {
        ticker: list(zip(days, map(Decimal, ("2", "3", "4", "5", "6", "7")), strict=True))
        for ticker in dashboard.TICKERS
    }
    gold_usd = list(zip(days, map(Decimal, ("10", "20", "30", "40", "50", "60")), strict=True))
    rates = [(day, Decimal("0.5") if day == days[3] else Decimal("1")) for day in days]
    opens = {ticker: {day: Decimal("10") for day in days} for ticker in dashboard.TICKERS}

    with (
        patch.object(dashboard, "_engine"),
        patch.object(dashboard, "load_prices", return_value=(stocks, gold_usd, rates)),
        patch.object(dashboard, "load_opens", return_value=opens),
        patch.object(
            dashboard, "backtest_valuation", wraps=dashboard.backtest_valuation
        ) as backtest,
        patch.object(dashboard, "st") as streamlit,
    ):
        use_fitted_input_defaults(streamlit)
        dashboard.main()

    charts_by_key = {
        call.kwargs["key"]: call.args[0]
        for call in streamlit.plotly_chart.call_args_list
        if "key" in call.kwargs
    }
    deviation_chart = charts_by_key["NST-deviation"]
    assert list(deviation_chart.data[0].x) == days[2:]
    assert list(deviation_chart.data[1].x) == days[2:]
    assert list(deviation_chart.data[1].y) == pytest.approx(
        [4.923076923, 6.076923077, 5.384615385, 5.615384615]
    )
    assert list(deviation_chart.data[2].x) == days[2:]
    assert list(deviation_chart.data[2].y) == pytest.approx(
        [-18.75, -17.721518987, 11.428571429, 24.657534247]
    )
    assert list(charts_by_key["NST-deviation-histogram"].data[0].x) == pytest.approx(
        [-18.75, -17.721518987, 11.428571429, 24.657534247]
    )
    assert list(charts_by_key["NST-valuation-trades"].data[0].x) == days[2:]
    assert len(backtest.call_args_list) == 4
    stock_closes, stock_opens, deviations = backtest.call_args_list[0].args
    assert [day for day, _ in stock_closes] == days[2:]
    assert list(stock_opens) == days[2:]
    assert [day for day, _ in deviations] == days[2:]
    assert [float(value) for _, value in deviations] == pytest.approx(
        [-0.1875, -0.17721518987, 0.11428571429, 0.24657534247]
    )
    assert any(
        "2021-11-16 to 2026-11-16" in call.args[0] for call in streamlit.subheader.call_args_list
    )


def test_sigma_thresholds_use_mean_and_population_standard_deviation() -> None:
    days = [date(2026, 11, day) for day in (12, 13, 16)]
    deviations = list(zip(days, map(Decimal, ("-0.1", "0", "0.1")), strict=True))

    buy, sell = dashboard.deviation_thresholds(deviations)

    assert float(buy) == pytest.approx(-0.1224744871)
    assert float(sell) == pytest.approx(-0.0408248290)
    assert buy < sell < 0


def test_valuation_deviation_skips_nonpositive_estimates_and_preserves_signed_values() -> None:
    days = [date(2026, 11, day) for day in (12, 13, 16)]
    pairs = list(zip(days, [2.0, 2.0, 3.0], [1.0, 2.0, 3.0], strict=True))
    fit = dashboard.RegressionFit(2.0, -2.0, 0.0, 3)

    assert dashboard.valuation_deviations(pairs, fit) == [
        (days[1], Decimal("0.0")),
        (days[2], Decimal("-0.25")),
    ]


def test_aligned_closes_requires_matching_positive_finite_dates() -> None:
    first, second, third = date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16)
    shares = [(first, Decimal("2")), (second, Decimal("NaN")), (third, Decimal("4"))]
    gold = [(first, Decimal("100")), (second, Decimal("200")), (third, Decimal("0"))]

    assert dashboard.aligned_closes(shares, gold) == [(first, 2.0, 100.0)]


def test_regression_fits_ols_and_reports_total_and_mean_squared_errors() -> None:
    days = [date(2026, 11, day) for day in (12, 13, 16)]
    pairs = list(zip(days, [2.0, 4.0, 9.0], [10.0, 20.0, 30.0], strict=True))

    fit = dashboard.fit_regression(pairs)

    assert fit is not None
    assert (fit.a, fit.b, fit.squared_error, fit.observations) == pytest.approx(
        (0.35, -2.0, 1.5, 3)
    )
    assert fit.squared_error / fit.observations == pytest.approx(0.5)
    chart = dashboard.estimate_chart("NST", pairs, fit)
    assert [list(trace.x) for trace in chart.data] == [days, days]
    assert list(chart.data[0].y) == [2.0, 4.0, 9.0]
    assert list(chart.data[1].y) == pytest.approx([1.5, 5, 8.5])
    assert chart.layout.yaxis.title.text == "NST.AX close (AUD/share)"


def test_regression_skips_insufficient_or_constant_gold_prices() -> None:
    day = date(2026, 11, 12)
    assert dashboard.fit_regression([(day, 1.0, 100.0)]) is None
    assert dashboard.fit_regression([(day, 1.0, 100.0), (date(2026, 11, 13), 2.0, 100.0)]) is None


@pytest.mark.parametrize("slope", [1e154, 1e308])
def test_edited_coefficients_reject_nonfinite_estimates_and_errors(slope: float) -> None:
    with patch.object(dashboard, "st") as streamlit:
        streamlit.number_input.side_effect = [slope, 0.0]
        selected = dashboard.editable_fit(
            "NST",
            "full history",
            [(date(2026, 11, 12), 2.0, 1000.0)],
            dashboard.RegressionFit(0.1, 1.0, 0.0, 1),
        )

    assert selected is None
    streamlit.error.assert_called_once()


def test_last_ten_years_uses_latest_matched_date_and_includes_cutoff() -> None:
    days = [
        date(2016, 11, 15),
        date(2016, 11, 16),
        date(2020, 11, 16),
        date(2026, 11, 16),
    ]
    pairs = [(day, float(index), float(index)) for index, day in enumerate(days)]

    assert dashboard.last_ten_years(pairs) == pairs[1:]
    assert dashboard.last_ten_years([]) == []
    leap_pairs = [
        (date(2010, 2, 27), 1.0, 10.0),
        (date(2010, 2, 28), 2.0, 20.0),
        (date(2020, 2, 29), 3.0, 30.0),
    ]
    assert dashboard.last_ten_years(leap_pairs) == leap_pairs[1:]


def test_main_renders_independent_ten_year_fit_using_aud_gold() -> None:
    days = [date(year, 11, day) for year, day in ((2015, 16), (2016, 16), (2020, 16), (2026, 16))]
    stocks = {
        ticker: list(
            zip(days, [Decimal("99"), Decimal("2"), Decimal("4"), Decimal("6")], strict=True)
        )
        for ticker in dashboard.TICKERS
    }
    gold_usd = list(
        zip(days, [Decimal("5"), Decimal("10"), Decimal("15"), Decimal("20")], strict=True)
    )
    rates = [(day, Decimal("0.5")) for day in days]

    with (
        patch.object(dashboard, "_engine"),
        patch.object(dashboard, "load_prices", return_value=(stocks, gold_usd, rates)),
        patch.object(dashboard, "st") as streamlit,
    ):
        use_fitted_input_defaults(streamlit)
        dashboard.main()

    charts = [call.args[0] for call in streamlit.plotly_chart.call_args_list]
    assert len(charts) == 12
    full_fit, recent_fit = charts[1:3]
    assert list(recent_fit.data[0].x) == days[1:]
    assert list(recent_fit.data[0].y) == [2.0, 4.0, 6.0]
    assert list(recent_fit.data[1].y) == pytest.approx([2.0, 4.0, 6.0])
    assert list(full_fit.data[1].y) != pytest.approx([99.0, 2.0, 4.0, 6.0])
    assert (
        "Selected a = 0.2 (AUD/share)/(AUD/troy oz)"
        in (streamlit.caption.call_args_list[3].args[0])
    )
    assert "2016-11-16 to 2026-11-16" in streamlit.caption.call_args_list[3].args[0]
    assert streamlit.plotly_chart.call_args_list[8].kwargs["key"] == "NEM-ten-year-estimate"


def test_editing_each_estimate_updates_only_its_window_and_five_year_trades() -> None:
    days = [date(2026, 11, day) for day in range(12, 18)]
    stocks = {
        ticker: [(day, Decimal(index + 2)) for index, day in enumerate(days)]
        for ticker in dashboard.TICKERS
    }
    gold = [(day, Decimal((index + 1) * 10)) for index, day in enumerate(days)]
    rates = [(day, Decimal("1")) for day in days]
    opens = {ticker: {day: Decimal("10") for day in days} for ticker in dashboard.TICKERS}
    edits = {
        "NST-full history-a": 0.0,
        "NST-full history-b": 10.0,
        "NST-last 10 years-a": 0.0,
        "NST-last 10 years-b": 20.0,
        "NST-last 5 years-a": 0.05,
        "NST-last 5 years-b": 2.0,
    }

    def render(selected: dict[str, float]) -> MagicMock:
        with (
            patch.object(dashboard, "_engine"),
            patch.object(dashboard, "load_prices", return_value=(stocks, gold, rates)),
            patch.object(dashboard, "load_opens", return_value=opens),
            patch.object(dashboard, "st") as streamlit,
        ):
            streamlit.number_input.side_effect = lambda _label, *, key, value, **_kwargs: (
                selected.get(key, value)
            )
            dashboard.main()
        return streamlit

    baseline = render({})
    edited = render(edits)
    inputs = {
        call.kwargs["key"]: call.kwargs["value"] for call in edited.number_input.call_args_list
    }
    assert len(inputs) == 24
    assert all(inputs[key] == pytest.approx(0.1 if key.endswith("-a") else 1.0) for key in edits)
    assert baseline.number_input.call_args_list[0].kwargs["value"] == pytest.approx(0.1)

    charts = [call.args[0] for call in edited.plotly_chart.call_args_list]
    assert list(charts[1].data[1].y) == [10.0] * 6
    assert list(charts[2].data[1].y) == [20.0] * 6
    assert list(charts[3].data[1].y) == [2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
    assert list(charts[3].data[2].y) == pytest.approx([-20.0, 0.0, 100 / 7, 25.0, 100 / 3, 40.0])
    assert "SSE): 199" in edited.caption.call_args_list[1].args[0]
    assert "SSE: 1,459" in edited.caption.call_args_list[3].args[0]
    assert "Completed trades: 1" in edited.write.call_args_list[0].args[0]
    assert "Completed trades: 0" in baseline.write.call_args_list[0].args[0]
    assert list(charts[7].data[1].y) == pytest.approx(
        list(baseline.plotly_chart.call_args_list[7].args[0].data[1].y)
    )


def test_main_keeps_comparison_and_warns_when_gold_prices_are_constant() -> None:
    first, second = date(2026, 11, 12), date(2026, 11, 13)
    stocks = {
        ticker: [(first, Decimal("10")), (second, Decimal("12"))] for ticker in dashboard.TICKERS
    }
    gold = [(first, Decimal("100")), (second, Decimal("100"))]
    rates = [(first, Decimal("0.5")), (second, Decimal("0.5"))]
    with (
        patch.object(dashboard, "_engine"),
        patch.object(dashboard, "load_prices", return_value=(stocks, gold, rates)),
        patch.object(dashboard, "st") as streamlit,
    ):
        use_fitted_input_defaults(streamlit)
        dashboard.main()

    assert streamlit.plotly_chart.call_count == 4
    assert streamlit.warning.call_count == 4
    assert "varying" in streamlit.warning.call_args_list[0].args[0]


def test_main_displays_root_mean_squared_error_in_share_price_units() -> None:
    days = [date(2026, 11, day) for day in (12, 13, 16)]
    latest = date(2026, 11, 17)
    stocks = {
        ticker: [
            *zip(days, [Decimal("2"), Decimal("4"), Decimal("9")], strict=True),
            (latest, Decimal("20")),
        ]
        for ticker in dashboard.TICKERS
    }
    gold = list(zip(days, [Decimal("10"), Decimal("20"), Decimal("30")], strict=True))
    rates = [(day, Decimal("1")) for day in days]
    with (
        patch.object(dashboard, "_engine"),
        patch.object(dashboard, "load_prices", return_value=(stocks, gold, rates)),
        patch.object(dashboard, "st") as streamlit,
    ):
        use_fitted_input_defaults(streamlit)
        dashboard.main()

    caption = streamlit.caption.call_args_list[1].args[0]
    assert "mean squared error (MSE): 0.5 (AUD/share)^2" in caption
    assert "root mean squared error (RMSE): 0.707107 AUD/share" in caption
    ratio_caption = streamlit.caption.call_args_list[2].args[0]
    assert (
        "RMSE / latest NST.AX close (2026-11-17, 20.0000 AUD/share): 0.0353553 (3.54%)"
    ) in ratio_caption


def test_main_warns_if_latest_stock_close_cannot_normalize_rmse() -> None:
    first, second, latest = date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 17)
    stocks = {
        ticker: [
            (first, Decimal("10")),
            (second, Decimal("12")),
            (latest, Decimal("0")),
        ]
        for ticker in dashboard.TICKERS
    }
    gold = [(first, Decimal("100")), (second, Decimal("200"))]
    rates = [(first, Decimal("1")), (second, Decimal("1"))]
    with (
        patch.object(dashboard, "_engine"),
        patch.object(dashboard, "load_prices", return_value=(stocks, gold, rates)),
        patch.object(dashboard, "st") as streamlit,
    ):
        use_fitted_input_defaults(streamlit)
        dashboard.main()

    assert streamlit.plotly_chart.call_count == 24
    assert streamlit.warning.call_count == 4
    assert "must be positive and finite" in streamlit.warning.call_args_list[0].args[0]


def test_main_requires_stored_aud_usd_without_using_unconverted_gold() -> None:
    stocks = {ticker: [] for ticker in dashboard.TICKERS}
    gold = [(date(2026, 11, 16), Decimal("4000"))]
    with (
        patch.object(dashboard, "_engine"),
        patch.object(dashboard, "load_prices", return_value=(stocks, gold, [])),
        patch.object(dashboard, "st") as streamlit,
    ):
        use_fitted_input_defaults(streamlit)
        dashboard.main()

    streamlit.plotly_chart.assert_not_called()
    streamlit.warning.assert_called_once()
    assert "No stored AUD/USD" in streamlit.warning.call_args.args[0]
