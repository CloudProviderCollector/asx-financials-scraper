"""Read-only VGN/Brent spread analysis with a fixed historical reference."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import floor, fsum, isfinite
from statistics import pstdev
from typing import cast

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import Engine, create_engine, or_, select

from asx_financials.config import get_settings
from asx_financials.infrastructure.persistence.schema import commodity_prices, daily_prices
from asx_financials.vea_crude_refresh import BRENT_SOURCE, BRENT_SYMBOL

REFERENCE_START = date(2018, 7, 1)
REFERENCE_END = date(2023, 7, 31)


@dataclass(frozen=True)
class SpreadPoint:
    day: date
    spread: float
    z_score: float


@dataclass(frozen=True)
class SpreadReference:
    beta: float
    mean: float
    sigma: float
    observations: int
    spreads: list[float]


@dataclass(frozen=True)
class Trade:
    buy_signal_date: date
    buy_date: date
    buy_price: Decimal
    sell_signal_date: date
    sell_date: date
    sell_price: Decimal

    @property
    def return_pct(self) -> Decimal:
        return (self.sell_price / self.buy_price - 1) * 100


@st.cache_resource
def _engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def load_prices(
    engine: Engine,
) -> tuple[list[tuple[date, Decimal]], list[tuple[date, Decimal]], dict[date, Decimal]]:
    with engine.connect() as connection:
        shares = connection.execute(
            select(daily_prices.c.trading_date, daily_prices.c.close, daily_prices.c.open)
            .where(
                daily_prices.c.exchange_code == "ASX",
                daily_prices.c.ticker == "VGN",
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
            cast(date, day): cast(Decimal, opening)
            for day, _, opening in shares
            if opening is not None
            and cast(Decimal, opening).is_finite()
            and cast(Decimal, opening) > 0
        },
    )


def aligned_closes(
    shares: list[tuple[date, Decimal]], oil: list[tuple[date, Decimal]]
) -> list[tuple[date, float, float]]:
    by_date = dict(oil)
    return [
        (day, float(close), float(by_date[day]))
        for day, close in shares
        if day in by_date
        and close.is_finite()
        and by_date[day].is_finite()
        and close > 0
        and by_date[day] > 0
    ]


def fit_reference(aligned: list[tuple[date, float, float]]) -> SpreadReference | None:
    """Use VGN-on-Brent OLS with an intercept, then standardize the raw hedged spread."""
    reference = [
        (vgn, brent) for day, vgn, brent in aligned if REFERENCE_START <= day <= REFERENCE_END
    ]
    if len(reference) < 2:
        return None
    mean_vgn = fsum(vgn for vgn, _ in reference) / len(reference)
    mean_brent = fsum(brent for _, brent in reference) / len(reference)
    variance = fsum((brent - mean_brent) ** 2 for _, brent in reference)
    if not isfinite(variance) or variance <= 1e-12 * max(1.0, mean_brent**2):
        return None
    beta = fsum((brent - mean_brent) * (vgn - mean_vgn) for vgn, brent in reference) / variance
    spreads = [vgn - beta * brent for vgn, brent in reference]
    mean = fsum(spreads) / len(spreads)
    sigma = pstdev(spreads)
    if not all(isfinite(value) for value in (beta, mean, sigma)) or sigma <= 1e-10 * max(
        1.0, abs(mean)
    ):
        return None
    return SpreadReference(beta, mean, sigma, len(reference), spreads)


def spread_scores(
    aligned: list[tuple[date, float, float]], reference: SpreadReference
) -> list[SpreadPoint]:
    return [
        SpreadPoint(day, spread, (spread - reference.mean) / reference.sigma)
        for day, vgn, brent in aligned
        if isfinite(spread := vgn - reference.beta * brent)
        and isfinite((spread - reference.mean) / reference.sigma)
    ]


def price_chart(shares: list[tuple[date, Decimal]], oil: list[tuple[date, Decimal]]) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in shares],
            y=[float(value) for _, value in shares],
            name="VGN.AX close (AUD/share)",
            mode="lines",
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in oil],
            y=[float(value) for _, value in oil],
            name="BZ=F close (USD/barrel)",
            mode="lines",
        ),
        secondary_y=True,
    )
    figure.update_xaxes(title_text="Trading date")
    figure.update_yaxes(title_text="VGN close (AUD/share)", secondary_y=False)
    figure.update_yaxes(title_text="Brent futures close (USD/barrel)", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def spread_chart(points: list[SpreadPoint]) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Scatter(
            x=[p.day for p in points],
            y=[p.spread for p in points],
            name="VGN - beta * BZ=F spread",
            mode="lines",
            connectgaps=False,
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=[p.day for p in points],
            y=[p.z_score for p in points],
            name="Reference spread Z-score",
            mode="lines",
            connectgaps=False,
        ),
        secondary_y=True,
    )
    for threshold, name, color in [
        (-1.5, "Buy below -1.5", "red"),
        (-0.5, "Sell above -0.5", "green"),
    ]:
        figure.add_hline(
            y=threshold, line_color=color, line_dash="dot", annotation_text=name, secondary_y=True
        )
    figure.add_vline(x=REFERENCE_END.isoformat(), line_dash="dash", line_color="gray")
    figure.update_xaxes(title_text="Trading date")
    figure.update_yaxes(
        title_text="VGN - beta * Brent (AUD/share scale; not FX-adjusted)", secondary_y=False
    )
    figure.update_yaxes(title_text="Reference spread Z-score", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def spread_histogram(reference: SpreadReference) -> go.Figure:
    minimum, maximum = min(reference.spreads), max(reference.spreads)
    width = (maximum - minimum) / 20
    counts = [0] * 20
    for value in reference.spreads:
        counts[min(floor((value - minimum) / width), 19)] += 1
    figure = go.Figure(
        go.Bar(
            x=[minimum + (index + 0.5) * width for index in range(20)],
            y=counts,
            width=width,
            name="Reference raw spread",
        )
    )
    for label, threshold, color in [
        ("Reference mean", reference.mean, "red"),
        ("Buy: mean - 1.5 SD", reference.mean - 1.5 * reference.sigma, "orange"),
        ("Sell: mean - 0.5 SD", reference.mean - 0.5 * reference.sigma, "green"),
    ]:
        figure.add_trace(
            go.Scatter(
                x=[threshold, threshold],
                y=[0, max(counts)],
                name=label,
                mode="lines",
                line={"color": color, "dash": "dot"},
            )
        )
    figure.update_xaxes(title_text="VGN close - beta * BZ=F close (mixed-unit spread)")
    figure.update_yaxes(title_text="Reference trading dates (count)")
    figure.update_layout(height=650, bargap=0)
    return figure


def backtest_trades(
    shares: list[tuple[date, Decimal]], opens: dict[date, Decimal], points: list[SpreadPoint]
) -> tuple[list[Trade], tuple[date, date] | None]:
    """Execute post-reference close signals at the next VGN trading session's open."""
    trades: list[Trade] = []
    position: tuple[date, date, Decimal] | None = None
    pending: tuple[str, date] | None = None
    signals = {p.day: p.z_score for p in points if p.day > REFERENCE_END}
    if not shares:
        return [], None
    days = sorted(
        {day for day, _ in shares} | {day for day in opens if REFERENCE_END < day <= shares[-1][0]}
    )
    for day in days:
        if day <= REFERENCE_END:
            continue
        if pending is not None:
            action, signal_date = pending
            pending = None
            opening = opens.get(day)
            if opening is not None and opening.is_finite() and opening > 0:
                if action == "buy":
                    position = signal_date, day, opening
                elif position is not None:
                    buy_signal, buy_day, buy_price = position
                    trades.append(Trade(buy_signal, buy_day, buy_price, signal_date, day, opening))
                    position = None
        score = signals.get(day)
        if score is not None and position is None and score < -1.5:
            pending = "buy", day
        elif score is not None and position is not None and score > -0.5:
            pending = "sell", day
    return trades, (position[0], position[1]) if position is not None else None


def trade_chart(
    shares: list[tuple[date, Decimal]], trades: list[Trade], open_buy: tuple[date, date] | None
) -> go.Figure:
    figure = go.Figure(
        go.Scatter(
            x=[day for day, _ in shares],
            y=[float(close) for _, close in shares],
            name="VGN.AX close",
            mode="lines",
        )
    )
    for trade in trades:
        figure.add_vline(x=trade.buy_date.isoformat(), line_color="red", line_dash="dot")
        figure.add_vline(x=trade.sell_date.isoformat(), line_color="green", line_dash="dot")
    if open_buy is not None:
        figure.add_vline(x=open_buy[1].isoformat(), line_color="red", line_dash="dot")
    figure.update_xaxes(title_text="Trading date")
    figure.update_yaxes(title_text="VGN close (AUD/share)")
    figure.update_layout(height=650)
    return figure


def main() -> None:
    st.set_page_config(page_title="VGN vs Brent spread", layout="wide")
    st.title("VGN vs BZ=F Brent futures spread")
    st.caption(
        "Stored unadjusted VGN.AX closes (AUD/share) and Yahoo BZ=F futures closes "
        "(USD/barrel). Reads PostgreSQL only; no live fetch or currency conversion."
    )
    try:
        shares, oil, opens = load_prices(_engine())
    except Exception:
        st.error("Could not read PostgreSQL. Check DATABASE_URL and run Alembic migrations.")
        return
    if not shares:
        st.warning("No stored VGN.AX daily closes. Ingest VGN first.")
    if not oil:
        st.warning("No stored BZ=F daily futures closes. Download commodity prices first.")
    if not shares or not oil:
        return
    oil = [(day, value) for day, value in oil if shares[0][0] <= day <= shares[-1][0]]
    st.plotly_chart(price_chart(shares, oil), width="stretch")
    aligned = aligned_closes(shares, oil)
    reference = fit_reference(aligned)
    if reference is None:
        st.warning(
            "Insufficient overlapping, varying positive closes from July 2018 "
            "through July 2023 to fit the spread reference."
        )
        return
    st.subheader("Fixed historical spread reference")
    st.caption(
        "Fit one VGN-on-Brent OLS slope WITH an intercept on matching positive finite "
        "closes from 1 July 2018 through 31 July 2023 inclusive. The raw spread is "
        "VGN close - beta * Brent close (intercept not subtracted). Its reference-period "
        "mean and population SD define Z-scores for all matched dates, including after "
        "July 2023. No future observations enter the fixed reference for post-period scores. "
        "The units differ (AUD/share versus USD/barrel): beta is a statistical scaling "
        "factor, not a currency conversion or tradable hedge. In-sample scores use the "
        "full reference period and therefore contain look-ahead bias."
    )
    st.write(
        f"Reference: {reference.observations:,} matched closes | beta: {reference.beta:.6f} | "
        f"spread mean: {reference.mean:.6f} | population SD: {reference.sigma:.6f}"
    )
    points = spread_scores(aligned, reference)
    st.plotly_chart(spread_chart(points), width="stretch")
    st.subheader("July 2018-July 2023 raw spread distribution")
    st.plotly_chart(spread_histogram(reference), width="stretch")
    st.subheader("Post-reference VGN-only strategy backtest")
    st.caption(
        "After 31 July 2023, buy one VGN share when Z < -1.5 and sell when Z > -0.5; "
        "strict thresholds. Orders execute at the next VGN trading session's positive "
        "open; a missing/invalid open cancels an order. Only matching VGN/Brent closes "
        "signal, and only one position is held. Brent is NOT traded. Returns exclude "
        "fees, slippage, dividends and corporate-action adjustments; completed trades "
        "only are compounded. Red/green markers show executed buys/sells."
    )
    post_shares = [(day, close) for day, close in shares if day > REFERENCE_END]
    if not post_shares:
        st.info("No VGN trading dates after the July 2023 reference period.")
        return
    trades, open_buy = backtest_trades(shares, opens, points)
    st.plotly_chart(trade_chart(post_shares, trades, open_buy), width="stretch")
    if trades:
        st.dataframe(
            [
                {
                    "Buy signal": trade.buy_signal_date,
                    "Buy date": trade.buy_date,
                    "Buy open (AUD)": float(trade.buy_price),
                    "Sell signal": trade.sell_signal_date,
                    "Sell date": trade.sell_date,
                    "Sell open (AUD)": float(trade.sell_price),
                    "Profit/loss (%)": float(trade.return_pct),
                }
                for trade in trades
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No completed trades after the reference period.")
    growth = Decimal(1)
    for trade in trades:
        growth *= trade.sell_price / trade.buy_price
    st.write(f"Completed trades: {len(trades)} | Compounded return: {(growth - 1) * 100:+.2f}%")
    if open_buy is not None:
        st.caption("One position remains open and is excluded from completed returns.")


if __name__ == "__main__":
    main()
