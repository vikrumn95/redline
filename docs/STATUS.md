# Redline Status

**Last updated: 2026-03-07** _(update manually when milestones change)_

---

## System Summary

- **What it is:** A daily BTC/ETH spot momentum bot running on Coinbase Advanced Trade. One rebalance per day (00:00 UTC), designed as a run-once-and-exit process for cron/systemd.
- **What it trades:** BTC-USD and ETH-USD spot only. Signal = SMA200 (close > SMA200 → risk-on). Sizing = inverse-vol risk parity targeting 25% annualised portfolio vol.
- **Execution modes:** Dry-run (plans + logs orders, no API calls to order endpoints) and live (submits Coinbase market orders, polls for fill). Both are code-complete.
- **Storage:** SQLite at `./data/redline.db` (5 tables: `runs`, `equity_history`, `halt_state`, `halt_events`, `orders`).
- **Logging:** Structured JSON lines to stdout; every line tagged with `run_id`.
- **Alerting:** Slack Incoming Webhook (`REBALANCE_COMPLETED`, `HALT_TRIGGERED`, `RISK_CIRCUIT_BREAKER`, `HALT_GATING_ACTIVE`, `TEST_OVERRIDE_ACTIVE`). If webhook is unset, logs `ALERT_DISABLED` per run.

---

## Current Milestone — Feature Status

| Feature | Status | Notes |
|---|---|---|
| Dry-run pipeline (end-to-end) | ✅ implemented | 57 tests passing |
| Live Coinbase order execution | ✅ implemented, ⚠️ not live-tested | `LiveExecutionPolicy` is code-complete; no real order placed yet |
| SQLite persistence (all tables) | ✅ implemented | Auto-migrates on startup; idempotent |
| Slack alerts | ✅ implemented | Disabled gracefully if webhook unset |
| Halt gating + `reset-halt` | ✅ implemented | Hard gate: every run exits until manually cleared |
| `halt_events` audit trail | ✅ implemented | Append-only; HALT_TRIGGERED + HALT_RESET rows |
| Test override flags | ✅ implemented | `--force-btc-signal`, `--force-eth-signal`, `--force-gross-usd`; double-gated |
| Circuit breaker liquidation (live) | ⚠️ partial | CB fires correctly and sets halt, but liquidation sells use `DryRunPolicy` even in live mode — positions are NOT auto-sold |
| Post-trade reconciliation | ❌ not implemented | `reconcile_check()` exists in `core/reconcile.py` but is never called from the run loop |
| Stale price data check | ❌ not implemented | Bot uses last daily candle close with no timestamp staleness guard |
| VM deployment (GCP + systemd) | 📄 documented | Systemd unit + timer defined in README; not yet verified in production |
| Mock mode (synthetic data) | ✅ implemented | Auto-enabled when `COINBASE_API_KEY` is empty; writes to `redline-mock.db`; blocked in prod |

---

## How to Run Right Now

```bash
# Activate virtualenv first
source .venv/bin/activate
```

**Check current state (halt status, balances, last run):**
```bash
python -m redline.main status
```

**Dry-run (safe — no orders ever placed):**
```bash
python -m redline.main run --dry-run
```

**Live run (submits real orders — ensure credentials and DRY_RUN=false):**
```bash
REDLINE_DRY_RUN=false python -m redline.main run
```

**Forced test trade — tiny ETH buy, dry-run first:**
```bash
REDLINE_ALLOW_TEST_OVERRIDES=true REDLINE_ENV=dev \
  python -m redline.main run --dry-run \
  --force-eth-signal ON --force-btc-signal OFF --force-gross-usd 60
```

**Forced test trade — live:**
```bash
REDLINE_ALLOW_TEST_OVERRIDES=true REDLINE_ENV=dev REDLINE_DRY_RUN=false \
  python -m redline.main run \
  --force-eth-signal ON --force-btc-signal OFF --force-gross-usd 60
```

**Clear a halt:**
```bash
python -m redline.main reset-halt --reason "reviewed, positions verified manually"
# Safer variant (reconciles before clearing):
python -m redline.main reset-halt --reason "..." --require-reconcile
```

**Run tests:**
```bash
pytest
```

---

## Environment / Config Assumptions

| Variable | Default | Purpose |
|---|---|---|
| `COINBASE_API_KEY` | _(empty)_ | CDP API key (ES256); empty → mock mode auto-enabled |
| `COINBASE_API_SECRET` | _(empty)_ | EC private key PEM; escape newlines as `\n` |
| `COINBASE_PORTFOLIO_ID` | _(unset)_ | Scope API to one portfolio (recommended) |
| `REDLINE_DRY_RUN` | `true` | `false` required for live order submission |
| `REDLINE_MOCK` | `false` | `true` → synthetic data, no Coinbase calls; **blocked when `REDLINE_ENV=prod`** |
| `REDLINE_ENV` | `dev` | Must be `dev` for override flags; set to `prod` in production |
| `REDLINE_DB_PATH` | _(env-dependent)_ | Default: `redline-mock.db` (mock), `redline-dev.db` (dev), `redline.db` (prod/other) |
| `REDLINE_ALLOW_TEST_OVERRIDES` | `false` | Must be `true` + `REDLINE_ENV=dev` to accept `--force-*` flags |
| `REDLINE_SLACK_WEBHOOK_URL` | _(unset)_ | Slack Incoming Webhook; unset = log-only mode |
| `REDLINE_MIN_TRADE_USD` | `50` | Orders below this notional are skipped |
| `REDLINE_MAX_GROSS_NOTIONAL` | `1.00` | Max total exposure as fraction of equity (1.0 = no leverage) |
| `REDLINE_MAX_PER_ASSET_NOTIONAL` | `0.70` | Max single-asset exposure as fraction of equity |
| `REDLINE_TARGET_VOL` | `0.25` | Annualised portfolio vol target (25%) |
| `REDLINE_MAX_DAILY_LOSS` | `0.05` | Circuit breaker: 5% single-day loss → halt |
| `REDLINE_MAX_DRAWDOWN` | `0.30` | Circuit breaker: 30% peak-to-trough → halt |

**For production:** set `REDLINE_ENV=prod`, `REDLINE_ALLOW_TEST_OVERRIDES=false` (or unset), `REDLINE_DRY_RUN=false`, and supply real credentials.

---

## Critical Invariants

- **Dry-run never touches order endpoints.** `DryRunPolicy` logs only; no HTTP calls. `LiveExecutionPolicy` raises `AssertionError` if called in dry-run mode.
- **Halt is a hard gate.** Once `halt_flag=1` in SQLite, every `run` invocation exits immediately (status `SKIPPED`) before fetching any data. Only `reset-halt` clears it.
- **Circuit breaker liquidation is always logged but never live-executed.** This is a known gap: CB fires, sets halt, alerts, but uses `DryRunPolicy` for liquidation sells regardless of `REDLINE_DRY_RUN`. Manual liquidation is required.
- **Equity formula:** `equity = USD_avail + BTC_avail × BTC_price + ETH_avail × ETH_price`. Available balances only; no Coinbase portfolio totals.
- **Price is last daily candle close**, not a live feed. No staleness check.
- **At most one order per product per run.** `order_exists_for_run(run_id, product_id)` guards against duplicates even across restarts.
- **Override flags require double gating:** both `REDLINE_ALLOW_TEST_OVERRIDES=true` AND `REDLINE_ENV=dev`. Missing either → `exit 1` before any data is fetched.
- **`halt_events` is append-only.** No delete/update path exists in the codebase.
- **Candle fetch failure exits with code 1 but does not set the halt flag.** All other failure modes (circuit breaker, execution halt) do set it.

---

## Next Tasks

1. **Execute first live test.** Follow the runbook in `docs/mvp/TESTING.md`: dry-run a forced $60 ETH buy → verify plan → go live → confirm `order_filled` in logs + Coinbase UI + DB → sell back to flat.
2. **Fix circuit breaker liquidation.** The CB path hard-codes `DryRunPolicy`, so positions are not auto-sold in live mode. This is the highest-priority safety gap vs. SPEC.
3. **Deploy and validate VM scheduling.** Provision GCP VM, install systemd unit + timer per README, fire one dry-run at 00:05 UTC and confirm journalctl output + Slack alert.
4. **Wire `reconcile_check()` into the run loop.** The function exists in `core/reconcile.py` but is never called; the SPEC requires a reconciliation mismatch kill switch.
5. **Add stale candle timestamp guard.** SPEC requires halting on price data older than 5 minutes. Currently the bot uses the last daily candle close with no recency check.

---

## Known Gaps / Things to Verify

| Gap | Detail |
|---|---|
| CB liquidation always dry-run | Hard-coded `DryRunPolicy` in circuit breaker path; documented in README Known Gaps §1 |
| `reconcile_check()` is dead code | Function implemented but never called from `cmd_run`; README Known Gaps §2 |
| No stale price guard | Last daily close used as "current price" with no age validation; README Known Gaps §3 |
| No post-trade reconciliation | Balances not re-fetched after order fill; README Known Gaps §4 |
| SPEC step order inverted | SPEC says reconcile balances first, then candles; code fetches candles first (needed for price → equity). Result is equivalent but ordering differs |
| `REDLINE_ENV` default is `dev` | A fresh install without `.env` accepts override flags with only `REDLINE_ALLOW_TEST_OVERRIDES=true`. Explicitly set `REDLINE_ENV=dev` in test, `REDLINE_ENV=prod` in production |
| No live order ever placed | `LiveExecutionPolicy` is code-complete and unit-tested with mocks, but no real Coinbase order has been submitted |

---

## Update Workflow

- Update this file whenever a milestone is completed or a known gap is resolved.
- Record the last completed test (date, what was tested, outcome) and the next intended task.
- Keep it short — this is a checkpoint doc, not a changelog.
