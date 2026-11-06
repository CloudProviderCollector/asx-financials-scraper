# ASX Financials Backend

Python backend for ingesting ASX financial data from Yahoo Finance into PostgreSQL.

## Stack

- FastAPI, Streamlit and yfinance
- PostgreSQL (SQLAlchemy, psycopg and Alembic)
- uv, Ruff, pytest and mypy

## Local setup

1. Install Python 3.13, Docker and [uv](https://docs.astral.sh/uv/).
2. Install dependencies and start the local PostgreSQL container:

```bash
uv sync --all-groups
docker compose up -d postgres
```

3. Copy `.env.example` to `.env` and adjust `DATABASE_URL` if needed. The example credentials are for local development only; the Compose port is bound to localhost. If port 5432 is already in use, set `POSTGRES_PORT` to a free host port before `docker compose up` and use the same port in `DATABASE_URL`.
4. Apply versioned schema migrations **before** starting the API or CLI:

```bash
uv run alembic upgrade head
```

Migrations are explicit and are not run by the app. After pulling schema changes, run `uv run alembic upgrade head` again. Use a fresh database: this change does not migrate pre-existing MongoDB data. Migration `0002` upgrades existing PostgreSQL rows to exchange code `ASX` and renames the ticker-universe columns to exchange-neutral names. Listings are identified by `(exchange_code, ticker)`; ingestion runs, raw payloads, statement revisions, and facts also carry a required exchange code (without a default for future writes). The current CSV provider, ticker validator, API, and ingestion workflow still support ASX only; this schema change does not enable ingestion from other exchanges.

## Run

API:

```bash
uv run uvicorn asx_financials.api.app:create_app --factory --reload
```

One-off ingestion (refreshes fundamentals and daily price/volume history):

```bash
uv run asx-financials ingest BHP
```

To ingest a ticker CSV gradually, use the batch runner (minimum 60 seconds between ticker starts). It reads the `ticker` column and records resumable progress as JSON Lines; successful and partially successful tickers are skipped when rerun, while failures can be retried. Stop the process to pause, then run the same command to resume. Check partial failures in the progress file and stored ingestion runs.

```powershell
uv run python -m asx_financials.ingest_ticker_csv C:\path\to\tickers.csv --progress .venv\ticker-progress.jsonl
uv run asx-financials ingest CSL --annual-only
uv run asx-financials ingest WBC --quarterly-only
```

The first ingest fetches all available Yahoo daily OHLCV history, including adjusted close when provided. Later ingests re-fetch from the latest stored trading day through today to pick up missing sessions and corrections. Price history is keyed by exchange, ticker, and trading date; existing older rows are retained. Each ingest also fetches Yahoo's sparse dividend and split history and upserts corrected actions without re-fetching all historical OHLCV rows. An unavailable history or actions segment is reported without discarding successfully fetched fundamentals. Daily bar counts appear in CLI output.

Read-only ticker dashboard (defaults to CBA):

```powershell
uv run streamlit run dashboards\dashboard.py
```

Open the local URL printed by Streamlit (typically `http://localhost:8501`) and enter an ASX ticker. The dashboard reads existing PostgreSQL data from `DATABASE_URL` in `.env`, showing a company overview (business description, market data, valuation, financial performance, balance sheet, cash flow, dividends, ownership, statement coverage and highlights), the complete saved profile, statement revisions, financial facts, raw responses, ingestion runs, and historical daily prices and share volumes. The Daily prices & volume tab combines closing prices (including derived adjusted close) and volume bars in one chart with a shared date axis, above the OHLCV history table. It does not scrape or modify data; ingest a ticker with the CLI first if it has no stored records.

## VEA and crude oil comparison

Run the read-only Streamlit dashboard using unadjusted VEA daily closes and the daily Yahoo Finance `BZ=F` Brent futures closes stored in PostgreSQL:

```powershell
uv run streamlit run dashboards\vea_crude_dashboard.py
```

The first graph shows VEA.AX unadjusted close in AUD per share on the left axis and Brent futures closes in USD per barrel on the right. A second graph plots VEA alongside Brent multiplied by the average of VEA/Brent ratios on matching trading dates across the available period, on a shared visual scale. A third dual-axis graph overlays the unadjusted VEA close on the left axis and normalized VEA divided by normalized Brent on the right axis, sharing the same dates. Each price series is independently scaled to [0, 1] using its minimum and maximum on matching dates over the full available period before division. The ratio axis autoscales to display the computed values; a horizontal dashed line marks the median, and two orange dashed lines mark the 15.865th and 84.135th percentiles, and two green dashed lines mark the 35th and 65th percentiles of all valid ratio points on that axis. A fourth graph shows the distribution of valid normalized VEA/Brent ratios in 20 equal-width histogram buckets, with trading dates counted on the vertical axis and vertical dashed lines for the median (red), 15.865th and 84.135th percentiles (orange), and 35th and 65th percentiles (green). Dates where normalized Brent is zero have no ratio; a constant series cannot be normalized. All graphs use the VEA history date range; unmatched dates are excluded from the ratios but remain visible on their respective price lines. This normalization is not a currency conversion. The dashboard does not fetch missing observations. A strategy backtest buys VEA at the next trading day's open after a ratio strictly below the 15.865th percentile and sells at the next trading day's open after a ratio strictly above the 35th percentile. A separate VEA unadjusted closing-price graph marks executed buy dates with red vertical lines and sell dates with green vertical lines, including a buy that remains open. The table shows completed buy/sell dates and prices, percentage profit/loss, calendar days held, win rate, and compounded total return. Thresholds and normalization use the entire period (look-ahead bias); missing opening prices skip executions, unmatched Brent days cannot generate signals, and open positions are excluded from results. Strategy 2 has its own VEA price graph, execution markers, completed-trades table, win rate, and compounded return. It buys on the same trigger; the first ratio strictly above the 35th percentile after buying enters prepare-to-sell, then a later observation strictly above the 65th percentile or strictly below the 35th percentile triggers a sale at the next trading day's open. Equality does not trigger a stage change or sale. Strategy 3 has its own VEA price graph, execution markers, completed-trades table, win rate, compounded return, and fund utilization. It follows strategy 2's staged selling rules, but requires two consecutive VEA trading-day ratios strictly meeting each trigger: both below the 15.865th percentile to buy, both above the 35th to enter prepare-to-sell, then two new observations both above the 65th or both below the 35th to sell at the following trading day's open. An unmatched Brent day or otherwise missing ratio breaks confirmation; the buy and stage-entry observations cannot also confirm the next stage. Equality does not qualify. All three strategies show fund utilization: calendar days in a position (buy day included, sell day excluded, and open positions counted through the final VEA close), divided by all calendar days from the first to last VEA close inclusive. It shares the same execution and look-ahead limitations. All VEA graphs and ratios use stored unadjusted closes; backtests use stored unadjusted opens. Returns exclude fees, slippage, dividends, and corporate-action adjustments. Ingest VEA and download commodity prices or run the Hatchet worker first if either series is empty. Legacy FRED/EIA Brent spot rows remain stored separately and are not used by these dashboards or signals. Futures are not spot prices; switching sources can change historical ratios, backtests, and signals.

### Dividend-adjusted VEA and crude oil comparison

Run the separate dividend-adjusted version with `uv run streamlit run dashboards\vea_crude_adjusted_dashboard.py --server.port 8503`. It includes the same graphs, ratios, three strategy backtests, and execution markers, but derives VEA dividend-adjusted closes from stored corporate actions and adjusts backtest opens for dividends and subsequent splits. Results depend on complete corporate-action history; adjusted returns are not cash-dividend payments.

## Daily VEA/Brent Hatchet alerts

A self-hosted Hatchet worker refreshes only VEA.AX (Yahoo) and Yahoo Finance BZ=F Brent futures (not EIA spot) through **yesterday in Australia/Sydney**, then evaluates the dashboard's three strategies on the latest matched observation. It reports pending buy/sell orders for the next available VEA open, **not executed trades**. It does not place orders. The threshold normalization and percentile calculations use the full supplied historical window (look-ahead bias); treat alerts as indicative, not investment advice. Yahoo Brent futures history is loaded on first run; the daily refresh updates that series, leaving older FRED/EIA spot rows untouched. A missing feed triggers an alert after five ASX trading sessions without an observation, once per last-published date while it remains stale (or once when no observations exist). Strategy signals recur on each new evaluation date while active.

1. Start the application PostgreSQL and apply migrations (`uv run alembic upgrade head`). If port 5432 is occupied, set `POSTGRES_PORT` in your shell and align `DATABASE_URL` with that port.
2. Start the separate local Hatchet Lite instance: `docker compose -f docker-compose.hatchet.yml up -d`. Open `http://localhost:8888`, replace the documented default admin credentials, create a tenant and obtain its worker API token. This deployment is bound to localhost; keep it private, and consult [Hatchet self-hosting instructions](https://docs.hatchet.run/self-hosting/hatchet-lite) before exposing it elsewhere.
3. Create an ignored `.env.local` in the repository root containing `HATCHET_CLIENT_TOKEN`, `TELEGRAM_BOT_TOKEN` (from BotFather), and `TELEGRAM_CHAT_ID`. Start a chat with the bot before using its ID. Keep this file private, restrict its filesystem permissions, never commit it, and rotate any tokens exposed in chat or logs. The worker uses the application PostgreSQL Compose network and connects to `postgres:5432` internally; start the application database with `docker compose up -d postgres` and apply its migrations first. Then start the Linux worker (the Hatchet SDK's worker subprocess does not support native Windows event loops):

```powershell
docker compose -f docker-compose.hatchet.yml --profile worker up -d --build worker
docker compose -f docker-compose.hatchet.yml --profile worker ps
docker compose -f docker-compose.hatchet.yml --profile worker logs --tail 50 worker
```

Keep Docker Desktop running and configure it to start at login: Hatchet, the application database, and the worker have `restart: unless-stopped`. The worker image is built from the pinned `uv.lock`; rebuild it after code or dependency changes. The worker requires the external application network created by `docker compose up -d postgres`; the database host port and local `.env` are not used inside the container. Hatchet registers UTC cron triggers at 22:00 and 23:00; only the one falling at 09:00 Sydney executes the daily check, covering daylight saving. Startup and hourly checks recover a missed evaluation after 09:00. On weekends/holidays the last completed ASX session is used, so repeated checks do not duplicate that session's result. The application database records completed session dates and sent alerts. Failed refreshes evaluate stored prices with a stale label, but leave the session incomplete for retry; absent price publications may still count toward the five-session alert. Refresh, evaluation, and notification failures appear in worker logs and Hatchet runs. Delivery and database commit are not atomic: a lost Telegram acknowledgement after acceptance can cause a duplicate on retry. Inspect `vea_crude_runs` and `vea_crude_alerts` to audit progress. No catch-up is possible while the computer is off; the next worker startup or hourly check enqueues the latest missed session (not every historical session). Stop the worker with `docker compose -f docker-compose.hatchet.yml --profile worker stop worker`; stop Hatchet without `-v` to retain its data.

## Commodity price history

Download the longest available public benchmark series directly into PostgreSQL using `DATABASE_URL` from `.env`. Apply migrations first:

```powershell
uv run alembic upgrade head
uv run python -m asx_financials.download_commodity_prices
```

The command upserts seven separate series in `commodity_prices`, keyed by source, series and observation date; reruns update revised prices and fetch timestamps without duplicating observations. Columns include commodity, frequency, price, unit, source, series and observation date. Inspect coverage with `SELECT source, series, min(observation_date), max(observation_date), count(*) FROM commodity_prices GROUP BY source, series;`. FRED/EIA provides daily WTI crude (USD/barrel, since 1986) and **US Gulf Coast jet fuel spot** (USD/gallon, since 1990); its Brent spot series is no longer downloaded. Yahoo Finance's `BZ=F` supplies daily Brent crude futures closes (USD/barrel, since 2007) for both VEA dashboards. Existing FRED/EIA Brent rows, if present, remain in the database but are not used by these dashboards. The FRED/EIA jet-fuel benchmark is not an Australian airline's contracted purchase price. The World Bank's October 2026 Pink Sheet workbook provides monthly gold (USD/troy ounce) and Brent (USD/barrel) back to 1960, plus WTI back to 1982; its latest observation is September 2026. Yahoo Finance's `GC=F` supplies daily COMEX gold futures closes from 2000, **not gold spot prices**. Frequencies, futures versus spot, and USD units must not be mixed without conversion. Blank/nonpositive observations are omitted; source revisions can change values. FRED's former LBMA gold-fixing series was removed, so a daily spot-gold fixing is not included. The script prints observed coverage, and reports failed sources rather than silently claiming complete data. The World Bank workbook URL is pinned to its October 2026 release; update it for later releases. No local CSV files are written; FRED's CSV-formatted HTTP response is parsed in memory.

## Debug in PyCharm

1. Open the repo and set the project interpreter to `.venv/bin/python` (or `.venv/Scripts/python.exe` on Windows).
2. Start PostgreSQL with `docker compose up -d postgres`, copy `.env.example` to `.env`, and run `uv run alembic upgrade head`.
3. Use `.run/ASX Financials API.run.xml`, which starts `uvicorn` with `asx_financials.api.app:create_app --factory --reload --host 127.0.0.1 --port 8000`.
4. Check `http://127.0.0.1:8000/health`. The app reads `.env` from the repository root.

## Quality checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

The PostgreSQL integration tests use `TEST_DATABASE_URL` pointing at a **disposable, already-migrated** database; they skip if the variable is absent. Never point it at a production database.

## API

- `POST /tickers/initialise`
- `POST /ingestions/{ticker}?include_annual=true&include_quarterly=true`
- `GET /ingestions/{ticker}`
- `GET /companies/{ticker}`

## Persistence

Stable ticker metadata, ingestion/sync runs, statement metadata, and numeric financial facts are stored in typed relational columns. Daily OHLCV and optional Yahoo adjusted-close reference are stored in the exchange-scoped `daily_prices` table for date-range lookups. Sparse cash dividends and split ratios live in `corporate_actions`, keyed by exchange, ticker, type, effective date and source. The dashboard derives adjusted close from stored closes and subsequent dividends (`1 - dividend / prior trading close`). Yahoo `Close` already reflects splits at fetch time; only splits occurring after an older bar was fetched change that bar's basis. The dividend and prior close are converted to the same share basis. Historical values can change when actions are corrected or new ones occur; for periods with incomplete Yahoo action history or rounding, the derivation can differ slightly from Yahoo's `Adj Close`. Volume counts shares, not dollars. Fluid Yahoo Finance profiles, statement values and raw responses are JSONB. Statement revisions are keyed by source/ticker/type/frequency/period and payload hash; the current revision supplies availability and previews. Failed or null provider responses do not overwrite successful data.

This repository targets local PostgreSQL; the former Cloud Run/MongoDB deployment path is removed.
