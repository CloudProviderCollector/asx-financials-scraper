"""Capture the S&P/ASX 200 index after the Sydney market closes."""

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import yfinance as yf
from hatchet_sdk import Hatchet
from hatchet_sdk.types.idempotency import StatusBasedIdempotencyConfig
from pydantic import BaseModel
from sqlalchemy import Engine, bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB

from asx_financials.config import get_settings
from workflows.asx_200_profiles_workflow import CALENDAR, SYDNEY, PacedSession, session_today

LOGGER = logging.getLogger(__name__)
SYMBOL = "^AXJO"


class CaptureInput(BaseModel):
    date: str = ""


def capture(engine: Engine, now: datetime | None = None) -> int:
    """Fill all sessions since the last stored index bar, including today's."""
    day = session_today(now or datetime.now(UTC))
    if day is None:
        return 0
    with engine.connect() as connection:
        last_day = connection.scalar(
            text("SELECT MAX(session_date) FROM asx_200_snapshots WHERE session_date <= :day"),
            {"day": day},
        )
    if last_day == day:
        return 0
    start = last_day + timedelta(days=1) if last_day is not None else day
    sessions = [stamp.date() for stamp in CALENDAR.sessions_in_range(start, day)]

    with PacedSession() as session:
        ticker = yf.Ticker(SYMBOL, session=session)
        frame = ticker.history(
            start=start.isoformat(),
            end=(day + timedelta(days=1)).isoformat(),
            auto_adjust=False,
        )
        if frame.empty:
            raise ValueError(f"Yahoo returned no {SYMBOL} bars for {start} through {day}")
        bars: dict[date, Any] = {}
        for stamp, bar in frame.iterrows():
            bar_day = stamp.astimezone(SYDNEY).date()
            if bar_day in bars:
                raise ValueError(f"Yahoo returned multiple {SYMBOL} bars for {bar_day}")
            bars[bar_day] = bar
        for bar_day in sessions:
            if bar_day not in bars:
                raise ValueError(f"Yahoo returned no {SYMBOL} bar for {bar_day}")
            bar = bars[bar_day]
            if any(
                field not in frame or not bool(bar[field] > 0)
                for field in ("Open", "High", "Low", "Close")
            ):
                raise ValueError(f"Yahoo returned an incomplete {SYMBOL} bar for {bar_day}")
        info = ticker.get_info()
        if not isinstance(info, dict):
            raise ValueError(f"Yahoo returned invalid {SYMBOL} info")
        quote_time = info.get("regularMarketTime")
        if not isinstance(quote_time, (int, float)) or isinstance(quote_time, bool):
            raise ValueError(f"Yahoo returned no {SYMBOL} quote time")
        observed = datetime.fromtimestamp(quote_time, UTC)
        if observed.astimezone(SYDNEY).date() < day:
            raise ValueError(f"Yahoo returned a stale {SYMBOL} quote for {day}")
        quote_matches = observed.astimezone(SYDNEY).date() == day
        fetched_at = datetime.now(UTC)
        if session_today(fetched_at) != day:
            raise RuntimeError(f"ASX 200 index capture for {day} ran past midnight")

    values = []
    for bar_day in sessions:
        bar = bars[bar_day]
        current_quote = bar_day == day and quote_matches
        value = _bar_value(bar_day, bar, "Volume" in frame, fetched_at)
        if current_quote:
            value.update(
                currency=info.get("currency"),
                regular_market_price=info.get("regularMarketPrice"),
                previous_close=info.get("regularMarketPreviousClose"),
                average_volume=info.get("averageVolume"),
                average_volume_10days=info.get("averageVolume10days"),
                trailing_pe=info.get("trailingPE"),
                forward_pe=info.get("forwardPE"),
                quote_time=observed,
                info=info,
            )
        values.append(value)
    count = _insert_bars(engine, values)
    LOGGER.info("Captured %s %s index snapshots through %s", count, SYMBOL, day)
    return count


def _bar_value(bar_day: date, bar: Any, has_volume: bool, fetched_at: datetime) -> dict[str, Any]:
    return {
        "session_date": bar_day,
        "symbol": SYMBOL,
        "currency": None,
        "open": float(bar["Open"]),
        "high": float(bar["High"]),
        "low": float(bar["Low"]),
        "close": float(bar["Close"]),
        "volume": int(bar["Volume"]) if has_volume and bar["Volume"] > 0 else None,
        "regular_market_price": None,
        "previous_close": None,
        "average_volume": None,
        "average_volume_10days": None,
        "trailing_pe": None,
        "forward_pe": None,
        "quote_time": None,
        "info": {},
        "fetched_at": fetched_at,
    }


def _insert_bars(engine: Engine, values: list[dict[str, Any]]) -> int:
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
    return result.rowcount


def backfill(engine: Engine, start: date, end: date, now: datetime | None = None) -> int:
    """Insert historical daily bars without replacing point-in-time captures."""
    if start > end:
        raise ValueError("Backfill start must not be after end")
    today = now or datetime.now(UTC)
    latest_closed = session_today(today)
    if latest_closed is None:
        local_day = today.astimezone(SYDNEY).date()
        prior_day = local_day - timedelta(days=1) if CALENDAR.is_session(local_day) else local_day
        latest_closed = CALENDAR.date_to_session(prior_day.isoformat(), direction="previous").date()
    if end > latest_closed:
        raise ValueError(
            f"Backfill end must not be after the latest closed session ({latest_closed})"
        )
    sessions = [stamp.date() for stamp in CALENDAR.sessions_in_range(start, end)]
    if not sessions:
        raise ValueError("Backfill range contains no ASX trading sessions")
    with PacedSession() as session:
        frame = yf.Ticker(SYMBOL, session=session).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=False,
        )
    if frame.empty:
        raise ValueError(f"Yahoo returned no {SYMBOL} bars for {start} through {end}")
    bars: dict[date, Any] = {}
    for stamp, bar in frame.iterrows():
        bar_day = stamp.astimezone(SYDNEY).date()
        if bar_day in bars:
            raise ValueError(f"Yahoo returned multiple {SYMBOL} bars for {bar_day}")
        bars[bar_day] = bar
    values = []
    for bar_day in sessions:
        if bar_day not in bars:
            raise ValueError(f"Yahoo returned no {SYMBOL} bar for {bar_day}")
        bar = bars[bar_day]
        if any(
            field not in frame or not bool(bar[field] > 0)
            for field in ("Open", "High", "Low", "Close")
        ):
            raise ValueError(f"Yahoo returned an incomplete {SYMBOL} bar for {bar_day}")
        values.append(_bar_value(bar_day, bar, "Volume" in frame, today))
    count = _insert_bars(engine, values)
    LOGGER.info("Backfilled %s %s index snapshots from %s through %s", count, SYMBOL, start, end)
    return count


def check(now: datetime | None = None) -> int:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        return capture(engine, now)
    finally:
        engine.dispose()


def register_workflow(hatchet: Hatchet) -> Any:
    job = hatchet.workflow(
        name="asx-200-index-capture",
        on_crons=["0 7 * * *"],
        cron_input=CaptureInput(),
        input_validator=CaptureInput,
        idempotency=StatusBasedIdempotencyConfig(
            key_expression='"asx-200-index-capture:" + input.date',
            fallback_ttl=timedelta(days=7),
        ),
        concurrency=1,
    )

    @job.task(execution_timeout=timedelta(minutes=30))
    def capture_task(input: CaptureInput, ctx: Any) -> dict[str, str | None]:
        now = datetime.now(UTC)
        day = session_today(now)
        if input.date and (day is None or input.date != day.isoformat()):
            raise RuntimeError(f"ASX 200 index capture for {input.date} is no longer eligible")
        if day is not None:
            check(now)
        return {"target": day.isoformat() if day is not None else None}

    return job
