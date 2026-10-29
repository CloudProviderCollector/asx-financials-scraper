from datetime import UTC, date, datetime
from decimal import Decimal

from asx_financials.domain.models import CorporateAction
from asx_financials.domain.price_adjustments import derive_adjusted_closes


def row(day: int, close: str, fetched: int = 20) -> dict:
    return {
        "trading_date": date(2026, 1, day),
        "close": Decimal(close),
        "fetched_at": datetime(2026, 1, fetched, tzinfo=UTC),
    }


def action(day: int, kind: str, value: str, fetched: int = 20) -> CorporateAction:
    return CorporateAction(
        kind, date(2026, 1, day), Decimal(value), "yfinance", datetime(2026, 1, fetched, tzinfo=UTC)
    )


def test_dividends_apply_to_preceding_sessions_only() -> None:
    prices = [row(1, "100"), row(2, "90"), row(3, "100"), row(4, "90"), row(5, "100")]
    actual = derive_adjusted_closes(
        prices, [action(2, "dividend", "10"), action(4, "dividend", "10")]
    )
    assert actual[date(2026, 1, 1)] == Decimal("81")
    assert actual[date(2026, 1, 2)] == Decimal("81")
    assert actual[date(2026, 1, 3)] == Decimal("90")
    assert actual[date(2026, 1, 4)] == Decimal("90")
    assert actual[date(2026, 1, 5)] == Decimal("100")


def test_split_only_changes_stale_price_basis_not_yahoo_adjusted_closes() -> None:
    prices = [row(1, "100", fetched=1), row(2, "50", fetched=20), row(3, "50", fetched=20)]
    actual = derive_adjusted_closes(prices, [action(2, "split", "2")])
    assert actual == {date(2026, 1, day): Decimal("50") for day in (1, 2, 3)}


def test_dividend_with_later_split_uses_matching_share_basis() -> None:
    prices = [row(1, "100", fetched=2), row(2, "90", fetched=2), row(3, "45", fetched=20)]
    actual = derive_adjusted_closes(
        prices, [action(2, "dividend", "10", fetched=2), action(3, "split", "2")]
    )
    assert actual[date(2026, 1, 1)] == Decimal("45")
    assert actual[date(2026, 1, 2)] == Decimal("45")


def test_dividend_fetched_after_split_is_already_on_current_basis() -> None:
    prices = [row(1, "50"), row(2, "45"), row(3, "45")]
    actual = derive_adjusted_closes(prices, [action(2, "dividend", "5"), action(3, "split", "2")])
    assert actual[date(2026, 1, 1)] == Decimal("45")
    assert actual[date(2026, 1, 2)] == Decimal("45")


def test_adjustment_updates_when_dividend_is_corrected() -> None:
    prices = [row(1, "100"), row(2, "95")]
    old = derive_adjusted_closes(prices, [action(2, "dividend", "5")])
    corrected = derive_adjusted_closes(prices, [action(2, "dividend", "10")])
    assert old[date(2026, 1, 1)] == Decimal("95")
    assert corrected[date(2026, 1, 1)] == Decimal("90")


def test_no_actions_preserves_close() -> None:
    assert derive_adjusted_closes([row(1, "12.30")], []) == {date(2026, 1, 1): Decimal("12.30")}
