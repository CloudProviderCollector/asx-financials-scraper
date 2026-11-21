"""Point-in-time ASX 200 valuations and coverage."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dashboards import asx_200_dashboard as dashboard


def _profile(ticker: str, day: int, hour: int, **payload: object) -> dict:
    return {
        "ticker": ticker,
        "fetched_at": datetime(2026, 11, day, hour, tzinfo=UTC),
        "payload": {"currency": "AUD", **payload},
    }


def test_dated_roster_contains_200_unique_tickers() -> None:
    roster = dashboard.load_roster()
    assert len(roster) == 200
    assert roster["BHP"]["name"]


def test_loads_stored_index_closes_in_date_order() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value = [
        SimpleNamespace(session_date=date(2026, 11, 12), close=Decimal("8700.25")),
        SimpleNamespace(session_date=date(2026, 11, 13), close=Decimal("8710.50")),
    ]
    assert dashboard.load_index(engine) == [
        (date(2026, 11, 12), 8700.25),
        (date(2026, 11, 13), 8710.5),
    ]
    query = str(connection.execute.call_args.args[0])
    assert "asx_200_snapshots" in query
    assert "ORDER BY session_date" in query
    assert connection.execute.call_args.args[1] == {"symbol": "^AXJO"}


def test_index_chart_defaults_to_latest_month_and_offers_longer_ranges() -> None:
    prices = [
        (date(2021, 11, 16), 7300.0),
        (date(2026, 10, 13), 8600.0),
        (date(2026, 11, 13), 8700.0),
    ]
    figure = dashboard.index_chart(prices)
    assert list(figure.data[0].y) == [7300.0, 8600.0, 8700.0]
    assert figure.layout.xaxis.range[0].date() == date(2026, 10, 13)
    assert figure.layout.xaxis.range[1] == date(2026, 11, 13)
    assert [button.label for button in figure.layout.xaxis.rangeselector.buttons] == [
        "1m",
        "3m",
        "6m",
        "1y",
        "3y",
        "5y",
        "All",
    ]


def test_index_chart_has_empty_state() -> None:
    with patch.object(dashboard, "st") as streamlit:
        dashboard.show_index([])
    streamlit.info.assert_called_once()
    streamlit.plotly_chart.assert_not_called()


def test_uses_latest_profile_each_sydney_day_and_separate_eligible_denominators() -> None:
    roster = {ticker: {"name": ticker} for ticker in ("AAA", "BBB", "CCC", "DDD")}
    profiles = [
        _profile("AAA", 10, 2, marketCap=100, trailingPE=10, forwardPE=8),
        _profile("AAA", 10, 3, marketCap=100, trailingPE=20, forwardPE=None),
        _profile("BBB", 10, 2, marketCap=300, trailingPE=40, forwardPE=16),
        _profile("CCC", 10, 2, marketCap=100, trailingPE=-1, forwardPE=4, currency="USD"),
        _profile("DDD", 10, 2, marketCap=0, trailingPE=30, forwardPE=3),
        _profile("AAA", 11, 2, marketCap=100, trailingPE=None, forwardPE=12),
    ]
    history, details = dashboard.summarize(profiles, roster)
    assert len(history) == 2
    assert history[0]["trailing_pe"] == Decimal("35")
    assert history[0]["forward_pe"] == Decimal("16")
    assert history[0]["trailing_count"] == 2
    assert history[0]["forward_count"] == 1
    assert history[0]["forward_cap_coverage"] == Decimal("0.75")
    assert history[0]["profiles"] == 4
    assert details[history[0]["date"]][0]["forward_pe"] is None
    assert history[1]["trailing_pe"] is None
    assert history[1]["forward_pe"] == 12
    assert history[1]["profiles"] == 1
    assert len(details[history[1]["date"]]) == 4
    assert details[history[1]["date"]][1]["fetched_at"] is None


def test_weekend_backfill_uses_session_date_and_retains_actual_fetch_time() -> None:
    roster = {"AAA": {"name": "AAA"}, "BBB": {"name": "BBB"}}
    friday = _profile("AAA", 13, 12, marketCap=100, trailingPE=10)
    saturday = {
        **_profile("BBB", 14, 3, marketCap=100, trailingPE=20),
        "session_date": datetime(2026, 11, 13).date(),
    }
    history, details = dashboard.summarize([friday, saturday], roster)

    assert len(history) == 1
    assert history[0]["date"] == saturday["session_date"]
    assert history[0]["profiles"] == 2
    assert history[0]["backfilled"] == 1
    assert history[0]["trailing_pe"] == 15
    assert details[history[0]["date"]][1]["fetched_at"] == saturday["fetched_at"]
    assert details[history[0]["date"]][1]["source"] == "Weekend backfill"


def test_discloses_older_and_unreported_backfill_quote_dates() -> None:
    roster = {"QUB": {"name": "QUB"}, "RYM": {"name": "RYM"}}
    profiles = [
        {
            **_profile("QUB", 15, 2, marketCap=100, trailingPE=10),
            "session_date": date(2026, 11, 13),
        },
        {
            **_profile(
                "RYM",
                15,
                2,
                marketCap=100,
                regularMarketTime=int(datetime(2026, 11, 12, 4, tzinfo=UTC).timestamp()),
            ),
            "session_date": date(2026, 11, 13),
        },
    ]
    history, details = dashboard.summarize(profiles, roster)
    assert [item["quote_date"] for item in details[date(2026, 11, 13)]] == [
        None,
        date(2026, 11, 12),
    ]
    with patch.object(dashboard, "st") as streamlit:
        streamlit.selectbox.return_value = date(2026, 11, 13)
        dashboard.show_dashboard(history, details)
    assert "2 have an older or unreported quote date" in streamlit.warning.call_args.args[0]


def test_uses_sydney_date_and_rejects_nonfinite_or_nonpositive_values() -> None:
    roster = {"AAA": {"name": "AAA"}}
    history, _ = dashboard.summarize(
        [
            {
                **_profile("AAA", 10, 14, marketCap="NaN", trailingPE=True),
                "fetched_at": datetime(2026, 11, 10, 14, tzinfo=timezone(timedelta(hours=0))),
            },
            _profile("AAA", 10, 23, marketCap=100, trailingPE="Infinity", forwardPE=5),
        ],
        roster,
    )
    assert [row["date"].isoformat() for row in history] == ["2026-11-11"]
    assert history[0]["trailing_pe"] is None
    assert history[0]["forward_pe"] == 5


def test_suppresses_pe_at_90_percent_missing_and_retains_independent_coverage() -> None:
    roster = {f"T{index:03d}": {"name": str(index)} for index in range(200)}
    profiles = [
        _profile(ticker, 10, 2, marketCap=100, trailingPE=10, forwardPE=20)
        for ticker in list(roster)[:20]
    ]
    profiles += [
        _profile(ticker, 11, 2, marketCap=100, trailingPE=10, forwardPE=20)
        for ticker in list(roster)[:20]
    ]
    profiles.append(_profile("T020", 11, 2, marketCap=100, trailingPE=10))
    history, details = dashboard.summarize(profiles, roster)

    assert history[0]["trailing_count"] == history[0]["forward_count"] == 20
    assert history[0]["trailing_pe"] is None
    assert history[0]["forward_pe"] is None
    assert history[1]["trailing_count"] == 21
    assert history[1]["trailing_pe"] == 10
    assert history[1]["forward_count"] == 20
    assert history[1]["forward_pe"] is None
    assert len(details[history[0]["date"]]) == 200
    with patch.object(dashboard, "st") as streamlit:
        streamlit.selectbox.return_value = history[0]["date"]
        dashboard.show_dashboard(history[:1], {history[0]["date"]: details[history[0]["date"]]})
    streamlit.plotly_chart.assert_not_called()
    streamlit.info.assert_called_once()


def test_loads_only_roster_company_profiles() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value = iter(
        [_profile("AAA", 10, 12, marketCap=100)]
    )
    assert len(dashboard.load_profiles(engine, {"AAA": {"name": "AAA"}})) == 1
    sql = str(connection.execute.call_args.args[0])
    assert "raw_source_payloads" in sql
    assert "company-profile" not in sql  # Bound parameters, not string interpolation.


def test_renders_two_series_and_missing_values_without_backfill() -> None:
    roster = {"AAA": {"name": "AAA"}}
    history, details = dashboard.summarize(
        [
            _profile("AAA", 10, 12, marketCap=100, trailingPE=10),
            _profile("AAA", 11, 12, marketCap=100, forwardPE=9),
        ],
        roster,
    )
    with patch.object(dashboard, "st") as streamlit:
        streamlit.selectbox.return_value = history[-1]["date"]
        dashboard.show_dashboard(history, details)
    figure = streamlit.plotly_chart.call_args.args[0]
    assert [list(trace.y) for trace in figure.data] == [[10.0, None], [None, 9.0]]
    assert all(not trace.connectgaps for trace in figure.data)
