import json
import re
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, and_, create_engine, func, select, update
from sqlalchemy.dialects.postgresql import insert

from asx_financials.domain.enums import IngestionRunStatus, StatementFrequency, StatementType
from asx_financials.domain.models import (
    AsxListedCompany,
    CompanyDetails,
    CompanyProfileSnapshot,
    CorporateAction,
    DailyPriceBar,
    FinancialStatementSnapshot,
    LatestIngestionRun,
    PersistedIngestionOutcome,
    RawSourcePayload,
    SegmentFailure,
    StatementAvailability,
    StatementPreview,
    TickerUniverseSyncResult,
)
from asx_financials.domain.value_objects import AsxTicker
from asx_financials.infrastructure.persistence.schema import (
    corporate_actions as corporate_actions_table,
)
from asx_financials.infrastructure.persistence.schema import (
    daily_prices,
    financial_facts,
    ingestion_runs,
    raw_source_payloads,
    statement_snapshots,
    ticker_universe_sync_runs,
    tickers,
)

ASX_EXCHANGE_CODE = "ASX"


def create_postgres_data_store(database_url: str) -> "PostgresFinancialDataStore":
    return PostgresFinancialDataStore(create_engine(database_url, pool_pre_ping=True))


class PostgresFinancialDataStore:
    """Synchronous adapter. Migrations must be explicitly applied before use."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def sync_ticker_universe(
        self,
        *,
        companies: list[AsxListedCompany],
        source_url: str,
        fetched_at_utc: datetime,
        invalid_count: int = 0,
    ) -> TickerUniverseSyncResult:
        sync_run_id = str(uuid4())
        fetched_at = _to_utc_datetime(fetched_at_utc)
        inserted = updated = reactivated = unchanged = 0
        seen = {company.ticker for company in companies}
        with self._engine.begin() as connection:
            # Serialize entire universe sync, including deactivation, across processes.
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(73495621)")
            connection.execute(
                ticker_universe_sync_runs.insert().values(
                    sync_run_id=sync_run_id,
                    exchange_code=ASX_EXCHANGE_CODE,
                    source_url=source_url,
                    started_at=_utcnow(),
                    status="running",
                )
            )
            for company in companies:
                existing = (
                    connection.execute(
                        select(tickers)
                        .where(
                            tickers.c.exchange_code == ASX_EXCHANGE_CODE,
                            tickers.c.ticker == company.ticker,
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if existing is None:
                    inserted += 1
                elif existing["status"] == "inactive" or not existing["listed_in_universe"]:
                    reactivated += 1
                elif _ticker_csv_fields_changed(
                    {
                        "companyName": existing["company_name"],
                        "industryGroup": existing["industry_group"],
                        "exchangeName": existing["exchange_name"],
                        "universeSourceUrl": existing["universe_source_url"],
                    },
                    company,
                    source_url,
                ):
                    updated += 1
                else:
                    unchanged += 1
                values = dict(
                    yahoo_symbol=AsxTicker(company.ticker).yahoo_symbol,
                    source_name="asx_listed_companies_csv",
                    company_name=company.company_name,
                    exchange_name="ASX",
                    industry_group=company.industry_group,
                    status="active",
                    listed_in_universe=True,
                    last_seen_in_universe=fetched_at,
                    last_universe_sync_at=fetched_at,
                    universe_source_url=source_url,
                    delisted_detected_at=None,
                    updated_at=fetched_at,
                )
                statement = insert(tickers).values(
                    exchange_code=ASX_EXCHANGE_CODE,
                    ticker=company.ticker,
                    created_at=_utcnow(),
                    first_seen_in_universe=fetched_at,
                    **values,
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[tickers.c.exchange_code, tickers.c.ticker], set_=values
                    )
                )
            condition = and_(
                tickers.c.exchange_code == ASX_EXCHANGE_CODE,
                tickers.c.first_seen_in_universe.is_not(None),
                tickers.c.listed_in_universe.is_(True),
            )
            if seen:
                condition = and_(condition, tickers.c.ticker.not_in(seen))
            deactivated = connection.execute(
                update(tickers)
                .where(condition)
                .values(
                    status="inactive",
                    listed_in_universe=False,
                    delisted_detected_at=fetched_at,
                    last_universe_sync_at=fetched_at,
                    updated_at=fetched_at,
                )
            ).rowcount
            result = TickerUniverseSyncResult(
                sync_run_id=sync_run_id,
                source_url=source_url,
                fetched_at_utc=fetched_at_utc,
                seen_count=len(companies),
                inserted_count=inserted,
                updated_count=updated,
                reactivated_count=reactivated,
                deactivated_count=deactivated,
                invalid_count=invalid_count,
                unchanged_count=unchanged,
            )
            connection.execute(
                update(ticker_universe_sync_runs)
                .where(ticker_universe_sync_runs.c.sync_run_id == sync_run_id)
                .values(
                    status="succeeded",
                    completed_at=_utcnow(),
                    seen_count=len(companies),
                    inserted_count=inserted,
                    updated_count=updated,
                    reactivated_count=reactivated,
                    deactivated_count=deactivated,
                    invalid_count=invalid_count,
                    unchanged_count=unchanged,
                )
            )
        return result

    def start_ingestion_run(
        self,
        ingestion_run_id: str,
        ticker: AsxTicker,
        yahoo_symbol: str,
        started_at_utc: datetime,
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                ingestion_runs.insert().values(
                    run_id=ingestion_run_id,
                    exchange_code=ASX_EXCHANGE_CODE,
                    run_type="ticker_fundamentals",
                    ticker=ticker.value,
                    yahoo_symbol=yahoo_symbol,
                    source_name="yfinance",
                    status=IngestionRunStatus.RUNNING.value,
                    started_at=_to_utc_datetime(started_at_utc),
                    message="Ingestion started.",
                    segment_failures=[],
                )
            )

    def complete_ingestion_run(
        self,
        *,
        ingestion_run_id: str,
        ticker: AsxTicker,
        yahoo_symbol: str,
        status: str,
        completed_at_utc: datetime,
        message: str,
        company_profile: CompanyProfileSnapshot | None,
        statement_snapshots: list[FinancialStatementSnapshot],
        raw_payloads: list[RawSourcePayload],
        segment_failures: list[SegmentFailure],
        daily_price_bars: Sequence[DailyPriceBar] = (),
        corporate_actions: Sequence[CorporateAction] = (),
    ) -> PersistedIngestionOutcome:
        with self._engine.begin() as connection:
            run = connection.execute(
                select(ingestion_runs.c.run_id)
                .where(ingestion_runs.c.run_id == ingestion_run_id)
                .with_for_update()
            ).first()
            if run is None:
                raise RuntimeError(f"Ingestion run {ingestion_run_id} was not found.")
            if company_profile is not None:
                values = dict(
                    yahoo_symbol=company_profile.yahoo_symbol,
                    source_name=company_profile.source_name,
                    company_name=company_profile.company_name,
                    exchange_name=company_profile.exchange_name,
                    currency=company_profile.currency,
                    sector=company_profile.sector,
                    industry=company_profile.industry,
                    country=company_profile.country,
                    website=company_profile.website,
                    profile_payload=_json_safe(company_profile.profile_payload),
                    last_fundamental_at=_to_utc_datetime(company_profile.fetched_at_utc),
                    updated_at=_to_utc_datetime(company_profile.fetched_at_utc),
                )
                statement = insert(tickers).values(
                    exchange_code=ASX_EXCHANGE_CODE,
                    ticker=company_profile.ticker,
                    status="active",
                    created_at=_utcnow(),
                    **values,
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[tickers.c.exchange_code, tickers.c.ticker], set_=values
                    )
                )
            raw_payload_ids: dict[tuple[str, str], str] = {}
            for payload in raw_payloads:
                payload_id = str(uuid4())
                connection.execute(
                    raw_source_payloads.insert().values(
                        raw_payload_id=payload_id,
                        exchange_code=ASX_EXCHANGE_CODE,
                        run_id=ingestion_run_id,
                        ticker=payload.ticker,
                        yahoo_symbol=payload.yahoo_symbol,
                        source_name=payload.source_name,
                        segment_name=payload.segment_name,
                        payload_hash=_hash_payload(payload.payload),
                        payload=_json_safe(payload.payload),
                        fetched_at=_to_utc_datetime(payload.fetched_at_utc),
                        created_at=_utcnow(),
                    )
                )
                raw_payload_ids[(payload.source_name, payload.segment_name)] = payload_id
            inserted_count = fact_count = 0
            for snapshot in statement_snapshots:
                inserted, facts = self._insert_statement_snapshot(
                    connection,
                    ingestion_run_id,
                    snapshot,
                    raw_payload_ids.get((snapshot.source_name, _segment_name(snapshot))),
                )
                inserted_count += int(inserted)
                fact_count += facts
            if (daily_price_bars or corporate_actions) and company_profile is None:
                listing = insert(tickers).values(
                    exchange_code=ASX_EXCHANGE_CODE,
                    ticker=ticker.value,
                    yahoo_symbol=yahoo_symbol,
                    source_name=(
                        daily_price_bars[0].source
                        if daily_price_bars
                        else corporate_actions[0].source
                    ),
                    status="active",
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                )
                connection.execute(
                    listing.on_conflict_do_nothing(
                        index_elements=[tickers.c.exchange_code, tickers.c.ticker]
                    )
                )
            for bar in daily_price_bars:
                fetched_at = _to_utc_datetime(bar.fetched_at_utc)
                values = dict(
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    adjusted_close=bar.adjusted_close,
                    volume=bar.volume,
                    source=bar.source,
                    fetched_at=fetched_at,
                    updated_at=_utcnow(),
                )
                statement = insert(daily_prices).values(
                    exchange_code=ASX_EXCHANGE_CODE,
                    ticker=ticker.value,
                    trading_date=bar.trading_date,
                    created_at=_utcnow(),
                    **values,
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            daily_prices.c.exchange_code,
                            daily_prices.c.ticker,
                            daily_prices.c.trading_date,
                        ],
                        set_=values,
                        where=daily_prices.c.fetched_at <= statement.excluded.fetched_at,
                    )
                )
            for action in corporate_actions:
                fetched_at = _to_utc_datetime(action.fetched_at_utc)
                values = dict(
                    value=action.value,
                    fetched_at=fetched_at,
                    updated_at=_utcnow(),
                )
                statement = insert(corporate_actions_table).values(
                    exchange_code=ASX_EXCHANGE_CODE,
                    ticker=ticker.value,
                    action_type=action.action_type,
                    effective_date=action.effective_date,
                    source=action.source,
                    created_at=_utcnow(),
                    **values,
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            corporate_actions_table.c.exchange_code,
                            corporate_actions_table.c.ticker,
                            corporate_actions_table.c.action_type,
                            corporate_actions_table.c.effective_date,
                            corporate_actions_table.c.source,
                        ],
                        set_=values,
                        where=corporate_actions_table.c.fetched_at <= statement.excluded.fetched_at,
                    )
                )
            connection.execute(
                update(ingestion_runs)
                .where(ingestion_runs.c.run_id == ingestion_run_id)
                .values(
                    ticker=ticker.value,
                    yahoo_symbol=yahoo_symbol,
                    status=status,
                    completed_at=_to_utc_datetime(completed_at_utc),
                    message=message,
                    segment_failures=[asdict(failure) for failure in segment_failures],
                    stored_snapshot_count=inserted_count,
                    stored_raw_payload_count=len(raw_payloads),
                    stored_fact_count=fact_count,
                )
            )
        return PersistedIngestionOutcome(inserted_count, len(raw_payloads), len(daily_price_bars))

    def _insert_statement_snapshot(
        self,
        connection: Any,
        run_id: str,
        snapshot: FinancialStatementSnapshot,
        raw_payload_id: str | None,
    ) -> tuple[bool, int]:
        # The advisory lock protects both the revision check and the current-row transition.
        # Hashtext collisions only over-serialize unrelated keys; they cannot affect correctness.
        key = (
            ASX_EXCHANGE_CODE,
            snapshot.ticker,
            snapshot.source_name,
            snapshot.statement_type.value,
            snapshot.frequency.value,
            str(snapshot.source_period_end),
        )
        connection.execute(select(func.pg_advisory_xact_lock(func.hashtext("|".join(key)))))
        base = and_(
            statement_snapshots.c.exchange_code == ASX_EXCHANGE_CODE,
            statement_snapshots.c.ticker == snapshot.ticker,
            statement_snapshots.c.source_name == snapshot.source_name,
            statement_snapshots.c.statement_type == snapshot.statement_type.value,
            statement_snapshots.c.frequency == snapshot.frequency.value,
            statement_snapshots.c.period_end == snapshot.source_period_end
            if snapshot.source_period_end is not None
            else statement_snapshots.c.period_end.is_(None),
        )
        revision = _hash_payload(snapshot.payload)
        duplicate = connection.execute(
            select(statement_snapshots.c.snapshot_id).where(
                base, statement_snapshots.c.revision_hash == revision
            )
        ).first()
        if duplicate:
            return False, 0
        connection.execute(update(statement_snapshots).where(base).values(is_current=False))
        fact_base = and_(
            financial_facts.c.exchange_code == ASX_EXCHANGE_CODE,
            financial_facts.c.ticker == snapshot.ticker,
            financial_facts.c.source_name == snapshot.source_name,
            financial_facts.c.statement_type == snapshot.statement_type.value,
            financial_facts.c.frequency == snapshot.frequency.value,
            financial_facts.c.period_end == snapshot.source_period_end
            if snapshot.source_period_end is not None
            else financial_facts.c.period_end.is_(None),
        )
        connection.execute(update(financial_facts).where(fact_base).values(is_current=False))
        snapshot_id = str(uuid4())
        document = dict(
            ticker=snapshot.ticker,
            sourceName=snapshot.source_name,
            statementType=snapshot.statement_type.value,
            frequency=snapshot.frequency.value,
            periodEnd=_to_optional_date_key(snapshot.source_period_end),
            payload=snapshot.payload,
            fetchedAt=_to_utc_datetime(snapshot.fetched_at_utc),
        )
        connection.execute(
            statement_snapshots.insert().values(
                snapshot_id=snapshot_id,
                exchange_code=ASX_EXCHANGE_CODE,
                run_id=run_id,
                ticker=snapshot.ticker,
                source_name=snapshot.source_name,
                statement_type=snapshot.statement_type.value,
                frequency=snapshot.frequency.value,
                period_end=snapshot.source_period_end,
                yahoo_symbol=snapshot.yahoo_symbol,
                source_period_key=snapshot.source_period_key,
                fiscal_year=snapshot.source_period_end.year if snapshot.source_period_end else None,
                fiscal_quarter=_fiscal_quarter(snapshot.source_period_end),
                revision_hash=revision,
                is_current=True,
                payload=_json_safe(snapshot.payload),
                raw_payload_id=raw_payload_id,
                fetched_at=_to_utc_datetime(snapshot.fetched_at_utc),
                created_at=_utcnow(),
            )
        )
        documents = _build_fact_documents(snapshot_id, document)
        for item in documents:
            connection.execute(
                financial_facts.insert().values(
                    fact_id=item["factId"],
                    exchange_code=ASX_EXCHANGE_CODE,
                    ticker=item["ticker"],
                    source_name=item["sourceName"],
                    statement_snapshot_id=snapshot_id,
                    statement_type=item["statementType"],
                    frequency=item["frequency"],
                    period_end=snapshot.source_period_end,
                    metric_key=item["metricKey"],
                    metric_label=item["metricLabel"],
                    value=Decimal(str(item["value"])),
                    currency=item["currency"],
                    unit=item["unit"],
                    is_current=True,
                    fetched_at=item["fetchedAt"],
                    created_at=item["createdAt"],
                )
            )
        return True, len(documents)

    def mark_ingestion_run_failed(
        self,
        ingestion_run_id: str,
        completed_at_utc: datetime,
        message: str,
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(ingestion_runs)
                .where(ingestion_runs.c.run_id == ingestion_run_id)
                .values(
                    status=IngestionRunStatus.FAILED.value,
                    completed_at=_to_utc_datetime(completed_at_utc),
                    message=message,
                )
            )

    def get_latest_price_date(
        self, ticker: AsxTicker, exchange_code: str = ASX_EXCHANGE_CODE
    ) -> date | None:
        with self._engine.connect() as connection:
            return connection.execute(
                select(func.max(daily_prices.c.trading_date)).where(
                    daily_prices.c.exchange_code == exchange_code,
                    daily_prices.c.ticker == ticker.value,
                )
            ).scalar_one()

    def get_latest_action_date(
        self, ticker: AsxTicker, exchange_code: str = ASX_EXCHANGE_CODE
    ) -> date | None:
        with self._engine.connect() as connection:
            return connection.execute(
                select(func.max(corporate_actions_table.c.effective_date)).where(
                    corporate_actions_table.c.exchange_code == exchange_code,
                    corporate_actions_table.c.ticker == ticker.value,
                )
            ).scalar_one()

    def get_corporate_actions(
        self, ticker: AsxTicker, exchange_code: str = ASX_EXCHANGE_CODE
    ) -> list[CorporateAction]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(corporate_actions_table)
                    .where(
                        corporate_actions_table.c.exchange_code == exchange_code,
                        corporate_actions_table.c.ticker == ticker.value,
                    )
                    .order_by(
                        corporate_actions_table.c.effective_date,
                        corporate_actions_table.c.action_type,
                        corporate_actions_table.c.source,
                    )
                )
                .mappings()
                .all()
            )
        return [
            CorporateAction(
                action_type=row["action_type"],
                effective_date=row["effective_date"],
                value=row["value"],
                source=row["source"],
                fetched_at_utc=_from_datetime(row["fetched_at"]),
            )
            for row in rows
        ]

    def get_latest_ingestion(self, ticker: AsxTicker) -> LatestIngestionRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(ingestion_runs)
                    .where(
                        ingestion_runs.c.exchange_code == ASX_EXCHANGE_CODE,
                        ingestion_runs.c.ticker == ticker.value,
                    )
                    .order_by(ingestion_runs.c.started_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return LatestIngestionRun(
            ingestion_run_id=row["run_id"],
            ticker=row["ticker"],
            yahoo_symbol=row["yahoo_symbol"],
            status=IngestionRunStatus(row["status"]),
            started_at_utc=_from_datetime(row["started_at"]),
            completed_at_utc=_from_optional_datetime(row["completed_at"]),
            message=row["message"],
            stored_snapshot_count=row["stored_snapshot_count"],
            stored_raw_payload_count=row["stored_raw_payload_count"],
            segment_failures=[SegmentFailure(**item) for item in row["segment_failures"]],
        )

    def get_company(self, ticker: AsxTicker) -> CompanyDetails | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(tickers).where(
                        tickers.c.exchange_code == ASX_EXCHANGE_CODE,
                        tickers.c.ticker == ticker.value,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            grouped = (
                connection.execute(
                    select(
                        statement_snapshots.c.statement_type,
                        statement_snapshots.c.frequency,
                        func.count().label("count"),
                        func.max(statement_snapshots.c.period_end).label("latest"),
                    )
                    .where(
                        statement_snapshots.c.exchange_code == ASX_EXCHANGE_CODE,
                        statement_snapshots.c.ticker == ticker.value,
                        statement_snapshots.c.is_current.is_(True),
                    )
                    .group_by(statement_snapshots.c.statement_type, statement_snapshots.c.frequency)
                    .order_by(statement_snapshots.c.statement_type, statement_snapshots.c.frequency)
                )
                .mappings()
                .all()
            )
        return CompanyDetails(
            ticker=row["ticker"],
            yahoo_symbol=row["yahoo_symbol"],
            source_name=row["source_name"],
            company_name=row["company_name"],
            exchange_name=row["exchange_name"],
            currency=row["currency"],
            sector=row["sector"],
            industry=row["industry"],
            country=row["country"],
            website=row["website"],
            profile_payload=row["profile_payload"],
            updated_at_utc=_from_datetime(row["updated_at"]),
            statement_availability=[
                StatementAvailability(
                    statement_type=StatementType(item["statement_type"]),
                    frequency=StatementFrequency(item["frequency"]),
                    snapshot_count=item["count"],
                    latest_source_period_end=item["latest"],
                )
                for item in grouped
            ],
        )

    def get_latest_statement_previews(self, ticker: AsxTicker) -> list[StatementPreview]:
        ranking = (
            func.row_number()
            .over(
                partition_by=(
                    statement_snapshots.c.statement_type,
                    statement_snapshots.c.frequency,
                ),
                order_by=(
                    statement_snapshots.c.period_end.desc().nulls_last(),
                    statement_snapshots.c.fetched_at.desc(),
                ),
            )
            .label("ranking")
        )
        ranked = (
            select(
                statement_snapshots.c.statement_type,
                statement_snapshots.c.frequency,
                statement_snapshots.c.period_end,
                statement_snapshots.c.payload,
                statement_snapshots.c.fetched_at,
                ranking,
            )
            .where(
                statement_snapshots.c.exchange_code == ASX_EXCHANGE_CODE,
                statement_snapshots.c.ticker == ticker.value,
                statement_snapshots.c.is_current.is_(True),
            )
            .subquery()
        )
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(ranked)
                    .where(ranked.c.ranking == 1)
                    .order_by(ranked.c.statement_type, ranked.c.frequency)
                )
                .mappings()
                .all()
            )
        return [
            StatementPreview(
                statement_type=StatementType(row["statement_type"]),
                frequency=StatementFrequency(row["frequency"]),
                source_period_end=row["period_end"],
                values=row["payload"].get("values", {}),
                fetched_at_utc=_from_datetime(row["fetched_at"]),
            )
            for row in rows
        ]


def _json_safe(payload: Any) -> Any:
    """Make provider values JSON-serializable without changing the revision hash."""
    return json.loads(json.dumps(payload, default=str))


def _build_fact_documents(
    snapshot_id: str,
    snapshot_document: dict[str, Any],
) -> list[dict[str, Any]]:
    values = snapshot_document.get("payload", {}).get("values", {})
    if not isinstance(values, dict):
        return []

    documents = []
    for metric_label, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        documents.append(
            {
                "factId": str(uuid4()),
                "ticker": snapshot_document["ticker"],
                "sourceName": snapshot_document["sourceName"],
                "statementSnapshotId": snapshot_id,
                "statementType": snapshot_document["statementType"],
                "frequency": snapshot_document["frequency"],
                "periodEnd": snapshot_document["periodEnd"],
                "metricKey": _metric_key(metric_label),
                "metricLabel": metric_label,
                "value": value,
                "currency": None,
                "unit": None,
                "isCurrent": True,
                "fetchedAt": snapshot_document["fetchedAt"],
                "createdAt": _utcnow(),
            }
        )
    return documents


def _ticker_csv_fields_changed(
    existing: dict[str, Any],
    company: AsxListedCompany,
    source_url: str,
) -> bool:
    return (
        existing.get("companyName") != company.company_name
        or existing.get("industryGroup") != company.industry_group
        or existing.get("exchangeName") != "ASX"
        or existing.get("universeSourceUrl") != source_url
    )


def _hash_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _metric_key(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value)
    return value.strip("_").lower()


def _segment_name(snapshot: FinancialStatementSnapshot) -> str:
    statement_type = snapshot.statement_type.value.replace("_", "-")
    return f"{statement_type}-{snapshot.frequency.value}"


def _fiscal_quarter(value: date | None) -> int | None:
    if value is None:
        return None
    return ((value.month - 1) // 3) + 1


def _to_optional_date_key(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _from_optional_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _to_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _from_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _from_optional_datetime(value: datetime | None) -> datetime | None:
    return _from_datetime(value) if value is not None else None


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)
