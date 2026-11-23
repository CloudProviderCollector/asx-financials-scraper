"""Browse and record reproducible trend-strategy backtests."""

from datetime import date, datetime, timedelta
from pathlib import Path

import streamlit as st
from backtest.archive import (
    list_run_sets,
    load_bars_snapshot,
    load_manifest,
    load_result,
    save_run_set,
)
from backtest.charts import trading_chart
from backtest.data import load_bars

from asx_financials.domain.value_objects import AsxTicker
from asx_financials.infrastructure.providers.yfinance_provider import ASX_TIMEZONE

RECORDS = Path(__file__).resolve().parents[1] / "backtest" / "records"


def main() -> None:
    st.set_page_config(page_title="Strategy backtests", layout="wide")
    st.title("Strategy backtests")
    st.caption(
        "Each saved run records every tried strategy against the same daily-bar snapshot. "
        "Signals use completed closes; trades fill at the next available open."
    )
    with st.expander("Run and record all strategies"):
        with st.form("record"):
            source = st.selectbox("Price source", ["yahoo", "postgres"])
            tickers = st.text_input("ASX tickers", "BHP CBA")
            start = st.date_input("Start", date(2015, 1, 2))
            end = st.date_input("End", datetime.now(ASX_TIMEZONE).date() - timedelta(days=1))
            submitted = st.form_submit_button("Run and record all")
        if submitted:
            symbols = [AsxTicker.parse(value).value for value in tickers.split()]
            if not symbols or len(set(symbols)) != len(symbols):
                st.error("Enter at least one ticker, without duplicates.")
            elif end < start:
                st.error("End date precedes start date.")
            else:
                with st.spinner("Loading prices and running all strategy versions..."):
                    bars = load_bars(source, symbols, end)
                    folder = save_run_set(RECORDS, bars, start, end, source)
                st.success(f"Saved run: {folder.name}")

    folders = list_run_sets(RECORDS)
    if not folders:
        st.info("No saved runs yet. Run and record all strategies above.")
        return
    folder = st.selectbox("Saved run", folders, format_func=lambda path: path.name)
    manifest = load_manifest(folder)
    st.caption(
        f"Captured {manifest['captured_at']} | {manifest['source']} | "
        f"{', '.join(manifest['symbols'])} | "
        f"requested {manifest['requested_start'] or 'first available'} "
        f"to {manifest['requested_end']}"
    )
    versions = manifest["versions"]
    st.subheader("Backtest results")
    st.dataframe(
        [
            {
                "Strategy": version["title"],
                "Final equity (AUD)": round(version["final_equity"], 2),
                "Return": f"{version['total_return']:.2%}",
                "Max drawdown": f"{version['max_drawdown']:.2%}",
                "Completed trades": version["completed_trades"],
                "Open positions": version["open_positions"],
            }
            for version in versions.values()
        ],
        hide_index=True,
    )
    bars = load_bars_snapshot(folder)
    st.subheader("Strategy details and graphs")
    for key, version in versions.items():
        st.markdown(f"### {version['title']}")
        st.write(f"**Buy:** {version['entry']}")
        st.write(f"**Sell:** {version['exit']}")
        st.write(f"**Parameters:** {version['parameters']}")
        st.write(
            f"**Run:** {version['first_day']} to {version['last_day']}; "
            f"initial cash AUD {manifest['settings']['cash']:,.2f}; "
            f"entry sizing uses {manifest['settings']['risk_fraction']:.1%} of equity divided by "
            "3 x 20-session ATR, subject to position/sector caps. "
            "MA versions do not use an ATR stop-loss."
        )
        averages = tuple(version["averages"])
        if key == "breakout" and not averages:
            averages = (version["parameters"]["trend_window"],)
        st.plotly_chart(
            trading_chart(bars, load_result(folder, key), averages),
            width="stretch",
            key=f"{folder.name}-{key}",
        )
    st.caption(
        "Adjusted prices approximate total returns; fills include commissions and slippage. "
        "Yahoo has no historical sectors; a present-day ticker selection has survivorship bias. "
        "Snapshots are saved locally under backtest/records."
    )


if __name__ == "__main__":
    main()
