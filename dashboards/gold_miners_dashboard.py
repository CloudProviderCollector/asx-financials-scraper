"""Read-only comparison of ASX gold miners and COMEX gold futures."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import isfinite, sqrt
from statistics import StatisticsError, linear_regression, mean, median, pstdev
from typing import Literal, cast

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.engine import make_url

from asx_financials.config import get_settings
from asx_financials.infrastructure.persistence.schema import (
    commodity_prices,
    daily_prices,
    exchange_rates,
)
from dashboards.vea_crude_dashboard import (
    Trade,
    fund_utilization,
    trade_chart,
    trade_summary,
)

TICKERS = ("NST", "EVN", "NEM", "PRU")
PriceSeries = list[tuple[date, Decimal]]


@dataclass(frozen=True)
class RegressionFit:
    a: float
    b: float
    squared_error: float
    observations: int


@st.cache_resource
def _engine() -> Engine:
    url = make_url(get_settings().database_url)
    if url.host == "localhost":
        # Docker's IPv4-only port mapping can leave libpq waiting on ::1 first.
        url = url.set(host="127.0.0.1")
    return create_engine(url, pool_pre_ping=True)


def load_prices(engine: Engine) -> tuple[dict[str, PriceSeries], PriceSeries, PriceSeries]:
    with engine.connect() as connection:
        stocks = connection.execute(
            select(daily_prices.c.ticker, daily_prices.c.trading_date, daily_prices.c.close)
            .where(
                daily_prices.c.exchange_code == "ASX",
                daily_prices.c.ticker.in_(TICKERS),
                daily_prices.c.close.is_not(None),
            )
            .order_by(daily_prices.c.ticker, daily_prices.c.trading_date)
        ).all()
        gold = connection.execute(
            select(commodity_prices.c.observation_date, commodity_prices.c.price)
            .where(
                commodity_prices.c.source == "Yahoo Finance",
                commodity_prices.c.series == "GC=F",
                commodity_prices.c.frequency == "daily",
                commodity_prices.c.commodity == "gold_comex_futures",
            )
            .order_by(commodity_prices.c.observation_date)
        ).all()
        aud_usd = connection.execute(
            select(exchange_rates.c.observation_date, exchange_rates.c.rate)
            .where(
                exchange_rates.c.source == "Yahoo Finance",
                exchange_rates.c.series == "AUDUSD=X",
                exchange_rates.c.base_currency == "AUD",
                exchange_rates.c.quote_currency == "USD",
            )
            .order_by(exchange_rates.c.observation_date)
        ).all()

    shares: dict[str, PriceSeries] = {ticker: [] for ticker in TICKERS}
    for ticker, day, close in stocks:
        shares[cast(str, ticker)].append((cast(date, day), cast(Decimal, close)))
    return (
        shares,
        [(cast(date, day), cast(Decimal, price)) for day, price in gold],
        [(cast(date, day), cast(Decimal, rate)) for day, rate in aud_usd],
    )


def load_opens(engine: Engine) -> dict[str, dict[date, Decimal]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(daily_prices.c.ticker, daily_prices.c.trading_date, daily_prices.c.open).where(
                daily_prices.c.exchange_code == "ASX",
                daily_prices.c.ticker.in_(TICKERS),
                daily_prices.c.open.is_not(None),
            )
        ).all()
    opens: dict[str, dict[date, Decimal]] = {ticker: {} for ticker in TICKERS}
    for ticker, day, price in rows:
        ticker, day, price = cast(str, ticker), cast(date, day), cast(Decimal, price)
        if price.is_finite() and price > 0:
            opens[ticker][day] = price
    return opens


def gold_in_aud(gold_usd: PriceSeries, aud_usd: PriceSeries) -> PriceSeries:
    rates = {day: rate for day, rate in aud_usd if rate.is_finite() and rate > 0}
    return [
        (day, price / rates[day])
        for day, price in gold_usd
        if day in rates and price.is_finite() and price > 0
    ]


def comparison_chart(ticker: str, shares: PriceSeries, gold: PriceSeries) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in shares],
            y=[float(value) for _, value in shares],
            name=f"{ticker}.AX close (AUD/share)",
            mode="lines",
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in gold],
            y=[float(value) for _, value in gold],
            name="GC=F gold futures close (AUD/troy oz)",
            mode="lines",
        ),
        secondary_y=True,
    )
    figure.update_xaxes(title_text="Date")
    figure.update_yaxes(title_text=f"{ticker}.AX close (AUD/share)", secondary_y=False)
    figure.update_yaxes(title_text="GC=F gold futures close (AUD/troy oz)", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=500)
    return figure


def aligned_closes(shares: PriceSeries, gold: PriceSeries) -> list[tuple[date, float, float]]:
    gold_by_day = {day: price for day, price in gold if price.is_finite() and price > 0}
    return [
        (day, float(share), float(gold_by_day[day]))
        for day, share in shares
        if day in gold_by_day
        and share.is_finite()
        and share > 0
        and isfinite(float(share))
        and isfinite(float(gold_by_day[day]))
    ]


def last_ten_years(pairs: list[tuple[date, float, float]]) -> list[tuple[date, float, float]]:
    if not pairs:
        return []
    end = pairs[-1][0]
    start = years_ago(end, 10)
    return [pair for pair in pairs if start <= pair[0] <= end]


def years_ago(end: date, years: int) -> date:
    try:
        return end.replace(year=end.year - years)
    except ValueError:
        return end.replace(year=end.year - years, day=28)


def fit_regression(pairs: list[tuple[date, float, float]]) -> RegressionFit | None:
    if len(pairs) < 2:
        return None
    try:
        result = linear_regression([gold for _, _, gold in pairs], [share for _, share, _ in pairs])
    except StatisticsError:
        return None
    if not (isfinite(result.slope) and isfinite(result.intercept)):
        return None
    squared_error = sum(
        (share - (result.slope * gold + result.intercept)) ** 2 for _, share, gold in pairs
    )
    if not isfinite(squared_error):
        return None
    return RegressionFit(result.slope, result.intercept, squared_error, len(pairs))


def editable_fit(
    ticker: str,
    window: str,
    pairs: list[tuple[date, float, float]],
    fitted: RegressionFit,
) -> RegressionFit | None:
    a = st.number_input(
        f"{ticker}.AX {window} a (slope)",
        value=fitted.a,
        format="%.10f",
        key=f"{ticker}-{window}-a",
    )
    b = st.number_input(
        f"{ticker}.AX {window} b (intercept)",
        value=fitted.b,
        format="%.10f",
        key=f"{ticker}-{window}-b",
    )
    if not (isfinite(a) and isfinite(b)):
        st.error(f"{ticker}.AX {window}: a and b must be finite numbers.")
        return None
    squared_error = 0.0
    for _, share, gold in pairs:
        estimated = a * gold + b
        if not isfinite(estimated):
            st.error(f"{ticker}.AX {window}: estimated prices are not finite.")
            return None
        error = share - estimated
        squared_error += error * error
    if not isfinite(squared_error):
        st.error(f"{ticker}.AX {window}: estimated prices or squared errors are not finite.")
        return None
    return RegressionFit(a, b, squared_error, len(pairs))


def estimate_chart(
    ticker: str, pairs: list[tuple[date, float, float]], fit: RegressionFit
) -> go.Figure:
    figure = go.Figure()
    days = [day for day, _, _ in pairs]
    figure.add_trace(
        go.Scatter(
            x=days,
            y=[share for _, share, _ in pairs],
            name=f"{ticker}.AX actual close",
            mode="lines",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=days,
            y=[fit.a * gold + fit.b for _, _, gold in pairs],
            name=f"{ticker}.AX estimated close",
            mode="lines",
        )
    )
    figure.update_xaxes(title_text="Matched closing date")
    figure.update_yaxes(title_text=f"{ticker}.AX close (AUD/share)")
    figure.update_layout(hovermode="x unified", height=500)
    return figure


def valuation_deviations(pairs: list[tuple[date, float, float]], fit: RegressionFit) -> PriceSeries:
    deviations = []
    for day, actual, gold in pairs:
        estimated = fit.a * gold + fit.b
        if isfinite(estimated) and estimated > 0:
            deviations.append((day, Decimal(str((actual - estimated) / estimated))))
    return deviations


def deviation_thresholds(deviations: PriceSeries) -> tuple[Decimal, Decimal]:
    values = [value for _, value in deviations]
    center = mean(values)
    sigma = pstdev(values)
    return center - Decimal("1.5") * sigma, center - Decimal("0.5") * sigma


def backtest_valuation(
    shares: PriceSeries,
    opens: dict[date, Decimal],
    deviations: PriceSeries,
) -> tuple[list[Trade], date | None]:
    buy_below, sell_above = deviation_thresholds(deviations)
    by_day = dict(deviations)
    trades: list[Trade] = []
    position: tuple[date, Decimal] | None = None
    pending: Literal["buy", "sell"] | None = None
    for day in sorted({day for day, _ in shares} | opens.keys()):
        if pending is not None:
            open_price = opens.get(day)
            if open_price is not None and open_price > 0:
                if pending == "buy":
                    position = (day, open_price)
                elif position is not None:
                    buy_day, buy_price = position
                    trades.append(Trade(buy_day, buy_price, day, open_price))
                    position = None
            pending = None
        deviation = by_day.get(day)
        if deviation is not None:
            if position is None and deviation < buy_below:
                pending = "buy"
            elif position is not None and deviation > sell_above:
                pending = "sell"
    return trades, position[0] if position is not None else None


def deviation_chart(
    ticker: str,
    shares: PriceSeries,
    pairs: list[tuple[date, float, float]],
    fit: RegressionFit,
    deviations: PriceSeries,
) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in shares],
            y=[float(price) for _, price in shares],
            name=f"{ticker}.AX actual close (AUD/share)",
            mode="lines",
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=[day for day, _, _ in pairs],
            y=[fit.a * gold + fit.b for _, _, gold in pairs],
            name=f"{ticker}.AX estimated close (AUD/share)",
            mode="lines",
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in deviations],
            y=[float(value) * 100 for _, value in deviations],
            name="Actual minus estimated / estimated (%)",
            mode="lines",
        ),
        secondary_y=True,
    )
    buy_below, sell_above = deviation_thresholds(deviations)
    for threshold, label, color in (
        (buy_below, "Buy: -1.5 sigma", "orange"),
        (sell_above, "Sell: -0.5 sigma", "green"),
    ):
        figure.add_hline(
            y=float(threshold) * 100,
            line_color=color,
            line_dash="dash",
            secondary_y=True,
            annotation_text=label,
        )
    figure.add_hline(y=0, line_dash="dot", line_color="gray", secondary_y=True)
    figure.update_xaxes(title_text="Date")
    figure.update_yaxes(title_text=f"{ticker}.AX close (AUD/share)", secondary_y=False)
    figure.update_yaxes(title_text="Deviation from estimated close (%)", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def deviation_histogram(deviations: PriceSeries) -> go.Figure:
    values = [float(value) * 100 for _, value in deviations]
    figure = go.Figure(go.Histogram(x=values, nbinsx=20, name="Valuation deviation (%)"))
    for threshold, color in zip(deviation_thresholds(deviations), ("orange", "green"), strict=True):
        figure.add_vline(
            x=float(threshold) * 100,
            line_color=color,
            line_dash="dash",
        )
    figure.add_vline(x=float(median(values)), line_color="red", line_dash="dash")
    figure.update_xaxes(title_text="(Actual close - estimated close) / estimated close (%)")
    figure.update_yaxes(title_text="Number of matched trading dates")
    figure.update_layout(height=500, bargap=0.05)
    return figure


def display_strategy(
    ticker: str,
    shares: PriceSeries,
    opens: dict[date, Decimal],
    deviations: PriceSeries,
) -> None:
    trades, open_buy_date = backtest_valuation(shares, opens, deviations)
    st.subheader(f"{ticker}.AX valuation trades")
    st.plotly_chart(
        trade_chart(shares, trades, open_buy_date, ticker=ticker),
        width="stretch",
        key=f"{ticker}-valuation-trades",
    )
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
        st.info("No completed trades for this strategy.")
    win_rate, compounded_return = trade_summary(trades)
    utilization = fund_utilization(trades, open_buy_date, shares[0][0], shares[-1][0])
    st.write(
        f"Completed trades: {len(trades)} | Win rate: {win_rate:.2f}% | "
        f"Total profit/loss (compounded): {compounded_return:+.2f}% | "
        f"Fund utilization: {utilization:.2f}%"
    )
    if open_buy_date is not None:
        st.caption("One position remains open; it is excluded from completed trade results.")


def main() -> None:
    st.set_page_config(page_title="ASX gold miners vs gold futures (AUD)", layout="wide")
    st.title("ASX gold miners vs gold futures (AUD)")
    st.caption(
        "Unadjusted ASX daily share closes (AUD/share, left axis) and Yahoo GC=F COMEX "
        "gold futures closes converted to AUD/troy oz (right axis): USD gold close "
        "divided by Yahoo AUDUSD=X (USD per AUD) on the same date. Dates without both "
        "quotes are excluded, not forward-filled. Each stock has full-history and "
        "last-10-years fits comparing actual and estimated closes on matching dates. "
        "The 10-year window ends at the latest matching close for that stock. "
        "GC=F is a rolling futures symbol, "
        "not a spot-gold price. This page reads stored PostgreSQL data only."
    )
    shares_by_ticker, gold_usd, aud_usd = load_prices(_engine())
    if not gold_usd:
        st.warning("No stored GC=F gold futures closes. Download gold futures history first.")
        return
    if not aud_usd:
        st.warning(
            "No stored AUD/USD closes. Run python -m asx_financials.download_exchange_rates first."
        )
        return
    gold = gold_in_aud(gold_usd, aud_usd)
    if not gold:
        st.warning("GC=F and AUD/USD have no matching dates with positive, finite closes.")
        return
    opens_by_ticker = load_opens(_engine())
    for ticker in TICKERS:
        st.header(f"{ticker}.AX vs GC=F")
        shares = shares_by_ticker[ticker]
        if not shares:
            st.warning(f"No stored {ticker}.AX daily closes. Ingest {ticker} first.")
            continue
        start = max(shares[0][0], gold[0][0])
        end = min(shares[-1][0], gold[-1][0])
        if start > end:
            st.warning(f"{ticker}.AX and GC=F have no overlapping date range.")
            continue
        stock_range = [(day, value) for day, value in shares if start <= day <= end]
        gold_range = [(day, value) for day, value in gold if start <= day <= end]
        if not stock_range or not gold_range:
            st.warning(f"{ticker}.AX and GC=F have no closes within their shared date range.")
            continue
        st.plotly_chart(comparison_chart(ticker, stock_range, gold_range), width="stretch")
        pairs = aligned_closes(stock_range, gold_range)
        fit = fit_regression(pairs)
        if fit is None:
            st.warning(
                f"Cannot fit {ticker}.AX: at least two matching dates with varying "
                "positive, finite gold prices are required."
            )
            continue
        selected_fit = editable_fit(ticker, "full history", pairs, fit)
        if selected_fit is None:
            continue
        mse = selected_fit.squared_error / selected_fit.observations
        rmse = sqrt(mse)
        st.caption(
            f"Ordinary least squares defaults for {ticker}.AX price = a * GC=F price + b "
            f"on {fit.observations:,} matching daily closes: "
            f"a = {fit.a:.8g}, b = {fit.b:.8g}. "
            f"Selected a = {selected_fit.a:.8g} (AUD/share)/(AUD/troy oz); "
            f"b = {selected_fit.b:.8g} AUD/share. "
            f"Sum of squared errors (SSE): {selected_fit.squared_error:,.6g} (AUD/share)^2; "
            f"mean squared error (MSE): {mse:,.6g} (AUD/share)^2; "
            f"root mean squared error (RMSE): {rmse:,.6g} AUD/share. "
            "In-sample fit; same-date AUD/USD conversion, no forward filling."
        )
        latest_day, latest_close = shares[-1]
        if (
            latest_close.is_finite()
            and latest_close > 0
            and isfinite(float(latest_close))
            and isfinite(rmse / float(latest_close))
        ):
            ratio = rmse / float(latest_close)
            st.caption(
                f"RMSE / latest {ticker}.AX close ({latest_day}, "
                f"{latest_close:,.4f} AUD/share): {ratio:.6g} ({ratio:.2%})."
            )
        else:
            st.warning(
                f"Cannot compute RMSE / latest {ticker}.AX close ({latest_day}): "
                "the close must be positive and finite."
            )
        st.plotly_chart(estimate_chart(ticker, pairs, selected_fit), width="stretch")
        recent_pairs = last_ten_years(pairs)
        recent_fit = fit_regression(recent_pairs)
        if recent_fit is None:
            st.warning(
                f"Cannot fit the last 10 years for {ticker}.AX: at least two matching "
                "dates with varying positive, finite gold prices are required."
            )
            continue
        selected_recent_fit = editable_fit(ticker, "last 10 years", recent_pairs, recent_fit)
        if selected_recent_fit is None:
            continue
        recent_mse = selected_recent_fit.squared_error / selected_recent_fit.observations
        st.caption(
            f"Last 10 years ({recent_pairs[0][0]} to {recent_pairs[-1][0]}): "
            f"ordinary least squares defaults on {recent_fit.observations:,} matching "
            f"daily closes: a = {recent_fit.a:.8g}, b = {recent_fit.b:.8g}. "
            f"Selected a = {selected_recent_fit.a:.8g} (AUD/share)/(AUD/troy oz); "
            f"b = {selected_recent_fit.b:.8g} AUD/share. "
            f"SSE: {selected_recent_fit.squared_error:,.6g} (AUD/share)^2; "
            f"MSE: {recent_mse:,.6g} (AUD/share)^2; "
            f"RMSE: {sqrt(recent_mse):,.6g} AUD/share."
        )
        st.plotly_chart(
            estimate_chart(ticker, recent_pairs, selected_recent_fit),
            width="stretch",
            key=f"{ticker}-ten-year-estimate",
        )
        st.subheader("Last five years: retrospective regression-valuation trading analysis")
        st.caption(
            "The inputs default to a fit of actual share close = a * same-date AUD GC=F "
            "close + b over this window. Editing a or b updates estimates, signals, "
            "thresholds, and trades. "
            "Valuation deviation = (actual close - estimated close) / estimated close; "
            "negative values are below the fitted price. Buy below the mean deviation "
            "minus 1.5 standard deviations and sell above the mean minus 0.5 standard "
            "deviations, both at the next stock trading open. Nonpositive estimates have "
            "no signal; missing opens skip execution. The full-window fit and thresholds "
            "use future observations (look-ahead bias): results are in-sample, NOT "
            "deployable signals. Returns compound completed trades only and exclude "
            "fees, slippage, dividends and corporate actions. Fund utilization is "
            "calendar days invested divided by days in the displayed window."
        )
        end = pairs[-1][0]
        start = years_ago(end, 5)
        stock_range = [
            (day, value)
            for day, value in shares
            if start <= day <= end and value.is_finite() and value > 0
        ]
        trading_pairs = aligned_closes(stock_range, gold)
        trading_fit = fit_regression(trading_pairs)
        if trading_fit is None:
            st.warning(f"Cannot fit five-year AUD gold valuation for {ticker}.AX.")
            continue
        selected_trading_fit = editable_fit(ticker, "last 5 years", trading_pairs, trading_fit)
        if selected_trading_fit is None:
            continue
        deviations = valuation_deviations(trading_pairs, selected_trading_fit)
        if not deviations:
            st.warning(f"No positive estimated closes for {ticker}.AX valuation signals.")
            continue
        opens = {
            day: price for day, price in opens_by_ticker[ticker].items() if start <= day <= end
        }
        buy_below, sell_above = deviation_thresholds(deviations)
        st.subheader(f"{ticker}.AX regression valuation ({stock_range[0][0]} to {end})")
        st.caption(
            f"{len(deviations):,} defined matched deviations; "
            f"OLS defaults a = {trading_fit.a:.8g}, b = {trading_fit.b:.8g}; "
            f"selected a = {selected_trading_fit.a:.8g}, b = {selected_trading_fit.b:.8g}; "
            f"buy below {buy_below:.2%} (mean - 1.5 sigma); "
            f"sell above {sell_above:.2%} (mean - 0.5 sigma). "
            "The right axis and histogram show signed deviations in percent."
        )
        st.plotly_chart(
            deviation_chart(ticker, stock_range, trading_pairs, selected_trading_fit, deviations),
            width="stretch",
            key=f"{ticker}-deviation",
        )
        st.plotly_chart(
            deviation_histogram(deviations),
            width="stretch",
            key=f"{ticker}-deviation-histogram",
        )
        display_strategy(ticker, stock_range, opens, deviations)


if __name__ == "__main__":
    main()
