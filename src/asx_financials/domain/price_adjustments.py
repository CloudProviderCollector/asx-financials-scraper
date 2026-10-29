"""Derive adjusted closes from split-normalized Yahoo closes and cash dividends."""

from datetime import date
from decimal import Decimal
from typing import Any

from asx_financials.domain.models import CorporateAction


def derive_adjusted_closes(
    prices: list[dict[str, Any]], actions: list[CorporateAction]
) -> dict[date, Decimal | None]:
    """Apply later cash distributions to earlier Yahoo closes.

    Yahoo closes are already split-normalized as of their fetch. A later split
    requires updating the scale of older stored closes before computing returns.
    """
    if not prices:
        return {}
    ordered = sorted(prices, key=lambda row: row["trading_date"])
    splits = [action for action in actions if action.action_type == "split"]
    normalized: dict[date, Decimal | None] = {}
    for row in ordered:
        day = row["trading_date"]
        value = row["close"]
        if value is None:
            normalized[day] = None
            continue
        close = Decimal(str(value))
        for split in splits:
            if split.effective_date > day and split.effective_date > row["fetched_at"].date():
                close /= split.value
        normalized[day] = close

    factors: dict[date, Decimal] = {}
    days = [row["trading_date"] for row in ordered]
    for action in actions:
        if action.action_type != "dividend":
            continue
        preceding = next((day for day in reversed(days) if day < action.effective_date), None)
        if preceding is None or normalized[preceding] is None:
            continue
        # Dividend currency/share must be on the same split basis as the close.
        dividend = action.value
        for split in splits:
            if (
                split.effective_date > action.effective_date
                and split.effective_date > action.fetched_at_utc.date()
            ):
                dividend /= split.value
        previous_close = normalized[preceding]
        if previous_close is None or previous_close <= dividend:
            continue
        factor = Decimal(1) - dividend / previous_close
        factors[action.effective_date] = factors.get(action.effective_date, Decimal(1)) * factor

    multiplier = Decimal(1)
    result: dict[date, Decimal | None] = {}
    events = sorted(factors.items(), reverse=True)
    event_index = 0
    for day in reversed(days):
        while event_index < len(events) and events[event_index][0] > day:
            multiplier *= events[event_index][1]
            event_index += 1
        normalized_close = normalized[day]
        result[day] = normalized_close * multiplier if normalized_close is not None else None
    return result
