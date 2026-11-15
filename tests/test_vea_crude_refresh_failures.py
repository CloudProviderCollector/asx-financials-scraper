"""One unavailable price source must not prevent refreshing the other."""

from datetime import date
from unittest.mock import MagicMock, Mock

import pandas as pd
import pytest

from asx_financials import vea_crude_refresh as refresh


def test_refresh_replaces_closed_yahoo_session_for_each_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[object] = []
    tickers: list[str] = []

    def new_session(*, impersonate: str) -> object:
        assert impersonate == "chrome"
        session = object()
        sessions.append(session)
        return session

    def ticker(symbol: str, *, session: object | None = None) -> Mock:
        if session is None:
            raise RuntimeError("Session is closed, cannot send request")
        tickers.append(symbol)
        result = Mock()
        result.history.return_value = pd.DataFrame(
            {"Close": [80]},
            index=pd.to_datetime(["2026-01-09"]),
        )
        return result

    provider = Mock()
    provider._fetch_price_history.return_value = []
    monkeypatch.setattr(refresh, "YFinanceProvider", lambda: provider)
    monkeypatch.setattr(refresh, "latest_dates", lambda engine: (None, None))
    monkeypatch.setattr(refresh, "save_prices", Mock())
    monkeypatch.setattr(refresh.requests, "Session", new_session)
    monkeypatch.setattr(refresh.yf, "Ticker", ticker)

    refresh.refresh_prices(MagicMock(), date(2026, 1, 9))

    assert tickers == ["VEA.AX", "BZ=F"]
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]


@pytest.mark.parametrize("failed", ["VEA", "Brent"])
def test_refresh_both_feeds_despite_one_failure(
    monkeypatch: pytest.MonkeyPatch, failed: str
) -> None:
    calls: list[str] = []

    def source(name: str):
        def run(engine: object, through: date) -> None:
            calls.append(name)
            if name == failed:
                raise RuntimeError("network unavailable")

        return run

    monkeypatch.setattr(refresh, "refresh_vea", source("VEA"))
    monkeypatch.setattr(refresh, "refresh_brent", source("Brent"))
    with pytest.raises(RuntimeError, match=f"{failed}: network unavailable"):
        refresh.refresh_prices(Mock(), date(2026, 1, 9))
    assert calls == ["VEA", "Brent"]
