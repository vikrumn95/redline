# BTC/ETH Spot Momentum Bot (MVP)

## Goal

Automated trading bot for Coinbase spot that trades BTC-USD and ETH-USD using a simple trend rule and volatility targeting.

Must be safe, deterministic, restart-safe (state persisted), and easy to extend later for Alpaca equities.

Designed to run both:
- locally on a laptop (dev), and
- on a GCP Compute Engine VM (prod-ish),
without changing code (config via environment variables).

---

## Universe

- Coinbase spot products: **BTC-USD**, **ETH-USD**

---

## Schedule

- Rebalance **once daily** at **00:00 UTC**, using **daily candles aligned to this cutoff**.
- The bot may run 24/7, but the strategy acts only on the scheduled rebalance.
- The bot must support a **run-once-and-exit** command suitable for cron/systemd timers:
  - `python -m redline.main run [--dry-run]`

---

## Configuration (environment-agnostic)

- All runtime configuration must be provided via environment variables (optionally loaded from a local `.env` file in dev).
- Required env vars (minimum):
  - `COINBASE_API_KEY`
  - `COINBASE_API_SECRET`
  - `REDLINE_DRY_RUN` (`true|false`)
  - `REDLINE_DB_PATH` (SQLite file path; see Persistence)
- Optional env vars:
  - `COINBASE_PORTFOLIO_ID` (optional if your key is already scoped, but recommended)
  - `REDLINE_LOG_LEVEL` (default `INFO`)
  - `REDLINE_REBALANCE_TIME_UTC` (default `00:00`)
  - `REDLINE_SLACK_WEBHOOK_URL` (optional; if unset, Slack alerting is disabled but alerts must still be logged)

---

## Signal (choose ONE — no optimization in MVP)

- Trend rule: **risk-on if Close > SMA200** on daily closes; else risk-off.

Notes:
- SMA200 is computed from **daily close prices** aligned to the rebalance cutoff.

---

## Positions

- If risk-off: target **0 BTC and 0 ETH** (100% USD cash on Coinbase).
- If risk-on:
  - Allocate **equally by risk** between BTC and ETH **only if both are risk-on**.
  - If only one is risk-on, allocate only to that asset.

---

## Volatility targeting

### Target

- Target portfolio volatility: **25% annualized**.

### Returns and volatility math (explicit)

- Use daily close prices aligned to rebalance time.
- Daily **log returns**:
  - \( R_t = \ln(P_t/P_{t-1}) \)

- Use the most recent **N = 20** daily log returns for volatility.
- Compute **sample** daily volatility:
  - \( \sigma_d = \sqrt{\frac{\sum_{i=1}^{20}(R_i-\bar R)^2}{19}} \)

- Annualize for crypto (365 days):
  - \( \sigma_{ann} = \sigma_d \times \sqrt{365} \)

### Sizing

- Compute inverse-vol (risk parity style) weights using annualized vols:
  - \( w_i \propto 1/\sigma_{ann,i} \)
  - Normalize weights over the set of risk-on assets only.

#### Portfolio volatility scaling (MVP simplification — avoid covariance matrices)

- For MVP portfolio volatility scaling, assume **correlation = 1.0** between all assets.
- Therefore, compute portfolio volatility as the **weighted sum** of individual vols:
  - \( \sigma_{port} = \sum_i w_i \cdot \sigma_{ann,i} \)
- **Do not** use covariance matrices / Markowitz / matrix multiplications in MVP.

- Scale total notional exposure so the portfolio targets **25% annualized vol** (subject to caps):
  - scaling_factor = target_vol / sigma_port
  - desired_gross_notional = equity * scaling_factor
  - then apply caps below

### Hard caps (MVP, no leverage)

- Max gross notional exposure = **100% of account equity** (no leverage in MVP).
- Max per-asset notional = **70% of equity**.

---

## Data fetching (candles + constraints)

### Historical candles (required for SMA200)

- The bot must reliably obtain at least **250 daily candles** per product (buffer beyond 200).
- If a single API call returns insufficient candles, implement **pagination backward in time** until ≥250 candles are collected.

### Rate limits & retries

- Implement exponential backoff with jitter for HTTP **429 Too Many Requests** and transient 5xx errors.
- If candle fetching fails after reasonable retries, the bot must **halt trading** and alert.

### Product constraints (required)

- The bot must fetch product constraints for each market (min order size, increments/step sizes).
- Order quantities must be **rounded to valid increments**.
- If rounding would result in an invalid order or below-minimum order size, skip that order and alert (do not loop/spam).

---

## Execution lifecycle (explicit ordering)

Each scheduled run at 00:00 UTC must follow this order:

1) Load config and open SQLite.
2) Reconcile balances/positions and compute **current equity** (see definition below).
3) Evaluate circuit breakers (daily loss and drawdown) **before** the SMA/strategy logic.
   - If triggered: go flat immediately (market-sell to USD), set halt flag, alert, persist, and exit.
4) Fetch candles, compute signal/vol/targets, plan orders.
5) Apply risk caps and minimum trade threshold.
6) If not dry-run: submit orders and verify completion.
7) Persist results (state, orders, equity snapshot) and alert summary.

---

## Execution (MVP: Market orders only)

- Coinbase Advanced Trade API.
- **Use market orders only** in MVP (no limit orders, no timeout fallback loops).
- Place at most **one market order per asset** per rebalance.
- Min trade threshold: ignore trades < **$50 notional**.

### Coinbase Advanced Trade market order quirk (required)

- When creating Market Orders via the Coinbase Advanced Trade API:
  - **BUY** orders must use `quote_size` (specified in USD).
  - **SELL** orders must use `base_size` (specified in crypto quantity).
- Calculate buy/sell sizes based on: `target_notional - current_notional`:
  - For BUY: `quote_size_usd = max(0, target_notional_usd - current_notional_usd)` (rounded appropriately)
  - For SELL: `base_size = max(0, current_qty - target_qty)` (rounded appropriately)

### Order handling / polling (minimal)

- After placing market orders, verify completion with conservative polling:
  - Do not poll faster than **once every 2 seconds per order**
  - Apply exponential backoff on 429
  - Stop polling after **60 seconds** and alert if any order remains unresolved
- Any unresolved/unknown order state after timeout triggers a **halt** until manual review.

---

## Risk controls

### Kill switch conditions (halt trading and alert)

- stale price data > **5 minutes** at rebalance time
- broker/API errors persist beyond configured retry limits
- reconciliation mismatch > **$5** or > **0.1% of equity**
- missing/insufficient historical candle data for SMA200/vol calc
- order state unresolved after the execution timeout window

### Circuit breakers

- max daily loss: **-5%** → halt and go flat
- max drawdown: **-30%** peak-to-trough → halt and go flat until manual reset

#### Circuit breaker timing (MVP constraint)

- Because the MVP runs on a daily schedule, circuit breakers are **ONLY evaluated once per day** at the start of the 00:00 UTC run (before the SMA strategy).
- If a breaker is triggered at startup:
  - the bot must immediately issue Market **SELL** orders to liquidate all BTC/ETH to USD (“go flat”),
  - log and alert `RISK_CIRCUIT_BREAKER`,
  - set halt flag in SQLite,
  - persist updated equity/positions/orders,
  - and **exit without executing** the standard SMA trend strategy.

Notes:
- “Daily loss” and “drawdown” must be computed from persisted equity history (not in-memory only).

---

## Reconciliation + Persistence (SQLite required)

### Persistence requirement (must survive restarts)

- Persist bot state to a local **SQLite** database.
- SQLite database path must be configurable via env var: `REDLINE_DB_PATH`.
  - Default: `./data/redline.db`
  - The bot must create the parent directory if it does not exist.
- The bot must be restart-safe: after restart, it must restore state from SQLite.

### Definition of “Account Equity” (required, deterministic)

- Calculate “Current Account Equity” manually as:
  - `equity_usd = available_usd
              + (available_btc * current_btc_price_usd)
              + (available_eth * current_eth_price_usd)`
- Use **available** balances only (not locked/staked/unsupported assets).
- Do **not** rely on Coinbase aggregate portfolio balance, since it may include unsupported or locked assets.

### Minimum persisted state

- last rebalance run timestamp + run status
- current halt flag + halt reason
- peak equity, current equity, daily equity history (or sufficient records to compute daily loss + drawdown)
- latest computed signal states (risk-on/off) for BTC and ETH
- latest computed vols and target weights/notional
- orders placed (order id, timestamps, side, size, status) and fills (if available)
- current positions and cash per latest reconciliation

### Reconciliation rules

- Reconcile with Coinbase balances:
  - at the start of each rebalance run
  - after the order batch completes
- If mismatch exceeds tolerance → halt and do not place new orders.

---

## Observability

### Logging (required)

- Structured logs (JSON) for each rebalance run (JSON lines) written to **stdout** so they work both locally and on GCP VM (Cloud Logging).
- Every run must have a unique `run_id` included in every log line.
- Each run log must include:
  - inputs (candles timestamps, prices)
  - signal states
  - vols
  - target weights and any cap application
  - planned orders
  - submitted orders
  - fills/status
  - post-trade positions
  - equity snapshot and drawdown

### Alerts (required: Slack Incoming Webhook)

- Alerts must be sent via **Slack Incoming Webhook**.
- Config:
  - `REDLINE_SLACK_WEBHOOK_URL` (optional; if unset, Slack alerting is disabled but alerts must still be logged as events)
- If `REDLINE_SLACK_WEBHOOK_URL` is unset:
  - the bot must NOT attempt network calls for Slack,
  - and must log an `ALERT_DISABLED` event once per `run_id`.

#### Events that MUST trigger a Slack alert (when enabled)

1) `HALT_TRIGGERED`
2) `REBALANCE_COMPLETED`
3) `REBALANCE_SKIPPED`
4) `RISK_CIRCUIT_BREAKER`

#### Slack message format

- Post a single message with:
  - Title line: `redline: <EVENT_TYPE> (<env>)`
  - Include: UTC timestamp, `run_id`, `dry_run` flag
  - Include key-value bullets:
    - equity, peak_equity, drawdown
    - BTC risk-on, ETH risk-on
    - target notionals / weights (after caps)
    - orders placed (side, qty/notional, status) or “no orders”
  - Keep messages concise and consistent (plain text is fine).

#### Alert spam control

- Alerts must be rate-limited:
  - At most **one Slack alert per event type per run_id**.

### CLI status (required)

- `python -m redline.main status` prints:
  - current positions (BTC, ETH, USD)
  - last rebalance time (UTC)
  - last run status
  - current drawdown and peak equity
  - halt state + reason (if halted)

---

## Testing

- Unit tests for:
  - SMA200 computation
  - log returns + annualized vol formula (must match the explicit formula above)
  - inverse-vol weighting + cap application
  - portfolio-vol scaling simplification (correlation=1, weighted-sum vol; no covariance matrices)
  - order planning (current → target)
  - kill switch triggers and circuit breaker startup behavior
- Paper trading mode supported:
  - “dry-run” mode that computes targets and logs the exact orders that would be sent, but does not send them.

---

## Extensibility (future)

- Define abstract interfaces for:
  - DataProvider
  - BrokerAdapter
  - Strategy
  - RiskEngine
  - Reconciler
  - Store (SQLite)
- Architecture should allow adding:
  - Alpaca equities strategy module
  - additional strategies and assets
  - WebSocket market/order streams (optional upgrade later)