"""Checks for resumable, paced CSV ingestion inputs."""

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from asx_financials.ingest_ticker_csv import load_tickers, read_progress


def test_csv_tickers_are_normalized_and_duplicates_rejected(tmp_path: Path) -> None:
    source = tmp_path / "tickers.csv"
    with source.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=["ticker", "name"])
        writer.writeheader()
        writer.writerow({"ticker": "cba", "name": "Commonwealth Bank"})
        writer.writerow({"ticker": "NAB", "name": "National Australia Bank"})
    assert load_tickers(source) == ["CBA", "NAB"]
    with source.open("a", encoding="utf-8", newline="") as output:
        csv.writer(output).writerow(["CBA", "Duplicate"])
    with pytest.raises(ValueError, match="duplicate"):
        load_tickers(source)


def test_progress_resumes_completed_only_and_preserves_last_start(tmp_path: Path) -> None:
    progress = tmp_path / "progress.jsonl"
    stamp = datetime(2026, 10, 30, tzinfo=UTC)
    events = [
        {"event": "start", "ticker": "CBA", "time": stamp.isoformat()},
        {"event": "finish", "ticker": "CBA", "status": "succeeded"},
        {"event": "start", "ticker": "NAB", "time": stamp.isoformat()},
        {"event": "finish", "ticker": "NAB", "status": "error"},
    ]
    progress.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    assert read_progress(progress) == ({"CBA"}, stamp)
