"""Interactive trade and equity chart for a completed backtest."""

from datetime import date

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from backtest.engine import Bar, Result


def trading_chart(
    bars: list[Bar], result: Result, moving_average_windows: tuple[int, ...] = ()
) -> go.Figure:
    """Plot adjusted close, optional moving averages, fills, and portfolio equity."""
    first, last = result.equity[0][0], result.equity[-1][0]
    symbols = sorted({bar.ticker for bar in bars if first <= bar.day <= last})
    figure = make_subplots(
        rows=len(symbols) + 1,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        subplot_titles=[f"{symbol} adjusted price (AUD)" for symbol in symbols]
        + ["Portfolio equity (AUD)"],
    )
    for row, symbol in enumerate(symbols, 1):
        full_history = sorted(
            (bar for bar in bars if bar.ticker == symbol),
            key=lambda bar: bar.day,
        )
        history = [bar for bar in full_history if first <= bar.day <= last]
        figure.add_trace(
            go.Scatter(
                x=[bar.day for bar in history],
                y=[bar.close for bar in history],
                mode="lines",
                name=f"{symbol} adjusted close",
                line={"color": "#64748b", "width": 1.5},
            ),
            row=row,
            col=1,
        )
        for color_index, window in enumerate(moving_average_windows):
            if window < 1:
                raise ValueError("Moving-average windows must be positive")
            figure.add_trace(
                go.Scatter(
                    x=[bar.day for bar in history],
                    y=[
                        (
                            sum(
                                prior.close
                                for prior in full_history[index - window + 1 : index + 1]
                            )
                            / window
                            if index + 1 >= window
                            else None
                        )
                        for index, bar in enumerate(full_history)
                        if first <= bar.day <= last
                    ],
                    mode="lines",
                    name=f"{symbol} {window}-day MA",
                    line={
                        "color": ("#eab308", "#0d9488", "#9333ea")[color_index % 3],
                        "width": 1.5,
                    },
                ),
                row=row,
                col=1,
            )
        entries: list[tuple[date, float, int]] = [
            (trade.entry_day, trade.entry_price, trade.shares)
            for trade in result.trades
            if trade.ticker == symbol
        ]
        position = result.open_positions.get(symbol)
        if position is not None:
            entries.append((position.entry_day, position.entry_price, position.shares))
        exits = [
            (trade.exit_day, trade.exit_price, trade.shares)
            for trade in result.trades
            if trade.ticker == symbol
        ]
        for label, fills, color, marker in (
            ("Buy", entries, "#16a34a", "triangle-up"),
            ("Sell", exits, "#dc2626", "triangle-down"),
        ):
            figure.add_trace(
                go.Scatter(
                    x=[day for day, _, _ in fills],
                    y=[price for _, price, _ in fills],
                    customdata=[shares for _, _, shares in fills],
                    mode="markers",
                    name=f"{symbol} {label.lower()}",
                    marker={"symbol": marker, "size": 11, "color": color},
                    hovertemplate=(
                        f"{symbol} {label.lower()}<br>"
                        "%{x|%Y-%m-%d}<br>"
                        "Adjusted fill: AUD %{y:.2f}<br>"
                        "Shares: %{customdata}<extra></extra>"
                    ),
                ),
                row=row,
                col=1,
            )
        figure.update_yaxes(title_text="AUD/share", row=row, col=1)

    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in result.equity],
            y=[value for _, value in result.equity],
            mode="lines",
            name="Portfolio equity",
            line={"color": "#2563eb", "width": 2},
            hovertemplate="%{x|%Y-%m-%d}<br>AUD %{y:,.2f}<extra></extra>",
        ),
        row=len(symbols) + 1,
        col=1,
    )
    figure.update_yaxes(title_text="AUD", row=len(symbols) + 1, col=1)
    figure.update_xaxes(title_text="Trading date", row=len(symbols) + 1, col=1)
    figure.update_layout(
        title="Trend-following backtest: executed trades and equity",
        height=680 * (len(symbols) + 1),
        hovermode="x",
        legend={"orientation": "h", "y": -0.07},
    )
    return figure
