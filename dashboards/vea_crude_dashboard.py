"""Read-only Streamlit comparison of VEA shares and Brent crude oil."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import Literal, cast

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import Engine, create_engine, or_, select

from asx_financials.config import get_settings
from asx_financials.infrastructure.persistence.schema import commodity_prices, daily_prices
from asx_financials.vea_crude_refresh import BRENT_SOURCE, BRENT_SYMBOL


@dataclass(frozen=True)
class Trade:
    buy_date: date
    buy_price: Decimal
    sell_date: date
    sell_price: Decimal

    @property
    def return_pct(self) -> Decimal:
        return (self.sell_price / self.buy_price - 1) * 100

    @property
    def holding_days(self) -> int:
        return (self.sell_date - self.buy_date).days


@dataclass(frozen=True)
class StrategySignal:
    pending_action: Literal["buy", "sell"] | None
    open_buy_date: date | None
    preparing_to_sell: bool


@dataclass(frozen=True)
class LatestStrategySignals:
    matched_date: date | None
    strategies: dict[int, StrategySignal]


@st.cache_resource
def _engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def load_prices(
    engine: Engine,
) -> tuple[
    list[tuple[date, Decimal]],
    list[tuple[date, Decimal]],
    dict[date, Decimal],
]:
    with engine.connect() as connection:
        shares = connection.execute(
            select(daily_prices.c.trading_date, daily_prices.c.close, daily_prices.c.open)
            .where(
                daily_prices.c.exchange_code == "ASX",
                daily_prices.c.ticker == "VEA",
                or_(daily_prices.c.close.is_not(None), daily_prices.c.open.is_not(None)),
            )
            .order_by(daily_prices.c.trading_date)
        ).all()
        oil = connection.execute(
            select(commodity_prices.c.observation_date, commodity_prices.c.price)
            .where(
                commodity_prices.c.source == BRENT_SOURCE,
                commodity_prices.c.series == BRENT_SYMBOL,
                commodity_prices.c.frequency == "daily",
            )
            .order_by(commodity_prices.c.observation_date)
        ).all()
    return (
        [(cast(date, day), cast(Decimal, close)) for day, close, _ in shares if close is not None],
        [(cast(date, day), cast(Decimal, value)) for day, value in oil],
        {
            cast(date, day): cast(Decimal, open_price)
            for day, _, open_price in shares
            if open_price is not None and cast(Decimal, open_price) > 0
        },
    )


def comparison_chart(
    shares: list[tuple[date, Decimal]], oil: list[tuple[date, Decimal]]
) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in shares],
            y=[float(value) for _, value in shares],
            name="VEA.AX close (AUD/share)",
            mode="lines",
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in oil],
            y=[float(value) for _, value in oil],
            name="Brent crude (USD/barrel)",
            mode="lines",
        ),
        secondary_y=True,
    )
    figure.update_xaxes(title_text="Date")
    figure.update_yaxes(title_text="VEA close (AUD/share)", secondary_y=False)
    figure.update_yaxes(title_text="Brent crude (USD/barrel)", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def average_vea_oil_ratio(
    shares: list[tuple[date, Decimal]], oil: list[tuple[date, Decimal]]
) -> tuple[Decimal, int] | None:
    oil_by_date = dict(oil)
    ratios = [
        value / oil_by_date[day]
        for day, value in shares
        if day in oil_by_date and oil_by_date[day] > 0
    ]
    if not ratios:
        return None
    return sum(ratios, Decimal(0)) / len(ratios), len(ratios)


def normalized_chart(
    shares: list[tuple[date, Decimal]], oil: list[tuple[date, Decimal]], ratio: Decimal
) -> go.Figure:
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in shares],
            y=[float(value) for _, value in shares],
            name="VEA.AX close (AUD/share)",
            mode="lines",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in oil],
            y=[float(value * ratio) for _, value in oil],
            name="Brent crude scaled by average VEA/Brent ratio",
            mode="lines",
        )
    )
    figure.update_xaxes(title_text="Date")
    figure.update_yaxes(title_text="VEA close / normalized Brent (AUD/share scale)")
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def _percentile(values: list[Decimal], fraction: Decimal) -> Decimal:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    weight = position - lower
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * weight


def _normalized_ratios(
    shares: list[tuple[date, Decimal]], oil: list[tuple[date, Decimal]]
) -> list[tuple[date, Decimal]]:
    oil_by_date = dict(oil)
    paired = [(day, value, oil_by_date[day]) for day, value in shares if day in oil_by_date]
    ratios: list[tuple[date, Decimal]] = []
    if paired:
        share_min = min(value for _, value, _ in paired)
        share_max = max(value for _, value, _ in paired)
        oil_min = min(value for _, _, value in paired)
        oil_max = max(value for _, _, value in paired)
        if share_max > share_min and oil_max > oil_min:
            ratios = [
                (
                    day,
                    ((share_value - share_min) / (share_max - share_min))
                    / ((oil_value - oil_min) / (oil_max - oil_min)),
                )
                for day, share_value, oil_value in paired
                if oil_value > oil_min
            ]
    return ratios


def _meets_threshold(
    value: Decimal,
    previous: Decimal | None,
    threshold: Decimal,
    *,
    above: bool,
    confirm_two_days: bool,
) -> bool:
    if above:
        return value > threshold and (
            not confirm_two_days or (previous is not None and previous > threshold)
        )
    return value < threshold and (
        not confirm_two_days or (previous is not None and previous < threshold)
    )


def _evaluate_trades(
    shares: list[tuple[date, Decimal]],
    opens: dict[date, Decimal],
    ratios: list[tuple[date, Decimal]],
    *,
    staged_sell: bool = False,
    confirm_two_days: bool = False,
) -> tuple[list[Trade], StrategySignal]:
    if not ratios:
        return [], StrategySignal(None, None, False)
    values = [value for _, value in ratios]
    buy_below = _percentile(values, Decimal("0.15865"))
    sell_above = _percentile(values, Decimal("0.35"))
    sell_high = _percentile(values, Decimal("0.65")) if staged_sell else None
    ratios_by_date = dict(ratios)
    trades: list[Trade] = []
    position: tuple[date, Decimal] | None = None
    preparing_to_sell = False
    pending: Literal["buy", "sell"] | None = None
    previous: Decimal | None = None
    for day in sorted({day for day, _ in shares} | opens.keys()):
        open_price = opens.get(day)
        if pending is not None:
            if open_price is None or open_price <= 0:
                pending = None
                previous = None
                continue
            if pending == "buy":
                position = (day, open_price)
                preparing_to_sell = False
            elif position is not None:
                buy_date, buy_price = position
                trades.append(Trade(buy_date, buy_price, day, open_price))
                position = None
                preparing_to_sell = False
            pending = None
            previous = None
        value = ratios_by_date.get(day)
        stage_changed = False
        if value is not None:
            if position is None and _meets_threshold(
                value, previous, buy_below, above=False, confirm_two_days=confirm_two_days
            ):
                pending = "buy"
            elif position is not None:
                if staged_sell:
                    if preparing_to_sell and (
                        _meets_threshold(
                            value,
                            previous,
                            sell_above,
                            above=False,
                            confirm_two_days=confirm_two_days,
                        )
                        or (
                            sell_high is not None
                            and _meets_threshold(
                                value,
                                previous,
                                sell_high,
                                above=True,
                                confirm_two_days=confirm_two_days,
                            )
                        )
                    ):
                        pending = "sell"
                    elif not preparing_to_sell and _meets_threshold(
                        value,
                        previous,
                        sell_above,
                        above=True,
                        confirm_two_days=confirm_two_days,
                    ):
                        preparing_to_sell = True
                        stage_changed = True
                elif value > sell_above:
                    pending = "sell"
        previous = None if stage_changed else value
    return trades, StrategySignal(
        pending,
        position[0] if position is not None else None,
        preparing_to_sell,
    )


def backtest_trades(
    shares: list[tuple[date, Decimal]],
    opens: dict[date, Decimal],
    ratios: list[tuple[date, Decimal]],
    *,
    staged_sell: bool = False,
    confirm_two_days: bool = False,
) -> tuple[list[Trade], date | None]:
    trades, state = _evaluate_trades(
        shares, opens, ratios, staged_sell=staged_sell, confirm_two_days=confirm_two_days
    )
    return trades, state.open_buy_date


def latest_strategy_signals(
    shares: list[tuple[date, Decimal]],
    oil: list[tuple[date, Decimal]],
    opens: dict[date, Decimal],
) -> LatestStrategySignals:
    """Evaluate next-open orders on the supplied, already cutoff-limited price history.

    Callers must exclude all prices after their selected cutoff from all three inputs.
    matched_date is the last date with a defined normalized ratio. Normalization and
    percentile thresholds use the entire supplied matched history.
    A pending action is an order for the next trading open, not an executed trade;
    prepare-to-sell is a stage change, not a pending sale.
    """
    ratios = _normalized_ratios(shares, oil)
    matched_date = max((day for day, _ in ratios), default=None)
    last_trading_date = max({day for day, _ in shares} | opens.keys(), default=None)
    strategies = {}
    for number, staged_sell, confirm_two_days in (
        (1, False, False),
        (2, True, False),
        (3, True, True),
    ):
        _, state = _evaluate_trades(
            shares, opens, ratios, staged_sell=staged_sell, confirm_two_days=confirm_two_days
        )
        if matched_date != last_trading_date:
            state = StrategySignal(None, state.open_buy_date, state.preparing_to_sell)
        strategies[number] = state
    return LatestStrategySignals(matched_date, strategies)


def trade_summary(trades: list[Trade]) -> tuple[Decimal, Decimal]:
    if not trades:
        return Decimal(0), Decimal(0)
    win_rate = Decimal(sum(trade.return_pct > 0 for trade in trades)) / len(trades) * 100
    growth = Decimal(1)
    for trade in trades:
        growth *= trade.sell_price / trade.buy_price
    return win_rate, (growth - 1) * 100


def fund_utilization(
    trades: list[Trade], open_buy_date: date | None, start: date, end: date
) -> Decimal:
    total_days = (end - start).days + 1
    if total_days <= 0:
        return Decimal(0)
    used_days = sum(
        max(0, (min(trade.sell_date, end + timedelta(days=1)) - max(trade.buy_date, start)).days)
        for trade in trades
    )
    if open_buy_date is not None:
        used_days += max(0, (end - max(open_buy_date, start)).days + 1)
    return Decimal(used_days) / total_days * 100


def trade_chart(
    shares: list[tuple[date, Decimal]], trades: list[Trade], open_buy_date: date | None
) -> go.Figure:
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in shares],
            y=[float(price) for _, price in shares],
            name="VEA.AX close (AUD/share)",
            mode="lines",
        )
    )
    for trade in trades:
        figure.add_vline(x=trade.buy_date.isoformat(), line_color="red", line_dash="dash")
        figure.add_vline(x=trade.sell_date.isoformat(), line_color="green", line_dash="dash")
    if open_buy_date is not None:
        figure.add_vline(x=open_buy_date.isoformat(), line_color="red", line_dash="dash")
    figure.update_xaxes(title_text="Date")
    figure.update_yaxes(title_text="VEA close (AUD/share)")
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def ratio_histogram(ratios: list[tuple[date, Decimal]]) -> go.Figure:
    values = [float(value) for _, value in ratios]
    histogram = go.Histogram(x=values, name="Normalized VEA / normalized Brent")
    if values:
        minimum, maximum = min(values), max(values)
        if minimum == maximum:
            start, end = minimum - 0.5, maximum + 0.5
        else:
            margin = (maximum - minimum) * 1e-9
            start, end = minimum - margin, maximum + margin
        histogram.xbins = {"start": start, "end": end, "size": (end - start) / 20}
    figure = go.Figure(histogram)
    if ratios:
        ratio_values = [value for _, value in ratios]
        figure.add_vline(x=float(median(ratio_values)), line_color="red", line_dash="dash")
        for percentile, color in (
            (Decimal("0.15865"), "orange"),
            (Decimal("0.84135"), "orange"),
            (Decimal("0.35"), "green"),
            (Decimal("0.65"), "green"),
        ):
            figure.add_vline(
                x=float(_percentile(ratio_values, percentile)),
                line_color=color,
                line_dash="dash",
            )
    figure.update_xaxes(title_text="Normalized VEA / normalized Brent (unitless)")
    figure.update_yaxes(title_text="Number of trading dates")
    figure.update_layout(height=500, bargap=0.05)
    return figure


def vea_per_crude_chart(
    shares: list[tuple[date, Decimal]],
    oil: list[tuple[date, Decimal]],
    ratios: list[tuple[date, Decimal]] | None = None,
) -> go.Figure:
    if ratios is None:
        ratios = _normalized_ratios(shares, oil)
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in shares],
            y=[float(value) for _, value in shares],
            name="VEA.AX close (AUD/share)",
            mode="lines",
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in ratios],
            y=[float(value) for _, value in ratios],
            name="Normalized VEA / normalized Brent",
            mode="lines",
        ),
        secondary_y=True,
    )
    if ratios:
        median_ratio = median(value for _, value in ratios)
        figure.add_trace(
            go.Scatter(
                x=[shares[0][0], shares[-1][0]],
                y=[float(median_ratio)] * 2,
                name=f"Median normalized VEA / Brent ({median_ratio:.3f})",
                mode="lines",
                line={"dash": "dash"},
            ),
            secondary_y=True,
        )
        ratio_values = [value for _, value in ratios]
        for percentile, color in (
            (Decimal("0.15865"), "orange"),
            (Decimal("0.84135"), "orange"),
            (Decimal("0.35"), "green"),
            (Decimal("0.65"), "green"),
        ):
            threshold = _percentile(ratio_values, percentile)
            label = f"{percentile * 100:.3f}".rstrip("0").rstrip(".")
            figure.add_trace(
                go.Scatter(
                    x=[shares[0][0], shares[-1][0]],
                    y=[float(threshold)] * 2,
                    name=f"{label}th percentile ({threshold:.3f})",
                    mode="lines",
                    line={"color": color, "dash": "dash"},
                ),
                secondary_y=True,
            )
    figure.update_xaxes(title_text="Date")
    figure.update_yaxes(title_text="VEA close (AUD/share)", secondary_y=False)
    figure.update_yaxes(title_text="Normalized VEA / normalized Brent (unitless)", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def display_trade_results(
    shares: list[tuple[date, Decimal]],
    trades: list[Trade],
    open_buy_date: date | None,
    period_start: date,
    period_end: date,
) -> None:
    st.plotly_chart(trade_chart(shares, trades, open_buy_date), width="stretch")
    if trades:
        st.dataframe(
            [
                {
                    "Buy date": trade.buy_date,
                    "Buy price (AUD)": float(trade.buy_price),
                    "Sell date": trade.sell_date,
                    "Sell price (AUD)": float(trade.sell_price),
                    "Profit/loss (%)": float(trade.return_pct),
                    "Holding days": trade.holding_days,
                }
                for trade in trades
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No completed trades for this period.")
    win_rate, total_return = trade_summary(trades)
    utilization = fund_utilization(trades, open_buy_date, period_start, period_end)
    st.write(
        f"Completed trades: {len(trades)} | Win rate: {win_rate:.2f}% | "
        f"Total profit/loss (compounded): {total_return:+.2f}% | "
        f"Fund utilization: {utilization:.2f}%"
    )
    if open_buy_date is not None:
        st.caption("One position remains open; it is excluded from completed trade results.")


def main() -> None:
    st.set_page_config(page_title="VEA vs Brent crude", layout="wide")
    st.title("VEA vs Brent crude oil")
    st.caption(
        "VEA.AX daily closing share price (AUD, left axis) and Yahoo BZ=F Brent futures close "
        "(USD/barrel, right axis). Dates are shared; values are not currency-converted. "
        "This page reads stored PostgreSQL data only."
    )
    try:
        shares, oil, opens = load_prices(_engine())
    except Exception:
        st.error("Could not read PostgreSQL. Check DATABASE_URL and run Alembic migrations.")
        return
    if not shares:
        st.warning("No stored VEA.AX daily closes. Ingest VEA first.")
    if not oil:
        st.warning("No stored daily Brent prices. Download commodity prices first.")
    if not shares or not oil:
        return
    start = shares[0][0]
    end = shares[-1][0]
    overlapping_oil = [(day, value) for day, value in oil if start <= day <= end]
    if not overlapping_oil:
        st.warning("VEA and Brent crude have no overlapping date range.")
        return
    st.plotly_chart(comparison_chart(shares, overlapping_oil), width="stretch")
    average = average_vea_oil_ratio(shares, overlapping_oil)
    if average is None:
        st.warning("No dates have both a valid VEA close and Brent price for normalization.")
        return
    ratio, matched_days = average
    st.subheader("VEA vs normalized Brent crude")
    st.caption(
        "Normalized Brent = Brent price multiplied by mean(VEA close / Brent price) over "
        f"{matched_days:,} matching trading dates (factor: {ratio:.6f}). "
        "Both lines use one visual scale; this is not a currency conversion."
    )
    st.plotly_chart(normalized_chart(shares, overlapping_oil, ratio), width="stretch")
    st.subheader("VEA price and normalized VEA / normalized Brent")
    st.caption(
        "For matching dates, scale each price series independently to [0, 1] using "
        "(price - minimum) / (maximum - minimum) across the entire matched period. "
        "The original VEA close uses the left axis and normalized VEA divided by "
        "normalized Brent uses the right axis on the same graph, which autoscales to "
        "the computed ratios. The dashed line marks their median across all valid "
        "matching dates; orange dashed lines mark the 15.865th and 84.135th "
        "percentiles, and green dashed lines mark the 35th and 65th percentiles. "
        "Dates where normalized Brent is zero have no ratio; a constant "
        "series has no defined ratio."
    )
    ratios = _normalized_ratios(shares, overlapping_oil)
    st.plotly_chart(vea_per_crude_chart(shares, overlapping_oil, ratios), width="stretch")
    st.subheader("Distribution of normalized VEA / normalized Brent")
    st.caption(
        "Frequency of valid daily ratios across 20 equal-width buckets; the red dashed line "
        "marks the median, orange lines mark the 15.865th and 84.135th percentiles, "
        "and green lines mark the 35th and 65th percentiles."
    )
    st.plotly_chart(ratio_histogram(ratios), width="stretch")
    st.subheader("VEA trading strategy backtest")
    st.caption(
        "Buy at the next VEA trading day's open after a ratio strictly below its 15.865th "
        "percentile; sell at the next trading day's open after a ratio strictly above its "
        "35th percentile. Thresholds and normalization use the entire displayed history "
        "(look-ahead bias). One position at a time; signals require a matching Brent date. "
        "No fees, slippage, dividends, or corporate-action adjustments are included. "
        "Returns compound by reinvesting proceeds from each completed trade. "
        "The VEA close chart marks executed buys in red and sells in green. "
        "Fund utilization counts calendar days from buy through the day before sale "
        "(or through the period end for an open position), divided by all calendar days "
        "from the first to last VEA close, inclusive."
    )
    trades, open_buy_date = backtest_trades(shares, opens, ratios)
    display_trade_results(shares, trades, open_buy_date, start, end)
    st.subheader("VEA trading strategy 2 backtest")
    st.caption(
        "Buy at the next VEA trading day's open after a ratio strictly below its 15.865th "
        "percentile. The first ratio strictly above the 35th percentile after buying "
        "enters prepare-to-sell; from the next observation onward, a ratio strictly above "
        "the 65th percentile or strictly below the 35th percentile triggers a sale at the "
        "next trading day's open. Equality does not trigger a stage change or sale. "
        "One position at a time; signals require a matching Brent date. As in strategy 1, "
        "thresholds and normalization use the entire history (look-ahead bias); missing "
        "opening prices skip executions, and returns exclude fees, slippage, dividends, "
        "and corporate-action adjustments. The VEA close chart marks executed buys in red "
        "and sells in green."
    )
    staged_trades, staged_open_buy_date = backtest_trades(shares, opens, ratios, staged_sell=True)
    display_trade_results(shares, staged_trades, staged_open_buy_date, start, end)
    st.subheader("VEA trading strategy 3 backtest")
    st.caption(
        "Strategy 2 with two consecutive VEA trading-day ratios confirming every trigger: "
        "both strictly below the 15.865th percentile to buy, both strictly above "
        "the 35th percentile to prepare to sell, and then two new consecutive ratios "
        "both strictly above the 65th percentile or both strictly below the 35th "
        "percentile to sell. Execution is at the following VEA trading day's open. "
        "A missing ratio breaks confirmation; observations used to enter a stage "
        "cannot also confirm the next stage. The same look-ahead, missing-open, "
        "and return limitations as strategies 1 and 2 apply."
    )
    confirmed_trades, confirmed_open_buy_date = backtest_trades(
        shares, opens, ratios, staged_sell=True, confirm_two_days=True
    )
    display_trade_results(shares, confirmed_trades, confirmed_open_buy_date, start, end)


if __name__ == "__main__":
    main()
