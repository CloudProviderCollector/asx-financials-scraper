"""Tests for the read-only VEA and Brent crude comparison."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from asx_financials.vea_crude_dashboard import (
    average_vea_oil_ratio,
    comparison_chart,
    crude_per_vea_chart,
    load_prices,
    main,
    normalized_chart,
)


def test_queries_target_vea_and_daily_brent() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.side_effect = [
        MagicMock(all=MagicMock(return_value=[(date(2026, 1, 2), Decimal("2.5"))])),
        MagicMock(all=MagicMock(return_value=[(date(2026, 1, 2), Decimal("65"))])),
    ]
    shares, oil = load_prices(engine)
    assert len(shares) == len(oil) == 1
    queries = [
        str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
        for call in connection.execute.call_args_list
    ]
    assert "FROM daily_prices" in queries[0]
    assert "daily_prices.exchange_code = 'ASX'" in queries[0]
    assert "daily_prices.ticker = 'VEA'" in queries[0]
    assert "daily_prices.close IS NOT NULL" in queries[0]
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


def test_crude_per_vea_chart_min_max_scales_matched_prices_before_dividing() -> None:
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
    figure = crude_per_vea_chart(shares, oil)
    assert [trace.yaxis for trace in figure.data] == ["y", "y2"]
    assert list(figure.data[0].x) == [day for day, _ in shares]
    assert list(figure.data[0].y) == [2.0, 4.0, 6.0, 20.0]
    assert list(figure.data[1].x) == [date(2026, 1, 5), date(2026, 1, 6)]
    assert list(figure.data[1].y) == [1 / 3, 1.0]
    assert figure.layout.yaxis.title.text == "VEA close (AUD/share)"
    assert figure.layout.yaxis2.title.text == "Normalized Brent / normalized VEA (unitless)"
    assert list(figure.layout.yaxis2.range) == [0, 7]


def test_crude_per_vea_chart_limits_axis_without_capping_ratio_values() -> None:
    shares = [
        (date(2026, 1, 2), Decimal("2")),
        (date(2026, 1, 3), Decimal("3")),
        (date(2026, 1, 4), Decimal("20")),
    ]
    oil = [
        (date(2026, 1, 2), Decimal("60")),
        (date(2026, 1, 3), Decimal("120")),
        (date(2026, 1, 4), Decimal("121")),
    ]
    figure = crude_per_vea_chart(shares, oil)
    assert list(figure.data[1].y) == [float(Decimal(1080) / 61), 1.0]
    assert list(figure.layout.yaxis2.range) == [0, 7]


def test_crude_per_vea_chart_skips_constant_series_and_unmatched_dates() -> None:
    shares = [(date(2026, 1, 2), Decimal("2")), (date(2026, 1, 3), Decimal("4"))]
    oil = [(date(2026, 1, 2), Decimal("60")), (date(2026, 1, 3), Decimal("60"))]
    assert list(crude_per_vea_chart(shares, oil).data[1].x) == []
    assert list(crude_per_vea_chart(shares, []).data[1].x) == []


def test_main_limits_oil_history_to_vea_date_range() -> None:
    shares = [(date(2026, 1, 2), Decimal("2.5")), (date(2026, 1, 5), Decimal("2.6"))]
    oil = [
        (date(2026, 1, 1), Decimal("64")),
        (date(2026, 1, 2), Decimal("65")),
        (date(2026, 1, 6), Decimal("67")),
    ]
    with (
        patch("asx_financials.vea_crude_dashboard.st.set_page_config"),
        patch("asx_financials.vea_crude_dashboard.st.title"),
        patch("asx_financials.vea_crude_dashboard.st.caption") as caption,
        patch("asx_financials.vea_crude_dashboard.st.subheader"),
        patch("asx_financials.vea_crude_dashboard.st.plotly_chart") as plot,
        patch("asx_financials.vea_crude_dashboard._engine"),
        patch("asx_financials.vea_crude_dashboard.load_prices", return_value=(shares, oil)),
    ):
        main()
    assert plot.call_count == 3
    original, normalized, divided = [call.args[0] for call in plot.call_args_list]
    assert list(original.data[1].x) == [date(2026, 1, 2)]
    assert list(normalized.data[1].x) == [date(2026, 1, 2)]
    assert list(normalized.data[1].y) == [2.5]
    assert list(divided.data[1].x) == []
    assert list(divided.data[1].y) == []
    assert "1 matching trading dates" in caption.call_args_list[-2].args[0]
    assert all(call.kwargs["width"] == "stretch" for call in plot.call_args_list)


def test_main_handles_missing_series() -> None:
    with (
        patch("asx_financials.vea_crude_dashboard.st.set_page_config"),
        patch("asx_financials.vea_crude_dashboard.st.title"),
        patch("asx_financials.vea_crude_dashboard.st.caption"),
        patch("asx_financials.vea_crude_dashboard.st.warning") as warning,
        patch("asx_financials.vea_crude_dashboard.st.plotly_chart") as plot,
        patch("asx_financials.vea_crude_dashboard._engine"),
        patch("asx_financials.vea_crude_dashboard.load_prices", return_value=([], [])),
    ):
        main()
    assert warning.call_count == 2
    plot.assert_not_called()
