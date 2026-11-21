"""ASX 200 profile and daily price captures and request pacing."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from curl_cffi import requests
from workflows import asx_200_profiles_workflow as workflow


def test_roster_is_resolved_from_repository_root() -> None:
    assert workflow.ROSTER.is_file()
    tickers = workflow.load_tickers(workflow.ROSTER)
    assert len(tickers) == 200
    assert {"ASK", "IFL", "NSR", "XYX"} <= set(tickers)


class AfterClose(datetime):
    @classmethod
    def now(cls, tz: object = None) -> datetime:
        return datetime(2026, 11, 12, 17, 5, tzinfo=workflow.SYDNEY).astimezone(UTC)


def test_capture_rejects_asx_300_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow, "load_tickers", lambda path: [f"T{i:03}" for i in range(300)])
    with pytest.raises(ValueError, match="ASX 200 roster must contain exactly 200 tickers"):
        workflow.capture(Mock(), datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY))


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (datetime(2026, 11, 12, 16, tzinfo=workflow.SYDNEY), None),
        (datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY), "2026-11-12"),
        (datetime(2026, 6, 12, 17, tzinfo=workflow.SYDNEY), "2026-06-12"),
        (datetime(2026, 11, 14, 17, tzinfo=workflow.SYDNEY), None),
        (datetime(2026, 11, 13, 0, tzinfo=workflow.SYDNEY), None),
    ],
)
def test_capture_only_after_close_on_current_session(
    timestamp: datetime, expected: str | None
) -> None:
    day = workflow.session_today(timestamp.astimezone(UTC))
    assert (day.isoformat() if day else None) == expected


def test_http_requests_are_spaced_ten_seconds_including_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapsed = [0.0]
    sent: list[float] = []
    monkeypatch.setattr(workflow.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        workflow.time, "sleep", lambda duration: elapsed.__setitem__(0, elapsed[0] + duration)
    )
    monkeypatch.setattr(
        requests.Session, "request", lambda *args, **kwargs: sent.append(elapsed[0])
    )

    with workflow.PacedSession() as session:
        session.request("GET", "https://example.test/one")
        elapsed[0] = 2.0
        session.request("GET", "https://example.test/retry")
        elapsed[0] = 13.0
        session.request("GET", "https://example.test/next")
    assert sent == [0.0, 10.0, 20.0]


def test_completed_profiles_only_count_after_market_close() -> None:
    engine = MagicMock()
    workflow.completed_tickers(engine, date(2026, 11, 12))
    query = engine.connect.return_value.__enter__.return_value.scalars.call_args.args[0]
    bounds = [value for value in query.compile().params.values() if isinstance(value, datetime)]
    assert sorted(bounds) == [
        datetime(2026, 11, 12, 6, tzinfo=UTC),
        datetime(2026, 11, 12, 13, tzinfo=UTC),
    ]


def test_resume_requires_both_profile_and_daily_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock()
    store = Mock()
    provider = Mock()
    provider.fetch_profile.return_value = SimpleNamespace(
        company_profile=SimpleNamespace(
            fetched_at_utc=datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY)
        ),
        raw_payloads=[Mock()],
    )
    monkeypatch.setattr(
        workflow, "load_tickers", lambda path: ["BHP", "CBA"] + [f"T{i:03}" for i in range(198)]
    )
    monkeypatch.setattr(
        workflow,
        "completed_tickers",
        lambda engine, day: {"BHP", "CBA", *[f"T{i:03}" for i in range(198)]},
    )
    monkeypatch.setattr(
        workflow,
        "completed_price_tickers",
        lambda engine, day: {"BHP", *[f"T{i:03}" for i in range(198)]},
    )
    price_bar = Mock()
    provider.fetch_daily_price.return_value = price_bar
    monkeypatch.setattr(workflow, "PostgresFinancialDataStore", lambda engine: store)
    monkeypatch.setattr(workflow, "YFinanceProvider", lambda: provider)
    monkeypatch.setattr(workflow, "PacedSession", MagicMock())
    monkeypatch.setattr(workflow, "datetime", AfterClose)

    assert workflow.capture(engine, datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY)) == 1
    assert provider.fetch_profile.call_args.args[0].value == "CBA"
    assert provider.fetch_daily_price.call_args.args[:2][1] == date(2026, 11, 12)
    assert store.complete_ingestion_run.call_args.kwargs["daily_price_bars"] == [price_bar]
    assert (
        store.complete_ingestion_run.call_args.kwargs["raw_payloads"]
        == provider.fetch_profile.return_value.raw_payloads
    )
    assert store.complete_ingestion_run.call_args.kwargs["statement_snapshots"] == []
    engine.connect.return_value.__enter__.return_value.execute.assert_called()


def test_completed_price_tickers_only_reads_target_session() -> None:
    engine = MagicMock()
    workflow.completed_price_tickers(engine, date(2026, 11, 12))
    query = engine.connect.return_value.__enter__.return_value.scalars.call_args.args[0]
    assert date(2026, 11, 12) in query.compile().params.values()
    assert "daily_prices.volume IS NOT NULL" in str(query)
    assert "daily_prices.close IS NOT NULL" in str(query)


def test_failed_profile_remains_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    store = Mock()
    provider = Mock()
    provider.fetch_profile.return_value = SimpleNamespace(company_profile=None)
    monkeypatch.setattr(workflow, "load_tickers", lambda path: ["CBA"] * 200)
    monkeypatch.setattr(workflow, "completed_tickers", lambda engine, day: set())
    monkeypatch.setattr(workflow, "completed_price_tickers", lambda engine, day: set())
    monkeypatch.setattr(workflow, "PostgresFinancialDataStore", lambda engine: store)
    monkeypatch.setattr(workflow, "YFinanceProvider", lambda: provider)
    monkeypatch.setattr(workflow, "PacedSession", MagicMock())
    monkeypatch.setattr(workflow, "datetime", AfterClose)

    with pytest.raises(ValueError, match="no company profile"):
        workflow.capture(engine, datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY))
    store.mark_ingestion_run_failed.assert_called_once()
    store.complete_ingestion_run.assert_not_called()


def test_profile_finishing_after_midnight_is_not_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    store = Mock()
    provider = Mock()
    provider.fetch_profile.return_value = SimpleNamespace(
        company_profile=SimpleNamespace(
            fetched_at_utc=datetime(2026, 11, 13, 0, tzinfo=workflow.SYDNEY)
        )
    )
    monkeypatch.setattr(workflow, "load_tickers", lambda path: ["CBA"] * 200)
    monkeypatch.setattr(workflow, "completed_tickers", lambda engine, day: set())
    monkeypatch.setattr(workflow, "completed_price_tickers", lambda engine, day: set())
    monkeypatch.setattr(workflow, "PostgresFinancialDataStore", lambda engine: store)
    monkeypatch.setattr(workflow, "YFinanceProvider", lambda: provider)
    monkeypatch.setattr(workflow, "PacedSession", MagicMock())
    monkeypatch.setattr(workflow, "datetime", AfterClose)

    with pytest.raises(RuntimeError, match="past midnight"):
        workflow.capture(engine, datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY))
    store.mark_ingestion_run_failed.assert_called_once()
    store.complete_ingestion_run.assert_not_called()


def test_missing_daily_price_fails_without_saving_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    store = Mock()
    provider = Mock()
    provider.fetch_profile.return_value = SimpleNamespace(
        company_profile=SimpleNamespace(
            fetched_at_utc=datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY)
        )
    )
    provider.fetch_daily_price.side_effect = ValueError("no daily price and volume")
    monkeypatch.setattr(workflow, "load_tickers", lambda path: ["CBA"] * 200)
    monkeypatch.setattr(workflow, "completed_tickers", lambda engine, day: set())
    monkeypatch.setattr(workflow, "completed_price_tickers", lambda engine, day: set())
    monkeypatch.setattr(workflow, "PostgresFinancialDataStore", lambda engine: store)
    monkeypatch.setattr(workflow, "YFinanceProvider", lambda: provider)
    monkeypatch.setattr(workflow, "PacedSession", MagicMock())
    monkeypatch.setattr(workflow, "datetime", AfterClose)

    with pytest.raises(ValueError, match="no daily price and volume"):
        workflow.capture(engine, datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY))
    store.mark_ingestion_run_failed.assert_called_once()
    store.complete_ingestion_run.assert_not_called()


def test_registers_independent_idempotent_capture_job() -> None:
    client = Mock()
    workflows: list[Mock] = []

    def factory(**kwargs: object) -> Mock:
        item = Mock()
        item.name = kwargs["name"]
        item.idempotency = kwargs["idempotency"]
        item.task.side_effect = lambda **kwargs: lambda callback: callback
        workflows.append(item)
        return item

    client.workflow.side_effect = factory
    assert workflow.register_workflows(client) is workflows[0]
    assert [item.name for item in workflows] == ["asx-200-profile-capture"]
    assert workflows[0].idempotency.key_expression == "input.date"
    assert workflows[0].task.call_args.kwargs["execution_timeout"].total_seconds() == 7 * 3600
