"""Read-only, out-of-sample VEA/Brent spread opportunity dashboard."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import isfinite
from statistics import fmean, pstdev
from typing import cast

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import Engine, create_engine, select

from asx_financials.config import get_settings
from asx_financials.infrastructure.persistence.schema import commodity_prices, daily_prices
from asx_financials.vea_crude_refresh import BRENT_SOURCE, BRENT_SYMBOL

TRAIN_START = date(2018, 1, 1)
TRAIN_END = date(2023, 6, 30)
SIGNAL_START = date(2023, 7, 1)


@dataclass(frozen=True)
class SpreadModel:
    beta: float
    mean: float
    sigma: float
    observations: int


@dataclass(frozen=True)
class SpreadPoint:
    day: date
    vea: float
    brent: float
    spread: float
    z_score: float


@dataclass(frozen=True)
class Opportunity:
    day: date
    action: str
    direction: str
    z_score: float


@dataclass(frozen=True)
class HypotheticalTrade:
    entry: SpreadPoint
    exit: SpreadPoint

    @property
    def holding_days(self) -> int:
        return (self.exit.day - self.entry.day).days

    @property
    def vea_pnl(self) -> float:
        return self.exit.vea - self.entry.vea

    @property
    def vea_return_pct(self) -> float:
        return self.vea_pnl / self.entry.vea * 100


@st.cache_resource
def _engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def load_prices(
    engine: Engine,
) -> tuple[list[tuple[date, Decimal]], list[tuple[date, Decimal]]]:
    with engine.connect() as connection:
        shares = connection.execute(
            select(daily_prices.c.trading_date, daily_prices.c.close)
            .where(
                daily_prices.c.exchange_code == "ASX",
                daily_prices.c.ticker == "VEA",
                daily_prices.c.trading_date >= TRAIN_START,
                daily_prices.c.close.is_not(None),
            )
            .order_by(daily_prices.c.trading_date)
        ).all()
        oil = connection.execute(
            select(commodity_prices.c.observation_date, commodity_prices.c.price)
            .where(
                commodity_prices.c.source == BRENT_SOURCE,
                commodity_prices.c.series == BRENT_SYMBOL,
                commodity_prices.c.frequency == "daily",
                commodity_prices.c.observation_date >= TRAIN_START,
            )
            .order_by(commodity_prices.c.observation_date)
        ).all()
    return (
        [(cast(date, day), cast(Decimal, close)) for day, close in shares],
        [(cast(date, day), cast(Decimal, price)) for day, price in oil],
    )


def aligned_closes(
    shares: list[tuple[date, Decimal]], oil: list[tuple[date, Decimal]]
) -> list[tuple[date, float, float]]:
    oil_by_day = {day: price for day, price in oil if price.is_finite() and price > 0}
    return [
        (day, float(vea), float(oil_by_day[day]))
        for day, vea in shares
        if day in oil_by_day
        and vea.is_finite()
        and vea > 0
        and isfinite(float(vea))
        and isfinite(float(oil_by_day[day]))
    ]


def fit_model(pairs: list[tuple[date, float, float]]) -> SpreadModel | None:
    training = [
        (vea, brent)
        for day, vea, brent in pairs
        if TRAIN_START <= day <= TRAIN_END
        and isfinite(vea)
        and isfinite(brent)
        and vea > 0
        and brent > 0
    ]
    if len(training) < 2:
        return None
    vea_mean = fmean(vea for vea, _ in training)
    brent_mean = fmean(brent for _, brent in training)
    beta = vea_mean / brent_mean
    spreads = [vea - beta * brent for vea, brent in training]
    mean, sigma = fmean(spreads), pstdev(spreads)
    if not all(isfinite(value) for value in (beta, mean, sigma)) or sigma <= 1e-10 * max(
        1.0, abs(mean)
    ):
        return None
    return SpreadModel(beta, mean, sigma, len(training))


def score_closes(pairs: list[tuple[date, float, float]], model: SpreadModel) -> list[SpreadPoint]:
    points = []
    for day, vea, brent in pairs:
        if not (isfinite(vea) and isfinite(brent) and vea > 0 and brent > 0):
            continue
        spread = vea - model.beta * brent
        score = (spread - model.mean) / model.sigma
        if isfinite(spread) and isfinite(score):
            points.append(SpreadPoint(day, vea, brent, spread, score))
    return points


def find_opportunities(
    points: list[SpreadPoint], start: date = SIGNAL_START
) -> tuple[list[Opportunity], str | None]:
    """One signal-only position at a time; no fills or same-day reversals."""
    events: list[Opportunity] = []
    position: str | None = None
    for point in sorted(points, key=lambda p: p.day):
        if point.day < start or not isfinite(point.z_score):
            continue
        score = point.z_score
        if position is None and score < -1.5:
            position = "Long VEA"
            events.append(Opportunity(point.day, "Entry", position, score))
        elif position is not None and score > -0.5:
            events.append(Opportunity(point.day, "Exit", position, score))
            position = None
    return events, position


def pair_signals(points: list[SpreadPoint], events: list[Opportunity]) -> list[HypotheticalTrade]:
    by_day = {point.day: point for point in points}
    trades: list[HypotheticalTrade] = []
    entry: Opportunity | None = None
    for event in events:
        if event.action == "Entry":
            entry = event
        elif event.action == "Exit" and entry is not None:
            if entry.day in by_day and event.day in by_day:
                trades.append(HypotheticalTrade(by_day[entry.day], by_day[event.day]))
            entry = None
    return trades


def fund_utilization(
    trades: list[HypotheticalTrade],
    events: list[Opportunity],
    points: list[SpreadPoint],
    start: date = SIGNAL_START,
) -> float:
    """Calendar days held, including an unclosed signal through the last matched close."""
    post_training = [point.day for point in points if point.day >= start]
    if not post_training:
        return 0.0
    end = max(post_training)
    used_days = sum(trade.holding_days for trade in trades)
    if events and events[-1].action == "Entry":
        used_days += (end - events[-1].day).days + 1
    return used_days / ((end - start).days + 1) * 100


def closing_prices_chart(points: list[SpreadPoint]) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    days = [point.day for point in points]
    figure.add_trace(
        go.Scatter(
            x=days, y=[point.vea for point in points], name="VEA close (AUD/share)", mode="lines"
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=days,
            y=[point.brent for point in points],
            name="BZ=F close (USD/barrel)",
            mode="lines",
        ),
        secondary_y=True,
    )
    figure.update_xaxes(title_text="Matched closing date")
    figure.update_yaxes(title_text="VEA close (AUD/share)", secondary_y=False)
    figure.update_yaxes(title_text="BZ=F close (USD/barrel)", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=550)
    return figure


def price_chart(points: list[SpreadPoint], events: list[Opportunity]) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    days = [point.day for point in points]
    figure.add_trace(
        go.Scatter(
            x=days,
            y=[point.vea for point in points],
            name="VEA close (AUD/share)",
            mode="lines",
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=days,
            y=[point.z_score for point in points],
            name="Fixed-reference Z-score",
            mode="lines",
        ),
        secondary_y=True,
    )
    if days:
        for threshold, name in [
            (-1.5, "Buy VEA: Z < -1.5"),
            (-0.5, "Sell VEA: Z > -0.5"),
        ]:
            figure.add_trace(
                go.Scatter(
                    x=[days[0], days[-1]],
                    y=[threshold] * 2,
                    name=name,
                    mode="lines",
                    line={"dash": "dot"},
                ),
                secondary_y=True,
            )
    figure.add_vrect(
        x0=TRAIN_START.isoformat(),
        x1=TRAIN_END.isoformat(),
        fillcolor="grey",
        opacity=0.1,
        line_width=0,
    )
    for event in events:
        figure.add_vline(
            x=event.day.isoformat(),
            line_color="red" if event.action == "Entry" else "green",
            line_dash="dot",
        )
    figure.update_xaxes(title_text="Matched closing date")
    figure.update_yaxes(title_text="VEA close (AUD/share)", secondary_y=False)
    figure.update_yaxes(title_text="Z-score", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def histogram(points: list[SpreadPoint], model: SpreadModel) -> go.Figure:
    training = [point.spread for point in points if TRAIN_START <= point.day <= TRAIN_END]
    figure = go.Figure()
    if training:
        figure.add_trace(go.Histogram(x=training, nbinsx=20, name="Training spread"))
        for offset, label in [
            (0, "Training mean"),
            (-1.5, "Buy VEA"),
            (-0.5, "Sell VEA"),
        ]:
            figure.add_vline(
                x=model.mean + offset * model.sigma,
                line_color="red" if offset == 0 else "orange",
                line_dash="dot",
                annotation_text=label,
            )
    figure.update_xaxes(title_text="Training spread (VEA AUD/share - beta * Brent USD/barrel)")
    figure.update_yaxes(title_text="Matched trading dates")
    figure.update_layout(height=400)
    return figure


def main() -> None:
    st.set_page_config(page_title="VEA / Brent spread opportunities", layout="wide")
    st.title("VEA / BZ=F spread opportunities")
    st.caption(
        "Set beta = mean VEA close / mean Brent close using positive, finite, "
        "date-matched daily closes from 2018-01-01 through 2023-06-30 only. "
        "Spread = VEA close - beta * Brent close; its training mean is zero (up to "
        "floating-point rounding). Training population SD remains fixed for scores "
        "from July 2023 onward. "
        "VEA is AUD/share and Brent futures is USD/barrel: the spread and Z-score "
        "are statistical signals only. Hypothetical trades hold VEA alone, not Brent. "
        "Stored closes only; no live fetches or executable fills."
    )
    try:
        shares, oil = load_prices(_engine())
    except Exception:
        st.error("Could not read PostgreSQL. Check DATABASE_URL and run Alembic migrations.")
        return
    if not shares or not oil:
        st.warning("Missing VEA or BZ=F closes. Ingest VEA and download commodities first.")
        return
    pairs = aligned_closes(shares, oil)
    model = fit_model(pairs)
    if model is None:
        st.warning("Training requires at least two matched closes and a nonconstant spread.")
        return
    points = score_closes(pairs, model)
    events, position = find_opportunities(points)
    st.write(
        f"Training pairs: {model.observations} | beta: {model.beta:.6f} | "
        f"training spread mean: {model.mean:.6f} | population SD: {model.sigma:.6f}"
    )
    st.subheader("VEA and BZ=F closing prices")
    st.caption(
        "Matched closing dates only. VEA (AUD/share) uses the left axis; "
        "BZ=F Brent futures (USD/barrel) uses the right axis. "
        "Brent is a signal input, not a traded position."
    )
    st.plotly_chart(closing_prices_chart(points), width="stretch")
    st.subheader("VEA close and fixed-reference spread Z-score")
    st.caption(
        "Left axis: VEA closing price on matched dates. Right axis: Z-score of "
        "VEA - beta * Brent against the 2018-June 2023 training reference. "
        "Signals use the Z-score; only VEA is hypothetically traded."
    )
    st.plotly_chart(price_chart(points, events), width="stretch")
    st.subheader("2018-June 2023 training spread distribution")
    st.plotly_chart(histogram(points, model), width="stretch")
    st.subheader("Signals from July 2023")
    st.caption(
        "Strict Z < -1.5 signals buy one VEA share; Z > -0.5 signals sell. "
        "Long-only: no short-selling or Brent trades. Brent is used only to "
        "calculate the Z-score. Only one position at a time; no same-day "
        "reversal. Entry and exit are "
        "paired using their signal-day VEA closes as hypothetical prices. "
        "A close-triggered order cannot fill at the same close: this is a "
        "look-ahead-biased, non-executable illustration, not realized P&L. "
        "P/L is the VEA exit minus entry close per share in AUD; percentage "
        "return divides by the VEA entry close. No fees, slippage, dividends "
        "or corporate actions are included."
    )
    trades = pair_signals(points, events)
    if trades:
        st.dataframe(
            [
                {
                    "Buy signal date": trade.entry.day,
                    "VEA entry close (AUD/share)": trade.entry.vea,
                    "Entry Z-score": trade.entry.z_score,
                    "Sell signal date": trade.exit.day,
                    "VEA exit close (AUD/share)": trade.exit.vea,
                    "Exit Z-score": trade.exit.z_score,
                    "VEA P/L (AUD/share)": trade.vea_pnl,
                    "VEA return (%)": trade.vea_return_pct,
                    "Holding days (calendar)": trade.holding_days,
                }
                for trade in trades
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No completed hypothetical signal pairs since July 2023.")
    st.write(
        f"Completed pairs: {len(trades)} | "
        f"Total VEA P/L (AUD per share): {sum(trade.vea_pnl for trade in trades):+.4f} | "
        f"Fund utilization (calendar days): {fund_utilization(trades, events, points):.2f}%"
    )
    st.caption(
        "Utilization is days held from entry inclusive to exit exclusive, including "
        "an open signal through the latest matched close, divided by calendar days "
        "from 2023-07-01 through that close. Total VEA P/L assumes one share "
        "per completed trade and excludes open positions; it is not realized P/L."
    )
    if position:
        st.info(
            f"Unclosed hypothetical {position} signal since {events[-1].day}; excluded from P/L."
        )


if __name__ == "__main__":
    main()
