"""Tests for the read-only VGN and QAN closing-price comparison."""

from datetime import date, timedelta
from decimal import Decimal
from math import log
from statistics import pstdev
from unittest.mock import MagicMock, patch

import pytest
from dashboards.vgn_qan_dashboard import (
    BetaPoint,
    PairedPosition,
    PairedTrade,
    SpreadPoint,
    _rolling_spread_results,
    backtest_paired_trades,
    backtest_spread_trades,
    backtest_trades,
    beta_chart,
    display_paired_results,
    full_sample_spread_scores,
    fund_utilization,
    hedged_price_chart,
    load_prices,
    main,
    normalized_chart,
    paired_spread_chart,
    paired_summary,
    paired_trade_chart,
    paired_utilization,
    price_chart,
    ratio_chart,
    ratio_histogram,
    rolling_spread_scores,
    spread_chart,
    spread_histogram,
    trade_chart,
    trade_summary,
)


def test_load_prices_queries_both_asx_tickers_and_only_closes() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.all.return_value = [
        ("QAN", date(2026, 1, 2), Decimal("9"), Decimal("8")),
        ("VGN", date(2026, 1, 5), Decimal("2.5"), Decimal("2.4")),
        ("VGN", date(2026, 1, 6), None, Decimal("2.6")),
    ]
    assert load_prices(engine) == (
        {
            "VGN": [(date(2026, 1, 5), Decimal("2.5"))],
            "QAN": [(date(2026, 1, 2), Decimal("9"))],
        },
        {date(2026, 1, 5): Decimal("2.4"), date(2026, 1, 6): Decimal("2.6")},
        {date(2026, 1, 2): Decimal("8")},
    )
    query = str(
        connection.execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "FROM daily_prices" in query
    assert "daily_prices.exchange_code = 'ASX'" in query
    assert "daily_prices.ticker IN ('VGN', 'QAN')" in query
    assert "daily_prices.close IS NOT NULL" in query
    assert "daily_prices.open IS NOT NULL" in query
    assert "ORDER BY daily_prices.trading_date" in query


def test_raw_chart_limits_qan_to_vgn_dates_with_separate_price_axes() -> None:
    figure = price_chart(
        {
            "VGN": [
                (date(2026, 1, 5), Decimal("2.5")),
                (date(2026, 1, 7), Decimal("2.6")),
            ],
            "QAN": [
                (date(2026, 1, 2), Decimal("9")),
                (date(2026, 1, 5), Decimal("10")),
                (date(2026, 1, 6), Decimal("11")),
                (date(2026, 1, 8), Decimal("12")),
            ],
        }
    )
    assert [trace.name for trace in figure.data] == ["VGN.AX close", "QAN.AX close"]
    assert [trace.yaxis for trace in figure.data] == ["y", "y2"]
    assert list(figure.data[0].x) == [date(2026, 1, 5), date(2026, 1, 7)]
    assert list(figure.data[1].x) == [date(2026, 1, 5), date(2026, 1, 6)]
    assert list(figure.data[1].y) == [10.0, 11.0]
    assert list(figure.layout.xaxis.range) == [date(2026, 1, 5), date(2026, 1, 7)]
    assert figure.layout.yaxis.title.text == "VGN close (AUD/share)"
    assert figure.layout.yaxis2.title.text == "QAN close (AUD/share)"
    assert figure.layout.xaxis.title.text == "Trading date"


def test_normalized_chart_scales_each_displayed_series_independently() -> None:
    figure = normalized_chart(
        {
            "VGN": [
                (date(2026, 1, 5), Decimal("2")),
                (date(2026, 1, 6), Decimal("3")),
                (date(2026, 1, 7), Decimal("4")),
            ],
            "QAN": [
                (date(2026, 1, 2), Decimal("100")),
                (date(2026, 1, 5), Decimal("10")),
                (date(2026, 1, 6), Decimal("20")),
                (date(2026, 1, 8), Decimal("200")),
            ],
        }
    )
    assert [trace.name for trace in figure.data] == ["VGN.AX normalized", "QAN.AX normalized"]
    assert [trace.yaxis for trace in figure.data] == [None, None]
    assert list(figure.data[0].y) == [0.0, 0.5, 1.0]
    assert list(figure.data[1].y) == [0.0, 1.0]
    assert list(figure.data[1].x) == [date(2026, 1, 5), date(2026, 1, 6)]
    assert list(figure.layout.xaxis.range) == [date(2026, 1, 5), date(2026, 1, 7)]
    assert list(figure.layout.yaxis.range) == [0, 1]


def test_normalized_chart_handles_constant_and_missing_series() -> None:
    prices = {
        "VGN": [(date(2026, 1, 5), Decimal("2")), (date(2026, 1, 6), Decimal("2"))],
        "QAN": [],
    }
    figure = normalized_chart(prices)
    assert [trace.name for trace in figure.data] == ["VGN.AX normalized"]
    assert list(figure.data[0].y) == [0.0, 0.0]
    assert not normalized_chart({"VGN": [], "QAN": prices["VGN"]}).data


def test_ratio_chart_uses_displayed_normalization_and_leaves_zero_denominator_gaps() -> None:
    prices = {
        "VGN": [
            (date(2026, 1, 5), Decimal("2")),
            (date(2026, 1, 6), Decimal("3")),
            (date(2026, 1, 7), Decimal("4")),
            (date(2026, 1, 8), Decimal("5")),
        ],
        "QAN": [
            (date(2026, 1, 2), Decimal("20")),
            (date(2026, 1, 5), Decimal("4")),
            (date(2026, 1, 6), Decimal("6")),
            (date(2026, 1, 8), Decimal("10")),
            (date(2026, 1, 9), Decimal("20")),
        ],
    }
    figure = ratio_chart(prices)
    normalized = normalized_chart(prices)
    assert [trace.name for trace in figure.data] == [
        "VGN.AX close",
        "VGN / QAN normalized close ratio",
    ]
    assert [trace.yaxis for trace in figure.data] == ["y", "y2"]
    assert list(figure.data[0].y) == [2.0, 3.0, 4.0, 5.0]
    assert list(normalized.data[0].y) == [0.0, 1 / 3, 2 / 3, 1.0]
    assert list(normalized.data[1].y) == [0.0, 1 / 3, 1.0]
    assert list(figure.data[1].y) == [None, 1.0, None, 1.0]
    assert list(figure.data[1].x) == [
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
        date(2026, 1, 8),
    ]
    assert figure.data[1].connectgaps is False
    assert list(figure.layout.xaxis.range) == [date(2026, 1, 5), date(2026, 1, 8)]
    assert figure.layout.yaxis.title.text == "VGN close (AUD/share)"
    assert figure.layout.yaxis.type != "log"
    assert figure.layout.yaxis2.type == "log"
    assert figure.layout.yaxis2.title.text == (
        "VGN / QAN normalized close ratio (log scale; dimensionless)"
    )


def test_ratio_chart_log_axis_excludes_zero_negative_and_nonfinite_values() -> None:
    prices = {
        "VGN": [
            (date(2026, 1, 5), Decimal("2")),
            (date(2026, 1, 6), Decimal("3")),
            (date(2026, 1, 7), Decimal("4")),
            (date(2026, 1, 8), Decimal("5")),
            (date(2026, 1, 9), Decimal("6")),
        ],
        "QAN": [],
    }
    with patch(
        "dashboards.vgn_qan_dashboard._normalized_ratios",
        return_value=[0.0, -1.0, float("nan"), float("inf"), 2.0],
    ):
        figure = ratio_chart(prices)
    assert list(figure.data[0].y) == [2.0, 3.0, 4.0, 5.0, 6.0]
    assert list(figure.data[1].y) == [None, None, None, None, 2.0]
    assert figure.data[1].connectgaps is False
    assert figure.layout.yaxis2.type == "log"


def test_ratio_chart_without_valid_normalized_qan_only_shows_vgn() -> None:
    prices = {
        "VGN": [(date(2026, 1, 5), Decimal("2")), (date(2026, 1, 6), Decimal("4"))],
        "QAN": [(date(2026, 1, 5), Decimal("8")), (date(2026, 1, 6), Decimal("8"))],
    }
    assert [trace.name for trace in ratio_chart(prices).data] == ["VGN.AX close"]
    assert [trace.name for trace in ratio_chart({**prices, "QAN": []}).data] == ["VGN.AX close"]
    assert not ratio_chart({"VGN": [], "QAN": prices["QAN"]}).data


def test_ratio_chart_uses_normalized_values_not_raw_prices() -> None:
    prices = {
        "VGN": [
            (date(2026, 1, 5), Decimal("2")),
            (date(2026, 1, 6), Decimal("4")),
            (date(2026, 1, 7), Decimal("6")),
        ],
        "QAN": [
            (date(2026, 1, 5), Decimal("10")),
            (date(2026, 1, 6), Decimal("30")),
            (date(2026, 1, 7), Decimal("50")),
        ],
    }
    assert list(ratio_chart(prices).data[1].y) == [None, 1.0, 1.0]


def test_histogram_counts_exactly_valid_ratio_dates_in_20_buckets() -> None:
    prices = {
        "VGN": [
            (date(2026, 1, 5), Decimal("2")),
            (date(2026, 1, 6), Decimal("3")),
            (date(2026, 1, 7), Decimal("4")),
            (date(2026, 1, 8), Decimal("5")),
        ],
        "QAN": [
            (date(2026, 1, 2), Decimal("100")),
            (date(2026, 1, 5), Decimal("6")),
            (date(2026, 1, 6), Decimal("5")),
            (date(2026, 1, 7), Decimal("4")),
            (date(2026, 1, 8), Decimal("10")),
            (date(2026, 1, 9), Decimal("100")),
        ],
    }
    valid = [value for value in ratio_chart(prices).data[1].y if value is not None]
    assert valid == [2.0, 1.0]
    figure = ratio_histogram(prices)
    assert [trace.name for trace in figure.data] == [
        "ln(normalized VGN / QAN ratio)",
        "Median",
        "Median -1 SD",
        "Median +1 SD",
        "Median -2 SD",
        "Median +2 SD",
    ]
    assert figure.data[0].type == "bar"
    expected_sigma = pstdev([log(2), log(1)])
    assert [trace.x[0] for trace in figure.data[1:]] == pytest.approx(
        [
            log(2) / 2,
            log(2) / 2 - expected_sigma,
            log(2) / 2 + expected_sigma,
            log(2) / 2 - 2 * expected_sigma,
            log(2) / 2 + 2 * expected_sigma,
        ]
    )
    assert [trace.line.color for trace in figure.data[1:]] == [
        "red",
        "green",
        "green",
        "orange",
        "orange",
    ]
    assert all(trace.x[0] == trace.x[1] for trace in figure.data[1:])
    assert all(trace.line.dash == "dot" for trace in figure.data[1:])
    assert len(figure.data[0].x) == len(figure.data[0].y) == 20
    assert sum(figure.data[0].y) == len([ratio for ratio in valid if ratio > 0])
    assert figure.data[0].y[0] == figure.data[0].y[-1] == 1
    assert figure.data[0].width == pytest.approx(log(2) / 20)
    assert figure.data[0].x[0] - figure.data[0].width / 2 == pytest.approx(0.0)
    assert figure.data[0].x[-1] + figure.data[0].width / 2 == pytest.approx(log(2))
    assert figure.layout.xaxis.title.text == "ln(normalized VGN / QAN close ratio)"
    assert figure.layout.yaxis.title.text == "Trading dates (count)"


def test_histogram_handles_constant_or_missing_ratio_series() -> None:
    prices = {
        "VGN": [(date(2026, 1, 5), Decimal("2")), (date(2026, 1, 6), Decimal("4"))],
        "QAN": [(date(2026, 1, 5), Decimal("8")), (date(2026, 1, 6), Decimal("8"))],
    }
    assert not ratio_histogram(prices).data
    assert not ratio_histogram({"VGN": [], "QAN": prices["QAN"]}).data
    assert not ratio_histogram({**prices, "QAN": []}).data


def test_histogram_uses_median_center_and_population_sigma_of_logged_values() -> None:
    with patch(
        "dashboards.vgn_qan_dashboard._normalized_ratios",
        return_value=[None, 0.0, 1.0, 1.0, 4.0, float("inf")],
    ):
        figure = ratio_histogram({"VGN": [], "QAN": []})
    sigma = pstdev([0.0, 0.0, log(4)])
    assert sum(figure.data[0].y) == 3
    assert [trace.x[0] for trace in figure.data[1:]] == pytest.approx(
        [0.0, -sigma, sigma, -2 * sigma, 2 * sigma]
    )


def test_histogram_constant_valid_ratio_still_uses_20_buckets() -> None:
    prices = {
        "VGN": [(date(2026, 1, 5), Decimal("2")), (date(2026, 1, 6), Decimal("4"))],
        "QAN": [(date(2026, 1, 5), Decimal("10")), (date(2026, 1, 6), Decimal("20"))],
    }
    figure = ratio_histogram(prices)
    assert len(figure.data[0].y) == 20
    assert sum(figure.data[0].y) == 1
    assert [trace.name for trace in figure.data[1:]] == ["Median"]
    assert figure.data[1].x == (0.0, 0.0)


def test_histogram_constant_log_values_omit_overlapping_sigma_lines() -> None:
    with patch("dashboards.vgn_qan_dashboard._normalized_ratios", return_value=[2.0, 2.0]):
        figure = ratio_histogram({"VGN": [], "QAN": []})
    assert sum(figure.data[0].y) == 2
    assert [trace.name for trace in figure.data[1:]] == ["Median"]
    assert figure.data[1].x[0] == pytest.approx(log(2))


def _strategy_fixture() -> tuple[dict[str, list[tuple[date, Decimal]]], dict[date, Decimal]]:
    start = date(2026, 1, 1)
    days = [start + timedelta(days=index) for index in range(13)]
    prices = {
        "VGN": [(day, Decimal(index)) for index, day in enumerate(days, start=1)],
        "QAN": [(day, Decimal(index)) for index, day in enumerate(days, start=1)],
    }
    opens = {day: Decimal(index + 10) for index, day in enumerate(days)}
    return prices, opens


def test_backtest_executes_next_open_and_compounds_completed_trades() -> None:
    prices, opens = _strategy_fixture()
    days = [day for day, _ in prices["VGN"]]
    # The first/last ratio positions are undefined; thresholds use 10 valid observations.
    log_ratios = [None, 0.01, 1.0, 1.0, 1.0, 0.01, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, None]
    with patch("dashboards.vgn_qan_dashboard._normalized_ratios", return_value=log_ratios):
        trades, open_buy = backtest_trades(prices, opens)
    assert open_buy is None
    assert len(trades) == 2
    assert [(t.buy_signal_date, t.buy_date, t.sell_signal_date, t.sell_date) for t in trades] == [
        (days[1], days[2], days[2], days[3]),
        (days[5], days[6], days[6], days[7]),
    ]
    assert [(t.buy_price, t.sell_price) for t in trades] == [
        (Decimal(12), Decimal(13)),
        (Decimal(16), Decimal(17)),
    ]
    assert trades[0].return_pct == (Decimal(13) / 12 - 1) * 100
    assert trade_summary(trades) == (Decimal(13) / 12 * Decimal(17) / 16 - 1) * 100
    assert fund_utilization(trades, open_buy, days[0], days[-1]) == Decimal(2) / 13 * 100
    chart = trade_chart(prices["VGN"], trades, open_buy)
    assert [shape.line.color for shape in chart.layout.shapes] == ["red", "green", "red", "green"]
    assert all(shape.line.dash == "dot" for shape in chart.layout.shapes)
    assert [shape.x0 for shape in chart.layout.shapes] == [
        days[2].isoformat(),
        days[3].isoformat(),
        days[6].isoformat(),
        days[7].isoformat(),
    ]


def test_backtest_missing_open_cancels_pending_order_and_keeps_open_position() -> None:
    prices, opens = _strategy_fixture()
    days = [day for day, _ in prices["VGN"]]
    opens.pop(days[2])
    log_ratios = [None, 0.01, 1.0, 1.0, 1.0, 0.01, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, None]
    with patch("dashboards.vgn_qan_dashboard._normalized_ratios", return_value=log_ratios):
        trades, open_buy = backtest_trades(prices, opens)
    assert len(trades) == 1
    assert trades[0].buy_date == days[6]
    assert trades[0].sell_date == days[7]
    # The canceled sell is not re-signaled if later ratios are unavailable.
    without_later_ratios = log_ratios[:7] + [None] * 6
    with patch(
        "dashboards.vgn_qan_dashboard._normalized_ratios", return_value=without_later_ratios
    ):
        trades, open_buy = backtest_trades(prices, {**opens, days[7]: Decimal(0)})
    assert trades == []
    assert open_buy == (days[5], days[6])
    assert fund_utilization(trades, open_buy, days[0], days[-1]) == Decimal(7) / 13 * 100
    markers = trade_chart(prices["VGN"], trades, open_buy).layout.shapes
    assert [shape.line.color for shape in markers] == ["red"]


def test_main_renders_completed_trade_table_and_summary() -> None:
    prices, opens = _strategy_fixture()
    log_ratios = [None, 0.01, 1.0, 1.0, 1.0, 0.01, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, None]
    with (
        patch("dashboards.vgn_qan_dashboard._engine"),
        patch("dashboards.vgn_qan_dashboard.load_prices", return_value=(prices, opens, {})),
        patch("dashboards.vgn_qan_dashboard._normalized_ratios", return_value=log_ratios),
        patch("dashboards.vgn_qan_dashboard.st") as st,
    ):
        main()
    assert st.plotly_chart.call_count == 10
    rows = st.dataframe.call_args.args[0]
    assert len(rows) == 2
    assert rows[0]["Buy signal date"] == prices["VGN"][1][0]
    assert rows[0]["Buy price (AUD/share)"] == 12.0
    assert rows[0]["Sell price (AUD/share)"] == 13.0
    assert rows[0]["Profit/loss (%)"] == pytest.approx((13 / 12 - 1) * 100)
    assert "Completed trades: 2" in st.write.call_args_list[0].args[0]
    assert "Fund utilization: 15.38%" in st.write.call_args_list[0].args[0]
    assert "Completed trades: 0" in st.write.call_args_list[1].args[0]


def test_backtest_uses_valid_log_ratio_sample_and_requires_variation() -> None:
    prices, opens = _strategy_fixture()
    with patch("dashboards.vgn_qan_dashboard._normalized_ratios", return_value=[None] * 13):
        assert backtest_trades(prices, opens) == ([], None)
    with patch("dashboards.vgn_qan_dashboard._normalized_ratios", return_value=[1.0] * 13):
        assert backtest_trades(prices, opens) == ([], None)
    with patch("dashboards.vgn_qan_dashboard._normalized_ratios", return_value=[0.0] + [1.0] * 12):
        assert backtest_trades(prices, opens) == ([], None)


def _spread_fixture() -> tuple[dict[str, list[tuple[date, Decimal]]], list[date]]:
    start = date(2025, 1, 1)
    days = [start + timedelta(days=index) for index in range(45)]
    qan = [Decimal(10 + index % 7) for index in range(len(days))]
    vgn = [Decimal(2) * q + Decimal(index % 3) for index, q in enumerate(qan)]
    return {
        "VGN": list(zip(days, vgn, strict=True)),
        "QAN": list(zip(days, qan, strict=True)),
    }, days


def test_fixed_beta_uses_all_pairs_and_spread_uses_prior_reference() -> None:
    prices, days = _spread_fixture()
    scores, betas = _rolling_spread_results(prices)
    assert scores == rolling_spread_scores(prices)
    assert scores[0].day == days[20]
    assert [point.day for point in betas] == days
    aligned = [
        (float(v), float(q)) for (_, v), (_, q) in zip(prices["VGN"], prices["QAN"], strict=True)
    ]
    mean_v = sum(v for v, _ in aligned) / len(aligned)
    mean_q = sum(q for _, q in aligned) / len(aligned)
    beta = sum((v - mean_v) * (q - mean_q) for v, q in aligned) / sum(
        (q - mean_q) ** 2 for _, q in aligned
    )
    assert all(point.beta == pytest.approx(beta) for point in betas)
    history = aligned[:20]
    reference = [v - beta * q for v, q in history]
    expected_spread = aligned[20][0] - beta * aligned[20][1]
    assert scores[0].z_score == pytest.approx(
        (expected_spread - sum(reference) / len(reference)) / pstdev(reference)
    )
    assert scores[0].spread == pytest.approx(expected_spread)
    assert betas[20].vgn_close == aligned[20][0]
    assert betas[20].hedged_qan_close == pytest.approx(beta * aligned[20][1])

    future = {key: list(value) for key, value in prices.items()}
    future["VGN"][-1] = (days[-1], Decimal(10000))
    future_scores, future_betas = _rolling_spread_results(future)
    assert future_betas[0].beta != pytest.approx(beta)
    assert all(point.beta == future_betas[0].beta for point in future_betas)
    assert future_scores[0] != scores[0]  # Full-sample beta changes past estimates.
    future_beta = future_betas[0].beta
    future_reference = [v - future_beta * q for v, q in history]
    assert future_scores[0].z_score == pytest.approx(
        (
            aligned[20][0]
            - future_beta * aligned[20][1]
            - sum(future_reference) / len(future_reference)
        )
        / pstdev(future_reference)
    )
    changed_today = {key: list(value) for key, value in prices.items()}
    changed_today["VGN"][20] = (days[20], Decimal(2000))
    changed_scores = rolling_spread_scores(changed_today)
    assert changed_scores[0].z_score != scores[0].z_score
    assert changed_scores[0].day == scores[0].day
    assert rolling_spread_scores({"VGN": [], "QAN": prices["QAN"]}) == []


def test_rolling_spread_window_excludes_same_day_and_old_history() -> None:
    prices, days = _spread_fixture()
    original = rolling_spread_scores(prices)
    altered = {key: list(value) for key, value in prices.items()}
    altered["VGN"][25] = (days[25], Decimal(1000))
    altered_scores, altered_betas = _rolling_spread_results(altered)
    assert altered_betas[0].beta != _rolling_spread_results(prices)[1][0].beta
    history = [
        (float(v), float(q))
        for (_, v), (_, q) in zip(prices["VGN"][:24], prices["QAN"][:24], strict=True)
    ]
    beta = altered_betas[0].beta
    reference = [v - beta * q for v, q in history]
    current_spread = float(prices["VGN"][24][1]) - beta * float(prices["QAN"][24][1])
    assert altered_scores[4].z_score == pytest.approx(
        (current_spread - sum(reference) / len(reference)) / pstdev(reference)
    )
    assert altered_scores[4] != original[4]  # Future data changes the fixed beta.
    shifted = {
        key: [
            (day + timedelta(days=400) if index >= 20 else day, price)
            for index, (day, price) in enumerate(series)
        ]
        for key, series in prices.items()
    }
    assert rolling_spread_scores(shifted)[0].day == shifted["VGN"][40][0]
    assert _rolling_spread_results(shifted)[1][0].day == shifted["VGN"][0][0]


def test_rolling_spread_skips_degenerate_and_missing_reference() -> None:
    prices, days = _spread_fixture()
    constant_qan = {**prices, "QAN": [(day, Decimal(10)) for day in days]}
    assert _rolling_spread_results(constant_qan) == ([], [])
    linear = {**prices, "VGN": [(day, Decimal(2) * q + 1) for day, q in prices["QAN"]]}
    linear_scores, linear_betas = _rolling_spread_results(linear)
    assert linear_scores == []
    assert linear_betas[0].beta == pytest.approx(2)
    missing = {**prices, "QAN": prices["QAN"][:19]}
    missing_scores, missing_betas = _rolling_spread_results(missing)
    assert missing_scores == []
    assert len(missing_betas) == 19
    zero_qan = {**prices, "QAN": [(day, Decimal(0)) for day in days]}
    assert _rolling_spread_results(zero_qan) == ([], [])


def test_beta_charts_share_vgn_dates_and_mask_missing_or_invalid_fits() -> None:
    prices, days = _spread_fixture()
    _, fitted = _rolling_spread_results(prices)
    assert fitted[0].day == days[0]
    assert len({point.beta for point in fitted}) == 1
    beta_figure = beta_chart(prices["VGN"], fitted)
    price_figure = hedged_price_chart(prices["VGN"], fitted)
    assert beta_figure.data[0].y[19] == pytest.approx(fitted[19].beta)
    assert beta_figure.data[0].y[20] == pytest.approx(fitted[20].beta)
    assert beta_figure.layout.yaxis.title.text == "Fixed full-sample OLS beta (dimensionless slope)"
    assert [trace.name for trace in price_figure.data] == [
        "VGN.AX close",
        "Fixed beta * QAN.AX close",
    ]
    assert price_figure.data[0].y[19] == fitted[19].vgn_close
    assert price_figure.data[1].y[19] == pytest.approx(fitted[19].hedged_qan_close)
    assert price_figure.data[0].y[20] == fitted[20].vgn_close
    assert price_figure.data[1].y[20] == pytest.approx(fitted[20].hedged_qan_close)
    assert price_figure.layout.yaxis.title.text.endswith("(AUD/share)")
    assert beta_figure.layout.xaxis.range == price_figure.layout.xaxis.range
    sparse = [BetaPoint(days[20], -0.5, 25.0, -6.0), BetaPoint(days[22], 0.5, 26.0, 6.5)]
    for figure in (
        beta_chart(prices["VGN"], sparse),
        hedged_price_chart(prices["VGN"], sparse),
    ):
        assert all(trace.y[21] is None and trace.connectgaps is False for trace in figure.data)
    assert hedged_price_chart(prices["VGN"], sparse).data[1].y[20] == -6.0


def test_spread_strategy_strict_signals_and_next_open_execution() -> None:
    prices, days = _spread_fixture()
    opens = {day: Decimal(100 + index) for index, day in enumerate(days)}
    points = [SpreadPoint(day, 0.0, 0.0) for day in days]
    points[20] = SpreadPoint(days[20], -10.0, -2.01)
    points[21] = SpreadPoint(days[21], -5.0, -1.0)
    points[22] = SpreadPoint(days[22], -4.0, -0.99)
    trades, open_buy = backtest_spread_trades(prices, opens, points)
    assert open_buy is None
    assert [(t.buy_signal_date, t.buy_date, t.sell_signal_date, t.sell_date) for t in trades] == [
        (days[20], days[21], days[22], days[23])
    ]
    assert (trades[0].buy_price, trades[0].sell_price) == (Decimal(121), Decimal(123))
    assert [
        shape.line.color for shape in trade_chart(prices["VGN"], trades, open_buy).layout.shapes
    ] == ["red", "green"]
    points[20] = SpreadPoint(days[20], -10.0, -2.0)
    assert backtest_spread_trades(prices, opens, points) == ([], None)
    points[20] = SpreadPoint(days[20], -10.0, -2.01)
    opens[days[21]] = Decimal(0)
    assert backtest_spread_trades(prices, opens, points) == ([], None)


def test_spread_chart_preserves_missing_date_gaps_and_axes() -> None:
    prices, days = _spread_fixture()
    figure = spread_chart(
        prices["VGN"], [SpreadPoint(days[20], 1.0, -2.1), SpreadPoint(days[22], 2.0, -0.9)]
    )
    assert [trace.name for trace in figure.data] == [
        "VGN - beta * QAN spread",
        "Rolling spread Z-score",
        "Buy below -2",
        "Sell above -1",
    ]
    assert figure.data[0].y[21] is None
    assert figure.data[1].y[21] is None
    assert figure.data[1].yaxis == "y2"
    assert figure.layout.yaxis.title.text == "VGN - beta * QAN spread (AUD/share)"
    assert figure.layout.yaxis2.title.text == "Rolling spread Z-score (dimensionless)"
    assert list(figure.layout.xaxis.range) == [days[0], days[-1]]


def test_full_sample_spread_reference_and_degenerate_data() -> None:
    prices, days = _spread_fixture()
    _, betas = _rolling_spread_results(prices)
    points = full_sample_spread_scores(betas)
    spreads = [point.vgn_close - point.hedged_qan_close for point in betas]
    assert [point.day for point in points] == days
    assert points[10].z_score == pytest.approx(
        (spreads[10] - sum(spreads) / len(spreads)) / pstdev(spreads)
    )
    assert sum(point.z_score for point in points) == pytest.approx(0)
    assert full_sample_spread_scores([]) == []
    assert full_sample_spread_scores([BetaPoint(days[0], 1, 2, 1)]) == []
    assert full_sample_spread_scores([BetaPoint(day, 1, 2, 1) for day in days[:3]]) == []


def test_raw_spread_histogram_has_20_buckets_and_full_sample_reference() -> None:
    betas = [BetaPoint(date(2026, 1, index + 1), 1, float(index), 0.0) for index in range(1, 22)]
    figure = spread_histogram(betas)
    counts = list(figure.data[0].y)
    assert len(counts) == 20 and sum(counts) == len(betas)
    assert figure.data[0].width == pytest.approx(1.0)
    assert [trace.name for trace in figure.data[1:]] == [
        "Full-sample mean",
        "Long entry: mean - 1.5 SD",
        "Long exit: mean - 0.5 SD",
        "Short exit: mean + 0.5 SD",
        "Short entry: mean + 1.5 SD",
    ]
    center = sum(range(1, 22)) / 21
    sigma = pstdev(range(1, 22))
    assert [trace.x[0] for trace in figure.data[1:]] == pytest.approx(
        [
            center,
            center - 1.5 * sigma,
            center - 0.5 * sigma,
            center + 0.5 * sigma,
            center + 1.5 * sigma,
        ]
    )
    assert figure.layout.xaxis.title.text == "Raw VGN - beta * QAN spread (AUD/share)"
    assert figure.layout.yaxis.title.text == "Trading dates (count)"
    assert not spread_histogram([]).data
    constant = spread_histogram([BetaPoint(date(2026, 1, 1), 1, 4, 2)] * 3)
    assert len(constant.data) == 1 and sum(constant.data[0].y) == 3
    assert (
        sum(spread_histogram([*betas, BetaPoint(date(2026, 2, 1), 1, float("inf"), 0)]).data[0].y)
        == 21
    )


def test_paired_strict_thresholds_atomic_fills_and_no_reversal() -> None:
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(13)]
    betas = [BetaPoint(day, 2, 10, 4) for day in days]
    scores = [0, -1.5, -1.51, 3, -0.5, -0.49, 3, 1.5, 1.51, -3, 0.5, 0.49, -3]
    points = [SpreadPoint(day, 0, score) for day, score in zip(days, scores, strict=True)]
    vgn_opens = {day: Decimal(10 + index) for index, day in enumerate(days)}
    qan_opens = {day: Decimal(2 + index) for index, day in enumerate(days)}
    qan_opens[days[3]] = Decimal(0)  # Pending long entry cannot fill just VGN.
    trades, position = backtest_paired_trades(points, betas, vgn_opens, qan_opens)
    assert position is None
    assert len(trades) == 2
    long, short = trades
    assert long.position.direction == "Long VGN / short QAN"
    assert (long.position.signal_date, long.position.entry_date) == (days[2], days[4])
    assert (long.exit_signal_date, long.exit_date) == (days[5], days[6])
    assert short.position.direction == "Short VGN / long QAN"
    assert (short.position.signal_date, short.position.entry_date) == (days[8], days[9])
    assert (short.exit_signal_date, short.exit_date) == (days[11], days[12])
    assert long.vgn_pnl == Decimal(2)
    assert long.qan_pnl == Decimal(-4)
    assert long.net_pnl == Decimal(-2)
    assert long.return_pct == Decimal(-2) / (Decimal(14) + 2 * Decimal(6)) * 100
    assert short.vgn_pnl == Decimal(-3)
    assert short.qan_pnl == Decimal(6)
    assert short.net_pnl == Decimal(3)
    assert short.return_pct == Decimal(3) / (Decimal(19) + 2 * Decimal(11)) * 100
    assert paired_summary(trades) == pytest.approx(
        (1 + long.return_pct / 100) * (1 + short.return_pct / 100) * 100 - 100
    )
    assert (long.holding_days, short.holding_days) == (2, 3)
    assert paired_utilization(trades, None, days[0], days[-1]) == Decimal(5) / 13 * 100
    with patch("dashboards.vgn_qan_dashboard.st") as st:
        display_paired_results([(day, Decimal(10)) for day in days], [], trades, None)
    rows = st.dataframe.call_args.args[0]
    assert [row["Holding days (calendar)"] for row in rows] == [2, 3]
    assert {
        "Direction",
        "Entry signal date",
        "Entry execution date (next paired open)",
        "VGN entry open (AUD/share)",
        "QAN entry open (AUD/share)",
        "Exit signal date",
        "Exit execution date (next paired open)",
        "VGN exit open (AUD/share)",
        "QAN exit open (AUD/share)",
        "VGN P&L (AUD)",
        "QAN P&L (AUD)",
        "Net P&L (AUD)",
        "Net return (%)",
    } <= rows[0].keys()
    chart = paired_trade_chart([(day, Decimal(10)) for day in days], betas, trades, None)
    assert chart.data[0].yaxis == "y" and chart.data[1].yaxis == "y2"
    assert chart.layout.yaxis.title.text == "VGN close (AUD/share)"
    assert chart.layout.yaxis2.title.text == "Fixed beta * QAN close (AUD/share)"
    assert chart.layout.yaxis2.type != "log"
    assert chart.data[1].name == "Fixed beta * QAN.AX close (AUD/share)"
    assert list(chart.data[1].y) == [4] * len(days)
    assert [shape.line.color for shape in chart.layout.shapes] == ["red", "green", "red", "green"]
    assert [trace.name for trace in chart.data[2:]] == [
        "Long VGN / short QAN entry",
        "Long VGN / short QAN exit",
        "Short VGN / long QAN entry",
        "Short VGN / long QAN exit",
    ]
    assert [trace.marker.symbol for trace in chart.data[2:]] == [
        "triangle-up",
        "triangle-up",
        "triangle-down",
        "triangle-down",
    ]
    assert all(trace.yaxis == "y" for trace in chart.data[2:])
    spread = paired_spread_chart([(day, Decimal(10)) for day in days], points, trades, None)
    assert spread.data[1].yaxis == "y2"
    assert len(spread.layout.shapes) == 4
    assert [next(iter(trace.y)) for trace in spread.data[2:]] == [-1.5, -0.5, 0.5, 1.5]


def test_paired_price_chart_uses_fixed_hedge_and_leaves_missing_gaps() -> None:
    days = [date(2026, 1, 2) + timedelta(days=index) for index in range(5)]
    shares = [(day, Decimal(10)) for day in days]
    betas = [
        BetaPoint(days[0] - timedelta(days=1), -2, 10, -18),
        BetaPoint(days[0], -2, 10, -8),
        BetaPoint(days[2], -2, 10, -10),
        BetaPoint(days[3], -2, 10, float("nan")),
        BetaPoint(days[4] + timedelta(days=1), -2, 10, -12),
    ]
    chart = paired_trade_chart(shares, betas, [], None)
    assert list(chart.data[1].x) == days
    assert list(chart.data[1].y) == [-8, None, -10, None, None]
    assert chart.data[1].connectgaps is False
    assert chart.layout.yaxis2.type != "log"
    assert list(chart.layout.xaxis.range) == [days[0], days[-1]]
    empty = paired_trade_chart(shares, [], [], None)
    assert list(empty.data[1].y) == [None] * len(days)


def test_paired_close_trigger_waits_for_next_common_open_across_weekend() -> None:
    friday = date(2026, 1, 2)
    monday = date(2026, 1, 5)
    tuesday = date(2026, 1, 6)
    wednesday = date(2026, 1, 7)
    thursday = date(2026, 1, 8)
    days = [friday, monday, tuesday, wednesday, thursday]
    betas = [BetaPoint(day, 1, 10, 4) for day in days]
    points = [SpreadPoint(friday, 0, -1.6), SpreadPoint(tuesday, 0, -0.4)]
    vgn_opens = {day: Decimal(10) for day in days}
    qan_opens = {
        friday: Decimal(5),
        monday: Decimal(0),
        tuesday: Decimal(5),
        wednesday: Decimal(5),
        thursday: Decimal(5),
    }
    trades, position = backtest_paired_trades(points, betas, vgn_opens, qan_opens)
    assert trades == [] and position is not None
    assert position.signal_date == friday and position.entry_date == tuesday
    # Tuesday's close is not used as an exit signal on its own entry bar.
    points.append(SpreadPoint(wednesday, 0, -0.4))
    trades, position = backtest_paired_trades(points, betas, vgn_opens, qan_opens)
    assert position is None and len(trades) == 1
    assert trades[0].exit_signal_date == wednesday and trades[0].exit_date == thursday
    assert trades[0].position.entry_date > trades[0].position.signal_date
    assert trades[0].exit_date > trades[0].exit_signal_date
    assert trades[0].holding_days == 2


def test_paired_holding_days_span_weekend_and_same_date_corner_case() -> None:
    friday = date(2026, 1, 2)
    monday = date(2026, 1, 5)
    position = PairedPosition(
        "Long VGN / short QAN", friday, friday, Decimal(10), Decimal(5), Decimal(1)
    )
    friday_to_monday = PairedTrade(position, friday, monday, Decimal(11), Decimal(5))
    assert friday_to_monday.holding_days == 3
    assert paired_utilization([friday_to_monday], None, friday, monday) == Decimal(75)
    assert PairedTrade(position, friday, friday, Decimal(11), Decimal(5)).holding_days == 0


def test_paired_terminal_pending_and_open_position_missing_prices() -> None:
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(5)]
    betas = [BetaPoint(day, -0.5, 5, -1) for day in days]
    opens = {day: Decimal(10) for day in days}
    qan = {day: Decimal(3) for day in days}
    points = [SpreadPoint(day, 0, -3) for day in days]
    assert backtest_paired_trades(points[:1], betas, opens, qan, days[0]) == ([], None)
    trades, position = backtest_paired_trades(points, betas, opens, qan)
    assert trades == []
    assert position is not None and position.entry_date == days[1]
    assert position.beta == Decimal("-0.5")
    assert paired_utilization(trades, position, days[0], days[-1]) == Decimal(4) / 5 * 100
    exit_points = [points[0], SpreadPoint(days[2], 0, 0)]
    qan[days[3]] = Decimal("NaN")
    qan.pop(days[4])
    trades, position = backtest_paired_trades(exit_points, betas, opens, qan)
    assert trades == [] and position is not None
    assert position.entry_date == days[1]
    # Missing paired close on the opening day still permits an open-only paired fill.
    sparse_betas = [betas[0], betas[2], betas[3], betas[4]]
    trades, position = backtest_paired_trades(points, sparse_betas, opens, qan)
    assert position is not None and position.entry_date == days[1]
    assert trades == []
    assert backtest_paired_trades(points, betas, opens, qan, days[0]) == ([], None)
    assert backtest_paired_trades(
        points, [BetaPoint(day, 0, 5, 0) for day in days], opens, qan
    ) == ([], None)


def test_missing_vgn_warns_without_showing_qan_full_history() -> None:
    with (
        patch("dashboards.vgn_qan_dashboard._engine"),
        patch("dashboards.vgn_qan_dashboard.load_prices") as load,
        patch("dashboards.vgn_qan_dashboard.st") as st,
    ):
        load.return_value = ({"VGN": [], "QAN": [(date(2026, 1, 2), Decimal("9"))]}, {}, {})
        main()
    st.warning.assert_called_once_with("No stored VGN.AX daily closes. Ingest VGN first.")
    st.plotly_chart.assert_not_called()


def test_missing_qan_warns_and_shows_vgn() -> None:
    with (
        patch("dashboards.vgn_qan_dashboard._engine"),
        patch("dashboards.vgn_qan_dashboard.load_prices") as load,
        patch("dashboards.vgn_qan_dashboard.st") as st,
    ):
        load.return_value = ({"VGN": [(date(2026, 1, 5), Decimal("2.5"))], "QAN": []}, {}, {})
        main()
    st.warning.assert_called_once_with("No stored QAN.AX daily closes. Ingest QAN first.")
    assert st.plotly_chart.call_count == 6
    assert [call.args[0] for call in st.info.call_args_list] == [
        "No positive finite normalized VGN/QAN ratios to plot on a log scale.",
        "No completed trades for this period.",
        "Insufficient valid overlapping history to calculate fixed beta.",
        "Insufficient valid overlapping history to calculate rolling spread Z-scores.",
        "No completed trades for this period.",
        "Insufficient spread variation for full-sample paired Z-scores.",
        "No eligible overlapping VGN/QAN closes for a spread histogram.",
        "No completed paired trades for this period.",
    ]
    figure = st.plotly_chart.call_args_list[0].args[0]
    assert [trace.name for trace in figure.data] == ["VGN.AX close"]
    normalized = st.plotly_chart.call_args_list[1].args[0]
    assert [trace.name for trace in normalized.data] == ["VGN.AX normalized"]
    ratio = st.plotly_chart.call_args_list[2].args[0]
    assert [trace.name for trace in ratio.data] == ["VGN.AX close"]


def test_no_stored_closes_does_not_render_empty_chart() -> None:
    with (
        patch("dashboards.vgn_qan_dashboard._engine"),
        patch(
            "dashboards.vgn_qan_dashboard.load_prices",
            return_value=({"VGN": [], "QAN": []}, {}, {}),
        ),
        patch("dashboards.vgn_qan_dashboard.st") as st,
    ):
        main()
    assert st.warning.call_count == 2
    st.plotly_chart.assert_not_called()


def test_database_error_is_reported_without_rendering_chart() -> None:
    with (
        patch("dashboards.vgn_qan_dashboard._engine", side_effect=RuntimeError("connection")),
        patch("dashboards.vgn_qan_dashboard.st") as st,
    ):
        main()
    st.error.assert_called_once()
    st.plotly_chart.assert_not_called()
