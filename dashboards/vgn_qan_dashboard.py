"""Read-only Streamlit comparison of VGN and QAN closing share prices."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from math import floor, fsum, isfinite, log
from statistics import median, pstdev
from typing import cast

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import Engine, create_engine, or_, select

from asx_financials.config import get_settings
from asx_financials.infrastructure.persistence.schema import daily_prices

TICKERS = ("VGN", "QAN")


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

    @property
    def holding_days(self) -> int:
        return (self.sell_date - self.buy_date).days


@st.cache_resource
def _engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def load_prices(
    engine: Engine,
) -> tuple[dict[str, list[tuple[date, Decimal]]], dict[date, Decimal], dict[date, Decimal]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                daily_prices.c.ticker,
                daily_prices.c.trading_date,
                daily_prices.c.close,
                daily_prices.c.open,
            )
            .where(
                daily_prices.c.exchange_code == "ASX",
                daily_prices.c.ticker.in_(TICKERS),
                or_(
                    daily_prices.c.close.is_not(None),
                    daily_prices.c.open.is_not(None),
                ),
            )
            .order_by(daily_prices.c.trading_date)
        ).all()
    prices: dict[str, list[tuple[date, Decimal]]] = {ticker: [] for ticker in TICKERS}
    vgn_opens: dict[date, Decimal] = {}
    qan_opens: dict[date, Decimal] = {}
    for ticker, day, close, open_price in rows:
        if close is not None:
            prices[cast(str, ticker)].append((cast(date, day), cast(Decimal, close)))
        if open_price is not None and cast(Decimal, open_price) > 0:
            if ticker == "VGN":
                vgn_opens[cast(date, day)] = cast(Decimal, open_price)
            elif ticker == "QAN":
                qan_opens[cast(date, day)] = cast(Decimal, open_price)
    return prices, vgn_opens, qan_opens


def _display_prices(
    prices: dict[str, list[tuple[date, Decimal]]],
) -> dict[str, list[tuple[date, Decimal]]]:
    if not prices["VGN"]:
        return {ticker: [] for ticker in TICKERS}
    start, end = prices["VGN"][0][0], prices["VGN"][-1][0]
    return {
        ticker: [(day, close) for day, close in prices[ticker] if start <= day <= end]
        for ticker in TICKERS
    }


def price_chart(prices: dict[str, list[tuple[date, Decimal]]]) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    if not prices["VGN"]:
        return figure
    start, end = prices["VGN"][0][0], prices["VGN"][-1][0]
    for ticker, series in _display_prices(prices).items():
        if series:
            figure.add_trace(
                go.Scatter(
                    x=[day for day, _ in series],
                    y=[float(close) for _, close in series],
                    name=f"{ticker}.AX close",
                    mode="lines",
                    connectgaps=False,
                ),
                secondary_y=ticker == "QAN",
            )
    figure.update_xaxes(title_text="Trading date", range=[start, end] if start < end else None)
    figure.update_yaxes(title_text="VGN close (AUD/share)", secondary_y=False)
    figure.update_yaxes(title_text="QAN close (AUD/share)", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def _normalized_prices(
    series: list[tuple[date, Decimal]],
) -> list[tuple[date, Decimal]]:
    valid = [(day, close) for day, close in series if close.is_finite()]
    if not valid:
        return []
    low = min(close for _, close in valid)
    high = max(close for _, close in valid)
    return [
        (day, (close - low) / (high - low) if high > low else Decimal(0)) for day, close in valid
    ]


def normalized_chart(prices: dict[str, list[tuple[date, Decimal]]]) -> go.Figure:
    figure = go.Figure()
    if not prices["VGN"]:
        return figure
    start, end = prices["VGN"][0][0], prices["VGN"][-1][0]
    for ticker, series in _display_prices(prices).items():
        normalized = _normalized_prices(series)
        if normalized:
            figure.add_trace(
                go.Scatter(
                    x=[day for day, _ in normalized],
                    y=[float(value) for _, value in normalized],
                    name=f"{ticker}.AX normalized",
                    mode="lines",
                    connectgaps=False,
                )
            )
    figure.update_xaxes(title_text="Trading date", range=[start, end] if start < end else None)
    figure.update_yaxes(title_text="Normalized close (0-1)", range=[0, 1])
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def _normalized_ratios(
    displayed: dict[str, list[tuple[date, Decimal]]],
) -> list[float | None]:
    vgn_by_date = dict(_normalized_prices(displayed["VGN"]))
    qan_by_date = dict(_normalized_prices(displayed["QAN"]))
    ratios: list[float | None] = []
    for day, _ in displayed["VGN"]:
        denominator = qan_by_date.get(day)
        if day not in vgn_by_date or denominator is None or denominator == 0:
            ratios.append(None)
            continue
        value = float(vgn_by_date[day] / denominator)
        ratios.append(value if isfinite(value) else None)
    return ratios


def ratio_chart(prices: dict[str, list[tuple[date, Decimal]]]) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    if not prices["VGN"]:
        return figure
    start, end = prices["VGN"][0][0], prices["VGN"][-1][0]
    displayed = _display_prices(prices)
    shares = displayed["VGN"]
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in shares],
            y=[float(close) for _, close in shares],
            name="VGN.AX close",
            mode="lines",
            connectgaps=False,
        ),
        secondary_y=False,
    )
    ratios = _normalized_ratios(displayed)
    plotted_ratios = [
        value if value is not None and value > 0 and isfinite(value) else None for value in ratios
    ]
    if any(value is not None for value in plotted_ratios):
        figure.add_trace(
            go.Scatter(
                x=[day for day, _ in shares],
                y=plotted_ratios,
                name="VGN / QAN normalized close ratio",
                mode="lines",
                connectgaps=False,
            ),
            secondary_y=True,
        )
    figure.update_xaxes(title_text="Trading date", range=[start, end] if start < end else None)
    figure.update_yaxes(title_text="VGN close (AUD/share)", secondary_y=False)
    figure.update_yaxes(
        title_text="VGN / QAN normalized close ratio (log scale; dimensionless)",
        type="log",
        secondary_y=True,
    )
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def _log_ratios(prices: dict[str, list[tuple[date, Decimal]]]) -> list[float]:
    return [
        log(ratio)
        for ratio in _normalized_ratios(_display_prices(prices))
        if ratio is not None and ratio > 0 and isfinite(ratio)
    ]


def ratio_histogram(prices: dict[str, list[tuple[date, Decimal]]]) -> go.Figure:
    figure = go.Figure()
    valid = _log_ratios(prices)
    if valid:
        minimum, maximum = min(valid), max(valid)
        width = (maximum - minimum) / 20 if maximum > minimum else 1.0
        start = minimum if maximum > minimum else minimum - 10 * width
        counts = [0] * 20
        for ratio in valid:
            index = min(floor((ratio - start) / width), 19)
            counts[index] += 1
        figure.add_trace(
            go.Bar(
                x=[start + (index + 0.5) * width for index in range(20)],
                y=counts,
                width=width,
                name="ln(normalized VGN / QAN ratio)",
                hovertemplate="ln(ratio): %{x:.3f}<br>Trading dates: %{y}<extra></extra>",
            )
        )
        center = median(valid)
        markers = [("Median", center, "red")]
        if len(valid) > 1:
            sigma = pstdev(valid)
            if sigma > 0:
                markers.extend(
                    [
                        ("Median -1 SD", center - sigma, "green"),
                        ("Median +1 SD", center + sigma, "green"),
                        ("Median -2 SD", center - 2 * sigma, "orange"),
                        ("Median +2 SD", center + 2 * sigma, "orange"),
                    ]
                )
        for label, position, color in markers:
            figure.add_trace(
                go.Scatter(
                    x=[position, position],
                    y=[0, max(counts)],
                    mode="lines",
                    name=label,
                    line={"color": color, "width": 2, "dash": "dot"},
                    hovertemplate=f"{label}: %{{x:.3f}}<extra></extra>",
                )
            )
    figure.update_xaxes(title_text="ln(normalized VGN / QAN close ratio)")
    figure.update_yaxes(title_text="Trading dates (count)", rangemode="tozero")
    figure.update_layout(height=650, bargap=0)
    return figure


MIN_SPREAD_OBSERVATIONS = 20
SPREAD_LOOKBACK_DAYS = 365


@dataclass(frozen=True)
class SpreadPoint:
    day: date
    spread: float
    z_score: float


@dataclass(frozen=True)
class BetaPoint:
    day: date
    beta: float
    vgn_close: float
    hedged_qan_close: float


def _rolling_spread_results(
    prices: dict[str, list[tuple[date, Decimal]]],
) -> tuple[list[SpreadPoint], list[BetaPoint]]:
    """Fit one full-sample hedge ratio; standardize spreads against prior closes."""
    qan = dict(prices["QAN"])
    aligned = [
        (day, float(vgn_close), float(qan[day]))
        for day, vgn_close in prices["VGN"]
        if day in qan
        and vgn_close.is_finite()
        and qan[day].is_finite()
        and vgn_close > 0
        and qan[day] > 0
    ]
    if len(aligned) < 2:
        return [], []
    qan_mean = fsum(q for _, _, q in aligned) / len(aligned)
    vgn_mean = fsum(v for _, v, _ in aligned) / len(aligned)
    qan_variance = fsum((q - qan_mean) ** 2 for _, _, q in aligned)
    if qan_variance <= 1e-12 * max(1.0, qan_mean * qan_mean) or not isfinite(qan_variance):
        return [], []
    beta = fsum((q - qan_mean) * (v - vgn_mean) for _, v, q in aligned) / qan_variance
    if not isfinite(beta):
        return [], []
    points: list[SpreadPoint] = []
    betas: list[BetaPoint] = []
    for index, (day, current_vgn, current_qan) in enumerate(aligned):
        hedged_qan = beta * current_qan
        if isfinite(hedged_qan):
            betas.append(BetaPoint(day, beta, current_vgn, hedged_qan))
        history = [
            (past_vgn, past_qan)
            for past_day, past_vgn, past_qan in aligned[:index]
            if past_day >= day - timedelta(days=SPREAD_LOOKBACK_DAYS)
        ]
        if len(history) < MIN_SPREAD_OBSERVATIONS:
            continue
        if not isfinite(hedged_qan):
            continue
        reference = [v - beta * q for v, q in history]
        spread_mean = fsum(reference) / len(reference)
        sigma = pstdev(reference)
        current_spread = current_vgn - hedged_qan
        if sigma <= 1e-10 * max(1.0, abs(spread_mean)) or not all(
            isfinite(x) for x in (sigma, spread_mean, current_spread)
        ):
            continue
        z_score = (current_spread - spread_mean) / sigma
        if isfinite(z_score):
            points.append(SpreadPoint(day, current_spread, z_score))
    return points, betas


def rolling_spread_scores(
    prices: dict[str, list[tuple[date, Decimal]]],
) -> list[SpreadPoint]:
    return _rolling_spread_results(prices)[0]


def full_sample_spread_scores(betas: list[BetaPoint]) -> list[SpreadPoint]:
    """Use the same fixed beta with a full-sample spread reference."""
    spreads = [(point.day, point.vgn_close - point.hedged_qan_close) for point in betas]
    if len(spreads) < 2 or not all(isfinite(value) for _, value in spreads):
        return []
    center = fsum(value for _, value in spreads) / len(spreads)
    sigma = pstdev(value for _, value in spreads)
    if not isfinite(sigma) or sigma <= 1e-10 * max(1.0, abs(center)):
        return []
    return [SpreadPoint(day, spread, (spread - center) / sigma) for day, spread in spreads]


def spread_histogram(betas: list[BetaPoint]) -> go.Figure:
    """Histogram of raw eligible spreads used for the full-sample Z reference."""
    figure = go.Figure()
    values = [
        point.vgn_close - point.hedged_qan_close
        for point in betas
        if isfinite(point.vgn_close - point.hedged_qan_close)
    ]
    if values:
        minimum, maximum = min(values), max(values)
        width = (maximum - minimum) / 20 if maximum > minimum else 1.0
        start = minimum if maximum > minimum else minimum - 10 * width
        counts = [0] * 20
        for value in values:
            counts[min(max(floor((value - start) / width), 0), 19)] += 1
        figure.add_trace(
            go.Bar(
                x=[start + (index + 0.5) * width for index in range(20)],
                y=counts,
                width=width,
                name="Raw VGN - beta * QAN spread",
                hovertemplate="Spread: %{x:.4f} AUD/share<br>Trading dates: %{y}<extra></extra>",
            )
        )
        if maximum > minimum and len(values) > 1:
            center = fsum(values) / len(values)
            sigma = pstdev(values)
            for label, threshold, color in [
                ("Full-sample mean", center, "red"),
                ("Long entry: mean - 1.5 SD", center - 1.5 * sigma, "orange"),
                ("Long exit: mean - 0.5 SD", center - 0.5 * sigma, "green"),
                ("Short exit: mean + 0.5 SD", center + 0.5 * sigma, "green"),
                ("Short entry: mean + 1.5 SD", center + 1.5 * sigma, "orange"),
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
    figure.update_xaxes(title_text="Raw VGN - beta * QAN spread (AUD/share)")
    figure.update_yaxes(title_text="Trading dates (count)", rangemode="tozero")
    figure.update_layout(height=650, bargap=0)
    return figure


def beta_chart(shares: list[tuple[date, Decimal]], betas: list[BetaPoint]) -> go.Figure:
    by_date = {point.day: point for point in betas}
    days = [day for day, _ in shares]
    figure = go.Figure(
        go.Scatter(
            x=days,
            y=[by_date[day].beta if day in by_date else None for day in days],
            name="Fixed full-sample VGN on QAN beta",
            mode="lines",
            connectgaps=False,
        )
    )
    if len(days) > 1:
        figure.update_xaxes(range=[days[0], days[-1]])
    figure.update_xaxes(title_text="Trading date")
    figure.update_yaxes(title_text="Fixed full-sample OLS beta (dimensionless slope)")
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def hedged_price_chart(shares: list[tuple[date, Decimal]], betas: list[BetaPoint]) -> go.Figure:
    by_date = {point.day: point for point in betas}
    days = [day for day, _ in shares]
    figure = go.Figure()
    for name, field in [
        ("VGN.AX close", "vgn_close"),
        ("Fixed beta * QAN.AX close", "hedged_qan_close"),
    ]:
        figure.add_trace(
            go.Scatter(
                x=days,
                y=[getattr(by_date[day], field) if day in by_date else None for day in days],
                name=name,
                mode="lines",
                connectgaps=False,
            )
        )
    if len(days) > 1:
        figure.update_xaxes(range=[days[0], days[-1]])
    figure.update_xaxes(title_text="Trading date")
    figure.update_yaxes(title_text="Closing price / beta * QAN close (AUD/share)")
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def spread_chart(shares: list[tuple[date, Decimal]], points: list[SpreadPoint]) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    if points:
        by_date = {point.day: point for point in points}
        days = [day for day, _ in shares]
        figure.add_trace(
            go.Scatter(
                x=days,
                y=[by_date[day].spread if day in by_date else None for day in days],
                name="VGN - beta * QAN spread",
                mode="lines",
                connectgaps=False,
            ),
            secondary_y=False,
        )
        figure.add_trace(
            go.Scatter(
                x=days,
                y=[by_date[day].z_score if day in by_date else None for day in days],
                name="Rolling spread Z-score",
                mode="lines",
                connectgaps=False,
            ),
            secondary_y=True,
        )
        for threshold, label, color in [
            (-2, "Buy below -2", "red"),
            (-1, "Sell above -1", "green"),
        ]:
            figure.add_trace(
                go.Scatter(
                    x=[points[0].day, points[-1].day],
                    y=[threshold, threshold],
                    name=label,
                    mode="lines",
                    line={"color": color, "dash": "dot"},
                ),
                secondary_y=True,
            )
    if shares:
        figure.update_xaxes(range=[shares[0][0], shares[-1][0]] if len(shares) > 1 else None)
    figure.update_xaxes(title_text="Trading date")
    figure.update_yaxes(title_text="VGN - beta * QAN spread (AUD/share)", secondary_y=False)
    figure.update_yaxes(title_text="Rolling spread Z-score (dimensionless)", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=650)
    return figure


@dataclass(frozen=True)
class PairedPosition:
    direction: str
    signal_date: date
    entry_date: date
    vgn_entry: Decimal
    qan_entry: Decimal
    beta: Decimal


@dataclass(frozen=True)
class PairedTrade:
    position: PairedPosition
    exit_signal_date: date
    exit_date: date
    vgn_exit: Decimal
    qan_exit: Decimal

    @property
    def holding_days(self) -> int:
        return (self.exit_date - self.position.entry_date).days

    @property
    def vgn_pnl(self) -> Decimal:
        sign = Decimal(1) if self.position.direction == "Long VGN / short QAN" else Decimal(-1)
        return sign * (self.vgn_exit - self.position.vgn_entry)

    @property
    def qan_pnl(self) -> Decimal:
        sign = Decimal(-1) if self.position.direction == "Long VGN / short QAN" else Decimal(1)
        return sign * self.position.beta * (self.qan_exit - self.position.qan_entry)

    @property
    def net_pnl(self) -> Decimal:
        return self.vgn_pnl + self.qan_pnl

    @property
    def return_pct(self) -> Decimal:
        exposure = abs(self.position.vgn_entry) + abs(self.position.beta * self.position.qan_entry)
        return self.net_pnl / exposure * 100


def backtest_paired_trades(
    points: list[SpreadPoint],
    betas: list[BetaPoint],
    vgn_opens: dict[date, Decimal],
    qan_opens: dict[date, Decimal],
    final_date: date | None = None,
) -> tuple[list[PairedTrade], PairedPosition | None]:
    """Signal at aligned closes, fill both legs at the next aligned open or neither."""
    if not betas or not points:
        return [], None
    beta = betas[0].beta
    if not isfinite(beta) or abs(beta) <= 1e-12:
        return [], None
    hedge = Decimal(str(beta))
    signals = {point.day: point.z_score for point in points}
    trades: list[PairedTrade] = []
    position: PairedPosition | None = None
    pending: tuple[str, date] | None = None
    end = final_date if final_date is not None else betas[-1].day
    days = sorted(
        {point.day for point in betas if point.day <= end}
        | {day for day in vgn_opens.keys() & qan_opens.keys() if betas[0].day <= day <= end}
    )
    for day in days:
        if pending is not None:
            action, signal_date = pending
            if day <= signal_date:
                continue
            vgn_open, qan_open = vgn_opens.get(day), qan_opens.get(day)
            if (
                vgn_open is not None
                and qan_open is not None
                and vgn_open.is_finite()
                and qan_open.is_finite()
                and vgn_open > 0
                and qan_open > 0
            ):
                pending = None
                if action == "exit" and position is not None:
                    trades.append(PairedTrade(position, signal_date, day, vgn_open, qan_open))
                    position = None
                elif action != "exit" and position is None:
                    position = PairedPosition(action, signal_date, day, vgn_open, qan_open, hedge)
            # A pending order waits for two positive opens; never reverse on its fill day.
            continue
        score = signals.get(day)
        if score is None or not isfinite(score):
            continue
        if position is None:
            if score < -1.5:
                pending = "Long VGN / short QAN", day
            elif score > 1.5:
                pending = "Short VGN / long QAN", day
        elif (position.direction == "Long VGN / short QAN" and score > -0.5) or (
            position.direction == "Short VGN / long QAN" and score < 0.5
        ):
            pending = "exit", day
    return trades, position


def paired_spread_chart(
    shares: list[tuple[date, Decimal]],
    points: list[SpreadPoint],
    trades: list[PairedTrade],
    open_position: PairedPosition | None,
) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    days = [day for day, _ in shares]
    by_date = {point.day: point for point in points}
    figure.add_trace(
        go.Scatter(
            x=days,
            y=[by_date[day].spread if day in by_date else None for day in days],
            name="Full-sample VGN - beta * QAN spread",
            mode="lines",
            connectgaps=False,
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=days,
            y=[by_date[day].z_score if day in by_date else None for day in days],
            name="Full-sample spread Z-score",
            mode="lines",
            connectgaps=False,
        ),
        secondary_y=True,
    )
    if days:
        for threshold, name in [
            (-1.5, "Long entry < -1.5"),
            (-0.5, "Long exit > -0.5"),
            (0.5, "Short exit < +0.5"),
            (1.5, "Short entry > +1.5"),
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
        figure.update_xaxes(range=[days[0], days[-1]] if len(days) > 1 else None)
    for trade in trades:
        figure.add_vline(x=trade.position.entry_date.isoformat(), line_color="red", line_dash="dot")
        figure.add_vline(x=trade.exit_date.isoformat(), line_color="green", line_dash="dot")
    if open_position is not None:
        figure.add_vline(x=open_position.entry_date.isoformat(), line_color="red", line_dash="dot")
    figure.update_xaxes(title_text="Trading date")
    figure.update_yaxes(title_text="Spread (AUD/share)", secondary_y=False)
    figure.update_yaxes(title_text="Full-sample Z-score (dimensionless)", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def paired_trade_chart(
    shares: list[tuple[date, Decimal]],
    betas: list[BetaPoint],
    trades: list[PairedTrade],
    open_position: PairedPosition | None,
) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    days = [day for day, _ in shares]
    figure.add_trace(
        go.Scatter(
            x=days,
            y=[float(close) for _, close in shares],
            name="VGN.AX close (AUD/share)",
            mode="lines",
            connectgaps=False,
        ),
        secondary_y=False,
    )
    hedged_by_date = {point.day: point.hedged_qan_close for point in betas}
    figure.add_trace(
        go.Scatter(
            x=days,
            y=[
                hedged_by_date[day]
                if day in hedged_by_date and isfinite(hedged_by_date[day])
                else None
                for day in days
            ],
            name="Fixed beta * QAN.AX close (AUD/share)",
            mode="lines",
            connectgaps=False,
        ),
        secondary_y=True,
    )
    for trade in trades:
        figure.add_vline(x=trade.position.entry_date.isoformat(), line_color="red", line_dash="dot")
        figure.add_vline(x=trade.exit_date.isoformat(), line_color="green", line_dash="dot")
    if open_position is not None:
        figure.add_vline(x=open_position.entry_date.isoformat(), line_color="red", line_dash="dot")
    closes = dict(shares)
    for direction, symbol in [
        ("Long VGN / short QAN", "triangle-up"),
        ("Short VGN / long QAN", "triangle-down"),
    ]:
        for action, color, marker in [
            ("Entry", "red", symbol),
            ("Exit", "green", symbol),
        ]:
            dates = [
                trade.position.entry_date if action == "Entry" else trade.exit_date
                for trade in trades
                if trade.position.direction == direction
            ]
            if (
                action == "Entry"
                and open_position is not None
                and open_position.direction == direction
            ):
                dates.append(open_position.entry_date)
            visible = [day for day in dates if day in closes]
            figure.add_trace(
                go.Scatter(
                    x=visible,
                    y=[float(closes[day]) for day in visible],
                    name=f"{direction} {action.lower()}",
                    mode="markers",
                    marker={"color": color, "symbol": marker, "size": 12},
                ),
                secondary_y=False,
            )
    if shares:
        figure.update_xaxes(range=[shares[0][0], shares[-1][0]] if len(shares) > 1 else None)
    figure.update_xaxes(title_text="Trading date")
    figure.update_yaxes(title_text="VGN close (AUD/share)", secondary_y=False)
    figure.update_yaxes(title_text="Fixed beta * QAN close (AUD/share)", secondary_y=True)
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def paired_summary(trades: list[PairedTrade]) -> Decimal:
    growth = Decimal(1)
    for trade in trades:
        growth *= 1 + trade.return_pct / 100
    return (growth - 1) * 100


def paired_utilization(
    trades: list[PairedTrade],
    open_position: PairedPosition | None,
    start: date,
    end: date,
) -> Decimal:
    total_days = (end - start).days + 1
    if total_days <= 0:
        return Decimal(0)
    used = sum(trade.holding_days for trade in trades)
    if open_position is not None:
        used += (end - open_position.entry_date).days + 1
    return Decimal(used) / total_days * 100


def display_paired_results(
    shares: list[tuple[date, Decimal]],
    betas: list[BetaPoint],
    trades: list[PairedTrade],
    open_position: PairedPosition | None,
) -> None:
    st.plotly_chart(paired_trade_chart(shares, betas, trades, open_position), width="stretch")
    if trades:
        st.dataframe(
            [
                {
                    "Direction": trade.position.direction,
                    "Entry signal date": trade.position.signal_date,
                    "Entry execution date (next paired open)": trade.position.entry_date,
                    "VGN entry open (AUD/share)": float(trade.position.vgn_entry),
                    "QAN entry open (AUD/share)": float(trade.position.qan_entry),
                    "Exit signal date": trade.exit_signal_date,
                    "Exit execution date (next paired open)": trade.exit_date,
                    "VGN exit open (AUD/share)": float(trade.vgn_exit),
                    "QAN exit open (AUD/share)": float(trade.qan_exit),
                    "VGN P&L (AUD)": float(trade.vgn_pnl),
                    "QAN P&L (AUD)": float(trade.qan_pnl),
                    "Net P&L (AUD)": float(trade.net_pnl),
                    "Net return (%)": float(trade.return_pct),
                    "Holding days (calendar)": trade.holding_days,
                }
                for trade in trades
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No completed paired trades for this period.")
    utilization = paired_utilization(trades, open_position, shares[0][0], shares[-1][0])
    st.write(
        f"Completed paired trades: {len(trades)} | "
        f"Accumulated profit/loss (compounded): {paired_summary(trades):+.2f}% | "
        f"Fund utilization: {utilization:.2f}%"
    )
    if open_position is not None:
        st.caption(
            f"Open {open_position.direction} position since {open_position.entry_date}: "
            f"VGN {open_position.vgn_entry} and QAN {open_position.qan_entry} AUD/share "
            f"at beta {open_position.beta}; excluded from realized return."
        )


def backtest_trades(
    prices: dict[str, list[tuple[date, Decimal]]], opens: dict[date, Decimal]
) -> tuple[list[Trade], tuple[date, date] | None]:
    shares = _display_prices(prices)["VGN"]
    ratios = _normalized_ratios(_display_prices(prices))
    logged = [
        (day, log(ratio))
        for (day, _), ratio in zip(shares, ratios, strict=True)
        if ratio is not None and ratio > 0 and isfinite(ratio)
    ]
    if len(logged) < 2:
        return [], None
    values = [value for _, value in logged]
    sigma = pstdev(values)
    if sigma == 0:
        return [], None
    center = median(values)
    buy_below, sell_above = center - 2 * sigma, center - sigma
    return _execute_signals(shares, opens, dict(logged), buy_below, sell_above)


def backtest_spread_trades(
    prices: dict[str, list[tuple[date, Decimal]]],
    opens: dict[date, Decimal],
    points: list[SpreadPoint] | None = None,
) -> tuple[list[Trade], tuple[date, date] | None]:
    shares = _display_prices(prices)["VGN"]
    if not shares:
        return [], None
    scores = rolling_spread_scores(prices) if points is None else points
    return _execute_signals(
        shares, opens, {point.day: point.z_score for point in scores}, -2.0, -1.0
    )


def _execute_signals(
    shares: list[tuple[date, Decimal]],
    opens: dict[date, Decimal],
    signals: dict[date, float],
    buy_below: float,
    sell_above: float,
) -> tuple[list[Trade], tuple[date, date] | None]:
    trades: list[Trade] = []
    position: tuple[date, date, Decimal] | None = None
    pending: tuple[str, date] | None = None
    for day in sorted({day for day, _ in shares} | {day for day in opens if day <= shares[-1][0]}):
        if pending is not None:
            action, signal_date = pending
            open_price = opens.get(day)
            pending = None
            if open_price is None or open_price <= 0 or not open_price.is_finite():
                continue
            if action == "buy":
                position = signal_date, day, open_price
            elif position is not None:
                buy_signal, buy_date, buy_price = position
                trades.append(Trade(buy_signal, buy_date, buy_price, signal_date, day, open_price))
                position = None
        value = signals.get(day)
        if value is not None:
            if position is None and value < buy_below:
                pending = "buy", day
            elif position is not None and value > sell_above:
                pending = "sell", day
    return trades, (position[0], position[1]) if position is not None else None


def trade_chart(
    shares: list[tuple[date, Decimal]],
    trades: list[Trade],
    open_buy: tuple[date, date] | None,
) -> go.Figure:
    figure = go.Figure(
        go.Scatter(
            x=[day for day, _ in shares],
            y=[float(close) for _, close in shares],
            name="VGN.AX close (AUD/share)",
            mode="lines",
        )
    )
    for trade in trades:
        figure.add_vline(x=trade.buy_date.isoformat(), line_color="red", line_dash="dot")
        figure.add_vline(x=trade.sell_date.isoformat(), line_color="green", line_dash="dot")
    if open_buy is not None:
        figure.add_vline(x=open_buy[1].isoformat(), line_color="red", line_dash="dot")
    if shares:
        figure.update_xaxes(range=[shares[0][0], shares[-1][0]] if len(shares) > 1 else None)
    figure.update_xaxes(title_text="Trading date")
    figure.update_yaxes(title_text="VGN close (AUD/share)")
    figure.update_layout(hovermode="x unified", height=650)
    return figure


def trade_summary(trades: list[Trade]) -> Decimal:
    growth = Decimal(1)
    for trade in trades:
        growth *= trade.sell_price / trade.buy_price
    return (growth - 1) * 100


def fund_utilization(
    trades: list[Trade], open_buy: tuple[date, date] | None, start: date, end: date
) -> Decimal:
    total_days = (end - start).days + 1
    if total_days <= 0:
        return Decimal(0)
    used_days = sum(
        max(0, (min(trade.sell_date, end + timedelta(days=1)) - max(trade.buy_date, start)).days)
        for trade in trades
    )
    if open_buy is not None:
        used_days += max(0, (end - max(open_buy[1], start)).days + 1)
    return Decimal(used_days) / total_days * 100


def display_trade_results(
    shares: list[tuple[date, Decimal]],
    trades: list[Trade],
    open_buy: tuple[date, date] | None,
) -> None:
    st.plotly_chart(trade_chart(shares, trades, open_buy), width="stretch")
    if trades:
        st.dataframe(
            [
                {
                    "Buy signal date": trade.buy_signal_date,
                    "Buy date": trade.buy_date,
                    "Buy price (AUD/share)": float(trade.buy_price),
                    "Sell signal date": trade.sell_signal_date,
                    "Sell date": trade.sell_date,
                    "Sell price (AUD/share)": float(trade.sell_price),
                    "Profit/loss (%)": float(trade.return_pct),
                    "Holding days": trade.holding_days,
                }
                for trade in trades
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No completed trades for this period.")
    st.write(
        f"Completed trades: {len(trades)} | "
        f"Accumulated profit/loss (compounded): {trade_summary(trades):+.2f}% | "
        f"Fund utilization: {fund_utilization(trades, open_buy, shares[0][0], shares[-1][0]):.2f}%"
    )
    if open_buy is not None:
        st.caption("One position remains open; it is excluded from completed trade results.")


def main() -> None:
    st.set_page_config(page_title="VGN vs QAN prices", layout="wide")
    st.title("VGN vs QAN share prices")
    st.caption(
        "Stored ASX daily unadjusted closing prices in AUD per share. "
        "The first graph uses separate VGN (left) and QAN (right) AUD/share axes. "
        "The second independently scales each ticker's displayed closes to 0-1 "
        "(constant prices appear at 0). The third compares VGN's raw closing price "
        "(left, linear) with normalized VGN close divided by normalized QAN close "
        "(right, logarithmic ratio axis). All graphs show only VGN's available "
        "date range; missing dates and zero, negative or nonfinite ratios leave "
        "gaps on the log-scale ratio axis. The fourth graph counts the "
        "natural log of positive finite normalized-price ratios in 20 equal-width buckets. "
        "Red marks the median; green and orange mark median ±1 and ±2 population "
        "standard deviations of the logged values, respectively. "
        "This page reads PostgreSQL data only."
    )
    try:
        prices, opens, qan_opens = load_prices(_engine())
    except Exception:
        st.error("Could not read PostgreSQL. Check DATABASE_URL and run Alembic migrations.")
        return
    for ticker in TICKERS:
        if not prices[ticker]:
            st.warning(f"No stored {ticker}.AX daily closes. Ingest {ticker} first.")
    if not prices["VGN"]:
        return
    st.plotly_chart(price_chart(prices), width="stretch")
    st.subheader("Normalized closing prices")
    st.plotly_chart(normalized_chart(prices), width="stretch")
    st.subheader("VGN price and normalized VGN/QAN ratio")
    st.plotly_chart(ratio_chart(prices), width="stretch")
    st.subheader("Log normalized VGN/QAN ratio distribution")
    histogram = ratio_histogram(prices)
    if histogram.data:
        st.plotly_chart(histogram, width="stretch")
    else:
        st.info("No positive finite normalized VGN/QAN ratios to plot on a log scale.")
    st.subheader("VGN trading strategy backtest")
    st.caption(
        "Buy at the next VGN trading day's positive open after ln(normalized VGN/QAN ratio) "
        "is strictly below the full-period median - 2 population SD; sell at the next "
        "trading day's positive open after it is strictly above median - 1 population SD. "
        "Median and SD use the histogram's positive finite logged ratios; at least two "
        "nonconstant observations are required. One position at a time; missing opens "
        "cancel pending orders, and days without a valid ratio cannot signal. "
        "Normalization and thresholds use the full displayed period (look-ahead bias). "
        "Returns compound completed trades only, excluding fees, slippage, dividends, "
        "and corporate-action adjustments. Red/green dotted markers show executed "
        "buys/sells; fund utilization is calendar days held (buy inclusive, sell "
        "exclusive, open positions through period end) divided by the full VGN period."
    )
    shares = _display_prices(prices)["VGN"]
    trades, open_buy = backtest_trades(prices, opens)
    display_trade_results(shares, trades, open_buy)
    st.subheader("Rolling linear-spread Z-score strategy")
    st.caption(
        "Fit one fixed beta as the slope of VGN on QAN with an intercept using ALL "
        "overlapping positive finite VGN-period daily closes (at least 2 pairs). "
        "The spread is VGN - beta * QAN in AUD/share (without subtracting the fitted "
        "intercept). On each date, the same fixed beta applied to overlapping closes "
        "from the prior 365 calendar days (excluding today) defines the rolling "
        "reference mean and population SD. At least 20 matching prior closes and "
        "nonzero spread SD are required for a Z-score or signal. Missing/invalid "
        "estimates leave chart gaps; negative beta can make beta * QAN negative. "
        "The fixed beta uses future data for earlier dates (look-ahead bias), "
        "although the rolling reference excludes the current and future closes. "
        "Buy VGN when Z < -2 and sell when Z > -1 (strict thresholds), executing "
        "at the next VGN trading day's positive open. Red/green dotted markers show "
        "executed buys/sells. Missing opens cancel pending orders; only one position "
        "is held. Returns compound completed trades, excluding fees, slippage, "
        "dividends and corporate-action adjustments. Utilization includes open positions."
    )
    points, betas = _rolling_spread_results(prices)
    if betas:
        st.subheader("Fixed full-sample VGN on QAN beta")
        st.plotly_chart(beta_chart(shares, betas), width="stretch")
        st.subheader("VGN close and fixed beta * QAN close")
        st.plotly_chart(hedged_price_chart(shares, betas), width="stretch")
    else:
        st.info("Insufficient valid overlapping history to calculate fixed beta.")
    if points:
        st.plotly_chart(spread_chart(shares, points), width="stretch")
    else:
        st.info("Insufficient valid overlapping history to calculate rolling spread Z-scores.")
    spread_trades, spread_open_buy = backtest_spread_trades(prices, opens, points)
    display_trade_results(shares, spread_trades, spread_open_buy)
    st.subheader("Two-sided paired VGN/QAN spread strategy")
    st.caption(
        "Using the same fixed full-sample intercept-fitted beta, compute VGN close - "
        "beta * QAN close (without intercept) on overlapping positive finite closes. "
        "The ONE full-sample spread mean and population SD define each day's Z-score "
        "(at least two observations and nonzero SD required). The 20-bin raw spread "
        "histogram marks the mean, entry at mean +/- 1.5 SD and exit at mean +/- 0.5 SD. "
        "Both beta and reference "
        "use future observations on earlier dates: substantial look-ahead bias. "
        "Strict Z < -1.5 enters long 1 VGN / short beta QAN; Z > -0.5 exits. "
        "Strict Z > +1.5 enters short 1 VGN / long beta QAN; Z < +0.5 exits. "
        "Signed beta quantities apply even if beta is negative, reversing the named "
        "QAN side. Signals use paired closes; BOTH legs execute together at the next "
        "common trading day after the close trigger with positive finite VGN and QAN "
        "opens (never on the signal day, including across weekends/holidays). "
        "The trade table separates close-signal dates from both-legs open-execution "
        "dates. Orders await a valid paired open; unmatched/invalid opens never fill one leg. "
        "No overlapping positions or same-bar reversals. Red dotted lines denote "
        "entries and green dotted lines exits; up/down triangles distinguish long/short "
        "VGN directions on the price chart. That chart overlays VGN closing price "
        "on the left AUD/share axis and fixed beta times aligned QAN closing price "
        "on the right linear AUD/share axis (possibly negative if beta is negative). "
        "Net P&L is the sum of signed per-leg P&L for 1 VGN and beta QAN shares; "
        "return divides net P&L by gross opening exposure |VGN open| + |beta * QAN open|. "
        "Returns compound completed trades only; utilization counts held calendar days "
        "from entry inclusive to exit exclusive or final VGN date for open positions. "
        "No fees, borrowing costs, dividends, slippage or corporate actions are included."
    )
    paired_points = full_sample_spread_scores(betas)
    paired_trades, paired_open = backtest_paired_trades(
        paired_points, betas, opens, qan_opens, shares[-1][0]
    )
    if paired_points:
        st.plotly_chart(
            paired_spread_chart(shares, paired_points, paired_trades, paired_open), width="stretch"
        )
    else:
        st.info("Insufficient spread variation for full-sample paired Z-scores.")
    st.subheader("Full-sample raw spread distribution")
    histogram = spread_histogram(betas)
    if histogram.data:
        st.plotly_chart(histogram, width="stretch")
    else:
        st.info("No eligible overlapping VGN/QAN closes for a spread histogram.")
    display_paired_results(shares, betas, paired_trades, paired_open)


if __name__ == "__main__":
    main()
