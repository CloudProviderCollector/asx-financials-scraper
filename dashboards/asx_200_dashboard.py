"""Read-only dashboard for collected S&P/ASX 200 snapshots."""

import json
from datetime import date
from decimal import Decimal
from typing import Any

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import Engine, create_engine, text

from asx_financials.config import get_settings


@st.cache_resource
def _engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def load_snapshots(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text("SELECT * FROM asx_200_snapshots ORDER BY session_date DESC")
            ).mappings()
        ]


def history_chart(snapshots: list[dict[str, Any]]) -> go.Figure:
    ordered = list(reversed(snapshots))
    days = [row["session_date"] for row in ordered]
    figure = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06, row_heights=[0.7, 0.3]
    )
    figure.add_trace(
        go.Scatter(
            x=days,
            y=[float(row["close"]) if row["close"] is not None else None for row in ordered],
            name="Close (points)",
            mode="lines+markers",
        ),
        row=1,
        col=1,
    )
    for field, label in (
        ("volume", "Reported volume"),
        ("average_volume", "Average volume"),
        ("average_volume_10days", "10-day average volume"),
    ):
        figure.add_trace(
            go.Scatter(
                x=days,
                y=[row[field] for row in ordered],
                name=label,
                mode="lines+markers",
            ),
            row=2,
            col=1,
        )
    figure.update_yaxes(title_text="Index points", row=1, col=1)
    figure.update_yaxes(title_text="Yahoo volume units", row=2, col=1)
    figure.update_xaxes(title_text="Session date", row=2, col=1)
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def _display(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (int, float, Decimal)):
        return f"{value:,}"
    return str(value)


def show_snapshots(snapshots: list[dict[str, Any]]) -> None:
    if not snapshots:
        st.info("No ASX 200 snapshots have been collected yet.")
        return

    latest = snapshots[0]
    st.caption(
        f"Latest stored session: {latest['session_date']} | "
        f"Fetched: {latest['fetched_at']} | {len(snapshots)} stored session(s)"
    )
    columns = st.columns(5)
    for column, (label, value) in zip(
        columns,
        (
            ("Close (points)", latest["close"]),
            ("Reported volume", latest["volume"]),
            ("Average volume", latest["average_volume"]),
            ("10-day average volume", latest["average_volume_10days"]),
            ("Trailing P/E", latest["trailing_pe"]),
        ),
        strict=True,
    ):
        column.metric(label, _display(value))

    st.plotly_chart(history_chart(snapshots), width="stretch")
    st.caption(
        "Volume and averages are Yahoo-reported index figures, not shares in a fund. "
        "Missing values (including P/E) are not estimated; quote metadata is captured "
        "at the time of each snapshot."
    )
    st.subheader("All stored snapshots")
    st.dataframe(
        [{**row, "info": json.dumps(row["info"], sort_keys=True)} for row in snapshots],
        hide_index=True,
        width="stretch",
    )
    by_date: dict[date, dict[str, Any]] = {row["session_date"]: row for row in snapshots}
    selected = st.selectbox("Yahoo metadata captured on", list(by_date))
    st.json(by_date[selected]["info"])


def main() -> None:
    st.set_page_config(page_title="S&P/ASX 200", layout="wide")
    st.title("S&P/ASX 200 (^AXJO)")
    show_snapshots(load_snapshots(_engine()))


if __name__ == "__main__":
    main()
