"""Audit Hatchet run history and retry missing market captures."""

import logging
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any

from hatchet_sdk import Hatchet
from hatchet_sdk.clients.rest.models import V1TaskStatus
from hatchet_sdk.exceptions import IdempotencyCollisionError

from workflows.asx_300_workflow import SYDNEY, session_today
from workflows.asx_300_workflow import CaptureInput as Asx300Input
from workflows.vea_crude_workflow import CheckInput, target_session, yesterday

LOGGER = logging.getLogger(__name__)
job: Any = None
asx_300_job: Any = None


def register_workflow(hatchet: Hatchet, vea: Any, asx_300: Any) -> Any:
    global job, asx_300_job
    job, asx_300_job = vea, asx_300
    scheduler = hatchet.workflow(name="market-catch-up", on_crons=["10 * * * *"])

    @scheduler.task()
    def catch_up_check(input: Any, ctx: Any) -> None:
        run_catch_up(hatchet)

    return scheduler


def dispatch(target: date) -> None:
    try:
        job.run(input=CheckInput(date=target.isoformat()), wait_for_result=False)
    except IdempotencyCollisionError:
        LOGGER.info("Evaluation for %s is already queued or running", target)


def dispatch_asx_300(now: datetime) -> None:
    day = session_today(now)
    if day is None:
        return
    try:
        asx_300_job.run(input=Asx300Input(date=day.isoformat()), wait_for_result=False)
    except IdempotencyCollisionError:
        LOGGER.info("ASX 300 capture for %s is already queued or running", day)


def _due(day: date, hour: int) -> datetime:
    return datetime.combine(day, clock_time(hour), UTC)


def _run_target(run: Any, name: str) -> str | None:
    task = {
        "vea-crude-evaluate": "monitor",
        "asx-300-capture": "capture_task",
    }[name]
    output = run.output.get("target", run.output.get(task, {}))
    if isinstance(output, dict):
        output = output.get("target")
    if isinstance(output, str):
        return output
    data = run.input.get("input", {})
    if not isinstance(data, dict):
        raise ValueError(f"{name} run has invalid input")
    if data.get("date"):
        return date.fromisoformat(data["date"]).isoformat()
    if run.status in (V1TaskStatus.QUEUED, V1TaskStatus.RUNNING):
        if name == "vea-crude-evaluate":
            return target_session(yesterday(run.created_at)).isoformat()
        day = session_today(run.created_at)
        return day.isoformat() if day else None
    if run.status == V1TaskStatus.COMPLETED:
        raise ValueError(f"{name} cron run has no target in its output")
    return None


def _has_success_or_active_run(
    hatchet: Hatchet, name: str, target: date, due: datetime, now: datetime
) -> bool:
    since = datetime.combine(target, clock_time.min, SYDNEY).astimezone(UTC)
    offset = 0
    while True:
        page = hatchet.runs.list(since=since, until=now, limit=100, offset=offset)
        for run in page.rows:
            if run.workflow_name != name or run.created_at < due:
                continue
            if run.status not in (
                V1TaskStatus.COMPLETED,
                V1TaskStatus.RUNNING,
                V1TaskStatus.QUEUED,
            ):
                continue
            if _run_target(run, name) == target.isoformat():
                return True
        if len(page.rows) < 100:
            return False
        offset += len(page.rows)


def run_catch_up(hatchet: Hatchet, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    vea_day = target_session(yesterday(now))
    asx_300_day = session_today(now)
    jobs = (
        ("vea-crude-evaluate", vea_day, _due(vea_day, 23), lambda: dispatch(vea_day)),
        (
            "asx-300-capture",
            asx_300_day,
            _due(asx_300_day, 7) if asx_300_day else None,
            lambda: dispatch_asx_300(now),
        ),
    )
    errors: list[Exception] = []
    for name, day, due, send in jobs:
        if day is None or due is None or now < due + timedelta(minutes=10):
            continue
        try:
            if not _has_success_or_active_run(hatchet, name, day, due, now):
                send()
        except Exception as exc:
            LOGGER.exception("%s catch-up audit or dispatch failed", name)
            errors.append(exc)
    if errors:
        raise ExceptionGroup("Market catch-up dispatch failed", errors)
