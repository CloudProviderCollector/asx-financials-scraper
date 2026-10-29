import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, cast
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from asx_financials.domain.enums import StatementFrequency, StatementType
from asx_financials.domain.models import (
    CompanyProfileSnapshot,
    CorporateAction,
    DailyPriceBar,
    FinancialStatementSnapshot,
    ProviderFetchResult,
    RawSourcePayload,
    SegmentFailure,
)
from asx_financials.domain.value_objects import AsxTicker

PANDAS4_WARNING = getattr(pd.errors, "Pandas4Warning", FutureWarning)
ASX_TIMEZONE = ZoneInfo("Australia/Sydney")
PRICE_HISTORY_SEGMENT = "daily-price-history"
ACTION_SEGMENT = "corporate-actions"


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _normalize_scalar(value: Any) -> Any:
    if value is None:
        return None

    if hasattr(value, "item"):
        try:
            value = value.item()
        except ValueError:
            pass

    if isinstance(value, pd.Timestamp):
        if value.tzinfo is None:
            value = value.tz_localize(UTC)
        else:
            value = value.tz_convert(UTC)
        return value.isoformat()

    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, dict):
        return {str(key): _normalize_scalar(inner) for key, inner in value.items()}

    if isinstance(value, list):
        return [_normalize_scalar(item) for item in value]

    if pd.isna(value):
        return None

    return value


def _dataframe_to_raw_payload(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "columns": [str(column) for column in frame.columns.tolist()],
        "index": [str(index) for index in frame.index.tolist()],
        "values": {
            str(column): {
                str(index): _normalize_scalar(value) for index, value in frame[column].items()
            }
            for column in frame.columns
        },
    }


def _column_to_period_end(column: Any) -> date | None:
    if isinstance(column, pd.Timestamp):
        return column.date()

    if isinstance(column, datetime):
        return column.date()

    if isinstance(column, date):
        return column

    parsed = cast(Any, pd.to_datetime(column, errors="coerce"))
    if pd.isna(parsed):
        return None

    return cast(date, parsed.date())


def _payload_hash(payload: dict[str, Any]) -> str:
    return sha256(repr(payload).encode("utf-8")).hexdigest()[:16]


def _run_yfinance_without_pandas4_warning(callback: Callable[[], Any]) -> Any:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*Timestamp\.utcnow is deprecated.*",
            category=PANDAS4_WARNING,
            module=r"yfinance\..*",
        )
        return callback()


@dataclass(frozen=True, slots=True)
class StatementModuleDefinition:
    segment_name: str
    statement_type: StatementType
    frequency: StatementFrequency
    accessor_name: str


class YFinanceProvider:
    SOURCE_NAME = "yfinance"

    MODULES = (
        StatementModuleDefinition(
            segment_name="income-statement-annual",
            statement_type=StatementType.INCOME_STATEMENT,
            frequency=StatementFrequency.ANNUAL,
            accessor_name="get_income_stmt",
        ),
        StatementModuleDefinition(
            segment_name="income-statement-quarterly",
            statement_type=StatementType.INCOME_STATEMENT,
            frequency=StatementFrequency.QUARTERLY,
            accessor_name="get_income_stmt",
        ),
        StatementModuleDefinition(
            segment_name="balance-sheet-annual",
            statement_type=StatementType.BALANCE_SHEET,
            frequency=StatementFrequency.ANNUAL,
            accessor_name="get_balance_sheet",
        ),
        StatementModuleDefinition(
            segment_name="balance-sheet-quarterly",
            statement_type=StatementType.BALANCE_SHEET,
            frequency=StatementFrequency.QUARTERLY,
            accessor_name="get_balance_sheet",
        ),
        StatementModuleDefinition(
            segment_name="cash-flow-annual",
            statement_type=StatementType.CASH_FLOW,
            frequency=StatementFrequency.ANNUAL,
            accessor_name="get_cash_flow",
        ),
        StatementModuleDefinition(
            segment_name="cash-flow-quarterly",
            statement_type=StatementType.CASH_FLOW,
            frequency=StatementFrequency.QUARTERLY,
            accessor_name="get_cash_flow",
        ),
    )

    def fetch(
        self,
        ticker: AsxTicker,
        include_annual: bool,
        include_quarterly: bool,
        latest_price_date: date | None = None,
        latest_action_date: date | None = None,
    ) -> ProviderFetchResult:
        yahoo_ticker = yf.Ticker(ticker.yahoo_symbol)
        fetched_at_utc = _utcnow()

        raw_payloads: list[RawSourcePayload] = []
        failures: list[SegmentFailure] = []

        info = self._safe_info(yahoo_ticker)
        company_profile = self._build_company_profile(
            ticker=ticker,
            fetched_at_utc=fetched_at_utc,
            info=info,
            raw_payloads=raw_payloads,
            failures=failures,
        )

        statement_snapshots: list[FinancialStatementSnapshot] = []
        for module in self.MODULES:
            if module.frequency is StatementFrequency.ANNUAL and not include_annual:
                continue
            if module.frequency is StatementFrequency.QUARTERLY and not include_quarterly:
                continue

            frame = self._fetch_statement_frame(yahoo_ticker, module)
            if frame is None or frame.empty:
                failures.append(SegmentFailure(module.segment_name, "Statement data was empty."))
                continue

            raw_payloads.append(
                RawSourcePayload(
                    source_name=self.SOURCE_NAME,
                    ticker=ticker.value,
                    yahoo_symbol=ticker.yahoo_symbol,
                    segment_name=module.segment_name,
                    payload=_dataframe_to_raw_payload(frame),
                    fetched_at_utc=fetched_at_utc,
                )
            )

            statement_snapshots.extend(
                self._build_statement_snapshots(
                    ticker=ticker,
                    fetched_at_utc=fetched_at_utc,
                    frame=frame,
                    module=module,
                )
            )

        daily_price_bars = self._fetch_price_history(
            yahoo_ticker, latest_price_date, fetched_at_utc, failures
        )

        corporate_actions = self._fetch_actions(
            yahoo_ticker, latest_action_date, fetched_at_utc, failures
        )

        return ProviderFetchResult(
            source_name=self.SOURCE_NAME,
            yahoo_symbol=ticker.yahoo_symbol,
            company_profile=company_profile,
            statement_snapshots=statement_snapshots,
            daily_price_bars=daily_price_bars,
            corporate_actions=corporate_actions,
            raw_payloads=raw_payloads,
            segment_failures=failures,
        )

    def _fetch_actions(
        self,
        yahoo_ticker: Any,
        latest_action_date: date | None,
        fetched_at_utc: datetime,
        failures: list[SegmentFailure],
    ) -> list[CorporateAction]:
        try:
            # Fetch the complete, sparse event series to detect Yahoo corrections
            # without refreshing historical OHLCV rows.
            frame = _run_yfinance_without_pandas4_warning(
                lambda: yahoo_ticker.get_actions(period="max")
            )
            if not isinstance(frame, pd.DataFrame):
                failures.append(SegmentFailure(ACTION_SEGMENT, "Invalid actions response."))
                return []
            actions: list[CorporateAction] = []
            for index, row in frame.iterrows():
                try:
                    session = pd.Timestamp(index)
                    if pd.isna(session):
                        raise ValueError("Invalid action date")
                    if session.tzinfo is not None:
                        session = session.tz_convert(ASX_TIMEZONE)
                    effective_date = session.date()
                    if effective_date > fetched_at_utc.astimezone(ASX_TIMEZONE).date():
                        continue
                    for column, action_type in (
                        ("Dividends", "dividend"),
                        ("Stock Splits", "split"),
                    ):
                        value = row.get(column)
                        if value is None or pd.isna(value):
                            continue
                        amount = Decimal(str(value))
                        if not amount.is_finite() or amount < 0:
                            raise ValueError("Invalid corporate action value")
                        if amount > 0:
                            actions.append(
                                CorporateAction(
                                    action_type=action_type,
                                    effective_date=effective_date,
                                    value=amount,
                                    source=self.SOURCE_NAME,
                                    fetched_at_utc=fetched_at_utc,
                                )
                            )
                except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
                    failures.append(
                        SegmentFailure(ACTION_SEGMENT, f"Skipped malformed action: {exc}")
                    )
            return actions
        except Exception as exc:
            failures.append(SegmentFailure(ACTION_SEGMENT, str(exc)))
            return []

    def _fetch_price_history(
        self,
        yahoo_ticker: Any,
        latest_price_date: date | None,
        fetched_at_utc: datetime,
        failures: list[SegmentFailure],
    ) -> list[DailyPriceBar]:
        today = fetched_at_utc.astimezone(ASX_TIMEZONE).date()
        if latest_price_date is not None and latest_price_date > today:
            failures.append(SegmentFailure(PRICE_HISTORY_SEGMENT, "Stored date is in the future."))
            return []

        options: dict[str, Any] = {"interval": "1d", "auto_adjust": False}
        if latest_price_date is None:
            options["period"] = "max"
        else:
            options["start"] = latest_price_date.isoformat()
            options["end"] = (today + timedelta(days=1)).isoformat()

        try:
            frame = _run_yfinance_without_pandas4_warning(lambda: yahoo_ticker.history(**options))
            if not isinstance(frame, pd.DataFrame):
                failures.append(SegmentFailure(PRICE_HISTORY_SEGMENT, "Invalid history response."))
                return []
            if frame.empty:
                if latest_price_date is None:
                    failures.append(
                        SegmentFailure(PRICE_HISTORY_SEGMENT, "Price history was empty.")
                    )
                return []

            bars: list[DailyPriceBar] = []
            for index, row in frame.iterrows():
                try:
                    session = pd.Timestamp(index)
                    if pd.isna(session):
                        raise ValueError("Invalid trading date")
                    if session.tzinfo is not None:
                        session = session.tz_convert(ASX_TIMEZONE)
                    trading_date = session.date()
                    if trading_date > today or (
                        latest_price_date is not None and trading_date < latest_price_date
                    ):
                        continue
                    prices = [
                        self._price_number(row.get(key)) for key in ("Open", "High", "Low", "Close")
                    ]
                    if any(value is None for value in prices):
                        raise ValueError("Missing OHLC price")
                    volume_value = row.get("Volume")
                    if pd.isna(volume_value):
                        raise ValueError("Missing volume")
                    volume = int(volume_value)
                    if volume < 0 or volume != volume_value:
                        raise ValueError("Invalid volume")
                    bars.append(
                        DailyPriceBar(
                            trading_date=trading_date,
                            open=prices[0],
                            high=prices[1],
                            low=prices[2],
                            close=prices[3],
                            adjusted_close=self._price_number(row.get("Adj Close")),
                            volume=volume,
                            source=self.SOURCE_NAME,
                            fetched_at_utc=fetched_at_utc,
                        )
                    )
                except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
                    failures.append(
                        SegmentFailure(PRICE_HISTORY_SEGMENT, f"Skipped malformed price row: {exc}")
                    )
            if not bars and latest_price_date is None and not failures:
                failures.append(SegmentFailure(PRICE_HISTORY_SEGMENT, "No valid price bars."))
            return bars
        except Exception as exc:
            failures.append(SegmentFailure(PRICE_HISTORY_SEGMENT, str(exc)))
            return []

    @staticmethod
    def _price_number(value: Any) -> Decimal | None:
        if value is None or pd.isna(value):
            return None
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError("Invalid price")
        return number

    def _safe_info(self, yahoo_ticker: Any) -> dict[str, Any]:
        info = _run_yfinance_without_pandas4_warning(yahoo_ticker.get_info)
        if not isinstance(info, dict):
            return {}
        return {str(key): _normalize_scalar(value) for key, value in info.items()}

    def _build_company_profile(
        self,
        *,
        ticker: AsxTicker,
        fetched_at_utc: datetime,
        info: dict[str, Any],
        raw_payloads: list[RawSourcePayload],
        failures: list[SegmentFailure],
    ) -> CompanyProfileSnapshot | None:
        if not info:
            failures.append(SegmentFailure("company-profile", "Company profile data was empty."))
            return None

        raw_payloads.append(
            RawSourcePayload(
                source_name=self.SOURCE_NAME,
                ticker=ticker.value,
                yahoo_symbol=ticker.yahoo_symbol,
                segment_name="company-profile",
                payload=info,
                fetched_at_utc=fetched_at_utc,
            )
        )

        return CompanyProfileSnapshot(
            source_name=self.SOURCE_NAME,
            ticker=ticker.value,
            yahoo_symbol=ticker.yahoo_symbol,
            company_name=info.get("longName") or info.get("shortName"),
            exchange_name=(
                info.get("fullExchangeName") or info.get("exchange") or info.get("exchangeName")
            ),
            currency=info.get("financialCurrency") or info.get("currency"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            country=info.get("country"),
            website=info.get("website"),
            profile_payload=info,
            fetched_at_utc=fetched_at_utc,
        )

    def _fetch_statement_frame(
        self,
        yahoo_ticker: Any,
        module: StatementModuleDefinition,
    ) -> pd.DataFrame | None:
        accessor = getattr(yahoo_ticker, module.accessor_name)
        frequency = "yearly" if module.frequency is StatementFrequency.ANNUAL else "quarterly"
        frame = _run_yfinance_without_pandas4_warning(
            lambda: accessor(pretty=False, freq=frequency)
        )

        if frame is None:
            return None
        if isinstance(frame, pd.DataFrame):
            return frame
        return pd.DataFrame(frame)

    def _build_statement_snapshots(
        self,
        *,
        ticker: AsxTicker,
        fetched_at_utc: datetime,
        frame: pd.DataFrame,
        module: StatementModuleDefinition,
    ) -> list[FinancialStatementSnapshot]:
        snapshots: list[FinancialStatementSnapshot] = []

        for column in frame.columns:
            period_end = _column_to_period_end(column)
            values = {
                str(index): _normalize_scalar(value)
                for index, value in frame[column].items()
                if _normalize_scalar(value) is not None
            }

            if not values:
                continue

            payload = {
                "periodEnd": period_end.isoformat() if period_end is not None else None,
                "values": values,
            }

            source_period_key = (
                period_end.isoformat() if period_end is not None else _payload_hash(payload)
            )

            snapshots.append(
                FinancialStatementSnapshot(
                    source_name=self.SOURCE_NAME,
                    ticker=ticker.value,
                    yahoo_symbol=ticker.yahoo_symbol,
                    statement_type=module.statement_type,
                    frequency=module.frequency,
                    source_period_key=source_period_key,
                    source_period_end=period_end,
                    payload=payload,
                    fetched_at_utc=fetched_at_utc,
                )
            )

        return snapshots
