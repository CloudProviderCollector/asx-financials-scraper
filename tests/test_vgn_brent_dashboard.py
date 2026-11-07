"""Tests for the fixed-reference VGN/Brent spread dashboard."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from dashboards.vgn_brent_dashboard import (
    REFERENCE_END,
    SpreadPoint,
    aligned_closes,
    backtest_trades,
    fit_reference,
    load_prices,
    main,
    spread_chart,
    spread_histogram,
    spread_scores,
)


def test_load_prices_targets_vgn_and_yahoo_brent() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.side_effect = [
        MagicMock(all=MagicMock(return_value=[(date(2019, 1, 2), Decimal("2"), Decimal("1.9"))])),
        MagicMock(all=MagicMock(return_value=[(date(2019, 1, 2), Decimal("60"))])),
    ]
    shares, oil, opens = load_prices(engine)
    assert shares == [(date(2019, 1, 2), Decimal("2"))]
    assert oil == [(date(2019, 1, 2), Decimal("60"))]
    assert opens == {date(2019, 1, 2): Decimal("1.9")}
    queries = [
        str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
        for call in connection.execute.call_args_list
    ]
    assert "daily_prices.ticker = 'VGN'" in queries[0]
    assert "daily_prices.exchange_code = 'ASX'" in queries[0]
    assert "commodity_prices.source = 'Yahoo Finance'" in queries[1]
    assert "commodity_prices.series = 'BZ=F'" in queries[1]
    assert "commodity_prices.frequency = 'daily'" in queries[1]


def test_fixed_reference_uses_only_july_2018_through_july_2023() -> None:
    aligned = [
        (date(2018, 6, 30), 100.0, 100.0),
        (date(2018, 7, 1), 3.0, 1.0),
        (date(2019, 1, 2), 5.0, 2.0),
        (date(2023, 7, 31), 8.0, 3.0),
        (date(2023, 8, 1), 1000.0, 100.0),
    ]
    reference = fit_reference(aligned)
    assert reference is not None
    assert reference.observations == 3
    assert reference.beta == pytest.approx(2.5)
    assert reference.spreads == pytest.approx([0.5, 0.0, 0.5])
    assert reference.mean == pytest.approx(1 / 3)
    assert reference.sigma == pytest.approx(2**0.5 / 6)
    points = spread_scores(aligned, reference)
    assert points[3].z_score == pytest.approx((0.5 - reference.mean) / reference.sigma)
    assert points[4].spread == pytest.approx(750.0)
    assert points[4].z_score == pytest.approx((750 - reference.mean) / reference.sigma)
    assert sum(spread_histogram(reference).data[0].y) == 3
    assert [trace.yaxis for trace in spread_chart(points).data[:2]] == ["y", "y2"]


def test_insufficient_reference_or_constant_spread() -> None:
    assert fit_reference([(date(2023, 8, 1), 5.0, 2.0)]) is None
    assert fit_reference([(date(2019, 1, 1), 2.0, 2.0), (date(2019, 1, 2), 3.0, 2.0)]) is None
    assert fit_reference([(date(2019, 1, 1), 2.0, 1.0), (date(2019, 1, 2), 4.0, 2.0)]) is None
    shares = [(date(2019, 1, 1), Decimal("2")), (date(2019, 1, 2), Decimal("NaN"))]
    oil = [(date(2019, 1, 1), Decimal("60")), (date(2019, 1, 2), Decimal("65"))]
    assert aligned_closes(shares, oil) == [(date(2019, 1, 1), 2.0, 60.0)]


def test_post_reference_signals_execute_next_vgn_open_only() -> None:
    days = [
        date(2023, 7, 31),
        date(2023, 8, 1),
        date(2023, 8, 2),
        date(2023, 8, 3),
        date(2023, 8, 4),
        date(2023, 8, 7),
    ]
    shares = [(day, Decimal("5")) for day in days]
    opens = {day: Decimal(str(index + 4)) for index, day in enumerate(days)}
    points = [
        SpreadPoint(days[0], 0, -3),
        SpreadPoint(days[1], 0, -1.6),
        SpreadPoint(days[2], 0, -1.7),
        SpreadPoint(days[3], 0, -0.5),
        SpreadPoint(days[4], 0, -0.4),
    ]
    trades, open_buy = backtest_trades(shares, opens, points)
    assert open_buy is None
    assert len(trades) == 1
    trade = trades[0]
    assert (trade.buy_signal_date, trade.buy_date) == (days[1], days[2])
    assert (trade.sell_signal_date, trade.sell_date) == (days[4], days[5])
    assert trade.buy_price == Decimal("6")
    assert trade.sell_price == Decimal("9")
    assert trade.return_pct == Decimal("50.0")
    assert all(day > REFERENCE_END for day in (trade.buy_date, trade.sell_date))


def test_missing_open_cancels_pending_order_and_unmatched_days_do_not_signal() -> None:
    days = [date(2023, 8, day) for day in (1, 2, 3, 4, 5)]
    shares = [(day, Decimal("5")) for day in days]
    opens = {days[0]: Decimal("3"), days[2]: Decimal("4"), days[3]: Decimal("5")}
    points = [SpreadPoint(days[0], 0, -2), SpreadPoint(days[2], 0, -2)]
    trades, open_buy = backtest_trades(shares, opens, points)
    assert trades == []
    assert open_buy == (days[2], days[3])
    assert backtest_trades([], opens, points) == ([], None)


def test_main_warns_if_reference_cannot_be_fitted() -> None:
    with (
        patch("dashboards.vgn_brent_dashboard._engine"),
        patch(
            "dashboards.vgn_brent_dashboard.load_prices",
            return_value=(
                [(date(2023, 8, 1), Decimal("2"))],
                [(date(2023, 8, 1), Decimal("60"))],
                {},
            ),
        ),
        patch("dashboards.vgn_brent_dashboard.st") as st,
    ):
        main()
    st.warning.assert_called_once()
    assert "Insufficient overlapping" in st.warning.call_args.args[0]
