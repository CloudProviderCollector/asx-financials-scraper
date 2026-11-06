"""Daily VEA/Brent signal monitoring; no orders or trades are placed."""

import logging
import os
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import requests
from dashboards.vea_crude_dashboard import latest_strategy_signals, load_prices
from hatchet_sdk import Hatchet
from hatchet_sdk.exceptions import IdempotencyCollisionError
from hatchet_sdk.types.idempotency import StatusBasedIdempotencyConfig
from pydantic import BaseModel
from sqlalchemy import Engine, create_engine, text

from asx_financials.config import get_settings
from asx_financials.vea_crude_refresh import latest_dates, refresh_prices

LOGGER = logging.getLogger(__name__)
SYDNEY = ZoneInfo("Australia/Sydney")
CALENDAR = xcals.get_calendar("XASX")
LOCK_ID = 992637411


def yesterday(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(SYDNEY).date() - timedelta(days=1)


def target_session(day: date) -> date:
    """Last completed ASX session on or before yesterday."""
    return CALENDAR.date_to_session(day.isoformat(), direction="previous").date()


def missing_sessions(latest: date | None, through: date) -> int:
    if latest is None:
        return 5
    if latest > through:
        return 0
    cutoff = target_session(through)
    if latest >= cutoff:
        return 0
    start = (latest + timedelta(days=1)).isoformat()
    return len(CALENDAR.sessions_in_range(start, cutoff.isoformat()))


def telegram(message: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=20,
        )
        response.raise_for_status()
        accepted = response.json().get("ok") is True
    except (requests.RequestException, ValueError):
        raise RuntimeError(
            "Telegram delivery failed; check the bot credentials and connection"
        ) from None
    if not accepted:
        raise RuntimeError("Telegram rejected the message")


def _already_complete(engine: Engine, target: date) -> bool:
    with engine.connect() as connection:
        return (
            connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM vea_crude_runs WHERE target_date = :day)"),
                {"day": target},
            )
            is True
        )


def _notify_once(engine: Engine, target: date, key: str, message: str) -> None:
    # The caller holds the session-wide advisory lock for the entire evaluation.
    with engine.begin() as connection:
        already_sent = connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM vea_crude_alerts "
                "WHERE alert_key = :key AND (:key NOT LIKE 'strategy-%' OR target_date = :day))"
            ),
            {"day": target, "key": key},
        )
        if already_sent:
            return
        telegram(message)
        connection.execute(
            text(
                "INSERT INTO vea_crude_alerts (target_date, alert_key, sent_at) "
                "VALUES (:day, :key, :sent_at)"
            ),
            {"day": target, "key": key, "sent_at": datetime.now(UTC)},
        )


def evaluate(engine: Engine, target: date) -> None:
    """Run once per Sydney evaluation date; retry failed refresh on later checks."""
    with engine.connect() as lock_connection:
        acquired = lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": LOCK_ID}
        )
        if not acquired:
            raise RuntimeError("Another VEA/Brent run is in progress; retry later")
        try:
            if _already_complete(engine, target):
                return
            refresh_error: Exception | None = None
            try:
                refresh_prices(engine, target)
            except Exception as exc:
                refresh_error = exc
                LOGGER.exception("Price refresh failed; evaluating stored prices")
            vea_date, brent_date = latest_dates(engine)
            for name, last_date in (("VEA", vea_date), ("Brent", brent_date)):
                count = missing_sessions(last_date, target)
                if count >= 5:
                    _notify_once(
                        engine,
                        target,
                        f"missing-{name}-{last_date or 'none'}",
                        f"{name} price missing for {count} ASX sessions through {target}; "
                        f"last published: {last_date or 'none'}.",
                    )
            shares, oil, opens = load_prices(engine)
            shares = [(day, value) for day, value in shares if day <= target]
            oil = [(day, value) for day, value in oil if day <= target]
            opens = {day: value for day, value in opens.items() if day <= target}
            result = latest_strategy_signals(shares, oil, opens)
            for strategy, state in result.strategies.items():
                direction = state.pending_action
                if direction is not None:
                    stale = " (refresh failed; stored data)" if refresh_error else ""
                    _notify_once(
                        engine,
                        target,
                        f"strategy-{strategy}-{direction}",
                        f"VEA strategy {strategy}: {direction.upper()} signal on "
                        f"{result.matched_date}; pending next available VEA open "
                        f"(not an executed trade).{stale}",
                    )
            if refresh_error is not None:
                raise RuntimeError(
                    "Price refresh failed; date not marked complete"
                ) from refresh_error
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO vea_crude_runs (target_date, completed_at) "
                        "VALUES (:day, :completed_at) ON CONFLICT (target_date) DO NOTHING"
                    ),
                    {"day": target, "completed_at": datetime.now(UTC)},
                )
        finally:
            lock_connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": LOCK_ID}
            )


class CheckInput(BaseModel):
    date: str = ""


job: Any = None


def register_workflows() -> tuple[Hatchet, list[Any]]:
    global job
    hatchet = Hatchet()
    job = hatchet.workflow(
        name="vea-crude-evaluate",
        input_validator=CheckInput,
        idempotency=StatusBasedIdempotencyConfig(
            key_expression="input.date", fallback_ttl=timedelta(days=7)
        ),
        concurrency=1,
    )

    @job.task()
    def monitor(input: CheckInput, ctx: Any) -> None:
        day = date.fromisoformat(input.date)
        engine = create_engine(get_settings().database_url, pool_pre_ping=True)
        try:
            evaluate(engine, day)
        finally:
            engine.dispose()

    # Hatchet cron is UTC. Both UTC hours cover Sydney's 09:00 across DST.
    daily = hatchet.workflow(name="vea-crude-daily", on_crons=["0 22 * * *", "0 23 * * *"])
    hourly = hatchet.workflow(name="vea-crude-catch-up", on_crons=["10 * * * *"])

    @daily.task()
    def daily_check(input: Any, ctx: Any) -> None:
        dispatch(daily_only=True)

    @hourly.task()
    def catch_up_check(input: Any, ctx: Any) -> None:
        dispatch()

    return hatchet, [job, daily, hourly]


def dispatch(*, daily_only: bool = False) -> None:
    now = datetime.now(UTC).astimezone(SYDNEY)
    if daily_only and now.hour != 9:
        return
    if now.hour < 9:
        return
    target = target_session(yesterday(now))
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        if _already_complete(engine, target):
            return
    finally:
        engine.dispose()
    # The worker may start before its registration becomes visible; retry at next hourly check.
    try:
        job.run(input=CheckInput(date=target.isoformat()), wait_for_result=False)
    except IdempotencyCollisionError:
        LOGGER.info("Evaluation for %s is already queued or running", target)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before starting worker")
    hatchet, workflows = register_workflows()

    def startup_check() -> None:
        time.sleep(10)
        while True:
            try:
                dispatch()
            except Exception:
                LOGGER.exception("Catch-up check failed; retrying hourly")
            time.sleep(3600)

    threading.Thread(target=startup_check, daemon=True, name="vea-crude-catch-up").start()
    hatchet.worker("vea-crude-worker", workflows=workflows, slots=2).start()


if __name__ == "__main__":
    main()
