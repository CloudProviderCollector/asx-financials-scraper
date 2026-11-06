"""Scheduling, idempotency, and alert behavior for the VEA/Brent monitor."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from hatchet_sdk.exceptions import IdempotencyCollisionError
from sqlalchemy import create_engine, text

from asx_financials import vea_crude_workflow as workflow


def test_calendar_counts_asx_sessions_not_calendar_days() -> None:
    assert workflow.target_session(date(2026, 1, 4)) == date(2026, 1, 2)
    assert workflow.missing_sessions(date(2026, 1, 2), date(2026, 1, 9)) == 5
    assert workflow.missing_sessions(date(2026, 1, 5), date(2026, 1, 9)) == 4
    assert workflow.missing_sessions(date(2026, 1, 9), date(2026, 1, 9)) == 0


@pytest.mark.parametrize(
    ("hour", "daily_only", "should_dispatch"),
    [(8, False, False), (9, True, True), (10, True, False), (10, False, True)],
)
def test_dispatch_checks_sydney_clock_and_completion(
    monkeypatch: pytest.MonkeyPatch, hour: int, daily_only: bool, should_dispatch: bool
) -> None:
    class Clock:
        @staticmethod
        def now(tz: object) -> datetime:
            return datetime(2026, 1, 6, hour, tzinfo=workflow.SYDNEY).astimezone(UTC)

    monkeypatch.setattr(workflow, "datetime", Clock)
    monkeypatch.setattr(workflow, "create_engine", lambda *args, **kwargs: Mock())
    monkeypatch.setattr(workflow, "_already_complete", lambda *args: False)
    monkeypatch.setattr(workflow, "job", Mock())
    workflow.dispatch(daily_only=daily_only)
    assert workflow.job.run.call_count == int(should_dispatch)
    if should_dispatch:
        assert workflow.job.run.call_args.kwargs["input"].date == "2026-01-05"


def test_dispatch_ignores_duplicate_run_but_not_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Clock:
        @staticmethod
        def now(tz: object) -> datetime:
            return datetime(2026, 1, 6, 10, tzinfo=workflow.SYDNEY).astimezone(UTC)

    monkeypatch.setattr(workflow, "datetime", Clock)
    monkeypatch.setattr(workflow, "create_engine", lambda *args, **kwargs: Mock())
    monkeypatch.setattr(workflow, "_already_complete", lambda *args: False)
    monkeypatch.setattr(workflow, "job", Mock())
    workflow.job.run.side_effect = IdempotencyCollisionError("existing-run")
    workflow.dispatch()
    workflow.job.run.side_effect = RuntimeError("network unavailable")
    with pytest.raises(RuntimeError, match="network unavailable"):
        workflow.dispatch()


def test_hatchet_registers_sydney_dst_crons_and_hourly_catch_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows: list[Mock] = []
    client = Mock()

    def workflow_factory(**kwargs: object) -> Mock:
        item = Mock()
        item.name = kwargs["name"]
        item.crons = kwargs.get("on_crons")
        item.idempotency = kwargs.get("idempotency")
        item.task.side_effect = lambda: lambda callback: callback
        workflows.append(item)
        return item

    client.workflow.side_effect = workflow_factory
    monkeypatch.setattr(workflow, "Hatchet", lambda: client)
    registered_client, registered = workflow.register_workflows()
    assert registered_client is client
    assert registered == workflows
    assert [item.name for item in registered] == [
        "vea-crude-evaluate",
        "vea-crude-daily",
        "vea-crude-catch-up",
    ]
    assert registered[0].idempotency.key_expression == "input.date"
    assert registered[1].crons == ["0 22 * * *", "0 23 * * *"]
    assert registered[2].crons == ["10 * * * *"]


def test_telegram_rejection_and_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    response = Mock()
    response.json.return_value = {"ok": True}
    post = Mock(return_value=response)
    monkeypatch.setattr(workflow.requests, "post", post)
    workflow.telegram("buy")
    assert post.call_args.kwargs["timeout"] == 20
    assert post.call_args.kwargs["data"] == {"chat_id": "test-chat", "text": "buy"}
    response.json.return_value = {"ok": False}
    with pytest.raises(RuntimeError, match="rejected"):
        workflow.telegram("sell")
    post.side_effect = workflow.requests.Timeout("timeout")
    with pytest.raises(RuntimeError, match="Telegram delivery failed"):
        workflow.telegram("sell")


def test_stale_feed_alert_is_once_per_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable migrated PostgreSQL database")
    engine = create_engine(url)
    day = date(2026, 1, 8)
    next_day = date(2026, 1, 9)
    key = "missing-VEA-2026-01-02"
    sent: list[str] = []
    monkeypatch.setattr(workflow, "telegram", sent.append)
    try:
        workflow._notify_once(engine, day, key, "VEA stale")
        workflow._notify_once(engine, day, key, "VEA stale")
        workflow._notify_once(engine, next_day, key, "VEA stale")
        workflow._notify_once(engine, next_day, "strategy-1-buy", "buy")
        workflow._notify_once(engine, next_day, "strategy-1-buy", "buy")
        workflow._notify_once(engine, day, "strategy-1-buy", "buy")
        assert sent == ["VEA stale", "buy", "buy"]
    finally:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM vea_crude_alerts WHERE target_date IN (:day, :next_day) "
                    "AND alert_key IN (:key, 'strategy-1-buy')"
                ),
                {"day": day, "next_day": next_day, "key": key},
            )
        engine.dispose()


def test_evaluate_persists_alerts_and_retries_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable migrated PostgreSQL database")
    engine = create_engine(url)
    target = date(2026, 1, 9)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM vea_crude_runs WHERE target_date = :day"), {"day": target})
        conn.execute(text("DELETE FROM vea_crude_alerts WHERE target_date = :day"), {"day": target})
    sent: list[str] = []
    monkeypatch.setattr(workflow, "telegram", sent.append)
    monkeypatch.setattr(workflow, "latest_dates", lambda engine: (target, target))
    monkeypatch.setattr(
        workflow,
        "load_prices",
        lambda engine: (
            [(target, Decimal("10"))],
            [(target, Decimal("80"))],
            {target: Decimal("10")},
        ),
    )
    monkeypatch.setattr(
        workflow,
        "latest_strategy_signals",
        lambda *args: SimpleNamespace(
            matched_date=target,
            strategies={1: SimpleNamespace(pending_action="buy")},
        ),
    )

    def fail_refresh(*args: object) -> None:
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(workflow, "refresh_prices", fail_refresh)
    try:
        with pytest.raises(RuntimeError, match="not marked complete"):
            workflow.evaluate(engine, target)
        assert len(sent) == 1 and "stored data" in sent[0]
        assert not workflow._already_complete(engine, target)
        monkeypatch.setattr(workflow, "refresh_prices", lambda *args: None)
        workflow.evaluate(engine, target)
        workflow.evaluate(engine, target)
        assert len(sent) == 1
        assert workflow._already_complete(engine, target)
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM vea_crude_runs WHERE target_date = :day"), {"day": target}
            )
            conn.execute(
                text("DELETE FROM vea_crude_alerts WHERE target_date = :day"), {"day": target}
            )
        engine.dispose()
