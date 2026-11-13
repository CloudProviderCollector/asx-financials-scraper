"""ASX 200 daily snapshot capture and retry rules."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, Mock

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from workflows import asx_200_workflow as workflow

NOW = datetime(2026, 11, 10, 20, tzinfo=workflow.SYDNEY)


def test_before_close_and_weekend_choose_last_completed_session() -> None:
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.scalar.return_value = True
    assert workflow.capture(engine, datetime(2026, 11, 10, 18, tzinfo=workflow.SYDNEY)) is False
    assert workflow.capture(engine, datetime(2026, 11, 8, 20, tzinfo=workflow.SYDNEY)) is False
    calls = engine.connect.return_value.__enter__.return_value.scalar.call_args_list
    assert calls[0].args[1]["day"].isoformat() == "2026-11-09"
    assert calls[1].args[1]["day"].isoformat() == "2026-11-06"


def test_captures_history_and_full_info_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable migrated PostgreSQL database")
    engine = create_engine(url)
    stamp = pd.Timestamp("2026-11-10", tz="Australia/Sydney")
    frame = pd.DataFrame(
        [{"Open": 9029.7, "High": 9159.8, "Low": 9029.7, "Close": 9129.0, "Volume": 0}],
        index=[stamp],
    )
    ticker = Mock()
    ticker.history.return_value = frame
    ticker.get_info.return_value = {
        "currency": "AUD",
        "regularMarketTime": int(NOW.timestamp()),
        "regularMarketPrice": 9129.0,
        "averageVolume": 808938,
        "averageVolume10days": 857600,
        "fiftyTwoWeekHigh": 9296.7,
    }
    monkeypatch.setattr(workflow.yf, "Ticker", lambda symbol: ticker)
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM asx_200_snapshots WHERE session_date = '2026-11-10'"))
        assert workflow.capture(engine, NOW)
        assert not workflow.capture(engine, NOW)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT close, volume, average_volume, average_volume_10days, trailing_pe, "
                     "info FROM asx_200_snapshots WHERE session_date = '2026-11-10'")
            ).one()
        assert row.close == 9129
        assert row.volume is None
        assert row.average_volume == 808938
        assert row.average_volume_10days == 857600
        assert row.trailing_pe is None
        assert row.info["fiftyTwoWeekHigh"] == 9296.7
        assert ticker.history.call_count == 1
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM asx_200_snapshots WHERE session_date = '2026-11-10'"))
        engine.dispose()


def test_rejects_missing_or_stale_session(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.scalar.return_value = False
    ticker = Mock()
    ticker.history.return_value = pd.DataFrame(
        [{"Open": 1, "High": 1, "Low": 1, "Close": 1, "Volume": 1}],
        index=[pd.Timestamp("2026-11-09", tz="Australia/Sydney")],
    )
    monkeypatch.setattr(workflow.yf, "Ticker", lambda symbol: ticker)
    assert workflow.capture(engine, NOW) is False
    ticker.get_info.assert_not_called()
    ticker.history.return_value.index = [pd.Timestamp("2026-11-10", tz="Australia/Sydney")]
    ticker.get_info.return_value = {
        "regularMarketTime": int(datetime(2026, 11, 9, tzinfo=UTC).timestamp())
    }
    with pytest.raises(ValueError, match="stale"):
        workflow.capture(engine, NOW)


def test_missing_session_is_retried_after_newer_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock()
    captured = []
    monkeypatch.setattr(workflow, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(
        workflow, "capture", lambda engine, session_date: captured.append(session_date)
    )
    workflow.check(datetime(2026, 11, 12, 20, tzinfo=workflow.SYDNEY))
    assert [day.isoformat() for day in captured] == [
        "2026-11-12",
        "2026-11-11",
        "2026-11-10",
        "2026-11-09",
        "2026-11-06",
    ]
    engine.dispose.assert_called_once()
