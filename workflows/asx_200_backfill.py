"""Capture missing ASX 200 profiles for the last closed session on a non-trading day."""

import argparse
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Engine, create_engine, select, text

from asx_financials.config import get_settings
from asx_financials.domain.enums import IngestionRunStatus
from asx_financials.domain.value_objects import AsxTicker
from asx_financials.infrastructure.persistence.schema import (
    asx_profile_backfills,
    raw_source_payloads,
)
from asx_financials.infrastructure.persistence.store import PostgresFinancialDataStore
from asx_financials.infrastructure.providers.yfinance_provider import YFinanceProvider
from asx_financials.ingest_ticker_csv import load_tickers
from workflows.asx_200_profiles_workflow import (
    CALENDAR,
    LOCK_ID,
    ROSTER,
    SYDNEY,
    PacedSession,
    completed_tickers,
)


def backfill(engine: Engine, target: date, now: datetime | None = None) -> int:
    print(f"Checking weekend backfill eligibility for {target}", flush=True)
    now = now or datetime.now(UTC)
    today = now.astimezone(SYDNEY).date()
    if CALENDAR.is_session(today.isoformat()):
        raise ValueError("Backfill is only allowed on a non-trading day")
    latest = CALENDAR.date_to_session(today.isoformat(), direction="previous").date()
    if target != latest:
        raise ValueError(f"Only the most recent closed session ({latest}) can be backfilled")
    tickers = load_tickers(ROSTER)
    if len(tickers) != 200:
        raise ValueError("ASX 200 roster must contain exactly 200 tickers")

    print("Acquiring capture lock", flush=True)
    with engine.connect() as lock:
        if not lock.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": LOCK_ID}):
            raise RuntimeError("ASX 200 profile capture is already running")
        try:
            with engine.connect() as connection:
                mapped: set[str] = set(
                    connection.scalars(
                        select(asx_profile_backfills.c.ticker).where(
                            asx_profile_backfills.c.session_date == target
                        )
                    )
                )
            completed = completed_tickers(engine, target)
            missing = [
                ticker for ticker in tickers if ticker not in completed and ticker not in mapped
            ]
            print(f"{len(missing)} missing profiles for {target}", flush=True)
            store = PostgresFinancialDataStore(engine)
            provider = YFinanceProvider()
            captured = 0
            failed: list[str] = []
            with PacedSession() as session:
                for symbol in missing:
                    if CALENDAR.is_session(datetime.now(UTC).astimezone(SYDNEY).date().isoformat()):
                        raise RuntimeError("Trading resumed; stopping weekend backfill")
                    ticker = AsxTicker.parse(symbol)
                    run_id = str(uuid4())
                    store.start_ingestion_run(
                        run_id, ticker, ticker.yahoo_symbol, datetime.now(UTC)
                    )
                    try:
                        result = provider.fetch_profile(ticker, session=session)
                        if result.company_profile is None:
                            raise ValueError(f"{symbol}: Yahoo returned no company profile")
                        payload = result.company_profile.profile_payload
                        quote_time = payload.get("regularMarketTime")
                        if quote_time is None:
                            cap = payload.get("marketCap")
                            pe = (payload.get("trailingPE"), payload.get("forwardPE"))
                            if (
                                payload.get("currency") != "AUD"
                                or not isinstance(cap, (int, float))
                                or isinstance(cap, bool)
                                or cap <= 0
                                or not any(
                                    isinstance(value, (int, float))
                                    and not isinstance(value, bool)
                                    and value > 0
                                    for value in pe
                                )
                            ):
                                raise ValueError(
                                    f"{symbol}: no quote date or usable AUD valuation"
                                )
                        else:
                            if not isinstance(quote_time, (int, float)) or isinstance(
                                quote_time, bool
                            ):
                                raise ValueError(f"{symbol}: invalid market quote date")
                            quote_day = (
                                datetime.fromtimestamp(quote_time, UTC).astimezone(SYDNEY).date()
                            )
                            if not target - timedelta(days=7) <= quote_day <= target:
                                raise ValueError(
                                    f"{symbol}: quote date {quote_day} is outside the "
                                    f"seven-day window ending {target}"
                                )
                        store.complete_ingestion_run(
                            ingestion_run_id=run_id,
                            ticker=ticker,
                            yahoo_symbol=ticker.yahoo_symbol,
                            status=IngestionRunStatus.SUCCEEDED.value,
                            completed_at_utc=datetime.now(UTC),
                            message=(
                                f"Weekend profile backfill for {target}; "
                                "actual fetch time retained."
                            ),
                            company_profile=result.company_profile,
                            statement_snapshots=[],
                            raw_payloads=result.raw_payloads,
                            segment_failures=[],
                        )
                    except Exception as exc:
                        store.mark_ingestion_run_failed(
                            run_id, completed_at_utc=datetime.now(UTC), message=str(exc)
                        )
                        if not isinstance(exc, ValueError):
                            raise
                        failed.append(f"{symbol}: {exc}")
                        print(f"{symbol} failed: {exc}", flush=True)
                        continue
                    with engine.begin() as connection:
                        raw_id = connection.scalar(
                            select(raw_source_payloads.c.raw_payload_id).where(
                                raw_source_payloads.c.run_id == run_id,
                                raw_source_payloads.c.segment_name == "company-profile",
                            )
                        )
                        if raw_id is None:
                            raise RuntimeError(
                                f"{symbol}: completed run has no company-profile payload"
                            )
                        connection.execute(
                            asx_profile_backfills.insert().values(
                                session_date=target, ticker=symbol, raw_payload_id=raw_id
                            )
                        )
                    captured += 1
                    print(f"{captured}/{len(missing)} {symbol} captured", flush=True)
            if failed:
                raise RuntimeError(
                    f"Captured {captured}/{len(missing)} profiles; "
                    f"{len(failed)} failed: {'; '.join(failed)}"
                )
            return captured
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_date", type=date.fromisoformat)
    target = parser.parse_args().session_date
    print(f"Connecting to application database for {target}", flush=True)
    engine = create_engine(
        get_settings().database_url, pool_pre_ping=True, connect_args={"connect_timeout": 5}
    )
    try:
        print(f"Captured {backfill(engine, target)} weekend backfills", flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
