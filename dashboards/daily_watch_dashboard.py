"""Landing page for the VEA/Brent dashboards."""

import streamlit as st


def daily_watch() -> None:
    st.set_page_config(page_title="Daily Watch", layout="wide")
    st.title("Daily Watch")
    st.page_link(regression_page, label="VEA, Brent linear regression")


regression_page = st.Page(
    "vea_brent_regression_dashboard.py",
    title="VEA, Brent linear regression",
    url_path="vea-brent-linear-regression",
)


def main() -> None:
    st.navigation([st.Page(daily_watch, title="Daily Watch", default=True), regression_page]).run()


if __name__ == "__main__":
    main()
