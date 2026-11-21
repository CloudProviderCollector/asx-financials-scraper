"""ASX 200 index capture eligibility, persistence, and retry behavior."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pandas as pd
import pytest
from workflows import asx_200_index_workflow as workflow


class AfterClose(datetime):
    @classmethod
    def now(cls, tz: object = None) -> datetime:
        return datetime(2026, 11, 12, 17, 5, tzinfo=workflow.SYDNEY).astimezone(UTC)


def test_index_capture_saves_completed_session(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.scalar.return_value = None
    engine.begin.return_value.__enter__.return_value.execute.return_value.rowcount = 1
    ticker = Mock()
    ticker.history.return_value = pd.DataFrame(
        {"Open": [9000], "High": [9100], "Low": [8950], "Close": [9050], "Volume": [0]},
        index=pd.DatetimeIndex(["2026-11-12"], tz=workflow.SYDNEY),
    )
    ticker.get_info.return_value = {
        "regularMarketTime": datetime(2026, 11, 12, 16, tzinfo=workflow.SYDNEY).timestamp(),
        "currency": "AUD",
        "regularMarketPrice": 9050,
    }
    monkeypatch.setattr(workflow.yf, "Ticker", Mock(return_value=ticker))
    monkeypatch.setattr(workflow, "PacedSession", MagicMock())
    monkeypatch.setattr(workflow, "datetime", AfterClose)
    now = datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY)
    assert workflow.capture(engine, now) == 1
    assert workflow.yf.Ticker.call_args.args == ("^AXJO",)
    assert ticker.history.call_args.kwargs["start"] == "2026-11-12"
    assert ticker.history.call_args.kwargs["end"] == "2026-11-13"
    values = engine.begin.return_value.__enter__.return_value.execute.call_args.args[1][0]
    assert values["session_date"] == now.date()
    assert values["close"] == 9050
    assert values["volume"] is None
    assert values["quote_time"].astimezone(workflow.SYDNEY).date() == now.date()


def test_index_capture_skips_ineligible_or_existing_day(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    ticker = Mock()
    monkeypatch.setattr(workflow.yf, "Ticker", ticker)
    assert not workflow.capture(engine, datetime(2026, 11, 12, 16, tzinfo=workflow.SYDNEY))
    assert not workflow.capture(engine, datetime(2026, 11, 14, 17, tzinfo=workflow.SYDNEY))
    engine.connect.return_value.__enter__.return_value.scalar.return_value = date(2026, 11, 12)
    assert workflow.capture(engine, datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY)) == 0
    ticker.assert_not_called()


def test_missing_bar_fails_without_storing_and_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.scalar.return_value = None
    ticker = Mock(history=Mock(return_value=SimpleNamespace(empty=True)))
    monkeypatch.setattr(workflow.yf, "Ticker", Mock(return_value=ticker))
    monkeypatch.setattr(workflow, "PacedSession", MagicMock())
    with pytest.raises(ValueError, match="no \\^AXJO bars"):
        workflow.capture(engine, datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY))
    engine.begin.assert_not_called()


def test_capture_backfills_trading_sessions_without_backdating_quote_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.scalar.return_value = date(2026, 11, 6)
    engine.begin.return_value.__enter__.return_value.execute.return_value.rowcount = 4
    days = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]
    ticker = Mock()
    ticker.history.return_value = pd.DataFrame(
        {
            "Open": [9000, 9010, 9020, 9030],
            "High": [9100, 9110, 9120, 9130],
            "Low": [8950, 8960, 8970, 8980],
            "Close": [9050, 9060, 9070, 9080],
            "Volume": [100, 200, 300, 400],
        },
        index=pd.DatetimeIndex(days, tz=workflow.SYDNEY),
    )
    ticker.get_info.return_value = {
        "regularMarketTime": datetime(2026, 11, 12, 16, tzinfo=workflow.SYDNEY).timestamp(),
        "currency": "AUD",
        "regularMarketPrice": 9080,
    }
    monkeypatch.setattr(workflow.yf, "Ticker", Mock(return_value=ticker))
    monkeypatch.setattr(workflow, "PacedSession", MagicMock())
    monkeypatch.setattr(workflow, "datetime", AfterClose)

    assert workflow.capture(engine, datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY)) == 4
    assert ticker.history.call_args.kwargs["start"] == "2026-11-07"
    assert ticker.history.call_args.kwargs["end"] == "2026-11-13"
    values = engine.begin.return_value.__enter__.return_value.execute.call_args.args[1]
    assert [row["session_date"] for row in values] == [date.fromisoformat(day) for day in days]
    assert [row["volume"] for row in values] == [100, 200, 300, 400]
    assert [row["close"] for row in values] == [9050, 9060, 9070, 9080]
    assert all(
        row["info"] == {} and row["quote_time"] is None and row["trailing_pe"] is None
        for row in values[:-1]
    )
    assert values[-1]["info"] == ticker.get_info.return_value
    engine.begin.assert_called_once()


def test_missing_intermediate_session_fails_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.scalar.return_value = date(2026, 11, 9)
    ticker = Mock()
    ticker.history.return_value = pd.DataFrame(
        {"Open": [9000, 9020], "High": [9100, 9120], "Low": [8950, 8970], "Close": [9050, 9070]},
        index=pd.DatetimeIndex(["2026-11-10", "2026-11-12"], tz=workflow.SYDNEY),
    )
    monkeypatch.setattr(workflow.yf, "Ticker", Mock(return_value=ticker))
    monkeypatch.setattr(workflow, "PacedSession", MagicMock())
    with pytest.raises(ValueError, match="no \\^AXJO bar for 2026-11-11"):
        workflow.capture(engine, datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY))
    ticker.get_info.assert_not_called()
    engine.begin.assert_not_called()


def test_historical_backfill_preserves_existing_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value.execute.return_value.rowcount = 2
    ticker = Mock()
    ticker.history.return_value = pd.DataFrame(
        {
            "Open": [9000, 9010, 9020],
            "High": [9100, 9110, 9120],
            "Low": [8950, 8960, 8970],
            "Close": [9050, 9060, 9070],
            "Volume": [100, 0, 300],
        },
        index=pd.DatetimeIndex(["2026-11-09", "2026-11-10", "2026-11-11"], tz=workflow.SYDNEY),
    )
    monkeypatch.setattr(workflow.yf, "Ticker", Mock(return_value=ticker))
    monkeypatch.setattr(workflow, "PacedSession", MagicMock())
    now = datetime(2026, 11, 12, 12, tzinfo=workflow.SYDNEY)

    assert workflow.backfill(engine, date(2026, 11, 9), date(2026, 11, 11), now) == 2
    values = engine.begin.return_value.__enter__.return_value.execute.call_args.args[1]
    assert [row["session_date"] for row in values] == [
        date(2026, 11, 9),
        date(2026, 11, 10),
        date(2026, 11, 11),
    ]
    assert [row["volume"] for row in values] == [100, None, 300]
    assert all(row["info"] == {} and row["quote_time"] is None for row in values)
    assert "ON CONFLICT (session_date) DO NOTHING" in str(
        engine.begin.return_value.__enter__.return_value.execute.call_args.args[0]
    )
    ticker.get_info.assert_not_called()


def test_historical_backfill_rejects_open_session(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    monkeypatch.setattr(workflow.yf, "Ticker", Mock())
    now = datetime(2026, 11, 12, 12, tzinfo=workflow.SYDNEY)
    with pytest.raises(ValueError, match="latest closed session"):
        workflow.backfill(engine, date(2026, 11, 9), date(2026, 11, 12), now)
    workflow.yf.Ticker.assert_not_called()
    engine.begin.assert_not_called()


def test_historical_backfill_missing_bar_is_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    ticker = Mock()
    ticker.history.return_value = pd.DataFrame(
        {"Open": [9000], "High": [9100], "Low": [8950], "Close": [9050]},
        index=pd.DatetimeIndex(["2026-11-09"], tz=workflow.SYDNEY),
    )
    monkeypatch.setattr(workflow.yf, "Ticker", Mock(return_value=ticker))
    monkeypatch.setattr(workflow, "PacedSession", MagicMock())
    now = datetime(2026, 11, 12, 12, tzinfo=workflow.SYDNEY)
    with pytest.raises(ValueError, match="no \\^AXJO bar for 2026-11-10"):
        workflow.backfill(engine, date(2026, 11, 9), date(2026, 11, 10), now)
    engine.begin.assert_not_called()


def test_index_job_rejects_stale_dispatched_date(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    callbacks: list[object] = []
    client.workflow.return_value.task.side_effect = lambda **kwargs: (
        lambda callback: callbacks.append(callback) or callback
    )
    job = workflow.register_workflow(client)
    assert client.workflow.call_args.kwargs["on_crons"] == ["0 7 * * *"]
    assert (
        client.workflow.call_args.kwargs["idempotency"].key_expression
        == '"asx-200-index-capture:" + input.date'
    )
    assert job is client.workflow.return_value
    capture_task = client.workflow.return_value.task.call_args
    assert capture_task.kwargs["execution_timeout"].total_seconds() == 1800
    monkeypatch.setattr(workflow, "session_today", lambda now: None)
    with pytest.raises(RuntimeError, match="no longer eligible"):
        callbacks[0](workflow.CaptureInput(date="2026-11-12"), None)
