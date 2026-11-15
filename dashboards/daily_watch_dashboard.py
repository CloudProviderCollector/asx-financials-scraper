"""Landing page for market dashboards."""

import streamlit as st


def daily_watch() -> None:
    st.set_page_config(page_title="Daily Watch", layout="wide")
    st.title("Daily Watch")
    st.page_link(regression_page, label="VEA, Brent linear regression")
    st.page_link(asx_200_page, label="ASX 200 valuations")


regression_page = st.Page(
    "vea_brent_regression_dashboard.py",
    title="VEA, Brent linear regression",
    url_path="vea-brent-linear-regression",
)
asx_200_page = st.Page(
    "asx_200_dashboard.py",
    title="ASX 200 valuations",
    url_path="asx-200-valuations",
)


def main() -> None:
    st.navigation(
        [
            st.Page(daily_watch, title="Daily Watch", default=True),
            regression_page,
            asx_200_page,
        ]
    ).run()


if __name__ == "__main__":
    main()
