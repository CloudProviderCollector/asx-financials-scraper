"""Read-only Streamlit comparison of VEA shares and Brent crude oil."""

from datetime import date
from decimal import Decimal
from statistics import median
from typing import cast

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import Engine, create_engine, select

from asx_financials.config import get_settings
from asx_financials.infrastructure.persistence.schema import commodity_prices, daily_prices


@st.cache_resource
def _engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def load_prices(engine: Engine) -> tuple[list[tuple[date, Decimal]], list[tuple[date, Decimal]]]:
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
                commodity_prices.c.source == "FRED/EIA",
                commodity_prices.c.series == "DCOILBRENTEU",
                commodity_prices.c.frequency == "daily",
            )
            .order_by(commodity_prices.c.observation_date)
        ).all()
    return (
        [(cast(date, day), cast(Decimal, value)) for day, value in shares],
        [(cast(date, day), cast(Decimal, value)) for day, value in oil],
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


def vea_per_crude_chart(
    shares: list[tuple[date, Decimal]], oil: list[tuple[date, Decimal]]
) -> go.Figure:
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
    figure.update_xaxes(title_text="Date")
    figure.update_yaxes(title_text="VEA close (AUD/share)", secondary_y=False)
    figure.update_yaxes(title_text="Normalized VEA / normalized Brent (unitless)", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def main() -> None:
    st.set_page_config(page_title="VEA vs Brent crude", layout="wide")
    st.title("VEA vs Brent crude oil")
    st.caption(
        "VEA.AX daily closing share price (AUD, left axis) and FRED/EIA Brent crude spot "
        "(USD/barrel, right axis). Dates are shared; values are not currency-converted. "
        "This page reads stored PostgreSQL data only."
    )
    try:
        shares, oil = load_prices(_engine())
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
        "matching dates. Dates where normalized Brent is zero have no ratio; a constant "
        "series has no defined ratio."
    )
    st.plotly_chart(vea_per_crude_chart(shares, overlapping_oil), width="stretch")


if __name__ == "__main__":
    main()
