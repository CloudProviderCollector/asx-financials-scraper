"""Point-in-time ASX 300 valuations and coverage."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from dashboards import asx_300_dashboard as dashboard


def _profile(ticker: str, day: int, hour: int, **payload: object) -> dict:
    return {
        "ticker": ticker,
        "fetched_at": datetime(2026, 11, day, hour, tzinfo=UTC),
        "payload": {"currency": "AUD", **payload},
    }


def test_supplied_roster_contains_300_unique_tickers() -> None:
    roster = dashboard.load_roster()
    assert len(roster) == 300
    assert roster["BHP"]["name"]


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
    roster = {f"T{index:03d}": {"name": str(index)} for index in range(300)}
    profiles = [
        _profile(ticker, 10, 2, marketCap=100, trailingPE=10, forwardPE=20)
        for ticker in list(roster)[:30]
    ]
    profiles += [
        _profile(ticker, 11, 2, marketCap=100, trailingPE=10, forwardPE=20)
        for ticker in list(roster)[:30]
    ]
    profiles.append(_profile("T030", 11, 2, marketCap=100, trailingPE=10))
    history, details = dashboard.summarize(profiles, roster)

    assert history[0]["trailing_count"] == history[0]["forward_count"] == 30
    assert history[0]["trailing_pe"] is None
    assert history[0]["forward_pe"] is None
    assert history[1]["trailing_count"] == 31
    assert history[1]["trailing_pe"] == 10
    assert history[1]["forward_count"] == 30
    assert history[1]["forward_pe"] is None
    assert len(details[history[0]["date"]]) == 300
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
