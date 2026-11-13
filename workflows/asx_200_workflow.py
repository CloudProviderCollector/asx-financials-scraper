"""Capture the latest completed S&P/ASX 200 session from Yahoo Finance."""

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import yfinance as yf
from hatchet_sdk import Hatchet
from hatchet_sdk.types.idempotency import StatusBasedIdempotencyConfig
from pydantic import BaseModel
from sqlalchemy import Engine, bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB

from asx_financials.config import get_settings

LOGGER = logging.getLogger(__name__)
SYDNEY = ZoneInfo("Australia/Sydney")
CALENDAR = xcals.get_calendar("XASX")
SYMBOL = "^AXJO"


class CaptureInput(BaseModel):
    hour: str = ""


def latest_session(now: datetime) -> date:
    local = now.astimezone(SYDNEY)
    candidate = local.date() if local.hour >= 19 else local.date() - timedelta(days=1)
    return CALENDAR.date_to_session(candidate.isoformat(), direction="previous").date()


def capture(engine: Engine, now: datetime | None = None, session_date: date | None = None) -> bool:
    """Store one complete session; leave the row absent so failed fetches can retry."""
    day = session_date or latest_session(now or datetime.now(UTC))

    with engine.connect() as connection:
        if connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM asx_200_snapshots WHERE session_date = :day)"),
            {"day": day},
        ):
            return False

    ticker = yf.Ticker(SYMBOL)
    frame = ticker.history(period="1mo", auto_adjust=False)
    if frame.empty:
        raise ValueError(f"Yahoo returned no {SYMBOL} history")
    matches = frame.loc[[stamp for stamp in frame.index if stamp.date() == day]]
    if matches.empty:
        LOGGER.warning("Yahoo has no %s bar for %s; will retry", SYMBOL, day)
        return False
    if len(matches) != 1:
        raise ValueError(f"Yahoo returned multiple {SYMBOL} bars for {day}")
    bar = matches.iloc[0]
    if any(
        field not in frame or not bool(bar[field] > 0) for field in ("Open", "High", "Low", "Close")
    ):
        raise ValueError(f"Yahoo returned an incomplete {SYMBOL} bar for {day}")

    info = ticker.get_info()
    quote_time = info.get("regularMarketTime")
    if not isinstance(quote_time, (int, float)):
        raise ValueError(f"Yahoo returned no {SYMBOL} quote time")
    observed = datetime.fromtimestamp(quote_time, UTC)
    if observed.astimezone(SYDNEY).date() < day:
        raise ValueError(f"Yahoo returned a stale {SYMBOL} quote for {day}")
    quote_matches = observed.astimezone(SYDNEY).date() == day

    values = {
        "session_date": day,
        "symbol": SYMBOL,
        "currency": info.get("currency") if quote_matches else None,
        "open": float(bar["Open"]),
        "high": float(bar["High"]),
        "low": float(bar["Low"]),
        "close": float(bar["Close"]),
        "volume": int(bar["Volume"]) if "Volume" in frame and bar["Volume"] > 0 else None,
        "regular_market_price": info.get("regularMarketPrice") if quote_matches else None,
        "previous_close": info.get("regularMarketPreviousClose") if quote_matches else None,
        "average_volume": info.get("averageVolume") if quote_matches else None,
        "average_volume_10days": info.get("averageVolume10days") if quote_matches else None,
        "trailing_pe": info.get("trailingPE") if quote_matches else None,
        "forward_pe": info.get("forwardPE") if quote_matches else None,
        "quote_time": observed if quote_matches else None,
        "info": info if quote_matches else {},
        "fetched_at": datetime.now(UTC),
    }
    with engine.begin() as connection:
        result = connection.execute(
            text(
                "INSERT INTO asx_200_snapshots (session_date, symbol, currency, open, high, low, "
                "close, volume, regular_market_price, previous_close, average_volume, "
                "average_volume_10days, trailing_pe, forward_pe, quote_time, info, fetched_at) "
                "VALUES (:session_date, :symbol, :currency, :open, :high, :low, :close, :volume, "
                ":regular_market_price, :previous_close, :average_volume, :average_volume_10days, "
                ":trailing_pe, :forward_pe, :quote_time, :info, :fetched_at) "
                "ON CONFLICT (session_date) DO NOTHING"
            ).bindparams(bindparam("info", type_=JSONB)),
            values,
        )
    return result.rowcount > 0


def check(now: datetime | None = None) -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        session = CALENDAR.date_to_session(
            latest_session(now or datetime.now(UTC)).isoformat(), direction="none"
        )
        for _ in range(5):
            capture(engine, session_date=session.date())
            session = CALENDAR.previous_session(session)
    finally:
        engine.dispose()


def register_workflows(hatchet: Hatchet) -> Any:
    job = hatchet.workflow(
        name="asx-200-capture",
        on_crons=["0 9 * * *"],
        cron_input=CaptureInput(),
        input_validator=CaptureInput,
        idempotency=StatusBasedIdempotencyConfig(
            key_expression="input.hour", fallback_ttl=timedelta(days=7)
        ),
        concurrency=1,
    )

    @job.task()
    def capture_task(input: CaptureInput, ctx: Any) -> dict[str, str]:
        now = (
            datetime.strptime(input.hour, "%Y-%m-%dT%H").replace(tzinfo=SYDNEY)
            if input.hour
            else datetime.now(UTC)
        )
        day = latest_session(now)
        check(now)
        return {"target": day.isoformat()}

    return job
