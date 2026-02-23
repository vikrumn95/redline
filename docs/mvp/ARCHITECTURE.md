# Redline MVP Architecture

## Component Diagram

```
python -m redline.main run
        │
        ▼
┌──────────────────────────────────────────────────────────────────┐
│  Startup                                                         │
│  ├─ generate run_id (8-char hex)                                 │
│  ├─ RunLogger (structured JSON → stdout)                         │
│  ├─ Store(db_path)  ←── SQLite                                   │
│  └─ AlertManager(slack_webhook)                                  │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  Halt Gate                                                       │
│  store.get_halt_state_full()                                     │
│  if halt_flag=1 → log HALT_GATING_ACTIVE                        │
│                 → alert HALT_GATING_ACTIVE                       │
│                 → save SKIPPED run                               │
│                 → exit 0                                         │
└──────────────┬───────────────────────────────────────────────────┘
               │ (not halted)
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  Override Gate (only if --force-* flags present)                 │
│  require: REDLINE_ALLOW_TEST_OVERRIDES=true + REDLINE_ENV=dev    │
│  if gate fails → print error → exit 1                            │
│  if gate passes → log + alert TEST_OVERRIDE_ACTIVE               │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  Data Fetch (CoinbaseDataProvider)                               │
│  ├─ fetch_candles(BTC-USD, 250+)  ──► prices["BTC"] = last close │
│  └─ fetch_candles(ETH-USD, 250+)  ──► prices["ETH"] = last close │
│                                                                  │
│  (paginated backward; exponential backoff on 429 / 5xx)         │
│  candle fetch failure → alert + exit 1 (no halt flag set)        │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  Equity + Circuit Breakers                                       │
│  CoinbaseSpotBroker.fetch_balances()                             │
│  compute_equity(balances, prices)                                │
│    = USD_avail + BTC_avail×BTC_price + ETH_avail×ETH_price       │
│                                                                  │
│  evaluate_circuit_breakers(equity, peak_equity, history)         │
│  ├─ daily_loss  = (prior_equity - equity) / prior_equity         │
│  │  triggered if daily_loss > REDLINE_MAX_DAILY_LOSS (5%)        │
│  └─ drawdown    = (peak_equity - equity) / peak_equity           │
│     triggered if drawdown > REDLINE_MAX_DRAWDOWN (30%)           │
│                                                                  │
│  if triggered:                                                   │
│    plan_liquidation_orders → DryRunPolicy  ← always dry-run (!)  │
│    store.set_halt(reason)                                        │
│    save HALTED run record                                        │
│    alert RISK_CIRCUIT_BREAKER                                    │
│    exit 0                                                        │
└──────────────┬───────────────────────────────────────────────────┘
               │ (no breaker)
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  Signal + Volatility                                             │
│  for BTC-USD, ETH-USD:                                           │
│    closes = sorted daily close prices                            │
│    signal = sma200_signal(closes)  → True if close > SMA(200)    │
│    vol    = annualized_vol(closes) → σ_d × √365                  │
│      σ_d = √(Σ(Rᵢ-R̄)² / 19)  [N=20, denominator=19]           │
│                                                                  │
│  apply --force-btc-signal / --force-eth-signal if active         │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  Sizing                                                          │
│  risk_on_assets = {p: vol[p] for p if signal[p]}                 │
│                                                                  │
│  Vol-parity path (default):                                      │
│    weights = inverse_vol_weights(risk_on_assets)                 │
│      w_i ∝ 1/σᵢ  (normalised)                                   │
│    σ_port = Σ(wᵢ × σᵢ)  [correlation=1.0 simplification]        │
│    gross = min(equity × target_vol/σ_port,  equity × max_gross)  │
│    notional_i = min(wᵢ × gross, equity × max_per_asset)          │
│                                                                  │
│  Forced gross path (--force-gross-usd):                          │
│    both risk-on → 50/50 split (each capped at max_per_asset)     │
│    one  risk-on → 100% to that asset (capped at max_per_asset)   │
│    none risk-on → all targets = 0                                │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  Order Planning  (core/portfolio.py)                             │
│  fetch_product(BTC-USD), fetch_product(ETH-USD) → constraints    │
│  plan_orders(balances, target_notionals, prices, constraints)    │
│    delta = target_notional − current_notional                    │
│    BUY  → quote_size_usd (rounded to quote_increment)            │
│    SELL → base_size      (rounded down to base_increment)        │
│    skip if |delta| < REDLINE_MIN_TRADE_USD ($50)                 │
│    at most one order per product per run                         │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  Execution                                                       │
│                                                                  │
│  dry-run:  DryRunPolicy → log each order, return unchanged       │
│                                                                  │
│  live:     LiveExecutionPolicy                                   │
│    for each order:                                               │
│      if order_exists_for_run(run_id, product_id): skip           │
│      POST /orders → SubmittedOrder(status=pending)               │
│      store.save_order(sub)  ← crash-safe, before polling         │
│      poll GET /orders/{id} every ≥2s, up to 60s                 │
│        FILLED  → update status, continue                         │
│        FAILED / CANCELLED / EXPIRED → ExecutionHaltError         │
│        timeout → update status=timeout → ExecutionHaltError      │
│                                                                  │
│    ExecutionHaltError:                                           │
│      store.set_halt(reason)                                      │
│      save HALTED run record                                      │
│      alert HALT_TRIGGERED                                        │
│      exit 0                                                      │
└──────────────┬───────────────────────────────────────────────────┘
               │ (success)
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  Persist + Alert                                                 │
│  store.save_equity_snapshot(equity)                              │
│  store.save_run(RunRecord, status=OK, skip_reason if no orders)  │
│  alerts.send_alert(REBALANCE_COMPLETED)                          │
└──────────────────────────────────────────────────────────────────┘
```

---

## Source Layout

```
src/redline/
├── main.py                     CLI: cmd_run, cmd_status, cmd_reset_halt
├── config.py                   Config dataclass; load_config() from env vars
├── logging.py                  RunLogger — structured JSON lines to stdout
├── timeutil.py                 utcnow_iso()
├── core/
│   ├── types.py                Candle, Balance, PlannedOrder, SubmittedOrder,
│   │                           RunRecord, RunStatus, EquitySnapshot
│   ├── store.py                Store — all SQLite reads and writes
│   ├── risk.py                 evaluate_circuit_breakers, check_daily_loss,
│   │                           check_drawdown
│   ├── portfolio.py            plan_orders, plan_liquidation_orders
│   └── reconcile.py            compute_equity, reconcile_check (unused in run loop)
├── data/
│   ├── candles.py              close_prices() helper
│   └── providers/
│       └── coinbase.py         CoinbaseDataProvider — live + mock implementations
├── brokers/
│   ├── base.py                 BrokerBase abstract class
│   └── coinbase_spot.py        CoinbaseSpotBroker (thin wrapper over CoinbaseDataProvider)
├── execution/
│   ├── errors.py               ExecutionHaltError
│   └── policy.py               DryRunPolicy, LiveExecutionPolicy
├── monitoring/
│   └── alerts.py               AlertManager; ALERT_EVENTS set
├── sizing/
│   ├── risk_parity.py          inverse_vol_weights, portfolio_vol,
│   │                           compute_target_notionals
│   └── volatility.py           log_returns, sample_daily_vol, annualized_vol
└── strategies/
    └── crypto/
        └── btc_eth_trend.py    sma(), sma200_signal()
```

---

## Data Flow

### Inputs fetched from Coinbase each run

| Data | API endpoint | Notes |
|---|---|---|
| Daily candles | `GET /api/v3/brokerage/products/{id}/candles` | 250+ per product; paginated backward |
| Account balances | `GET /api/v3/brokerage/accounts` | Available balance only (not held) |
| Product constraints | `GET /api/v3/brokerage/products/{id}` | `base_increment`, `base_min_size`, `quote_increment` |
| Order status (live only) | `GET /api/v3/brokerage/orders/historical/{id}` | Polled after each submission |

### Computed values

| Value | Formula |
|---|---|
| Current price | Last candle close (not a live feed) |
| Equity | `USD_avail + BTC_avail × BTC_price + ETH_avail × ETH_price` |
| Log returns | `Rₜ = ln(Pₜ / Pₜ₋₁)` |
| Daily vol | `σ_d = √(Σ(Rᵢ−R̄)² / 19)` — N=20 most recent returns, N−1 denominator |
| Annualised vol | `σ_ann = σ_d × √365` |
| Risk signal | `risk_on = (last_close > SMA(200))` — per asset |
| Inverse-vol weights | `wᵢ ∝ 1/σᵢ`, normalised over risk-on assets only |
| Portfolio vol | `σ_port = Σ(wᵢ × σᵢ)` — correlation=1.0 simplification |
| Scaling factor | `target_vol / σ_port` |
| Gross notional | `min(equity × scaling_factor, equity × max_gross)` |
| Per-asset notional | `min(wᵢ × gross, equity × max_per_asset)` |

### What is persisted

| Table | Written when |
|---|---|
| `runs` | End of every run (OK / HALTED / SKIPPED) |
| `equity_history` | End of every run that reaches the persist step |
| `halt_state` | On `set_halt()` and `clear_halt()` |
| `halt_events` | On every `set_halt()` call (HALT_TRIGGERED) and every `reset-halt` (HALT_RESET) |
| `orders` | Immediately after each successful order POST (before polling) |

---

## Invariants

These properties hold in all code paths and must not be violated.

| Invariant | Enforced by |
|---|---|
| **Dry-run never calls order endpoints** | `DryRunPolicy.execute()` — logs orders only, no HTTP calls |
| **`LiveExecutionPolicy` never runs in dry-run mode** | Raises `AssertionError` if called with `dry_run=True` |
| **Circuit breaker liquidation is always dry-run** | `DryRunPolicy` hard-coded in the circuit breaker block of `cmd_run`; see Known Gaps in README |
| **Each live order is persisted before polling begins** | `store.save_order(sub)` is called immediately after a successful POST, before the poll loop |
| **At most one order per product per run** | `order_exists_for_run(run_id, product_id)` pre-check in `LiveExecutionPolicy` |
| **Halt flag persists across process restarts** | Written to `halt_state` in SQLite; checked on every startup |
| **Halt can only be cleared by `reset-halt`** | No code path in `cmd_run` calls `store.clear_halt()` |
| **Equity formula uses available balance only** | `compute_equity` reads `Balance.available`; does not use Coinbase portfolio totals |
| **Override flags are double-gated** | `_overrides_active()` check + `cfg.allow_test_overrides` + `cfg.env == "dev"` |
| **`halt_events` is append-only** | No `DELETE` or `UPDATE` on `halt_events` anywhere in the codebase |
| **`run_id` is present on every log line** | `RunLogger._json_formatter` injects `run_id` into every record |

---

## Operational Behavior

### On startup

1. SQLite is opened (parent directory created if needed); all tables are created with `CREATE TABLE IF NOT EXISTS`; column migrations are applied idempotently with `try/except`
2. Halt gate is evaluated immediately — halted bots exit before touching any external API

### On errors

| Error type | Behavior |
|---|---|
| HTTP 429 / 5xx | Exponential backoff with jitter, up to 5 retries (`MAX_RETRIES = 5`) |
| Candle fetch failure after retries | Alert, `sys.exit(1)` — no halt flag set |
| Circuit breaker triggered | Set halt, save HALTED run, alert, exit 0 |
| `ExecutionHaltError` (order failure/timeout) | Set halt, save HALTED run, alert, exit 0 |
| Order poll network error | Logged as warning; poll loop continues until timeout |
| Signal compute error (insufficient data) | Alert, `sys.exit(1)` — no halt flag set |

### Halt and reset flow

```
normal run → circuit breaker or order failure
                    │
                    ▼
             set_halt(reason)  →  halt_events: HALT_TRIGGERED
             save HALTED run
             alert sent
                    │
         subsequent runs:
                    ▼
             halt gate → SKIPPED run, exit 0 (no API calls)
                    │
    (operator reviews, decides to resume)
                    ▼
    reset-halt --reason "..."
             log_halt_event(HALT_RESET)
             clear_halt()
                    │
         next run proceeds normally
```
