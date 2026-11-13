"""Point-in-time valuation dashboard for the supplied ASX 300 roster."""

import csv
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import Engine, create_engine, select

from asx_financials.config import get_settings
from asx_financials.infrastructure.persistence.schema import raw_source_payloads

ROSTER = Path(__file__).resolve().parents[1] / "data" / "asx300" / "asx300_20260410.csv"
SYDNEY = ZoneInfo("Australia/Sydney")


@st.cache_resource
def _engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def load_roster(path: Path = ROSTER) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or not {"ticker", "name"}.issubset(reader.fieldnames):
            raise ValueError("ASX 300 roster must contain ticker and name columns")
        rows = list(reader)
        roster = {row["ticker"]: row for row in rows}
    if (
        len(rows) != 300
        or len(roster) != 300
        or any(not ticker or not row["name"] for ticker, row in roster.items())
    ):
        raise ValueError("ASX 300 roster must contain 300 distinct named tickers")
    return roster


def load_profiles(engine: Engine, roster: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    source = raw_source_payloads
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                select(source.c.ticker, source.c.fetched_at, source.c.payload)
                .where(
                    source.c.exchange_code == "ASX",
                    source.c.segment_name == "company-profile",
                    source.c.ticker.in_(roster),
                )
                .order_by(source.c.fetched_at)
            ).mappings()
        ]


def _positive(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number > 0 else None


def summarize(
    profiles: list[dict[str, Any]], roster: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[date, list[dict[str, Any]]]]:
    daily: dict[date, dict[str, dict[str, Any]]] = defaultdict(dict)
    for profile in profiles:
        ticker = profile["ticker"]
        if ticker not in roster:
            continue
        fetched_at = profile["fetched_at"]
        day = fetched_at.astimezone(SYDNEY).date()
        current = daily[day].get(ticker)
        if current is None or fetched_at > current["fetched_at"]:
            daily[day][ticker] = profile

    history = []
    details = {}
    for day, by_ticker in sorted(daily.items()):
        rows = []
        total_cap = Decimal(0)
        numerators = {"trailing": Decimal(0), "forward": Decimal(0)}
        denominators = {"trailing": Decimal(0), "forward": Decimal(0)}
        counts = {"trailing": 0, "forward": 0}
        for ticker, record in sorted(by_ticker.items()):
            payload = record["payload"]
            currency = payload.get("currency")
            cap = _positive(payload.get("marketCap")) if currency == "AUD" else None
            trailing = _positive(payload.get("trailingPE"))
            forward = _positive(payload.get("forwardPE"))
            if cap is not None:
                total_cap += cap
                for key, pe in (("trailing", trailing), ("forward", forward)):
                    if pe is not None:
                        numerators[key] += cap * pe
                        denominators[key] += cap
                        counts[key] += 1
            rows.append(
                {
                    "ticker": ticker,
                    "company": roster[ticker]["name"],
                    "currency": currency,
                    "market_cap_aud": cap,
                    "trailing_pe": trailing,
                    "forward_pe": forward,
                    "fetched_at": record["fetched_at"],
                }
            )
        for ticker in sorted(roster.keys() - by_ticker.keys()):
            rows.append(
                {
                    "ticker": ticker,
                    "company": roster[ticker]["name"],
                    "currency": None,
                    "market_cap_aud": None,
                    "trailing_pe": None,
                    "forward_pe": None,
                    "fetched_at": None,
                }
            )
        rows.sort(key=lambda item: item["ticker"])
        details[day] = rows
        history.append(
            {
                "date": day,
                "profiles": len(by_ticker),
                "market_cap_aud": total_cap,
                **{
                    f"{key}_pe": (
                        numerators[key] / denominators[key]
                        if counts[key] * 10 > len(roster)
                        else None
                    )
                    for key in ("trailing", "forward")
                },
                **{f"{key}_count": counts[key] for key in counts},
                **{
                    f"{key}_cap_coverage": (
                        denominators[key] / total_cap if total_cap else None
                    )
                    for key in counts
                },
            }
        )
    return history, details


def show_dashboard(
    history: list[dict[str, Any]], details: dict[date, list[dict[str, Any]]]
) -> None:
    if not history:
        st.info(
            "No stored company-profile snapshots for the supplied roster. "
            "Ingest the tickers first."
        )
        return

    selected = st.selectbox("Snapshot date (Australia/Sydney)", list(reversed(details)))
    row = next(item for item in history if item["date"] == selected)
    st.caption(
        "Fixed roster dated 2026-04-10 (user-supplied compilation, not verified historical "
        "S&P index membership). Each date uses the latest stored Yahoo profile fetched that "
        "Sydney day; no backfill or carry-forward. Only positive P/E and market caps quoted "
        "in AUD contribute. Each P/E uses its own eligible market-cap denominator. "
        "An aggregate is hidden if 90% or more of the roster lacks eligible data "
        "(30 or fewer of 300 stocks)."
    )
    columns = st.columns(4)
    columns[0].metric(
        "Market-cap weighted P/E",
        f"{row['trailing_pe']:.2f}" if row["trailing_pe"] else "N/A",
    )
    columns[1].metric(
        "Market-cap weighted forward P/E",
        f"{row['forward_pe']:.2f}" if row["forward_pe"] else "N/A",
    )
    columns[2].metric("Profiles / roster", f"{row['profiles']} / 300")
    columns[3].metric("AUD market cap observed", f"${row['market_cap_aud']:,.0f}")
    st.caption(
        f"Trailing: {row['trailing_count']} stocks, "
        f"{row['trailing_cap_coverage']:.1%} of observed AUD cap; "
        f"forward: {row['forward_count']} stocks, "
        f"{row['forward_cap_coverage']:.1%} of observed AUD cap."
        if row["market_cap_aud"]
        else "No eligible AUD market caps on this date."
    )

    if any(item[key] is not None for item in history for key in ("trailing_pe", "forward_pe")):
        figure = go.Figure()
        for key, label in (("trailing_pe", "Trailing P/E"), ("forward_pe", "Forward P/E")):
            figure.add_trace(
                go.Scatter(
                    x=[item["date"] for item in history],
                    y=[float(item[key]) if item[key] is not None else None for item in history],
                    name=label,
                    mode="lines+markers",
                    connectgaps=False,
                )
            )
        figure.update_layout(
            xaxis_title="Profile fetch date (Sydney)",
            yaxis_title="Market-cap weighted P/E",
            hovermode="x unified",
        )
        st.plotly_chart(figure, width="stretch")
    else:
        st.info("No dates have enough eligible stocks to display a weighted P/E.")
    st.subheader("Daily coverage and valuations")
    st.dataframe(pd.DataFrame(history), hide_index=True, width="stretch")
    st.subheader(f"Constituents observed on {selected}")
    st.dataframe(pd.DataFrame(details[selected]), hide_index=True, width="stretch")
    st.caption(
        "Missing profiles, non-AUD or absent market caps, and missing/nonpositive P/E "
        "are excluded. Coverage is against observed AUD market cap, not the entire index; "
        "these averages are not an official S&P index valuation."
    )


def main() -> None:
    st.set_page_config(page_title="ASX 300 valuations", layout="wide")
    st.title("ASX 300 roster valuations")
    try:
        roster = load_roster()
        profiles = load_profiles(_engine(), roster)
    except (OSError, ValueError) as exc:
        st.error(str(exc))
        return
    except Exception:
        st.error("Could not read PostgreSQL. Check DATABASE_URL and run Alembic migrations.")
        return
    show_dashboard(*summarize(profiles, roster))
    st.subheader("Supplied 300-stock roster")
    st.dataframe(pd.DataFrame(roster.values()), hide_index=True, width="stretch")


if __name__ == "__main__":
    main()
