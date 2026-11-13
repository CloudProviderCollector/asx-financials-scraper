"""Scheduling, idempotency, and alert behavior for the VEA/Brent monitor."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from hatchet_sdk.clients.rest.models import V1TaskStatus
from hatchet_sdk.exceptions import IdempotencyCollisionError
from sqlalchemy import create_engine, text
from workflows import catch_up, worker
from workflows import vea_crude_workflow as workflow


def test_calendar_counts_asx_sessions_not_calendar_days() -> None:
    assert workflow.target_session(date(2026, 1, 4)) == date(2026, 1, 2)
    assert workflow.missing_sessions(date(2026, 1, 2), date(2026, 1, 9)) == 5
    assert workflow.missing_sessions(date(2026, 1, 5), date(2026, 1, 9)) == 4
    assert workflow.missing_sessions(date(2026, 1, 9), date(2026, 1, 9)) == 0


def test_dispatch_passes_target_to_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catch_up, "job", Mock())
    catch_up.dispatch(date(2026, 1, 5))
    assert catch_up.job.run.call_args.kwargs["input"].date == "2026-01-05"


def test_dispatch_ignores_duplicate_run_but_not_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catch_up, "job", Mock())
    catch_up.job.run.side_effect = IdempotencyCollisionError("existing-run")
    catch_up.dispatch(date(2026, 1, 5))
    catch_up.job.run.side_effect = RuntimeError("network unavailable")
    with pytest.raises(RuntimeError, match="network unavailable"):
        catch_up.dispatch(date(2026, 1, 5))


def test_hatchet_registers_one_market_scheduler_and_independent_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows: list[Mock] = []
    client = Mock()

    def workflow_factory(**kwargs: object) -> Mock:
        item = Mock()
        item.name = kwargs["name"]
        item.crons = kwargs.get("on_crons")
        item.idempotency = kwargs.get("idempotency")
        item.task.side_effect = lambda **kwargs: lambda callback: callback
        workflows.append(item)
        return item

    client.workflow.side_effect = workflow_factory
    monkeypatch.setattr(worker, "Hatchet", lambda: client)
    registered_client, registered = worker.register_workflows()
    assert registered_client is client
    assert registered == workflows
    assert [item.name for item in registered] == [
        "vea-crude-evaluate",
        "asx-200-profile-capture",
        "market-catch-up",
    ]
    assert registered[0].idempotency.key_expression == "input.date"
    assert registered[1].idempotency.key_expression == "input.date"
    assert [item.crons for item in registered] == [
        ["0 23 * * *"],
        ["0 7 * * *"],
        ["10 * * * *"],
    ]


def test_catch_up_dispatches_each_missing_run_and_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fail(target: date) -> None:
        calls.append("VEA")
        raise RuntimeError("VEA unavailable")

    client = Mock()
    monkeypatch.setattr(catch_up, "dispatch", fail)
    monkeypatch.setattr(catch_up, "dispatch_asx_200", lambda now: calls.append("ASX 200"))
    monkeypatch.setattr(catch_up, "_has_success_or_active_run", lambda *args: False)
    with pytest.raises(ExceptionGroup, match="Market catch-up dispatch failed"):
        catch_up.run_catch_up(client, datetime(2026, 11, 12, 20, 10, tzinfo=workflow.SYDNEY))
    assert calls == ["VEA", "ASX 200"]


def test_asx_200_dispatch_uses_date_key_and_skips_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catch_up, "asx_200_job", Mock())
    before = datetime(2026, 11, 12, 16, tzinfo=workflow.SYDNEY)
    catch_up.dispatch_asx_200(before)
    catch_up.asx_200_job.run.assert_not_called()
    after = datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY)
    catch_up.dispatch_asx_200(after)
    assert catch_up.asx_200_job.run.call_args.kwargs == {
        "input": catch_up.Asx200Input(date="2026-11-12"),
        "wait_for_result": False,
    }


def test_scheduler_runs_catch_up_every_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    callbacks = {}
    client = Mock()

    def factory(**kwargs: object) -> Mock:
        item = Mock()
        item.task.side_effect = lambda **options: (
            lambda callback: callbacks.setdefault(kwargs["name"], callback)
        )
        return item

    client.workflow.side_effect = factory
    catch_up_check = Mock()
    monkeypatch.setattr(worker, "Hatchet", lambda: client)
    monkeypatch.setattr(worker.catch_up, "run_catch_up", catch_up_check)
    worker.register_workflows()
    callbacks["market-catch-up"](None, None)
    catch_up_check.assert_called_once_with(client)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (
            datetime(2026, 7, 6, 16, tzinfo=workflow.SYDNEY),
            ["vea-crude-evaluate"],
        ),
        (
            datetime(2026, 7, 6, 17, tzinfo=workflow.SYDNEY),
            ["vea-crude-evaluate"],
        ),
        (
            datetime(2026, 7, 6, 17, 10, tzinfo=workflow.SYDNEY),
            ["vea-crude-evaluate", "asx-200-profile-capture"],
        ),
        (
            datetime(2026, 11, 12, 17, tzinfo=workflow.SYDNEY),
            ["vea-crude-evaluate"],
        ),
        (
            datetime(2026, 11, 12, 18, tzinfo=workflow.SYDNEY),
            ["vea-crude-evaluate"],
        ),
        (
            datetime(2026, 11, 12, 18, 10, tzinfo=workflow.SYDNEY),
            ["vea-crude-evaluate", "asx-200-profile-capture"],
        ),
        (
            datetime(2026, 11, 12, 20, tzinfo=workflow.SYDNEY),
            ["vea-crude-evaluate", "asx-200-profile-capture"],
        ),
        (
            datetime(2026, 11, 12, 20, 10, tzinfo=workflow.SYDNEY),
            ["vea-crude-evaluate", "asx-200-profile-capture"],
        ),
    ],
)
def test_catch_up_waits_for_each_daily_utc_schedule(
    monkeypatch: pytest.MonkeyPatch, now: datetime, expected: list[str]
) -> None:
    checked: list[str] = []
    monkeypatch.setattr(
        catch_up,
        "_has_success_or_active_run",
        lambda client, name, day, due, at: checked.append(name) or True,
    )
    catch_up.run_catch_up(Mock(), now.astimezone(UTC))
    assert checked == expected


def _run(
    name: str,
    status: V1TaskStatus,
    created: datetime,
    *,
    payload: dict[str, str] | None = None,
    output: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_name=name,
        status=status,
        created_at=created,
        input={"input": payload or {}},
        output=output or {},
    )


@pytest.mark.parametrize(
    ("status", "output", "expected"),
    [
        (V1TaskStatus.COMPLETED, {"target": "2026-11-12"}, True),
        (V1TaskStatus.COMPLETED, {"capture_task": {"target": "2026-11-12"}}, True),
        (V1TaskStatus.COMPLETED, {"capture_task": {"target": "2026-11-11"}}, False),
        (V1TaskStatus.RUNNING, {}, True),
        (V1TaskStatus.QUEUED, {}, True),
        (V1TaskStatus.FAILED, {}, False),
        (V1TaskStatus.CANCELLED, {}, False),
    ],
)
def test_run_audit_checks_status_and_exact_target(
    status: V1TaskStatus, output: dict[str, object], expected: bool
) -> None:
    client = Mock()
    created = datetime(2026, 11, 12, 7, tzinfo=UTC)
    client.runs.list.return_value.rows = [
        _run("asx-200-profile-capture", status, created, output=output)
    ]
    assert (
        catch_up._has_success_or_active_run(
            client, "asx-200-profile-capture", date(2026, 11, 12), created, created.replace(hour=8)
        )
        is expected
    )


def test_run_audit_ignores_old_or_other_workflow_and_paginates() -> None:
    client = Mock()
    due = datetime(2026, 11, 12, 7, tzinfo=UTC)
    other = _run("vea-crude-evaluate", V1TaskStatus.COMPLETED, due)
    old = _run(
        "asx-200-profile-capture",
        V1TaskStatus.COMPLETED,
        datetime(2026, 11, 11, 7, tzinfo=UTC),
    )
    wanted = _run(
        "asx-200-profile-capture",
        V1TaskStatus.COMPLETED,
        due,
        payload={"date": "2026-11-12"},
    )
    client.runs.list.side_effect = [
        SimpleNamespace(rows=[other, old] * 50),
        SimpleNamespace(rows=[wanted]),
    ]
    assert catch_up._has_success_or_active_run(
        client, "asx-200-profile-capture", date(2026, 11, 12), due, due.replace(hour=8)
    )
    assert client.runs.list.call_args.kwargs["offset"] == 100


def test_run_audit_rejects_successful_unidentified_cron_run() -> None:
    client = Mock()
    due = datetime(2026, 11, 12, 7, tzinfo=UTC)
    client.runs.list.return_value.rows = [
        _run("asx-200-profile-capture", V1TaskStatus.COMPLETED, due)
    ]
    with pytest.raises(ValueError, match="no target"):
        catch_up._has_success_or_active_run(
            client, "asx-200-profile-capture", date(2026, 11, 12), due, due.replace(hour=8)
        )


def test_run_audit_retries_failed_run_even_with_matching_input() -> None:
    client = Mock()
    due = datetime(2026, 11, 12, 7, tzinfo=UTC)
    client.runs.list.return_value.rows = [
        _run("asx-200-profile-capture", V1TaskStatus.FAILED, due, payload={"date": "2026-11-12"})
    ]
    assert not catch_up._has_success_or_active_run(
        client, "asx-200-profile-capture", date(2026, 11, 12), due, due.replace(hour=8)
    )


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
