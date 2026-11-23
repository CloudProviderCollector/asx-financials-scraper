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

To backfill **only** daily OHLCV for the fixed 200-stock roster in `data\asx200\asx200_20261122.csv`, including dates before any previously stored bars, run `uv run python -m asx_financials.backfill_asx200_prices --progress C:\path\to\asx200-prices.jsonl`. It requests Yahoo's maximum available daily history for each ticker, inserts missing dates without changing existing rows, and leaves profiles and statements untouched. It starts tickers at least 60 seconds apart and paces Yahoo HTTP requests at least 10 seconds apart. Reuse the same progress file to resume after interruption; failed tickers remain eligible for retry. Malformed individual Yahoo bars are skipped and recorded in the progress file while valid history is retained; empty or wholly invalid history still fails. A rate-limit error stops the batch, and other ticker failures are reported after processing the rest of the roster. This roster is a dated TradingView snapshot supplemented by two ETF holdings, not an officially verified current or historical index list.

## Daily trend-following backtest

Run the read-only, long-only backtester against stored ASX daily prices (first ingest or
backfill the requested tickers). Alternatively use `--source yahoo` to download public
history without writing to PostgreSQL:

```powershell
uv run python -m backtest --tickers BHP CBA --start 2015-01-01 --equity-csv equity.csv --trades-csv trades.csv
uv run python -m backtest --source yahoo --tickers BHP CBA --start 2015-01-01 --chart-html trades.html
```

The default `moving-average` strategy buys only when the 20-session simple
moving average crosses strictly above the 65-session simple moving average
(below on the prior close, above on the current close). It sells whenever the
current close is strictly below the 5-session moving average, even if it was
already below on the prior day. Touching or starting from equality does not
count as an entry cross. The earlier 100-session breakout/200-session trend
filter with a 50-session low or ATR trailing-stop exit is available with
`--strategy breakout`.
All decisions use data available at that close; orders fill
at the next available trading open (not intraday). Historical bars before
`--start` warm up the indicators. By default the run excludes the current ASX
calendar day to avoid incomplete sessions; `--end` sets an explicit last date.
Pending orders at the end do not fill.

`--chart-html` writes a standalone interactive graph with each requested ticker's
adjusted daily close, green buy and red sell markers at executed adjusted opening
fills, and portfolio equity. For the default strategy, each ticker's price panel
also shows its 5-, 20-, and 65-session simple moving averages, warmed up using
history before `--start`. It includes executed entries for positions still open;
unfilled terminal signals are not plotted.

To compare and retain every strategy version tried, run:

```powershell
uv run python -m backtest --source yahoo --tickers BHP CBA --start 2015-01-02 --end 2026-11-20 --record-all
uv run streamlit run dashboards\backtest_dashboard.py
```

The dashboard can also run and record all versions from Yahoo or PostgreSQL.
Choose a saved run to compare final equity, return, drawdown, and trades, then
review each strategy's rules, parameters, and interactive price, moving-average,
trade, and equity graphs. The breakout graph overlays its 200-session trend
average; the MA strategies overlay the averages used in their rules. The five
versions are `breakout` (100-session high and 200-session
trend filter), `ma-state` (20/65 state entry, 5/20 state exit),
`ma-cross-10` (20/65 crossing entry, 10/20 crossing exit),
`ma-cross-5` (20/65 crossing entry, 5/20 crossing exit), and
`moving-average` (20/65 crossing entry, close below 5-session average exit).
Each saved set under `backtest\records` contains the exact adjusted input
bars, one result per version, and a manifest with rules, parameters, settings,
source, dates, and metrics. These local files are ignored by Git; copy them
elsewhere to preserve them across worktree removal. Earlier rules are rerun
on the *same current price snapshot*, not recovered historical Yahoo results.

The shared runner in `backtest\engine.py` takes daily bars and a strategy that
implements `signal` and `trailing_distance`; `backtest\trend.py` supplies both
strategies. It starts with AUD 100,000, sizes entries at 0.5% of equity divided
by three times the 20-session ATR (not a guaranteed risk limit for the
moving-average strategy, which has no stop-loss exit), limits a stock to 10%
and a sector to 25% of equity, and charges AUD 10 commission plus 5 basis points
of adverse slippage per fill.
It reports marked-to-market equity, maximum drawdown and realized trades;
unclosed positions remain in final equity. Prices use adjusted-close/close to
scale each day's OHLC into a total-return proxy. These adjusted prices are
synthetic (not executable quotes); the model does not explicitly settle
dividends or reconstruct split-adjusted share counts. Yahoo runs have no
historical sector data, so each ticker has its own sector bucket. PostgreSQL
uses the *current* stored sector, not a point-in-time sector. Incomplete or
inconsistent stored daily OHLCV/adjusted closes fail the run. Inconsistent
Yahoo OHLCV/adjusted bars are reported on stderr and skipped, while Yahoo
provider parse failures abort the run; gaps are not filled with invented prices.
No historical point-in-time membership is available: choosing today's roster
for an old period creates survivorship bias. This is a research backtest, not
an investable return estimate.

## ASX gold miners vs gold futures

Apply migrations (`uv run alembic upgrade head`), then run `uv run python -m asx_financials.download_exchange_rates` to backfill the longest Yahoo `AUDUSD=X` daily close history into PostgreSQL `exchange_rates`. The rate is **USD per AUD**; reruns update revised values. Run `uv run streamlit run dashboards\gold_miners_dashboard.py` as a standalone dashboard. The four upper charts compare stored NST, EVN, NEM, and PRU daily ASX closing share prices (AUD/share, left axis) against Yahoo `GC=F` COMEX gold futures closes converted to AUD/troy ounce (right axis). The AUD gold price is calculated on read as the USD gold close divided by the same-date AUD/USD rate; it is not separately stored. The available FX history begins in May 2006, so earlier gold closes and dates without matching rates are excluded, never forward-filled. Below each upper chart, ordinary least squares fits the stock close to the converted gold close on matching dates only and plots actual and estimated share prices. A further graph for each stock fits and plots actual and estimated closes over the last ten calendar years ending on that stock's latest matching close, or all available matched closes if shorter. The page shows the slope, intercept, sum of squared errors (SSE), mean squared error (MSE), root mean squared error (RMSE, in AUD/share), and number of matched observations for both fits; the full-history fit also shows RMSE divided by the latest available stock close (as a ratio and percentage, with the close date). The estimates are in-sample, not forecasts. These are unadjusted share closes and rolling futures prices, not gold spot prices. The dashboard reads PostgreSQL only; ingest each stock and download the `GC=F` commodity series and AUD/USD history first.

All charts for each miner are grouped together. Each full-history, ten-year, and five-year estimate has its own numeric `a` and `b` inputs, initialized to that window's fitted slope and intercept. Editing them redraws that window's estimated-price graph and recalculates its error metrics; editing the five-year inputs also recalculates its valuation signals, thresholds, and trades. The trading section fits `actual share close = a * AUD gold close + b` over the last five calendar years ending on that stock's latest matched close (or its shorter available history). Its valuation signal is `(actual share close - estimated share close) / estimated share close`: negative means below the selected estimate, positive means above it. The graphs show five-year actual and estimated share prices and the signed percentage deviation, its distribution, and one trading strategy with a five-year share-price graph. Buy when the deviation is strictly below its five-year mean minus 1.5 population standard deviations; sell when it is strictly above the mean minus 0.5 standard deviations. Orders execute at the next stock trading open. The trade table shows execution dates, returns, win rate, compounded return and calendar-day fund utilization. Nonpositive estimated prices have no defined signal; missing opens skip executions. **The default fit and thresholds use the entire five-year sample, including observations after earlier trades (look-ahead bias). These in-sample results are not a prospective strategy or live valuation signal.** Returns omit fees, slippage, dividends and corporate actions.

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

### VEA / Brent spread opportunities (fixed 2018–June 2023 training window)

Launch **Daily Watch**, a landing page with a link to the VEA/Brent linear regression dashboard, using `uv run streamlit run dashboards\daily_watch_dashboard.py`. Open the URL Streamlit prints (by default `http://localhost:8501`) and select **VEA, Brent linear regression** on the page or in the sidebar. The linked dashboard reads stored PostgreSQL prices, so the database must be available.

Bold red text above the opening VEA/Brent price chart shows the latest valid VEA and Brent close dates separately, including dates without a matching close from the other series. The chart defaults to the last calendar year relative to the latest matched close. Its range buttons offer 1d, 5d, 1m, 3m, 1y, 5y, and max (all matched history).

Run `uv run streamlit run dashboards\vea_brent_regression_dashboard.py` for the separate read-only **VEA, Brent linear regression** dashboard. A chart at the top shows matched VEA closes on the left AUD/share axis and Brent closes on the right USD/barrel axis across all available matched dates. It fits ordinary least squares with an intercept, `VEA = a × Brent + b`, using positive, finite, same-date VEA.AX unadjusted closes (AUD/share) and Yahoo BZ=F Brent futures closes (USD/barrel). One fit uses observations from 2018-07-01 through 2023-06-30 inclusive; the other uses all available matched observations. Each graph overlays actual VEA closes and estimated VEA on the same axis across the full matched history, including out-of-sample dates for the fixed-period fit. The dashboard reports each fit's coefficients and observation count. Below each overlay, a 20-bin histogram of `VEA − estimated VEA` uses residuals from that fit's observation period only; dashed lines label the residual mean and ±0.5 and ±1.5 population standard deviations with their values in AUD/share. These statistical estimates do not convert currencies or predict future Brent prices. At least two matched observations with varying Brent closes are needed for each fit. Both fits support separate hypothetical, long-only VEA trade simulations across all available matched dates, starting with the first valid matched close. Each simulation uses its own fitted `a` and `b`, and its fitting period's residual mean and population SD to calculate Z-scores. Signals before July 2023 under the fixed-period fit are in-sample and look-ahead-biased because it uses future observations relative to those signal dates. The all-data simulation is in-sample and look-ahead-biased across the historical period because its coefficients and reference statistics use the full history. Both score `VEA − (a × Brent + b)` against their own fixed fitting-period residual mean and population SD, buying at strict Z < −1.5 and selling at strict Z > −0.5. A dual-axis chart shows VEA closes, the residual Z-score, thresholds and red buy/green sell signal dates; completed trade rows show entry/exit prices, Z-scores, VEA-only P/L and returns, holding days, and fund utilization over calendar days from the first valid matched close through the last matched close inclusive. The two simulations have separate charts, trades, and utilization metrics. The simulation treats signal-day closes as hypothetical fills even though a close-triggered order cannot execute at that same close; it is look-ahead-biased and not realized P/L. No Brent position, dividends, fees, slippage or corporate actions are modeled.

Run `uv run streamlit run dashboards\vea_brent_spread_dashboard.py` for a separate, read-only out-of-sample spread dashboard. It sets β to the ratio of the mean VEA.AX close to the mean BZ=F close from positive, finite, date-matched closes between 2018-01-01 and 2023-06-30 inclusive. The raw spread is `VEA − β × Brent`, with a training mean of zero up to floating-point rounding; Z-scores use its **training-only** mean and population SD. Both β and the reference stay fixed for signals from 2023-07-01 onward; no post-training observation affects the calibration. The first chart compares date-matched VEA (AUD/share, left axis) and BZ=F Brent futures (USD/barrel, right axis) closing prices. The next chart displays the VEA closing price alongside the spread Z-score and entry/exit signal dates; a histogram shows the raw training spread distribution. This beta differs from an intercept-fitted OLS slope: the raw spread now averages zero over training, but neither series nor the spread is guaranteed to cross zero on every observation. Strict Z < −1.5 buys one VEA share, selling at Z > −0.5; **the strategy is long-only and never shorts VEA**. Brent is only a signal input: **no Brent position is traded**. Only one VEA position can be active, with no same-day reversal. The price chart and training histogram show only the buy and sell thresholds. Completed entry/exit signals appear together in each table row with their dates, VEA entry and exit closes and Z-scores, calendar days held, hypothetical VEA P/L in AUD per share (exit minus entry), and percentage return on the VEA entry close. The sum of completed VEA P/L (assuming one share per trade) and fund utilization appear below the table. Utilization is calendar days held (entry included, exit excluded), including an open signal through the latest matched close, divided by calendar days from 2023-07-01 through that close inclusive. Unpaired positions are shown separately and excluded from completed P/L. Insufficient training pairs or constant spread suppress signals. Dates are matched calendar dates, not nearest-session forward fills. **Signal-day closing prices are hypothetical fills with look-ahead bias, not executable trades or realized returns**: the trigger is known only at the close, and Brent opens are not stored. VEA closes are AUD/share, so hypothetical VEA-only P/L is denominated in AUD per share, not mixed units. The spread is a statistical signal mixing VEA and Brent units, not a currency-converted hedge. No fees, slippage, dividends or corporate-action adjustments are modeled. Ingest VEA and download commodity prices before opening the dashboard.

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

Keep Docker Desktop running and configure it to start at login: Hatchet, the application database, and the worker have `restart: unless-stopped`. The worker image is built from the pinned `uv.lock`; rebuild it after code or dependency changes. The worker requires the external application network created by `docker compose up -d postgres`; the database host port and local `.env` are not used inside the container. Workflow implementations live in the repository-root `workflows` package; `catch_up.py` owns run auditing and `worker.py` registers the workflows and starts the worker. In Hatchet, `vea-crude-evaluate` runs daily at 23:00 UTC (09:00 AEST/10:00 AEDT the next morning), and both `asx-200-profile-capture` and `asx-200-index-capture` run at 07:00 UTC (17:00 AEST/18:00 AEDT). Each has its own visible daily cron. `market-catch-up` audits Hatchet runs every 15 minutes at :10, :25, :40, and :55 UTC and once on worker startup, dispatching only when the latest eligible session has no successful, queued, or running run. A successful run counts even if some profiles are missing; failed runs can be retried on the next audit. The worker uses Hatchet's internal REST URL to read run history. Legacy workflows remain visible in Hatchet to preserve their run history, but their old cron triggers have been removed. On weekends/holidays the last completed ASX session is used for VEA, so repeated checks do not duplicate that session's result. The application database records completed session dates and sent alerts. Failed refreshes evaluate stored prices with a stale label, but leave the session incomplete for retry; absent price publications may still count toward the five-session alert. Refresh, evaluation, and notification failures appear in worker logs and Hatchet runs. Delivery and database commit are not atomic: a lost Telegram acknowledgement after acceptance can cause a duplicate on retry. Inspect `vea_crude_runs` and `vea_crude_alerts` to audit progress. No catch-up is possible while the computer is off; the next worker startup or next 15-minute audit enqueues the latest missed session (not every historical session). Stop the worker with `docker compose -f docker-compose.hatchet.yml --profile worker stop worker`; stop Hatchet without `-v` to retain its data.

## Historical ASX 200 snapshots

The April 2026 roster remains at `data\asx200\asx200_20260405.csv` as a historical membership snapshot; the active 200-symbol roster is `data\asx200\asx200_20261122.csv`. The latter contains 198 symbols from a TradingView ASX 200 snapshot dated 22 November 2026, supplemented with BJY and DVP from [State Street's STW holdings dated 20 November 2026](https://www.ssga.com/library-content/products/fund-data/etfs/apac/holdings-daily-au-en-stw.xlsx). Those two rows have `source=stw_holdings_20261120`; the other rows retain `source=tradingview_asx200`. ETF holdings are a proxy, **not an authoritative S&P/ASX 200 constituent feed**, so the combined roster is not independently verified as exact index membership. It excludes delisted IFL, NSR, and QUB; XYZ is the Block symbol and ASK's successor SKG is not in this November roster. Both CSVs use ticker, name, sector, added, removed, and source columns; added and removed dates are unavailable.

The former index-snapshot dashboard remains removed. The independent `asx-200-index-capture` job saves S&P/ASX 200 (`^AXJO`) snapshots in PostgreSQL table `asx_200_snapshots` at the same UTC time as stock profiles. After the close of the current ASX trading session, it fetches daily price and available volume bars for every trading session since the latest stored index snapshot, including today. If no prior snapshot exists, it starts with today. The batch is atomic: a missing or invalid bar fails the run without saving a partial range, allowing 15-minute catch-up to retry while today's session remains eligible. Already stored rows are not revised. Historical backfill rows do not receive today's quote-derived metadata or P/E. The separate `asx-200-profile-capture` job collects company profiles, not index snapshots. Inspect captures with `SELECT session_date, close, volume, average_volume, average_volume_10days, trailing_pe, fetched_at FROM asx_200_snapshots ORDER BY session_date DESC LIMIT 10;`.

The table stores historical OHLC and available volume from Yahoo's daily bar, the current quote/previous close, Yahoo's reported average and 10-day average volume, optional trailing/forward P/E, the quote timestamp, and the complete `Ticker.info` response as JSONB (including available 52-week range and other metadata). When the quote date differs from the completed bar (e.g. a delayed retry), current quote-derived fields are left NULL and the raw info is not attached to the older session. Absent Yahoo fields remain NULL; `^AXJO` currently provides no P/E, and zero daily index volume is treated as unavailable. The JSONB fields and averages are **point-in-time captures**, not reconstructed historical fundamentals; reruns do not revise existing snapshots.

For an older historical range, call `workflows.asx_200_index_workflow.backfill(engine, start, end)` with inclusive `date` arguments and an SQLAlchemy engine connected to the application database. It requires a completed ASX session at the end of the range, validates every expected trading bar before inserting, leaves existing dates untouched, and stores historical OHLC/available volume only. Quote metadata and P/E cannot be reconstructed for those dates.

## ASX 200 roster valuations

The dashboard uses the fixed 200-symbol roster at `data\asx200\asx200_20261122.csv`; it is **not verified current or historical S&P/ASX 200 membership**. Run `uv run streamlit run dashboards\daily_watch_dashboard.py` and select **ASX 200 valuations**, or run `uv run streamlit run dashboards\asx_200_dashboard.py` directly. At the top, the ASX 200 index chart reads stored `^AXJO` daily closes from `asx_200_snapshots`, defaults to the latest month, and offers 3m, 6m, 1y, 3y, 5y, and All range controls. The valuation section uses stored `company-profile` raw Yahoo snapshots from ticker ingestions; it does not fetch data on page load. The daily profile-capture workflow adds observations for this roster. The original ASX 300 CSV remains in `data\asx300\asx300_20260410.csv` for reference, but is no longer used by this dashboard.

The daily `asx-200-profile-capture` cron triggers at 07:00 UTC (17:00 AEST/18:00 AEDT) on all days; non-trading days perform no capture. The 15-minute catch-up checks the current trading date after its scheduled run and retries missing or failed runs while that date is still eligible. Rebuild the worker after updating the code; its image includes the dated 200-stock roster CSV. The job saves Yahoo `get_info()` payloads, including `trailingPE`, `forwardPE`, and `marketCap` when available, and fetches that session's daily OHLC price and volume into `daily_prices` without requesting full price or financial-statement histories. A missing or invalid daily bar fails the ticker's ingestion run; a ticker is skipped on retry only when both its post-close profile and that session's price bar are stored. Its Yahoo HTTP requests, including authentication and retries, start at least 10 seconds apart within the job; one ticker may require multiple HTTP requests and the full roster can take considerably longer than 34 minutes. Another worker or service making Yahoo requests is not rate-limited by this job. Failures are logged in `ingestion_runs` and retried after a failed run while that trading date is current. A successful Hatchet run is not retried just because fewer than 200 tickers were captured. No past-day profiles or prices are backfilled or mislabelled as historical observations. Yahoo can still rate-limit requests, in which case the job stops rather than continuing to send requests. Inspect `raw_source_payloads` for profile timestamps and `daily_prices` for price and volume.

Each Sydney date uses the most recently fetched profile for each ticker on that date. An explicitly recorded weekend backfill is assigned to the last closed session, while the actual Yahoo fetch time, reported quote date (when available), and a backfill label remain visible in the constituent table and daily coverage. Run `uv run python -m workflows.asx_200_backfill YYYY-MM-DD` only on a non-trading day before the next trading session; this captures only roster stocks without a post-close profile. A reported Yahoo quote may be up to seven calendar days older than the target session; a profile without a reported quote date is accepted only with a positive AUD market cap and trailing or forward P/E. Missing or older quote dates are explicitly disclosed, never presented as verified session-date fundamentals. Profiles lacking usable valuation or with a quote outside the allowed window are recorded as failed ingestion runs; remaining tickers are still attempted, and the command exits with an error listing those failures. Other fetch or storage errors stop the job. Weekend-fetched market caps and P/E estimates may have changed since that session, so backfilled valuations are **proxies, not verified historical fundamentals**. No other past dates are reconstructed or carried forward. Trailing and forward P/E are separately weighted by the positive AUD market caps of stocks with positive, finite values for that P/E; missing, zero, negative and non-AUD values are excluded. If **90% or more** of the roster lacks eligible inputs for a P/E (20 or fewer of 200 stocks), that day's aggregate is hidden from the metric, chart and daily table; the other P/E is assessed independently. The constituent rows and coverage counts remain visible. The dashboard shows both contributing stock counts and their fraction of **observed** AUD market cap; coverage is not measured against the full index. Dates without snapshots are not filled. Values and membership are point-in-time observations of this fixed roster, not official index P/E history.

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
