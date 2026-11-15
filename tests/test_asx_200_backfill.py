"""Weekend ASX 200 backfill safety checks."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, Mock, patch

import pytest
from workflows import asx_200_backfill as backfill


def test_rejects_trading_days_and_older_sessions() -> None:
    with pytest.raises(ValueError, match="non-trading day"):
        backfill.backfill(Mock(), date(2026, 11, 13), datetime(2026, 11, 16, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="most recent closed session"):
        backfill.backfill(Mock(), date(2026, 11, 12), datetime(2026, 11, 14, 1, tzinfo=UTC))


def test_failed_quote_does_not_prevent_later_profiles_from_being_mapped() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.scalar.side_effect = [True, "raw-id"]
    connection.scalars.return_value = []
    provider = Mock()
    provider.fetch_profile.side_effect = [
        Mock(company_profile=None),
        Mock(
            company_profile=Mock(
                profile_payload={
                    "regularMarketTime": int(datetime(2026, 11, 13, 4, tzinfo=UTC).timestamp())
                }
            ),
            raw_payloads=[],
        ),
    ]
    store = Mock()
    completed = {f"T{i:03}" for i in range(198)}
    with (
        patch.object(backfill, "load_tickers", return_value=["ASK", "BHP", *completed]),
        patch.object(backfill, "completed_tickers", return_value=completed),
        patch.object(backfill, "PostgresFinancialDataStore", return_value=store),
        patch.object(backfill, "YFinanceProvider", return_value=provider),
        patch.object(backfill, "PacedSession"),
    ):
        with pytest.raises(RuntimeError, match=r"Captured 1/2 profiles; 1 failed: ASK"):
            backfill.backfill(engine, date(2026, 11, 13), datetime(2026, 11, 14, 1, tzinfo=UTC))

    store.mark_ingestion_run_failed.assert_called_once()
    store.complete_ingestion_run.assert_called_once()
    engine.begin.return_value.__enter__.return_value.execute.assert_called_once()


def test_accepts_recent_older_quote_and_undated_usable_valuation() -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.scalar.side_effect = [True, "qub-id", "rym-id"]
    connection.scalars.return_value = []
    provider = Mock()
    provider.fetch_profile.side_effect = [
        Mock(
            company_profile=Mock(
                profile_payload={
                    "currency": "AUD",
                    "marketCap": 900,
                    "trailingPE": 12,
                    "regularMarketTime": None,
                }
            ),
            raw_payloads=[],
        ),
        Mock(
            company_profile=Mock(
                profile_payload={
                    "regularMarketTime": int(datetime(2026, 11, 12, 4, tzinfo=UTC).timestamp())
                }
            ),
            raw_payloads=[],
        ),
    ]
    store = Mock()
    completed = {f"T{i:03}" for i in range(198)}
    with (
        patch.object(backfill, "load_tickers", return_value=["QUB", "RYM", *completed]),
        patch.object(backfill, "completed_tickers", return_value=completed),
        patch.object(backfill, "PostgresFinancialDataStore", return_value=store),
        patch.object(backfill, "YFinanceProvider", return_value=provider),
        patch.object(backfill, "PacedSession"),
    ):
        assert backfill.backfill(
            engine, date(2026, 11, 13), datetime(2026, 11, 14, 1, tzinfo=UTC)
        ) == 2

    assert store.complete_ingestion_run.call_count == 2
    assert engine.begin.return_value.__enter__.return_value.execute.call_count == 2
