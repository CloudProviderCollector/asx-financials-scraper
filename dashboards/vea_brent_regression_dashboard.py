"""Compare VEA closes with Brent-based linear regression estimates."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import isfinite
from statistics import StatisticsError, fmean, linear_regression, pstdev
from typing import cast

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import Engine, create_engine, select

from asx_financials.config import get_settings
from asx_financials.infrastructure.persistence.schema import commodity_prices, daily_prices
from asx_financials.vea_crude_refresh import BRENT_SOURCE, BRENT_SYMBOL
from dashboards.vea_brent_spread_dashboard import (
    Opportunity,
    SpreadPoint,
    find_opportunities,
    fund_utilization,
    pair_signals,
)

TRAIN_START = date(2018, 7, 1)
TRAIN_END = date(2023, 6, 30)


@dataclass(frozen=True)
class RegressionModel:
    a: float
    b: float
    observations: int


@st.cache_resource
def _engine() -> Engine:
    return create_engine(
        get_settings().database_url, pool_pre_ping=True, connect_args={"connect_timeout": 5}
    )


def load_prices(
    engine: Engine,
) -> tuple[list[tuple[date, Decimal]], list[tuple[date, Decimal]]]:
    with engine.connect() as connection:
        shares = connection.execute(
            select(daily_prices.c.trading_date, daily_prices.c.close)
            .where(
                daily_prices.c.exchange_code == "ASX",
                daily_prices.c.ticker == "VEA",
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


def fit_regression(
    pairs: list[tuple[date, float, float]],
    start: date | None = None,
    end: date | None = None,
) -> RegressionModel | None:
    training = [
        (vea, brent)
        for day, vea, brent in pairs
        if (start is None or day >= start)
        and (end is None or day <= end)
        and isfinite(vea)
        and isfinite(brent)
        and vea > 0
        and brent > 0
    ]
    if len(training) < 2:
        return None
    try:
        result = linear_regression([brent for _, brent in training], [vea for vea, _ in training])
    except StatisticsError:
        return None
    if not (isfinite(result.slope) and isfinite(result.intercept)):
        return None
    return RegressionModel(result.slope, result.intercept, len(training))


def regression_chart(pairs: list[tuple[date, float, float]], model: RegressionModel) -> go.Figure:
    valid = [
        (day, vea, estimate)
        for day, vea, brent in pairs
        if isfinite(vea) and isfinite(brent) and isfinite(estimate := model.a * brent + model.b)
    ]
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=[day for day, _, _ in valid],
            y=[vea for _, vea, _ in valid],
            name="VEA close",
            mode="lines",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=[day for day, _, _ in valid],
            y=[estimate for _, _, estimate in valid],
            name="Estimated VEA",
            mode="lines",
        )
    )
    figure.update_xaxes(title_text="Matched closing date")
    figure.update_yaxes(title_text="VEA (AUD/share)")
    figure.update_layout(hovermode="x unified", height=550)
    return figure


def residual_histogram(
    pairs: list[tuple[date, float, float]],
    model: RegressionModel,
    start: date | None = None,
    end: date | None = None,
) -> go.Figure:
    residuals = [
        vea - estimate
        for day, vea, brent in pairs
        if (start is None or day >= start)
        and (end is None or day <= end)
        and isfinite(vea)
        and isfinite(brent)
        and isfinite(estimate := model.a * brent + model.b)
        and isfinite(vea - estimate)
    ]
    figure = go.Figure()
    if residuals:
        mean, sigma = fmean(residuals), pstdev(residuals)
        lower, upper = min(residuals), max(residuals)
        if lower == upper:
            lower -= 0.5
            upper += 0.5
        figure.add_trace(
            go.Histogram(
                x=residuals,
                xbins=dict(start=lower, end=upper, size=(upper - lower) / 20),
                name="VEA - estimated VEA",
            )
        )
        for offset in (0, -0.5, 0.5, -1.5, 1.5):
            value = mean + offset * sigma
            label = "Mean" if offset == 0 else f"{offset:+g} sigma"
            figure.add_vline(
                x=value,
                line_color="red" if offset == 0 else "orange",
                line_dash="dot",
                annotation_text=f"{label}: {value:+.4f}",
            )
    figure.update_xaxes(title_text="VEA - estimated VEA (AUD/share)")
    figure.update_yaxes(title_text="Matched trading dates")
    figure.update_layout(height=400)
    return figure


def score_residuals(
    pairs: list[tuple[date, float, float]],
    model: RegressionModel,
    start: date | None = TRAIN_START,
    end: date | None = TRAIN_END,
) -> tuple[list[SpreadPoint], float] | None:
    reference_residuals = [
        vea - (model.a * brent + model.b)
        for day, vea, brent in pairs
        if (start is None or day >= start)
        and (end is None or day <= end)
        and isfinite(vea)
        and isfinite(brent)
        and vea > 0
        and brent > 0
    ]
    if len(reference_residuals) < 2 or not all(isfinite(x) for x in reference_residuals):
        return None
    mean, sigma = fmean(reference_residuals), pstdev(reference_residuals)
    if not isfinite(sigma) or sigma <= 1e-10 * max(1.0, abs(mean)):
        return None
    points = []
    for day, vea, brent in pairs:
        if not (isfinite(vea) and isfinite(brent) and vea > 0 and brent > 0):
            continue
        residual = vea - (model.a * brent + model.b)
        score = (residual - mean) / sigma
        if isfinite(residual) and isfinite(score):
            points.append(SpreadPoint(day, vea, brent, residual, score))
    return points, sigma


def trade_chart(
    points: list[SpreadPoint], events: list[Opportunity], show_training_window: bool = True
) -> go.Figure:
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
            y=[point.z_score for point in points],
            name="Fixed-reference residual Z-score",
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
    if show_training_window:
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


def main() -> None:
    st.set_page_config(page_title="VEA, Brent linear regression", layout="wide")
    st.title("VEA, Brent linear regression")
    st.caption(
        "Ordinary least-squares fit: VEA close (AUD/share) = a * BZ=F Brent futures "
        "close (USD/barrel) + b. Uses positive, finite closes on matching dates only; "
        "no currency conversion or forward filling. Stored data only."
    )
    try:
        shares, oil = load_prices(_engine())
    except Exception:
        st.error("Could not read PostgreSQL. Check DATABASE_URL and run Alembic migrations.")
        return
    pairs = aligned_closes(shares, oil)
    if not pairs:
        st.warning(
            "Missing matched VEA and BZ=F closes. Ingest VEA and download commodities first."
        )
        return

    for title, start, end in [
        ("Fit: July 2018-June 2023", TRAIN_START, TRAIN_END),
        ("Fit: all available data", None, None),
    ]:
        st.subheader(title)
        model = fit_regression(pairs, start, end)
        if model is None:
            st.warning("At least two matched closes with varying Brent prices are required.")
            continue
        st.write(
            f"VEA = {model.a:.6f} * Brent + {model.b:.6f} "
            f"| Fitting observations: {model.observations}"
        )
        st.caption(
            "Both series show all available matched dates; the first fit uses only "
            "July 2018-June 2023 observations, so later estimates are out of sample."
            if start is not None
            else "Both the fit and the displayed series use all available matched dates."
        )
        st.plotly_chart(regression_chart(pairs, model), width="stretch")
        st.caption(
            "Residual distribution for the fitting period only; dashed lines mark "
            "the mean and +/-0.5 and +/-1.5 population standard deviations."
        )
        st.plotly_chart(residual_histogram(pairs, model, start, end), width="stretch")

        scored = score_residuals(pairs, model, start, end)
        if scored is None:
            st.warning(
                "Trading signals require a nonconstant training residual."
                if start is not None
                else "Trading signals require a nonconstant all-data residual."
            )
            continue
        points, sigma = scored
        simulation_start = min(point.day for point in points)
        events, position = find_opportunities(points, start=simulation_start)
        trades = pair_signals(points, events)
        reference = "July 2018-June 2023" if start is not None else "All-data"
        st.subheader(f"VEA close and {reference} residual Z-score")
        st.caption(
            f"{reference} residual population SD: {sigma:.6f} AUD/share. "
            "Z = (VEA - (a * Brent + b) - fitting-period residual mean) / "
            "fitting-period SD. The coefficients and reference stay fixed for all dates. "
            "Red lines mark buy signals; green lines mark sell signals."
        )
        st.plotly_chart(
            trade_chart(points, events, show_training_window=start is not None), width="stretch"
        )
        st.subheader(f"{reference} fit: signals across all available matched dates")
        st.caption(
            "Strict Z < -1.5 signals buy one VEA share; Z > -0.5 signals sell. "
            "Long-only, one position at a time, no same-day reversal or Brent trades. "
            "Signals start at the first valid matched close. "
            + (
                "Before July 2023, this fixed 2018-2023 fit uses future observations "
                "relative to signal dates, so those trades are in-sample and look-ahead-biased. "
                if start is not None
                else "The full-data fit uses future observations relative to earlier signal "
                "dates, so historical trades are in-sample and look-ahead-biased. "
            )
            + "Entry and exit use "
            "signal-day VEA closes as hypothetical prices. A close-triggered order "
            "cannot fill at the same close: results are not executable or realized "
            "P/L. No fees, slippage, dividends, or corporate actions are included."
        )
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
            st.info("No completed hypothetical signal pairs across the matched history.")
        st.write(
            f"Completed pairs: {len(trades)} | "
            f"Total VEA P/L (AUD per share): {sum(trade.vea_pnl for trade in trades):+.4f} | "
            f"Fund utilization (calendar days): "
            f"{fund_utilization(trades, events, points, start=simulation_start):.2f}%"
        )
        st.caption(
            "Utilization includes closed holdings and any open position through the last "
            "matched close, divided by calendar days from the first valid matched close. "
            "Total P/L assumes one VEA share per completed trade and excludes open positions."
        )
        if position:
            st.info(
                f"Unclosed hypothetical {position} signal since {events[-1].day}; "
                "excluded from P/L."
            )


if __name__ == "__main__":
    main()
