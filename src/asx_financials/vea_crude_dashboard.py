"""Read-only Streamlit comparison of VEA shares and Brent crude oil."""

from datetime import date
from decimal import Decimal
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


if __name__ == "__main__":
    main()
