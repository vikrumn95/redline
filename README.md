# Redline

BTC/ETH spot momentum trading bot for Coinbase Advanced Trade. Uses a daily SMA200 trend signal and inverse-volatility-parity sizing to allocate between BTC-USD and ETH-USD. Designed to run as a single daily cron/systemd job; all state is persisted in SQLite so it survives restarts cleanly.

---

## Table of Contents

1. [Quickstart (Local)](#quickstart-local)
2. [Configuration](#configuration)
3. [Operating Modes](#operating-modes)
4. [CLI Reference](#cli-reference)
5. [Halting and Reset (Runbook)](#halting-and-reset-runbook)
6. [Live Trading Checklist](#live-trading-checklist)
7. [Deployment (GCP VM)](#deployment-gcp-vm)
8. [Known Gaps](#known-gaps)

---

## Quickstart (Local)

### 1. Create virtualenv and install

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — see Configuration section below
# Leave REDLINE_DRY_RUN=true until you are ready to trade real money
```

### 3. Run in dry-run (safe, no orders placed)

```bash
python -m redline.main run --dry-run
```

### 4. Check status

```bash
python -m redline.main status
```

The SQLite database is created automatically at `./data/redline.db` (configurable via `REDLINE_DB_PATH`).

---

## Configuration

All configuration is via environment variables (loaded from `.env` if present).

| Variable | Required | Default | Description |
|---|---|---|---|
| `COINBASE_API_KEY` | For live trading | _(empty)_ | Coinbase CDP API key ID (EC/ES256, trading scope) |
| `COINBASE_API_SECRET` | For live trading | _(empty)_ | EC private key PEM; escape newlines as `\n` in the file |
| `COINBASE_PORTFOLIO_ID` | No | _(empty)_ | Restrict API calls to this portfolio UUID; recommended when key is shared |
| `REDLINE_DRY_RUN` | No | `true` | `true` = plan orders but never submit; `false` = live mode |
| `REDLINE_MOCK` | No | `false` | `true` = synthetic candles and balances, no Coinbase calls; auto-enabled when `COINBASE_API_KEY` is empty |
| `REDLINE_ENV` | No | `dev` | Deployment tag. Must be `dev` for test override flags to be accepted |
| `REDLINE_DB_PATH` | No | `./data/redline.db` | Path to the SQLite file; parent directory is created automatically |
| `REDLINE_LOG_LEVEL` | No | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `REDLINE_REBALANCE_TIME_UTC` | No | `00:00` | Target rebalance time (stored in config; not enforced by the run command) |
| `REDLINE_TARGET_VOL` | No | `0.25` | Annualised portfolio volatility target (0.25 = 25%) |
| `REDLINE_MAX_DRAWDOWN` | No | `0.30` | Circuit breaker threshold: halt at 30% peak-to-trough drawdown |
| `REDLINE_MAX_DAILY_LOSS` | No | `0.05` | Circuit breaker threshold: halt at 5% single-day equity loss |
| `REDLINE_MIN_TRADE_USD` | No | `50` | Skip any order whose notional is below this amount |
| `REDLINE_MAX_GROSS_NOTIONAL` | No | `1.00` | Hard cap on total exposure as a fraction of equity (1.0 = 100%, no leverage) |
| `REDLINE_MAX_PER_ASSET_NOTIONAL` | No | `0.70` | Hard cap on any single asset as a fraction of equity |
| `REDLINE_SLACK_WEBHOOK_URL` | No | _(empty)_ | Slack Incoming Webhook URL; if unset, alerts are logged locally but not sent |
| `REDLINE_ALLOW_TEST_OVERRIDES` | No | `false` | Must be `true` **and** `REDLINE_ENV=dev` before `--force-*` CLI flags are accepted |

---

## Operating Modes

### Dry-run mode

Enabled by `REDLINE_DRY_RUN=true` (the default) or the `--dry-run` flag.

**Dry-run must never call any Coinbase order endpoint.** Planned orders are logged to stdout as structured JSON lines and saved to the database, but nothing is submitted. Safe to run at any time, including in production for verification.

### Live mode

`REDLINE_DRY_RUN=false` with valid Coinbase credentials. Submits market orders via the Coinbase Advanced Trade API, polls for fill confirmation, and halts on any unresolved order.

### Mock mode

`REDLINE_MOCK=true` (or automatic when `COINBASE_API_KEY` is empty). Replaces all Coinbase API calls with deterministic synthetic data:

- **Candles**: 260 daily candles per product, fixed random seed (BTC seed=42, ETH seed=43)
- **Balances**: $10,000 USD, 0 BTC, 0 ETH
- **Order submission**: returns a mock order ID; polling returns `FILLED` immediately

Use mock mode for local development and CI. It exercises the full code path without credentials.

---

## CLI Reference

```
python -m redline.main <command> [flags]
```

### `run`

Execute one full rebalance cycle and exit.

```bash
python -m redline.main run [--dry-run] [--mock]
    [--force-btc-signal COMPUTED|ON|OFF]
    [--force-eth-signal COMPUTED|ON|OFF]
    [--force-gross-usd FLOAT]
```

| Flag | Default | Description |
|---|---|---|
| `--dry-run` | off | Plan orders without submitting (overrides `REDLINE_DRY_RUN`) |
| `--mock` | off | Use synthetic data (overrides `REDLINE_MOCK`) |
| `--force-btc-signal` | `COMPUTED` | Override BTC SMA200 signal; requires `REDLINE_ALLOW_TEST_OVERRIDES=true` + `REDLINE_ENV=dev` |
| `--force-eth-signal` | `COMPUTED` | Override ETH SMA200 signal; same gate as above |
| `--force-gross-usd` | _(unset)_ | Override target gross notional; 50/50 split when both risk-on, 100% when one; same gate |

### `status`

Print current halt state, last run summary, live balances, last signals, and last targets.

```bash
python -m redline.main status [--mock]
```

### `reset-halt`

Clear a halt and write an audit record to `halt_events`.

```bash
python -m redline.main reset-halt --reason "TEXT" [--require-reconcile] [--mock]
```

| Flag | Description |
|---|---|
| `--reason TEXT` | **Required.** Human-readable reason stored in the audit trail |
| `--require-reconcile` | Fetch live balances + prices and compute equity before clearing; exits 2 if fetch fails |

---

## Halting and Reset (Runbook)

### What triggers a halt

| Trigger | Source |
|---|---|
| Daily equity loss > 5% vs prior snapshot | Circuit breaker (`core/risk.py`) |
| Peak-to-trough drawdown > 30% | Circuit breaker (`core/risk.py`) |
| Candle fetch failure (all retries exhausted) | `cmd_run` — exits 1, no halt flag set |
| Live order submission failure | `ExecutionHaltError` from `LiveExecutionPolicy` |
| Live order unresolved after 60 seconds | `ExecutionHaltError` — timeout |
| Live order returned FAILED / CANCELLED / EXPIRED | `ExecutionHaltError` — terminal status |

> **Note:** Candle fetch failure exits with code 1 but does **not** set the SQLite halt flag. All other triggers above do set the halt flag.

### Hard-gate behavior

Once `halt_state.halt_flag = 1`, **every subsequent `run` exits immediately** (status `SKIPPED`, `skip_reason = "HALTED"`) after:

1. Logging a `HALT_GATING_ACTIVE` JSON event to stdout
2. Sending a `HALT_GATING_ACTIVE` Slack alert (if configured)
3. Saving a SKIPPED run record to the database

No candles are fetched, no equity is computed, no orders are planned or submitted. The only way to resume trading is `reset-halt`.

### How to reset a halt

```bash
# Basic reset
python -m redline.main reset-halt --reason "reviewed drawdown, positions verified manually"

# Reset only if live data is fetchable (safer)
python -m redline.main reset-halt \
    --reason "equity recovered above circuit breaker threshold" \
    --require-reconcile
```

`--require-reconcile` fetches live candles and balances before clearing. If that fetch fails, it exits with code 2 and leaves the halt in place.

### Where to look for evidence

| Evidence | Location |
|---|---|
| Slack alert | `HALT_TRIGGERED`, `RISK_CIRCUIT_BREAKER`, `HALT_GATING_ACTIVE` |
| Structured logs | stdout JSON lines — look for `circuit_breaker_triggered`, `execution_halt`, `HALT_GATING_ACTIVE` |
| Current halt state | `SELECT * FROM halt_state;` |
| Full audit trail | `SELECT * FROM halt_events ORDER BY ts_utc DESC LIMIT 20;` |
| Affected run record | `SELECT run_id, status, halt_reason FROM runs WHERE halt_flag=1 ORDER BY ts_utc DESC;` |

---

## Live Trading Checklist

> **WARNING: This bot trades real money. Circuit breakers exist but cannot guarantee against loss. Start small.**

### Before going live

- [ ] `REDLINE_DRY_RUN=true` dry-run completes without errors
- [ ] `python -m redline.main status` shows the expected live balances
- [ ] Coinbase API key has order read + write permissions on the correct portfolio
- [ ] `COINBASE_PORTFOLIO_ID` is set (recommended to scope the key)
- [ ] `REDLINE_MIN_TRADE_USD=50` is appropriate for your account size
- [ ] `REDLINE_ALLOW_TEST_OVERRIDES=false` (or unset) in production config
- [ ] `REDLINE_ENV=prod` in production config
- [ ] DB backup path is configured (see Deployment)
- [ ] Slack webhook is set so you receive halt alerts

### Recommended first live trade

Use test override flags in dev to force a small, deterministic trade before relying on market signals. See [`docs/mvp/TESTING.md`](docs/mvp/TESTING.md) for full flag semantics, examples, and a step-by-step runbook.

```bash
# Force a $100 ETH buy (requires $100+ USD available)
REDLINE_ALLOW_TEST_OVERRIDES=true REDLINE_ENV=dev \
    python -m redline.main run \
    --force-eth-signal ON --force-btc-signal OFF --force-gross-usd 100
```

Verify the order appears in your Coinbase account and in the `orders` table. Then switch to normal live mode:

```bash
REDLINE_DRY_RUN=false python -m redline.main run
```

---

## Deployment (GCP VM)

### systemd timer (recommended)

**1. Store credentials** in a file readable only by the service user:

```bash
sudo mkdir -p /etc/redline
sudo chmod 700 /etc/redline
# Create /etc/redline/redline.env with your production env vars
sudo chmod 600 /etc/redline/redline.env
```

**2. Service unit** — `/etc/systemd/system/redline.service`:

```ini
[Unit]
Description=Redline daily rebalance

[Service]
Type=oneshot
User=redline
EnvironmentFile=/etc/redline/redline.env
WorkingDirectory=/opt/redline
ExecStart=/opt/redline/.venv/bin/python -m redline.main run
StandardOutput=journal
StandardError=journal
```

**3. Timer unit** — `/etc/systemd/system/redline.timer`:

```ini
[Unit]
Description=Redline daily rebalance timer

[Timer]
OnCalendar=*-*-* 00:05:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now redline.timer
```

### Logs

```bash
journalctl -u redline.service -f          # live tail
journalctl -u redline.service --since today  # today's runs
```

All output is structured JSON (one object per line), readable by Cloud Logging if stdout is forwarded.

### DB backup

The SQLite file is a single file. Copy it to GCS after each run:

```bash
# Add to ExecStartPost= in the service unit, or run as a separate timer
gsutil cp "$REDLINE_DB_PATH" "gs://your-bucket/redline-$(date +%Y%m%d).db"
```

### Alerts

Slack alerts fire for: `HALT_TRIGGERED`, `RISK_CIRCUIT_BREAKER`, `REBALANCE_COMPLETED`, `REBALANCE_SKIPPED`, `HALT_GATING_ACTIVE`, `TEST_OVERRIDE_ACTIVE`. Set `REDLINE_SLACK_WEBHOOK_URL` in your environment file. If unset, a single `ALERT_DISABLED` log line is emitted per run (no network call is made).

---

## Known Gaps

Mismatches between `SPEC.md` and current code behavior. These are documented here rather than silently corrected.

### 1. Circuit breaker liquidation is always dry-run

**SPEC says:** "If a circuit breaker triggers, issue Market SELL orders to liquidate all BTC/ETH."
**Code does:** Uses `DryRunPolicy` for the liquidation path regardless of `REDLINE_DRY_RUN`. In live mode, the liquidation sell orders are **logged but never submitted to Coinbase.**
**Impact:** If a circuit breaker triggers in live mode, you will be alerted and the halt flag will be set, but positions will not be automatically liquidated. Manual action is required.

### 2. `reconcile_check()` is never called in the run loop

**SPEC says:** "If reconciliation mismatch > $5 or > 0.1% of equity → halt."
**Code:** `core/reconcile.py` contains `reconcile_check(db_equity, live_equity)` but it is not called from `main.py`. The bot computes live equity from fetched balances but never compares it against the prior run's stored equity.
**Impact:** The reconciliation kill switch described in the spec is not active.

### 3. Stale price data check not implemented

**SPEC says:** "Stale price data > 5 minutes" is a kill switch condition.
**Code:** No timestamp staleness check exists. The bot uses the last daily candle close as the current price without validating recency.

### 4. Post-trade reconciliation not implemented

**SPEC says:** "Reconcile with Coinbase balances after the order batch completes."
**Code:** After live orders are filled, the bot saves the pre-trade equity snapshot and exits. No re-fetch of balances or re-computation of equity is performed post-trade.

### 5. SPEC execution order differs from code order

**SPEC order:** Step 2 = reconcile balances → Step 4 = fetch candles.
**Code order:** Candles are fetched first (step 2a) because the last candle close price is needed to compute equity. Balances are fetched immediately after (step 2b). The net result is the same, but the ordering is inverted from the spec.

### 6. Features not in SPEC

The following were added as operational safeguards beyond the original spec:

- Hard-gate halt behavior (every run exits until `reset-halt` is run)
- `halt_events` audit table
- `reset-halt` CLI command
- Test override flags (`--force-btc-signal`, `--force-eth-signal`, `--force-gross-usd`)
- `HALT_GATING_ACTIVE` and `TEST_OVERRIDE_ACTIVE` Slack alert events
