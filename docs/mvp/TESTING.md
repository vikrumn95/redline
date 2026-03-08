# MVP Live Testing — Test Override Flags

> **Dev/testing only.** This document describes CLI flags and procedures for forcing specific trade scenarios during MVP verification. These flags must never be enabled in a production deployment.

---

## Table of Contents

1. [Purpose](#1-purpose)
2. [Safety Gates](#2-safety-gates)
3. [Flag Reference](#3-flag-reference)
4. [Detailed Semantics](#4-detailed-semantics)
5. [Practical Examples](#5-practical-examples)
6. [Interaction with Halt Behavior](#6-interaction-with-halt-behavior)
7. [Recommended Test Workflow](#7-recommended-test-workflow)
8. [Gotchas and Troubleshooting](#8-gotchas-and-troubleshooting)

---

## 1. Purpose

The `run` subcommand supports three CLI flags (`--force-btc-signal`, `--force-eth-signal`, `--force-gross-usd`) that let you override the bot's normal SMA200 signal computation and volatility-based sizing for a single run. They exist so that, during MVP testing, you can force a predictable, small buy or sell without waiting for the market to produce the right conditions.

**These flags are for dev/testing only.** They are gated by environment variables that must be explicitly set. They should never be present in a production environment file.

---

## 2. Safety Gates

### Required environment variables

Both of the following must be set for any `--force-*` flag to be accepted:

```bash
REDLINE_ALLOW_TEST_OVERRIDES=true   # explicit opt-in
REDLINE_ENV=dev                     # environment tag must be "dev"
```

`REDLINE_ENV` defaults to `dev` if unset (see `config.py:53`), so in practice only `REDLINE_ALLOW_TEST_OVERRIDES=true` is strictly required on a fresh local install. However, **set both explicitly** when testing to match the code's intent and to make overrides visible in logs.

### What happens if the gates are missing

The check occurs in `main.py:187` immediately after the halt check, before any data is fetched:

```
if not (cfg.allow_test_overrides and cfg.env == "dev"):
    print("Error: test override flags require REDLINE_ALLOW_TEST_OVERRIDES=true "
          "and REDLINE_ENV=dev", file=sys.stderr)
    sys.exit(1)
```

The process exits with code 1. No candles are fetched, no balances are read, no orders are planned.

### Dry-run vs live during testing

`REDLINE_DRY_RUN` defaults to `true`. The `--dry-run` CLI flag also forces dry-run for that invocation. Dry-run is additive: either the env var or the flag is sufficient.

| Scenario | Behavior |
|---|---|
| `REDLINE_DRY_RUN=true` (default) | Orders are planned and logged; nothing is submitted to Coinbase |
| `--dry-run` flag | Same as above, for a single run |
| `REDLINE_DRY_RUN=false` (no flag) | Orders are submitted live to Coinbase |

**Always run a dry-run first** to confirm the planned order looks correct before going live.

### Audit trail

Whenever any override flag is active, two things happen regardless of dry-run mode:

- A `TEST_OVERRIDE_ACTIVE` structured log line is written to stdout with the flag values.
- A `TEST_OVERRIDE_ACTIVE` Slack alert is sent (if `REDLINE_SLACK_WEBHOOK_URL` is configured).

The flags used are also persisted in the `runs` table (`force_btc_signal`, `force_eth_signal`, `force_gross_usd` columns) and are shown by `python -m redline.main status` under "Last Run Overrides".

---

## 3. Flag Reference

All three flags belong to the `run` subcommand.

### `--force-btc-signal`

| | |
|---|---|
| **Type** | Choice: `COMPUTED` \| `ON` \| `OFF` |
| **Default** | `COMPUTED` |
| **Gate** | `REDLINE_ALLOW_TEST_OVERRIDES=true` + `REDLINE_ENV=dev` |

Replaces the BTC-USD SMA200 signal for this run. `COMPUTED` means the flag is inactive and the real signal is used. Requires the gate env vars only when the value is `ON` or `OFF`.

### `--force-eth-signal`

| | |
|---|---|
| **Type** | Choice: `COMPUTED` \| `ON` \| `OFF` |
| **Default** | `COMPUTED` |
| **Gate** | Same as above |

Same as `--force-btc-signal` but for ETH-USD.

### `--force-gross-usd`

| | |
|---|---|
| **Type** | `float` (USD amount) |
| **Default** | unset (None) |
| **Gate** | Same as above |

Replaces the volatility-parity target notional calculation entirely. When set, `_compute_forced_targets` is called instead of `compute_target_notionals`. The value is the total desired gross exposure across all risk-on assets for this run.

**What `--force-gross-usd` does NOT change:**

- It does not change your actual account balances or equity.
- It does not change `equity_usd` used for circuit breaker evaluation.
- It does not bypass the per-asset cap (`REDLINE_MAX_PER_ASSET_NOTIONAL`) or gross cap (`REDLINE_MAX_GROSS_NOTIONAL`). Both caps are enforced against `equity` even when this flag is set (see `_compute_forced_targets` in `main.py:101-121`).
- It does not bypass `REDLINE_MIN_TRADE_USD`. The resulting delta still has to clear $50 (default) after rounding.
- It does not affect SMA200 computation (which still runs and is logged before overrides are applied).

---

## 4. Detailed Semantics

### Signal overrides: what they change

Signal overrides replace the value of `signals[product_id]` — a boolean — after the SMA200 is computed. The pipeline continues normally from that point: inverse-vol weights are computed from actual volatility, sizing caps are applied against live equity, and `plan_orders` computes the delta versus live balances.

The volatility values (`vols`) are never overridden. They come from real candle data even in override mode.

### Signal = ON

The bot treats the asset as "risk-on": it should be held. Target notional for that asset becomes non-zero (either from vol-parity sizing or from `--force-gross-usd`). If you currently hold less than the target, a **BUY** order is planned. If you already hold the target amount (within $50), nothing happens.

### Signal = OFF

The bot treats the asset as "risk-off": target notional is 0. If you hold any of that asset, a **SELL** order is planned to bring the position to zero. If you hold nothing, nothing happens.

### Both signals OFF (`--force-btc-signal OFF --force-eth-signal OFF`)

Both targets become 0. The bot plans SELL orders for any BTC or ETH currently held. This is the "go flat" / liquidation test. The `skip_reason` logged at the end will be `risk_off_both` if you were already flat.

### One signal ON, one OFF

The risk-on asset gets 100% of the gross allocation. For example, `--force-eth-signal ON --force-btc-signal OFF` with `--force-gross-usd 60` sets `target_notionals = {"ETH-USD": 60.0, "BTC-USD": 0.0}`. Any BTC held is sold; ETH is bought up to $60 target.

### Both signals ON

The gross allocation is split 50/50 between BTC and ETH (after applying the per-asset cap). With `--force-gross-usd 100`, each asset gets a $50 target notional (subject to the 70% equity cap).

### `--force-gross-usd` cap logic (from `main.py:114-121`)

```
gross = min(force_gross_usd, max_gross_notional * equity)

# Two risk-on assets:
per_asset = min(gross / 2.0, max_per_asset_notional * equity)

# One risk-on asset:
target = min(gross, max_per_asset_notional * equity)
```

If `force_gross_usd` exceeds 100% of equity (default cap), it is silently clamped. If `gross / 2` exceeds 70% of equity (default per-asset cap), each asset is clamped to 70%.

### When `--force-gross-usd` is omitted

If the signal override flags are used without `--force-gross-usd`, the normal volatility-parity sizing runs on the overridden signal set. This means the trade size will be determined by realized volatility and `REDLINE_TARGET_VOL` (default 25% annualized). For small accounts, this will likely produce a larger trade than the $50+ minimum; for very small accounts, it may be below the cap. Use `--force-gross-usd` when you want full control over the size.

### Order side determination (`plan_orders` in `core/portfolio.py`)

- **BUY**: uses `quote_size_usd` (USD amount), rounded down to `quote_increment` (e.g. `0.01`).
- **SELL**: uses `base_size` (crypto quantity), rounded down to `base_increment` (e.g. `0.00000001` for BTC). The sell quantity is the delta between current holdings and target (zero), rounded down. The rounded notional must still be ≥ `min_trade_usd`.

---

## 5. Practical Examples

### Prerequisites for all live examples

```bash
export REDLINE_ALLOW_TEST_OVERRIDES=true
export REDLINE_ENV=dev
export REDLINE_DRY_RUN=false   # omit this line to stay in dry-run
```

---

### Example 1: Force a small ETH buy (dry-run first)

**Step 1 — dry-run to confirm the plan:**

```bash
REDLINE_ALLOW_TEST_OVERRIDES=true \
REDLINE_ENV=dev \
  python -m redline.main run --dry-run \
  --force-eth-signal ON \
  --force-btc-signal OFF \
  --force-gross-usd 60
```

**What this means:** Treat ETH as risk-on, BTC as risk-off, and target a $60 ETH position. Any existing BTC is sold; ETH is bought to reach $60.

**Expected output:** A `dry_run_planned_order` log line with `product_id=ETH-USD`, `side=buy`, `quote_size_usd` close to 60.00. If no order appears, see [Section 8](#8-gotchas-and-troubleshooting).

**Step 2 — go live:**

```bash
REDLINE_ALLOW_TEST_OVERRIDES=true \
REDLINE_ENV=dev \
REDLINE_DRY_RUN=false \
  python -m redline.main run \
  --force-eth-signal ON \
  --force-btc-signal OFF \
  --force-gross-usd 60
```

**Expected behavior:** One market buy order for ETH-USD is submitted to Coinbase. The bot polls every 2 seconds (backing off to 30s max) until the order is `FILLED`. On success: `order_submitted`, `order_filled`, and `REBALANCE_COMPLETED` log lines appear. The run record is saved to the DB.

---

### Example 2: Force a small BTC buy

```bash
REDLINE_ALLOW_TEST_OVERRIDES=true \
REDLINE_ENV=dev \
REDLINE_DRY_RUN=false \
  python -m redline.main run \
  --force-btc-signal ON \
  --force-eth-signal OFF \
  --force-gross-usd 60
```

**What this means:** Treat BTC as risk-on, ETH as risk-off, target $60 BTC position. Equivalent to Example 1 but for BTC.

---

### Example 3: Force both assets — buy BTC and ETH

```bash
REDLINE_ALLOW_TEST_OVERRIDES=true \
REDLINE_ENV=dev \
REDLINE_DRY_RUN=false \
  python -m redline.main run \
  --force-btc-signal ON \
  --force-eth-signal ON \
  --force-gross-usd 120
```

**What this means:** Both assets risk-on. The $120 gross is split 50/50: $60 target for BTC, $60 target for ETH (subject to 70% equity cap per asset). If you have no current holdings, two buy orders are submitted sequentially.

---

### Example 4: Force liquidation — sell all to flat

```bash
REDLINE_ALLOW_TEST_OVERRIDES=true \
REDLINE_ENV=dev \
REDLINE_DRY_RUN=false \
  python -m redline.main run \
  --force-btc-signal OFF \
  --force-eth-signal OFF
```

**What this means:** Both assets risk-off. Targets become 0 for both. Any BTC or ETH currently held produces a SELL order. If you are already flat (holding only USD), the run completes with `skip_reason=risk_off_both` and no orders.

> Note: `--force-gross-usd` is ignored when both signals are OFF because there are no risk-on assets to allocate to.

---

### Example 5: Signal override without size override (use vol-parity sizing)

```bash
REDLINE_ALLOW_TEST_OVERRIDES=true \
REDLINE_ENV=dev \
  python -m redline.main run --dry-run \
  --force-eth-signal ON \
  --force-btc-signal OFF
```

**What this means:** Force ETH signal ON, but let the volatility-parity algorithm decide the size based on `REDLINE_TARGET_VOL` and live realized volatility. The resulting order size depends on ETH's annualized vol and your equity. Check the `targets_computed` log line to see what notional was chosen.

---

### Verifying orders in the database

```bash
sqlite3 ./data/redline.db \
  "SELECT order_id, product_id, side, quote_size_usd, base_size, status, submitted_at, filled_at
   FROM orders ORDER BY submitted_at DESC LIMIT 5;"
```

```bash
sqlite3 ./data/redline.db \
  "SELECT run_id, ts_utc, status, skip_reason, force_btc_signal, force_eth_signal, force_gross_usd
   FROM runs ORDER BY ts_utc DESC LIMIT 3;"
```

---

## 6. Interaction with Halt Behavior

**Override flags do not bypass the halt gate.** The halt check runs first in `cmd_run` (`main.py:137-177`). If `halt_flag = 1`, the run exits immediately with status `SKIPPED` before any override logic is evaluated. This is intentional — a halted bot must be manually reviewed and reset before any further orders are placed, including test orders.

**If the bot is halted before you test:**

```bash
# Check current state
python -m redline.main status

# If halted, clear it with a reason (creates an audit record)
python -m redline.main reset-halt --reason "manual test session, positions verified"

# Optionally: reconcile before clearing (safer)
python -m redline.main reset-halt \
  --reason "manual test session, positions verified" \
  --require-reconcile
```

**A test run can trigger a halt.** If a live order fails to fill within 60 seconds, or if Coinbase returns `FAILED`/`CANCELLED`/`EXPIRED`, `LiveExecutionPolicy` raises `ExecutionHaltError`, which sets the halt flag and exits. This is the same behavior as a production run. After investigating, clear the halt before testing again.

**Circuit breakers also still run.** If daily loss > 5% or drawdown > 30%, a circuit breaker fires before any orders are planned. Override flags do not disable circuit breaker evaluation.

---

## 7. Recommended Test Workflow

Follow this sequence for a complete MVP live test. Do not skip the dry-run step.

### Step 1: Check status and balances

```bash
python -m redline.main status
```

Confirm:
- `Halt flag: False` — if halted, run `reset-halt` first.
- USD balance is sufficient for the test trade (need > $50 + buffer for the buy; the minimum is `REDLINE_MIN_TRADE_USD`, default $50).
- No unexpected BTC/ETH positions if you want a clean buy test.

### Step 2: Dry-run the intended trade

```bash
REDLINE_ALLOW_TEST_OVERRIDES=true \
REDLINE_ENV=dev \
  python -m redline.main run --dry-run \
  --force-eth-signal ON \
  --force-btc-signal OFF \
  --force-gross-usd 60
```

Look for `dry_run_planned_order` in stdout. Confirm `product_id`, `side=buy`, and `quote_size_usd`. If you see `dry_run_no_orders`, check [Section 8](#8-gotchas-and-troubleshooting).

### Step 3: Execute the live buy

```bash
REDLINE_ALLOW_TEST_OVERRIDES=true \
REDLINE_ENV=dev \
REDLINE_DRY_RUN=false \
  python -m redline.main run \
  --force-eth-signal ON \
  --force-btc-signal OFF \
  --force-gross-usd 60
```

Watch for `order_submitted` → `order_filled` → `REBALANCE_COMPLETED`.

### Step 4: Verify in Coinbase and DB

- Check your Coinbase portfolio for the ETH position.
- Query the `orders` table (see examples in Section 5).
- Check Slack if `REDLINE_SLACK_WEBHOOK_URL` is set — expect a `REBALANCE_COMPLETED` alert tagged with `TEST_OVERRIDE`.

### Step 5: Run status again

```bash
python -m redline.main status
```

Confirm `Halt flag: False` (no halt triggered), and the "Last Run Overrides" section shows the flags you used.

### Step 6: Sell back to flat (unwind)

```bash
REDLINE_ALLOW_TEST_OVERRIDES=true \
REDLINE_ENV=dev \
REDLINE_DRY_RUN=false \
  python -m redline.main run \
  --force-btc-signal OFF \
  --force-eth-signal OFF
```

This sets both targets to 0. The bot sells the ETH acquired in Step 3 back to USD.

### Step 7: Check halt state and final status

```bash
python -m redline.main status
```

If anything went wrong (order timeout, fill failure), the halt flag will be set. Investigate before clearing.

### Step 8 (if halted): Reset and investigate

```bash
# Check what triggered the halt
sqlite3 ./data/redline.db \
  "SELECT ts_utc, event_type, reason FROM halt_events ORDER BY ts_utc DESC LIMIT 10;"

# Clear the halt once you have verified positions are safe
python -m redline.main reset-halt --reason "test session complete, position verified manually"
```

---

## 8. Gotchas and Troubleshooting

### No orders are planned (dry-run shows `dry_run_no_orders`)

| Cause | How to diagnose |
|---|---|
| Already at target | You already hold the target amount. Check balances in `status`. The `skip_reason` will be `at_target`. |
| Both signals OFF | If you passed both `--force-btc-signal OFF` and `--force-eth-signal OFF`, targets are 0 and there is nothing to buy. `skip_reason=risk_off_both`. |
| Delta below $50 minimum | The difference between current and target notional is less than `REDLINE_MIN_TRADE_USD`. The `skip_reason` will be `min_trade_threshold`. Try increasing `--force-gross-usd`. |
| No available USD | Your USD balance is 0 or below the minimum trade size. Check `status`. |
| Already flat with both OFF | Forcing both OFF when you hold nothing = no orders. Expected behavior. |

### Order is planned but smaller than expected

- `--force-gross-usd` is capped to `100% * equity` for gross and `70% * equity` per asset. If equity is small, the cap silently reduces the order.
- Rounding. `quote_size_usd` is rounded down to the nearest `quote_increment` (e.g. 0.01 USD). Values close to $50 may round to below the minimum and be dropped.
- Use a value slightly above $50 (e.g., $55 or $60) to leave room for rounding.

### Override flags are rejected (`exit 1`)

- Either `REDLINE_ALLOW_TEST_OVERRIDES` is not `true` or `REDLINE_ENV` is not `dev`.
- Check: `echo $REDLINE_ALLOW_TEST_OVERRIDES $REDLINE_ENV` — if those are blank, the `.env` file is not being picked up or you need to export them in the shell.

### Order submitted but no `order_filled` event

- The order timed out (60 seconds). The bot set the halt flag with reason `order_timeout:<order_id>`.
- Check your Coinbase account — the order may still be open. Cancel it manually if needed.
- Run `reset-halt` after verifying positions, then investigate why the order did not fill.

### Bot is in mock mode unexpectedly

- Mock mode activates automatically when `COINBASE_API_KEY` is empty (`config.py:41`). In mock mode, orders are fake — no real trades occur. Check that `COINBASE_API_KEY` and `COINBASE_API_SECRET` are set in your `.env` and that `REDLINE_MOCK` is not set to `true`.
- The `run_start` log line includes `"mock": true/false` — check this first.

### Circuit breaker fires before orders are planned

- If daily loss > 5% or drawdown > 30%, the circuit breaker fires and the bot halts before reaching override logic. Override flags do not suppress circuit breakers.
- Check `status` to see the drawdown figure. If the circuit breaker is stale from a prior test, reset the halt.

### Sell produces smaller quantity than expected

- SELL orders use `base_size` (crypto quantity) rounded down to `base_increment`. Small ETH or BTC positions that round to zero after applying the increment are dropped.
- The rounded quantity's notional must also be ≥ $50. If the position is too small, it will not be sold.

---

## Notes on discrepancies with `quick_live_test.txt`

The earlier `quick_live_test.txt` file is consistent with the current implementation. One clarification:

- The note in `quick_live_test.txt` says "the override flags gate on both `REDLINE_ALLOW_TEST_OVERRIDES=true` and `REDLINE_ENV=dev` — if either is missing the run exits 1". This is accurate per `main.py:187`. However, the default value of `REDLINE_ENV` is `"dev"` (not `"prod"`), so a fresh local install without any `.env` only requires `REDLINE_ALLOW_TEST_OVERRIDES=true`. Set `REDLINE_ENV=dev` explicitly anyway — it makes the requirement visible.
