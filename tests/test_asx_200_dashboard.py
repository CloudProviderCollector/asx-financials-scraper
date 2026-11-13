"""ASX 200 dashboard reads and presents every stored snapshot field."""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from dashboards import asx_200_dashboard as dashboard


def _snapshots() -> list[dict]:
    return [
        {
            "session_date": date(2026, 11, 11),
            "symbol": "^AXJO",
            "currency": "AUD",
            "open": Decimal("9010"),
            "high": Decimal("9170"),
            "low": Decimal("9000"),
            "close": Decimal("9130"),
            "volume": None,
            "regular_market_price": Decimal("9130"),
            "previous_close": Decimal("9129"),
            "average_volume": 808938,
            "average_volume_10days": 857600,
            "trailing_pe": None,
            "forward_pe": None,
            "quote_time": datetime(2026, 11, 11, 6, tzinfo=UTC),
            "info": {"fiftyTwoWeekHigh": 9296.7, "shortName": "S&P/ASX 200 [XJO]"},
            "fetched_at": datetime(2026, 11, 11, 8, tzinfo=UTC),
        },
        {
            "session_date": date(2026, 11, 10),
            "symbol": "^AXJO",
            "currency": "AUD",
            "open": Decimal("9029.7"),
            "high": Decimal("9159.8"),
            "low": Decimal("9029.7"),
            "close": Decimal("9129"),
            "volume": 900000,
            "regular_market_price": Decimal("9129"),
            "previous_close": Decimal("9020.8"),
            "average_volume": 808000,
            "average_volume_10days": 850000,
            "trailing_pe": Decimal("20.5"),
            "forward_pe": None,
            "quote_time": datetime(2026, 11, 10, 6, tzinfo=UTC),
            "info": {},
            "fetched_at": datetime(2026, 11, 10, 8, tzinfo=UTC),
        },
    ]


def test_loads_all_columns_and_sessions_in_descending_order() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value = iter(_snapshots())

    assert dashboard.load_snapshots(engine) == _snapshots()
    statement = str(connection.execute.call_args.args[0])
    assert "SELECT * FROM asx_200_snapshots ORDER BY session_date DESC" in statement


def test_renders_all_columns_history_and_selected_metadata() -> None:
    snapshots = _snapshots()
    with patch.object(dashboard, "st") as streamlit:
        streamlit.columns.return_value = [MagicMock() for _ in range(5)]
        streamlit.selectbox.return_value = date(2026, 11, 11)
        dashboard.show_snapshots(snapshots)

    figure = streamlit.plotly_chart.call_args.args[0]
    assert list(figure.data[0].x) == [date(2026, 11, 10), date(2026, 11, 11)]
    assert list(figure.data[0].y) == [9129, 9130]
    assert list(figure.data[1].y) == [900000, None]
    assert [trace.name for trace in figure.data[1:]] == [
        "Reported volume",
        "Average volume",
        "10-day average volume",
    ]
    rows = streamlit.dataframe.call_args.args[0]
    assert set(rows[0]) == set(snapshots[0])
    assert '"fiftyTwoWeekHigh": 9296.7' in rows[0]["info"]
    assert rows[0]["trailing_pe"] is None
    assert rows[1]["trailing_pe"] == Decimal("20.5")
    streamlit.json.assert_called_once_with(snapshots[0]["info"])
    assert streamlit.columns.return_value[4].metric.call_args.args == (
        "Trailing P/E",
        "N/A",
    )


def test_empty_state_does_not_render_chart_or_table() -> None:
    with patch.object(dashboard, "st") as streamlit:
        dashboard.show_snapshots([])
    streamlit.info.assert_called_once()
    streamlit.plotly_chart.assert_not_called()
    streamlit.dataframe.assert_not_called()
