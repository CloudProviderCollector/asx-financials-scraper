"""Ingest tickers from a CSV at a fixed minimum interval, with resumable progress."""

import argparse
import csv
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from asx_financials.application.services import (
    IngestTickerCommand,
    SystemClock,
    TickerIngestionService,
)
from asx_financials.config import get_settings
from asx_financials.domain.value_objects import AsxTicker
from asx_financials.infrastructure.persistence.store import create_postgres_data_store
from asx_financials.infrastructure.providers.yfinance_provider import YFinanceProvider


def load_tickers(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or "ticker" not in reader.fieldnames:
            raise ValueError("CSV must have a ticker column")
        tickers = [AsxTicker.parse(row["ticker"]).value for row in reader]
    if len(tickers) != len(set(tickers)):
        raise ValueError("CSV contains duplicate tickers")
    return tickers


def read_progress(path: Path) -> tuple[set[str], datetime | None]:
    completed: set[str] = set()
    last_start = None
    if path.exists():
        with path.open(encoding="utf-8") as source:
            for line in source:
                event = json.loads(line)
                if event["event"] == "start":
                    last_start = datetime.fromisoformat(event["time"])
                elif event["event"] == "finish" and event["status"] in {
                    "succeeded",
                    "partially_succeeded",
                }:
                    completed.add(event["ticker"])
    return completed, last_start


def record(path: Path, event: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(event) + "\n")
        output.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument(
        "--interval", type=int, default=60, help="Minimum seconds between ticker starts"
    )
    args = parser.parse_args()
    if args.interval < 60:
        parser.error("The minimum interval is 60 seconds")
    tickers = load_tickers(args.csv)
    completed, last_start = read_progress(args.progress)
    remaining = [ticker for ticker in tickers if ticker not in completed]
    print(
        f"Loaded {len(tickers)} tickers; {len(completed)} completed, {len(remaining)} remaining.",
        flush=True,
    )
    if not remaining:
        return

    service = TickerIngestionService(
        YFinanceProvider(), create_postgres_data_store(get_settings().database_url), SystemClock()
    )
    for index, ticker in enumerate(remaining, 1):
        if last_start is not None:
            wait = args.interval - (datetime.now(UTC) - last_start).total_seconds()
            if wait > 0:
                print(f"Waiting {wait:.0f}s before {ticker}", flush=True)
                time.sleep(wait)
        last_start = datetime.now(UTC)
        record(args.progress, {"event": "start", "ticker": ticker, "time": last_start.isoformat()})
        print(f"[{index}/{len(remaining)}] {ticker} started {last_start.isoformat()}", flush=True)
        result_errors: list[str] = []
        try:
            result = service.ingest(IngestTickerCommand(ticker=ticker))
            status = result.status.value
            result_errors = [failure.reason for failure in result.segment_failures]
            detail = {
                "prices": result.stored_price_bar_count,
                "snapshots": result.inserted_snapshot_count,
                "segment_failures": [failure.segment_name for failure in result.segment_failures],
            }
        except Exception as exc:
            status = "error"
            detail = {"error": str(exc)}
        record(
            args.progress,
            {
                "event": "finish",
                "ticker": ticker,
                "time": datetime.now(UTC).isoformat(),
                "status": status,
                **detail,
            },
        )
        print(f"[{index}/{len(remaining)}] {ticker}: {status} {detail}", flush=True)
        errors = [str(detail.get("error", "")), *result_errors]
        if any(
            marker in error.lower()
            for error in errors
            for marker in ("rate limit", "too many requests", "429", "yfratelimiterror")
        ):
            print("Rate limiting detected; stopping batch to avoid further requests.", flush=True)
            break
    print("Batch finished. Check progress for partial failures and errors.", flush=True)


if __name__ == "__main__":
    main()
