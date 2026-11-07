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

## VGN and QAN price comparison

Run the read-only dashboard to compare stored Virgin Australia (VGN) and Qantas (QAN)
daily unadjusted closing share prices. The first graph uses separate AUD-per-share axes
(VGN left, QAN right); the second shows both series independently min-max normalized
to 0–1 over the displayed period (a constant series stays at 0). A third graph
shows the raw VGN close on the left AUD/share axis and the same-day ratio of
independently normalized VGN close to normalized QAN close on the right dimensionless
logarithmic ratio axis (the left raw VGN price axis stays linear), using the second
graph's VGN-period normalization. The ratio skips dates without both normalized
closes or with a zero normalized QAN value (including the QAN minimum or a constant
QAN series); zero, negative and nonfinite ratios cannot appear on the log axis,
and gaps are not joined. A fourth graph
shows a 20-bucket histogram of the natural logarithm ln(ratio) for positive finite
ratios from the third graph. Zero, negative, missing, and nonfinite ratios cannot
be logged and are excluded; the horizontal axis is ln(normalized VGN/QAN ratio)
and the vertical axis counts trading dates. A red line marks the median of those
logged values. Green lines mark median ±1 population standard deviation and
orange lines mark median ±2 population standard deviations. All five marker lines
are dotted; the standard deviation
is calculated from the logged observations, not the unlogged ratio, and the offsets
are around the median rather than the mean. For one observation or a constant
sample, only the median line is shown (σ lines would overlap it); if none qualify,
it shows a message instead:

```powershell
uv run streamlit run dashboards\vgn_qan_dashboard.py
```

A fifth graph backtests a VGN-only strategy over the same VGN period. A logged
ratio strictly below median −2 population standard deviations signals a buy;
a logged ratio strictly above median −1 standard deviation signals a sell.
Each signal executes at the next VGN trading day's positive opening price, not
at the signal's closing price. Red dotted vertical lines mark executed buys and
green dotted lines mark executed sells on the VGN closing-price graph. A table
lists completed trades with signal and execution dates, opening prices, holding
days and percentage profit/loss. The summary shows the compounded percentage
return from completed trades and fund utilization: calendar days held (buy date
inclusive, sell date exclusive, including an open position through the final
VGN close) divided by all calendar days of the displayed VGN period. Only one
position is held at a time; missing or invalid opens cancel pending orders and
dates without a valid logged ratio cannot signal. Unclosed positions are marked
and counted toward utilization but excluded from realized returns. Signals on
the final day remain unexecuted. The normalization and thresholds are computed
using the *entire* displayed period, introducing look-ahead bias; this is an
illustrative retrospective backtest, not a deployable live strategy. Returns
exclude fees, slippage, dividends and corporate-action adjustments.

A separate rolling linear-spread Z-score strategy compares VGN (A) with QAN (B).
It fits one fixed slope β by regressing VGN on QAN using ordinary least squares
*with an intercept* over **all** overlapping positive finite VGN-period daily
closes (at least two pairs). The spread is `VGN close − β × QAN close` in AUD/share
(the fitted intercept is **not** subtracted). On each date, the historical
reference spreads use that same fixed β on all matched pairs from the previous
365 calendar days, excluding the signal date. Their mean and **population**
standard deviation define the current close's Z-score. There is no signal when
fewer than 20 matched prior days exist, QAN has negligible full-sample variance,
the spread SD is zero, or prices are missing/invalid; this can suppress early
VGN-history trades. The fixed β uses future prices on earlier dates and thus
introduces **look-ahead bias** to historical backtests, although the rolling
spread reference excludes the current and future dates. A separate graph plots
the fixed dimensionless β as a horizontal line; another overlays VGN close and
that day's fixed β × QAN close on a shared AUD/share axis. Both show gaps when
either close is missing/invalid. β can be negative, so β × QAN can also be
negative; a zero spread SD suppresses the Z-score but does not suppress a valid
β. A graph plots raw spread (left axis) and
Z-score (right axis) with dotted −2 buy and −1 sell thresholds; both axes share
VGN's date range. The strategy buys VGN on a strict Z < −2 and sells on a strict
Z > −1, at the *next* VGN trading day's positive open. A separate VGN-price plot
marks executed buys red and sells green with dotted vertical lines, followed by
a completed-trades table, compounded return and calendar-day fund utilization.
It holds at most one position; invalid next opens cancel pending orders and open
positions count toward utilization but not realized return. The underlying daily
closes are unadjusted; fees, slippage, dividends and corporate actions are not
included. Unlike the rolling reference, the fixed hedge ratio is estimated from
the entire sample, so this remains a look-ahead-biased retrospective illustration.

A separate **two-sided paired spread strategy** uses the same fixed full-sample
intercept-fitted β, but computes one full-sample mean and *population* standard
deviation from all eligible overlapping VGN-period daily spreads
`VGN close − β × QAN close` (the fitted intercept is omitted). The resulting
full-sample Z-score requires at least two spreads and nonzero variance. A
20-equal-width-bucket histogram counts eligible **raw spreads (AUD/share)** used
for the full-sample reference, with dotted lines for the mean, mean ±1.5
population SD (entries), and mean ±0.5 population SD (exits). A constant
spread is shown in a centered bucket without misleading threshold lines; an
empty series produces a message. The spread/Z-score chart shows dotted ±1.5
entry and ±0.5 exit thresholds; red dotted execution-entry lines and green
dotted execution-exit lines appear on both that chart and the VGN price chart.
Up/down triangle markers on the price chart distinguish long/short VGN trades
in the legend. The paired-trade price chart overlays VGN closing price on the
left AUD/share axis and the same fixed β times date-aligned QAN closing price
on the right **linear** AUD/share axis (which can be negative for negative β);
missing/invalid QAN prices or β leave gaps, and execution markers remain on
VGN's axis. A strict
Z < −1.5 opens long one VGN share and short β QAN
shares, closing at strict Z > −0.5; strict Z > +1.5 opens short one VGN share
and long β QAN shares, closing at strict Z < +0.5. Signed β quantities are
used, so if β is negative, the QAN position's actual side reverses. There is
only one position at a time, with no same-bar reversal. Signals occur at
paired daily **closes**; **both** legs fill together at the next common
trading day's positive finite VGN and QAN **opens**, never at the triggering
close or on the same calendar day. Weekends/holidays and unmatched or invalid
opens defer the order to a later common eligible open without filling one leg.
An unfilled terminal signal is not a trade. Completed-trade rows show signal and
execution dates, direction, both legs' entry/exit open prices, each leg's and
net AUD P&L, net percentage return, and holding days measured as calendar days
from entry execution inclusive to exit execution exclusive. For signed quantities (+1, −β) on
the long side or (−1, +β) on the short side, each leg's P&L is its quantity
times `(exit open − entry open)`. Net percentage return divides net P&L by
gross opening exposure `|VGN entry open| + |β × QAN entry open|`. The summary
compounds completed-trade net returns; fund utilization counts calendar days
from entry inclusive to exit exclusive, counting any open position through the
last VGN date. Open positions are disclosed separately, without unrealized
returns. Trading fees, borrow costs, slippage, dividends and corporate actions
are excluded. **Both β and the full-sample mean/SD use future data for earlier
signals, so this is a look-ahead-biased retrospective illustration, not a
prospective performance estimate.** The original prior-365-day rolling-reference
strategy remains independent.

All graphs read PostgreSQL through `DATABASE_URL` and do not fetch missing data. They
show only dates from VGN's first to last stored trading day; older QAN prices remain
in PostgreSQL but are excluded from these graphs. Ingest both tickers first with
`uv run asx-financials ingest VGN` and `uv run asx-financials ingest QAN`. If QAN has
no stored closes, the dashboard warns and shows VGN alone. If VGN has no stored
closes, it warns and does not show a chart because there is no comparison period.

## VEA and crude oil comparison

Run the read-only Streamlit dashboard using unadjusted VEA daily closes and the daily Yahoo Finance `BZ=F` Brent futures closes stored in PostgreSQL:

```powershell
uv run streamlit run dashboards\vea_crude_dashboard.py
```

The first graph shows VEA.AX unadjusted close in AUD per share on the left axis and Brent futures closes in USD per barrel on the right. A second graph plots VEA alongside Brent multiplied by the average of VEA/Brent ratios on matching trading dates across the available period, on a shared visual scale. A third dual-axis graph overlays the unadjusted VEA close on the left axis and normalized VEA divided by normalized Brent on the right axis, sharing the same dates. Each price series is independently scaled to [0, 1] using its minimum and maximum on matching dates over the full available period before division. The ratio axis autoscales to display the computed values; a horizontal dashed line marks the median, and two orange dashed lines mark the 15.865th and 84.135th percentiles, and two green dashed lines mark the 35th and 65th percentiles of all valid ratio points on that axis. A fourth graph shows the distribution of the natural log of positive normalized VEA/Brent ratios in 20 equal-width histogram buckets, with trading dates counted on the vertical axis. Zero ratios are excluded because their logarithm is undefined; vertical dashed lines mark the logarithm of the median (red), 15.865th and 84.135th percentiles (orange), and 35th and 65th percentiles (green) of positive ratios. Strategy thresholds and the other charts continue to use untransformed ratios. Dates where normalized Brent is zero have no ratio; a constant series cannot be normalized. All graphs use the VEA history date range; unmatched dates are excluded from the ratios but remain visible on their respective price lines. This normalization is not a currency conversion. The dashboard does not fetch missing observations. A strategy backtest buys VEA at the next trading day's open after a ratio strictly below the 15.865th percentile and sells at the next trading day's open after a ratio strictly above the 35th percentile. A separate VEA unadjusted closing-price graph marks executed buy dates with red vertical lines and sell dates with green vertical lines, including a buy that remains open. The table shows completed buy/sell dates and prices, percentage profit/loss, calendar days held, win rate, and compounded total return. Thresholds and normalization use the entire period (look-ahead bias); missing opening prices skip executions, unmatched Brent days cannot generate signals, and open positions are excluded from results. Strategy 2 has its own VEA price graph, execution markers, completed-trades table, win rate, and compounded return. It buys on the same trigger; the first ratio strictly above the 35th percentile after buying enters prepare-to-sell, then a later observation strictly above the 65th percentile or strictly below the 35th percentile triggers a sale at the next trading day's open. Equality does not trigger a stage change or sale. Strategy 3 has its own VEA price graph, execution markers, completed-trades table, win rate, compounded return, and fund utilization. It follows strategy 2's staged selling rules, but requires two consecutive VEA trading-day ratios strictly meeting each trigger: both below the 15.865th percentile to buy, both above the 35th to enter prepare-to-sell, then two new observations both above the 65th or both below the 35th to sell at the following trading day's open. An unmatched Brent day or otherwise missing ratio breaks confirmation; the buy and stage-entry observations cannot also confirm the next stage. Equality does not qualify. All three strategies show fund utilization: calendar days in a position (buy day included, sell day excluded, and open positions counted through the final VEA close), divided by all calendar days from the first to last VEA close inclusive. It shares the same execution and look-ahead limitations. All VEA graphs and ratios use stored unadjusted closes; backtests use stored unadjusted opens. Returns exclude fees, slippage, dividends, and corporate-action adjustments. Ingest VEA and download commodity prices or run the Hatchet worker first if either series is empty. Legacy FRED/EIA Brent spot rows remain stored separately and are not used by these dashboards or signals. Futures are not spot prices; switching sources can change historical ratios, backtests, and signals.

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
