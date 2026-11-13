"""Capture point-in-time ASX 300 valuations after the Sydney market closes."""

import logging
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
from curl_cffi import requests
from hatchet_sdk import Hatchet
from hatchet_sdk.types.idempotency import StatusBasedIdempotencyConfig
from pydantic import BaseModel
from sqlalchemy import Engine, create_engine, select, text

from asx_financials.config import get_settings
from asx_financials.domain.enums import IngestionRunStatus
from asx_financials.domain.value_objects import AsxTicker
from asx_financials.infrastructure.persistence.schema import raw_source_payloads
from asx_financials.infrastructure.persistence.store import PostgresFinancialDataStore
from asx_financials.infrastructure.providers.yfinance_provider import YFinanceProvider
from asx_financials.ingest_ticker_csv import load_tickers

LOGGER = logging.getLogger(__name__)
SYDNEY = ZoneInfo("Australia/Sydney")
CALENDAR = xcals.get_calendar("XASX")
ROSTER = Path(__file__).resolve().parents[1] / "data" / "asx300" / "asx300_20260410.csv"
LOCK_ID = 992637412
INTERVAL_SECONDS = 10


class CaptureInput(BaseModel):
    date: str = ""


class PacedSession(requests.Session):
    """Space this job's outbound Yahoo requests, including retries and auth requests."""

    def __init__(self) -> None:
        super().__init__(impersonate="chrome")
        self.last_request: float | None = None

    def request(self, *args: Any, **kwargs: Any) -> Any:
        now = time.monotonic()
        if self.last_request is not None:
            time.sleep(max(0, INTERVAL_SECONDS - (now - self.last_request)))
        self.last_request = time.monotonic()
        return super().request(*args, **kwargs)


def session_today(now: datetime) -> date | None:
    local = now.astimezone(SYDNEY)
    if local.hour < 17:
        return None
    day = local.date()
    return day if CALENDAR.is_session(day.isoformat()) else None


def completed_tickers(engine: Engine, day: date) -> set[str]:
    start = datetime.combine(day, clock_time(17), SYDNEY).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), clock_time.min, SYDNEY).astimezone(UTC)
    source = raw_source_payloads
    with engine.connect() as connection:
        return set(
            connection.scalars(
                select(source.c.ticker).where(
                    source.c.exchange_code == "ASX",
                    source.c.source_name == "yfinance",
                    source.c.segment_name == "company-profile",
                    source.c.fetched_at >= start,
                    source.c.fetched_at < end,
                )
            )
        )


def capture(engine: Engine, now: datetime | None = None) -> int:
    """Resume today's roster; a failed fetch leaves that ticker eligible for retry."""
    day = session_today(now or datetime.now(UTC))
    if day is None:
        return 0
    tickers = load_tickers(ROSTER)
    if len(tickers) != 300:
        raise ValueError("ASX 300 roster must contain exactly 300 tickers")

    with engine.connect() as lock_connection:
        if not lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": LOCK_ID}
        ):
            raise RuntimeError("ASX 300 capture is already running; retry later")
        try:
            completed = completed_tickers(engine, day)
            store = PostgresFinancialDataStore(engine)
            provider = YFinanceProvider()
            captured = 0
            with PacedSession() as session:
                for symbol in tickers:
                    if symbol in completed:
                        continue
                    if session_today(datetime.now(UTC)) != day:
                        raise RuntimeError(f"ASX 300 capture for {day} ran past midnight")
                    ticker = AsxTicker.parse(symbol)
                    run_id = str(uuid4())
                    store.start_ingestion_run(
                        run_id, ticker, ticker.yahoo_symbol, datetime.now(UTC)
                    )
                    try:
                        result = provider.fetch_profile(ticker, session=session)
                        if result.company_profile is None:
                            raise ValueError(f"{symbol}: Yahoo returned no company profile")
                        if result.company_profile.fetched_at_utc.astimezone(SYDNEY).date() != day:
                            raise RuntimeError(f"{symbol}: profile fetch ran past midnight")
                        store.complete_ingestion_run(
                            ingestion_run_id=run_id,
                            ticker=ticker,
                            yahoo_symbol=ticker.yahoo_symbol,
                            status=IngestionRunStatus.SUCCEEDED.value,
                            completed_at_utc=datetime.now(UTC),
                            message="Point-in-time company profile captured.",
                            company_profile=result.company_profile,
                            statement_snapshots=[],
                            raw_payloads=result.raw_payloads,
                            segment_failures=[],
                        )
                    except Exception as exc:
                        store.mark_ingestion_run_failed(
                            run_id, completed_at_utc=datetime.now(UTC), message=str(exc)
                        )
                        raise
                    captured += 1
            LOGGER.info(
                "Captured %s ASX 300 profiles for %s (%s already stored)",
                captured,
                day,
                len(completed),
            )
            return captured
        finally:
            lock_connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": LOCK_ID}
            )


def check(now: datetime | None = None) -> int:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        return capture(engine, now)
    finally:
        engine.dispose()


def register_workflows(hatchet: Hatchet) -> Any:
    job = hatchet.workflow(
        name="asx-300-capture",
        on_crons=["0 7 * * *"],
        cron_input=CaptureInput(),
        input_validator=CaptureInput,
        idempotency=StatusBasedIdempotencyConfig(
            key_expression="input.date", fallback_ttl=timedelta(days=7)
        ),
        concurrency=1,
    )

    @job.task(execution_timeout=timedelta(hours=7))
    def capture_task(input: CaptureInput, ctx: Any) -> dict[str, str | None]:
        now = datetime.now(UTC)
        day = session_today(now)
        if input.date and (day is None or input.date != day.isoformat()):
            raise RuntimeError(f"ASX 300 capture for {input.date} is no longer eligible")
        if day is not None:
            check(now)
        return {"target": day.isoformat() if day is not None else None}

    return job
