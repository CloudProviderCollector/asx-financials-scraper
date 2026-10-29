"""Dashboard tests run without starting a browser."""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from asx_financials.dashboard import (
    _daily_prices_view,
    _financial_highlights,
    _format_number,
    _is_monetary_fact,
    _statement_summary,
    load_ticker_data,
    main,
)
from asx_financials.domain.value_objects import AsxTicker


def test_amounts_have_dollar_sign_and_grouped_digits_without_changing_other_values():
    assert _format_number(Decimal("1234567.89"), money=True) == "$1,234,567.89"
    assert _format_number(-1234567, money=True) == "$-1,234,567"
    assert _format_number(1234567) == "1,234,567"
    assert _format_number(25.14638) == "25.14638"
    assert _format_number(None, money=True) == "N/A"
    assert _format_number(True, money=True) == "True"
    assert _is_monetary_fact("TotalAssets")
    assert _is_monetary_fact("diluted_eps")
    assert not _is_monetary_fact("ordinary_shares_number")
    assert not _is_monetary_fact("TaxRateForCalcs")


def test_overview_uses_only_latest_current_statement_facts():
    facts = [
        {
            "statement_type": "income_statement",
            "frequency": "annual",
            "period_end": date(2024, 6, 30),
            "metric_key": "net_income",
            "metric_label": "Net Income",
            "value": 10,
            "currency": "AUD",
            "is_current": True,
        },
        {
            "statement_type": "income_statement",
            "frequency": "annual",
            "period_end": date(2025, 6, 30),
            "metric_key": "net_income",
            "metric_label": "Net Income",
            "value": 20,
            "currency": "AUD",
            "is_current": True,
        },
        {
            "statement_type": "income_statement",
            "frequency": "annual",
            "period_end": date(2026, 6, 30),
            "metric_key": "net_income",
            "metric_label": "Net Income",
            "value": 30,
            "currency": "AUD",
            "is_current": False,
        },
    ]
    highlights = _financial_highlights(facts)
    assert len(highlights) == 1
    assert highlights[0]["value"] == "$20"
    assert highlights[0]["period"] == date(2025, 6, 30)


def test_statement_coverage_deduplicates_revisions():
    snapshots = [
        {
            "statement_type": "cash_flow",
            "frequency": "annual",
            "period_end": date(2025, 6, 30),
            "is_current": True,
        },
        {
            "statement_type": "cash_flow",
            "frequency": "annual",
            "period_end": date(2025, 6, 30),
            "is_current": False,
        },
        {
            "statement_type": "cash_flow",
            "frequency": "annual",
            "period_end": date(2024, 6, 30),
            "is_current": True,
        },
    ]
    assert _statement_summary(snapshots) == [
        {
            "statement": "cash_flow",
            "frequency": "annual",
            "periods": 2,
            "latest period": date(2025, 6, 30),
        }
    ]


def test_daily_prices_show_chronological_charts_and_formatted_newest_first_table():
    prices = [
        {
            "trading_date": date(2026, 10, 28),
            "open": Decimal("1234.50"),
            "high": Decimal("1250.00"),
            "low": Decimal("1200.00"),
            "close": Decimal("1240.25"),
            "adjusted_close": None,
            "volume": 1234567,
            "source": "yahoo_finance",
            "fetched_at": datetime(2026, 10, 28, tzinfo=UTC),
        },
        {
            "trading_date": date(2026, 10, 27),
            "open": Decimal("1100.00"),
            "high": Decimal("1200.00"),
            "low": Decimal("1090.00"),
            "close": Decimal("1180.75"),
            "adjusted_close": Decimal("1170.50"),
            "volume": 900000,
            "source": "yahoo_finance",
            "fetched_at": datetime(2026, 10, 27, tzinfo=UTC),
        },
    ]
    with (
        patch("asx_financials.dashboard.st.subheader"),
        patch("asx_financials.dashboard.st.line_chart") as line_chart,
        patch("asx_financials.dashboard.st.bar_chart") as bar_chart,
        patch("asx_financials.dashboard.st.dataframe") as dataframe,
    ):
        _daily_prices_view(
            prices,
            [
                {
                    "action_type": "dividend",
                    "effective_date": date(2026, 10, 28),
                    "value": Decimal("10"),
                    "source": "yfinance",
                    "fetched_at": datetime(2026, 10, 28, tzinfo=UTC),
                }
            ],
        )

    close = line_chart.call_args.args[0]
    assert list(close.index) == [date(2026, 10, 27), date(2026, 10, 28)]
    assert list(close["Close"]) == [1180.75, 1240.25]
    assert list(close["Derived adjusted close"].dropna()) == [1170.75, 1240.25]
    volume = bar_chart.call_args.args[0]
    assert list(volume["Volume"]) == [900000, 1234567]
    rows = dataframe.call_args.args[0]
    assert rows.iloc[0]["open"] == "$1,234.50"
    assert rows.iloc[0]["adjusted_close"] == "N/A"
    assert rows.iloc[0]["volume"] == "1,234,567"
    assert rows.iloc[1]["adjusted_close"] == "$1,170.50"


def test_daily_prices_empty_state_does_not_render_charts_or_table():
    with (
        patch("asx_financials.dashboard.st.info") as info,
        patch("asx_financials.dashboard.st.line_chart") as line_chart,
        patch("asx_financials.dashboard.st.bar_chart") as bar_chart,
        patch("asx_financials.dashboard.st.dataframe") as dataframe,
    ):
        _daily_prices_view([])
    info.assert_called_once()
    line_chart.assert_not_called()
    bar_chart.assert_not_called()
    dataframe.assert_not_called()


def test_daily_history_query_is_scoped_to_asx_and_newest_first():
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    listing = {"exchange_code": "ASX", "ticker": "CBA"}
    connection.execute.return_value.mappings.return_value.first.return_value = listing
    connection.execute.return_value.mappings.return_value.__iter__.return_value = iter(())
    result = load_ticker_data(engine, AsxTicker.parse("CBA"))
    assert result is not None
    assert result["daily_prices"] == []
    query = connection.execute.call_args_list[1].args[0]
    sql = str(query.compile(compile_kwargs={"literal_binds": True}))
    assert "FROM daily_prices" in sql
    assert "daily_prices.exchange_code = 'ASX'" in sql
    assert "daily_prices.ticker = 'CBA'" in sql
    assert "ORDER BY daily_prices.trading_date DESC" in sql


def test_dashboard_shows_empty_ticker_prompt():
    with (
        patch("asx_financials.dashboard.st.set_page_config"),
        patch("asx_financials.dashboard.st.title"),
        patch("asx_financials.dashboard.st.caption"),
        patch("asx_financials.dashboard.st.text_input", return_value=""),
        patch("asx_financials.dashboard.st.info") as info,
    ):
        main()
    info.assert_called_once()


def test_dashboard_rejects_invalid_ticker_before_database_access():
    with (
        patch("asx_financials.dashboard.st.set_page_config"),
        patch("asx_financials.dashboard.st.title"),
        patch("asx_financials.dashboard.st.caption"),
        patch("asx_financials.dashboard.st.text_input", return_value="INVALID-TICKER"),
        patch("asx_financials.dashboard.st.error") as error,
        patch("asx_financials.dashboard._engine") as engine,
    ):
        main()
    error.assert_called_once()
    engine.assert_not_called()
