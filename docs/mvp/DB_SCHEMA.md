# Database Schema

Redline persists all state to a single SQLite file. The path is set by `REDLINE_DB_PATH` (default: `./data/redline.db`). The parent directory is created automatically on first run.

Tables are created with `CREATE TABLE IF NOT EXISTS` on every startup. Column additions use `ALTER TABLE ... ADD COLUMN` wrapped in `try/except`, so they are safe to run against existing databases.

---

## Tables

### `runs`

One row per execution of `python -m redline.main run`. Includes all computed values and the final disposition of the run.

| Column | Type | Notes |
|---|---|---|
| `run_id` | TEXT PRIMARY KEY | 8-character hex prefix of a UUID4 |
| `ts_utc` | TEXT NOT NULL | ISO-8601 UTC timestamp of the run |
| `status` | TEXT NOT NULL | `ok` \| `halted` \| `skipped` \| `error` |
| `dry_run` | INTEGER NOT NULL | `1` = dry-run, `0` = live |
| `equity_usd` | REAL | Computed equity at run time (0.0 for SKIPPED halt-gate runs) |
| `peak_equity_usd` | REAL | Historical peak equity at run time |
| `drawdown` | REAL | `(peak − equity) / peak` at run time |
| `halt_flag` | INTEGER DEFAULT 0 | `1` if this run triggered or was blocked by a halt |
| `halt_reason` | TEXT | Human-readable halt reason (NULL if not halted) |
| `skip_reason` | TEXT | `risk_off_both` \| `min_trade_threshold` \| `at_target` \| `HALTED`; NULL if orders were placed |
| `signals_json` | TEXT | `{"BTC-USD": true, "ETH-USD": false}` |
| `vols_json` | TEXT | `{"BTC-USD": 0.72, "ETH-USD": 0.85}` |
| `targets_json` | TEXT | `{"BTC-USD": 4200.0, "ETH-USD": 0.0}` |
| `planned_orders_json` | TEXT | JSON array of `PlannedOrder.to_dict()` |
| `submitted_orders_json` | TEXT | JSON array of `SubmittedOrder.to_dict()` (live only; `[]` for dry-run) |
| `positions_json` | TEXT | `{"USD": 5000.0, "BTC": 0.05, "ETH": 0.0}` at run time |
| `force_btc_signal` | TEXT | `COMPUTED` \| `ON` \| `OFF`; added via migration, NULL on old rows read back as `COMPUTED` |
| `force_eth_signal` | TEXT | Same as above for ETH |
| `force_gross_usd` | REAL | NULL unless `--force-gross-usd` was set |

**`skip_reason` values:**

| Value | Meaning |
|---|---|
| `risk_off_both` | Both SMA200 signals were risk-off; targets = 0; no sells needed |
| `min_trade_threshold` | Deltas exist but all are below the $50 minimum |
| `at_target` | Already positioned at target; no orders needed |
| `HALTED` | Halt gate fired; run exited before any computation |
| NULL | At least one order was planned (and executed or logged) |

---

### `equity_history`

One row per completed run. Used to compute `daily_loss` and track `peak_equity` across runs.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `run_id` | TEXT NOT NULL | Foreign reference to `runs.run_id` (not enforced by FK) |
| `ts_utc` | TEXT NOT NULL | ISO-8601 UTC timestamp |
| `equity_usd` | REAL NOT NULL | Equity at this point in time |

Only written for runs that reach the persist step (i.e., not for SKIPPED halt-gate runs).

---

### `halt_state`

Singleton table (always exactly one row with `id = 1`). Represents the current halt status of the bot.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY CHECK (id = 1) | Always 1; enforced by check constraint |
| `halt_flag` | INTEGER DEFAULT 0 | `1` = halted, `0` = operational |
| `halt_reason` | TEXT | Reason string set when halted; NULL when cleared |
| `updated_utc` | TEXT | ISO-8601 timestamp of the last `set_halt()` or `clear_halt()` call |

The row is seeded with `INSERT OR IGNORE INTO halt_state (id, halt_flag) VALUES (1, 0)` on every startup.

---

### `halt_events`

Append-only audit trail. One row every time a halt is triggered or cleared.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `ts_utc` | TEXT NOT NULL | ISO-8601 UTC timestamp |
| `event_type` | TEXT NOT NULL | `HALT_TRIGGERED` \| `HALT_RESET` |
| `run_id` | TEXT | The `run_id` that triggered or cleared the halt; NULL if not available |
| `reason` | TEXT NOT NULL | The halt reason (HALT_TRIGGERED) or the operator-supplied justification (HALT_RESET) |
| `details_json` | TEXT | Optional JSON with extra context: equity, drawdown, error text, previous halt info |

**HALT_TRIGGERED** rows are written by `store.set_halt()`, which is called from:
- Circuit breaker block in `cmd_run`
- `ExecutionHaltError` handler in `cmd_run`

**HALT_RESET** rows are written by `cmd_reset_halt` before calling `store.clear_halt()`.

This table is never updated or deleted.

---

### `orders`

One row per submitted live order. Written immediately after a successful POST to Coinbase, before polling begins (crash-safe).

| Column | Type | Notes |
|---|---|---|
| `order_id` | TEXT PRIMARY KEY | Coinbase-assigned order ID |
| `client_order_id` | TEXT NOT NULL | UUID4 hex generated client-side; used for server-side deduplication |
| `run_id` | TEXT NOT NULL | The `run_id` that submitted this order |
| `product_id` | TEXT NOT NULL | `BTC-USD` \| `ETH-USD` |
| `side` | TEXT NOT NULL | `buy` \| `sell` |
| `base_size` | REAL | Crypto quantity (SELL orders); NULL for BUY |
| `quote_size_usd` | REAL | USD amount (BUY orders); NULL for SELL |
| `status` | TEXT NOT NULL | `pending` → `filled` \| `failed` \| `timeout` |
| `submitted_at` | TEXT NOT NULL | ISO-8601 UTC timestamp of the POST |
| `filled_at` | TEXT | ISO-8601 UTC timestamp of fill confirmation; NULL until filled |

**Idempotency:** Before submitting an order, `LiveExecutionPolicy` checks `order_exists_for_run(run_id, product_id)`. If a row already exists, the submit is skipped. This prevents duplicate orders if the process crashes and restarts mid-run.

---

## How to Inspect the DB

### Open the database

```bash
sqlite3 ./data/redline.db
```

### List all tables

```sql
.tables
```

### Inspect a table's schema

```sql
PRAGMA table_info(runs);
PRAGMA table_info(orders);
PRAGMA table_info(halt_events);
```

### Latest run

```sql
SELECT run_id, ts_utc, status, skip_reason, equity_usd, drawdown
FROM runs
ORDER BY ts_utc DESC
LIMIT 1;
```

### Current halt state

```sql
SELECT halt_flag, halt_reason, updated_utc
FROM halt_state
WHERE id = 1;
```

### Last 10 orders

```sql
SELECT order_id, run_id, product_id, side, status, submitted_at, filled_at
FROM orders
ORDER BY submitted_at DESC
LIMIT 10;
```

### Last 20 halt and reset events

```sql
SELECT ts_utc, event_type, run_id, reason, details_json
FROM halt_events
ORDER BY ts_utc DESC
LIMIT 20;
```

### All SKIPPED runs (with reason)

```sql
SELECT run_id, ts_utc, skip_reason, halt_reason
FROM runs
WHERE status = 'skipped'
ORDER BY ts_utc DESC;
```

### Equity history (last 30 days)

```sql
SELECT ts_utc, equity_usd
FROM equity_history
ORDER BY ts_utc DESC
LIMIT 30;
```

### Pretty-print JSON columns

```bash
sqlite3 -json ./data/redline.db \
  "SELECT run_id, signals_json, targets_json FROM runs ORDER BY ts_utc DESC LIMIT 1;" \
  | python3 -m json.tool
```

---

## Migration Notes

The following columns were added to `runs` after the initial schema and are applied via idempotent `ALTER TABLE` migrations on every startup:

| Column | Added when |
|---|---|
| `skip_reason` | Phase 1 → Phase 2 transition |
| `submitted_orders_json` | Phase 2 (live execution) |
| `force_btc_signal` | Test override flags feature |
| `force_eth_signal` | Test override flags feature |
| `force_gross_usd` | Test override flags feature |

The `halt_events` table was added after the initial schema and is created via `CREATE TABLE IF NOT EXISTS` — no migration needed for existing databases.

Rows written before these migrations will have `NULL` in the new columns. The application reads them back with safe defaults:
- `force_btc_signal`: NULL → `"COMPUTED"`
- `force_eth_signal`: NULL → `"COMPUTED"`
- `force_gross_usd`: NULL → `None`
- `skip_reason`: NULL → displayed as `"none (orders placed)"`
- `submitted_orders_json`: NULL → `[]`
