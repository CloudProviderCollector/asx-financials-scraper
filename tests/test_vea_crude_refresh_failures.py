"""One unavailable price source must not prevent refreshing the other."""

from datetime import date
from unittest.mock import Mock

import pytest

from asx_financials import vea_crude_refresh as refresh


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
