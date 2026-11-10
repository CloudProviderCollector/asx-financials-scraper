"""Daily Watch navigation tests."""

from unittest.mock import patch

from dashboards import daily_watch_dashboard


def test_daily_watch_links_to_regression_page() -> None:
    with patch.object(daily_watch_dashboard, "st") as streamlit:
        daily_watch_dashboard.daily_watch()

    streamlit.set_page_config.assert_called_once_with(page_title="Daily Watch", layout="wide")
    streamlit.title.assert_called_once_with("Daily Watch")
    streamlit.page_link.assert_called_once_with(
        daily_watch_dashboard.regression_page, label="VEA, Brent linear regression"
    )


def test_navigation_registers_daily_watch_as_default() -> None:
    with patch.object(daily_watch_dashboard, "st") as streamlit:
        daily_watch_dashboard.main()

    streamlit.Page.assert_called_once_with(
        daily_watch_dashboard.daily_watch, title="Daily Watch", default=True
    )
    streamlit.navigation.assert_called_once_with(
        [streamlit.Page.return_value, daily_watch_dashboard.regression_page]
    )
    streamlit.navigation.return_value.run.assert_called_once_with()
