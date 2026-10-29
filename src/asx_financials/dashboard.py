"""Read-only Streamlit viewer for collected ASX ticker data."""

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import Engine, create_engine, select

from asx_financials.config import get_settings
from asx_financials.domain.models import CorporateAction
from asx_financials.domain.price_adjustments import derive_adjusted_closes
from asx_financials.domain.value_objects import AsxTicker
from asx_financials.infrastructure.persistence.schema import (
    corporate_actions,
    daily_prices,
    financial_facts,
    ingestion_runs,
    raw_source_payloads,
    statement_snapshots,
    tickers,
)

EXCHANGE_CODE = "ASX"


@st.cache_resource
def _engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def load_ticker_data(engine: Engine, ticker: AsxTicker) -> dict[str, Any] | None:
    """Fetch one ASX listing and its related records without modifying the database."""
    with engine.connect() as connection:
        listing = (
            connection.execute(
                select(tickers).where(
                    tickers.c.exchange_code == EXCHANGE_CODE, tickers.c.ticker == ticker.value
                )
            )
            .mappings()
            .first()
        )
        if listing is None:
            return None
        records: dict[str, Any] = {"listing": dict(listing)}
        for name, table, ordering in (
            ("daily_prices", daily_prices, daily_prices.c.trading_date.desc()),
            ("corporate_actions", corporate_actions, corporate_actions.c.effective_date.desc()),
            ("runs", ingestion_runs, ingestion_runs.c.started_at.desc()),
            ("snapshots", statement_snapshots, statement_snapshots.c.fetched_at.desc()),
            ("facts", financial_facts, financial_facts.c.period_end.desc().nulls_last()),
            ("raw", raw_source_payloads, raw_source_payloads.c.fetched_at.desc()),
        ):
            records[name] = [
                dict(row)
                for row in connection.execute(
                    select(table)
                    .where(table.c.exchange_code == EXCHANGE_CODE, table.c.ticker == ticker.value)
                    .order_by(ordering)
                ).mappings()
            ]
        return records


def _format_number(value: Any, *, money: bool = False) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return str(value)
    amount = f"{value:,}"
    return f"${amount}" if money else amount


def _is_monetary_fact(metric_key: str) -> bool:
    key = metric_key.lower().replace("_", "")
    return not any(
        part in key for part in ("sharesnumber", "averageshares", "shareissued", "taxrate")
    )


def _table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        st.info("No data collected for this section.")
        return
    st.dataframe(pd.DataFrame(rows)[columns], hide_index=True, width="stretch")


MONEY_PROFILE_FIELDS = frozenset(
    {
        "currentPrice",
        "previousClose",
        "marketCap",
        "enterpriseValue",
        "fiftyTwoWeekLow",
        "fiftyTwoWeekHigh",
        "totalRevenue",
        "grossProfits",
        "ebitda",
        "netIncomeToCommon",
        "trailingEps",
        "forwardEps",
        "totalCash",
        "totalDebt",
        "totalCashPerShare",
        "bookValue",
        "operatingCashflow",
        "freeCashflow",
        "dividendRate",
    }
)

PROFILE_SECTIONS = {
    "Market and valuation": (
        "currentPrice",
        "previousClose",
        "marketCap",
        "enterpriseValue",
        "sharesOutstanding",
        "trailingPE",
        "forwardPE",
        "priceToBook",
        "priceToSalesTrailing12Months",
        "fiftyTwoWeekLow",
        "fiftyTwoWeekHigh",
        "averageVolume",
    ),
    "Financial performance": (
        "totalRevenue",
        "revenueGrowth",
        "grossProfits",
        "grossMargins",
        "ebitda",
        "ebitdaMargins",
        "operatingMargins",
        "netIncomeToCommon",
        "profitMargins",
        "earningsGrowth",
        "trailingEps",
        "forwardEps",
        "returnOnEquity",
    ),
    "Balance sheet and cash flow": (
        "totalCash",
        "totalDebt",
        "totalCashPerShare",
        "bookValue",
        "operatingCashflow",
        "freeCashflow",
        "returnOnAssets",
    ),
    "Dividends and ownership": (
        "dividendRate",
        "dividendYield",
        "payoutRatio",
        "exDividendDate",
        "heldPercentInsiders",
        "heldPercentInstitutions",
        "fullTimeEmployees",
    ),
}


def _label(key: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", key).replace("_", " ").capitalize()


def _profile_table(profile: dict[str, Any], keys: tuple[str, ...]) -> None:
    rows = [
        {
            "field": _label(key),
            "value": _format_number(profile[key], money=key in MONEY_PROFILE_FIELDS),
        }
        for key in keys
        if profile.get(key) is not None
    ]
    if rows:
        _table(rows, ["field", "value"])
    else:
        st.info("No data collected for this section.")


def _statement_summary(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    periods: dict[tuple[str, str], set[date]] = defaultdict(set)
    for snapshot in snapshots:
        if snapshot["is_current"] and snapshot["period_end"] is not None:
            periods[(snapshot["statement_type"], snapshot["frequency"])].add(snapshot["period_end"])
    return [
        {
            "statement": statement,
            "frequency": frequency,
            "periods": len(dates),
            "latest period": max(dates),
        }
        for (statement, frequency), dates in sorted(periods.items())
    ]


def _daily_prices_view(
    prices: list[dict[str, Any]], actions: list[dict[str, Any]] | None = None
) -> None:
    if not prices:
        st.info("No daily price and volume history collected for this ticker.")
        return

    events = [
        CorporateAction(
            action_type=row["action_type"],
            effective_date=row["effective_date"],
            value=row["value"],
            source=row["source"],
            fetched_at_utc=row["fetched_at"],
        )
        for row in (actions or [])
    ]
    adjusted = derive_adjusted_closes(prices, events)
    chronological = sorted(prices, key=lambda row: row["trading_date"])

    def adjusted_float(day: date) -> float | None:
        value = adjusted.get(day)
        return float(value) if value is not None else None

    close = pd.DataFrame(
        {
            "Trading date": [row["trading_date"] for row in chronological],
            "Close": [
                float(row["close"]) if row["close"] is not None else None for row in chronological
            ],
            "Derived adjusted close": [
                adjusted_float(row["trading_date"]) for row in chronological
            ],
        }
    ).set_index("Trading date")
    if close["Derived adjusted close"].isna().all():
        close = close.drop(columns="Derived adjusted close")
    st.subheader("Daily closing prices")
    st.line_chart(close, y_label="Price ($)")
    volume = pd.DataFrame(
        {
            "Trading date": [row["trading_date"] for row in chronological],
            "Volume": [row["volume"] for row in chronological],
        }
    ).set_index("Trading date")
    st.subheader("Daily trading volume")
    st.bar_chart(volume, y_label="Shares")
    st.subheader("Daily OHLCV history")
    columns = (
        "trading_date",
        "open",
        "high",
        "low",
        "close",
        "derived_adjusted_close",
        "adjusted_close",
        "volume",
        "source",
        "fetched_at",
    )
    _table(
        [
            {
                key: (
                    _format_number(adjusted.get(row["trading_date"]), money=True)
                    if key == "derived_adjusted_close"
                    else _format_number(row[key], money=True)
                    if key in {"open", "high", "low", "close", "adjusted_close"}
                    else _format_number(row[key])
                    if key == "volume"
                    else row[key]
                )
                for key in columns
            }
            for row in prices
        ],
        list(columns),
    )


def _financial_highlights(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "total_revenue",
        "net_income",
        "total_assets",
        "total_liabilities_net_minority_interest",
        "stockholders_equity",
        "operating_cash_flow",
        "free_cash_flow",
        "diluted_eps",
    )
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for fact in facts:
        if not fact["is_current"] or fact["period_end"] is None or fact["metric_key"] not in keys:
            continue
        key = (fact["statement_type"], fact["frequency"], fact["metric_key"])
        if key not in latest or fact["period_end"] > latest[key]["period_end"]:
            latest[key] = fact
    return [
        {
            "statement": fact["statement_type"],
            "frequency": fact["frequency"],
            "period": fact["period_end"],
            "metric": fact["metric_label"],
            "value": _format_number(fact["value"], money=_is_monetary_fact(fact["metric_key"])),
            "currency": fact["currency"],
        }
        for fact in sorted(
            latest.values(),
            key=lambda row: (row["statement_type"], row["frequency"], row["metric_key"]),
        )
    ]


def main() -> None:
    st.set_page_config(page_title="ASX Financials", layout="wide")
    st.title("ASX Financials")
    st.caption("Read-only view of collected PostgreSQL data; this page does not trigger a scrape.")
    ticker_input = st.text_input("ASX ticker", value="CBA").strip()
    if not ticker_input:
        st.info("Enter an ASX ticker to see its collected data.")
        return
    try:
        ticker = AsxTicker.parse(ticker_input)
    except ValueError as exc:
        st.error(str(exc))
        return
    try:
        data = load_ticker_data(_engine(), ticker)
    except Exception:
        st.error("Could not read PostgreSQL. Check DATABASE_URL and run Alembic migrations.")
        return
    if data is None:
        st.warning(
            f"No stored data for {ticker.value}. "
            f"Run `uv run asx-financials ingest {ticker.value}` first."
        )
        return

    listing = data["listing"]
    st.subheader(f"{listing['company_name'] or ticker.value} ({ticker.value}.AX)")
    overview, daily, statements, facts, raw, runs = st.tabs(
        [
            "Overview",
            "Daily prices & volume",
            "Statements",
            "Financial facts",
            "Raw responses",
            "Ingestion runs",
        ]
    )
    with overview:
        profile = listing["profile_payload"] or {}
        st.caption(
            f"{listing['exchange_code']} · {listing['sector'] or 'Sector unavailable'} · "
            f"{listing['industry'] or 'Industry unavailable'} · "
            f"Profile fetched: {listing['last_fundamental_at'] or 'N/A'}"
        )
        if profile.get("longBusinessSummary"):
            st.write(profile["longBusinessSummary"])
        columns = st.columns(4)
        for column, (label, value) in zip(
            columns,
            (
                ("Current price", profile.get("currentPrice")),
                ("Market cap", profile.get("marketCap")),
                ("Total revenue", profile.get("totalRevenue")),
                ("Net income", profile.get("netIncomeToCommon")),
            ),
            strict=True,
        ):
            column.metric(label, _format_number(value, money=True))
        st.subheader("Company details")
        _table(
            [
                {
                    "field": _label(key),
                    "value": str(listing[key]) if listing[key] is not None else "N/A",
                }
                for key in (
                    "exchange_code",
                    "ticker",
                    "yahoo_symbol",
                    "company_name",
                    "exchange_name",
                    "industry_group",
                    "sector",
                    "industry",
                    "country",
                    "website",
                    "currency",
                    "status",
                    "listed_in_universe",
                    "source_name",
                    "last_fundamental_at",
                )
            ],
            ["field", "value"],
        )
        for section, keys in PROFILE_SECTIONS.items():
            st.subheader(section)
            _profile_table(profile, keys)
        st.subheader("Financial statement coverage")
        _table(
            _statement_summary(data["snapshots"]),
            ["statement", "frequency", "periods", "latest period"],
        )
        st.subheader("Latest statement highlights")
        _table(
            _financial_highlights(data["facts"]),
            ["statement", "frequency", "period", "metric", "value", "currency"],
        )
        st.subheader("Collected records")
        st.write(
            {
                "statement revisions": len(data["snapshots"]),
                "financial facts": len(data["facts"]),
                "raw responses": len(data["raw"]),
                "ingestion runs": len(data["runs"]),
            }
        )
        with st.expander("Complete stored company profile (all fields)"):
            st.json(profile)
    with daily:
        _daily_prices_view(data["daily_prices"], data["corporate_actions"])
        st.subheader("Corporate actions")
        _table(data["corporate_actions"], ["effective_date", "action_type", "value", "source"])
    with statements:
        snapshots = data["snapshots"]
        if snapshots:
            _table(
                snapshots,
                [
                    "statement_type",
                    "frequency",
                    "period_end",
                    "is_current",
                    "source_name",
                    "fetched_at",
                ],
            )
            labels = [
                f"{item['statement_type']} / {item['frequency']} / "
                f"{item['period_end'] or 'undated'} / {item['snapshot_id'][:8]}"
                for item in snapshots
            ]
            selected = snapshots[
                st.selectbox(
                    "Statement revision",
                    range(len(labels)),
                    format_func=lambda index: labels[index],
                )
            ]
            values = selected["payload"].get("values", {})
            _table(
                [
                    {
                        "metric": key,
                        "value": _format_number(value, money=_is_monetary_fact(key)),
                    }
                    for key, value in values.items()
                ],
                ["metric", "value"],
            )
            with st.expander("Full statement payload"):
                st.json(selected["payload"])
        else:
            st.info("No statements collected for this ticker.")
    with facts:
        _table(
            [
                {
                    **fact,
                    "value": _format_number(
                        fact["value"], money=_is_monetary_fact(fact["metric_key"])
                    ),
                }
                for fact in data["facts"]
            ],
            [
                "statement_type",
                "frequency",
                "period_end",
                "metric_key",
                "value",
                "currency",
                "is_current",
            ],
        )
    with raw:
        payloads = data["raw"]
        if payloads:
            _table(payloads, ["segment_name", "source_name", "fetched_at", "run_id"])
            labels = [f"{item['segment_name']} / {item['fetched_at']}" for item in payloads]
            index = st.selectbox(
                "Raw response", range(len(labels)), format_func=lambda value: labels[value]
            )
            st.json(payloads[index]["payload"])
        else:
            st.info("No raw responses collected for this ticker.")
    with runs:
        _table(
            data["runs"],
            [
                "run_id",
                "status",
                "started_at",
                "completed_at",
                "stored_snapshot_count",
                "stored_raw_payload_count",
                "message",
            ],
        )
        for run in data["runs"]:
            if run["segment_failures"]:
                with st.expander(f"Segment failures: {run['run_id']}"):
                    st.json(run["segment_failures"])


if __name__ == "__main__":
    main()
