"""SQLite persistence layer."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from redline.core.types import EquitySnapshot, OrderSide, RunRecord, RunStatus, SubmittedOrder

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    run_id                TEXT PRIMARY KEY,
    ts_utc                TEXT NOT NULL,
    status                TEXT NOT NULL,
    dry_run               INTEGER NOT NULL,
    equity_usd            REAL,
    peak_equity_usd       REAL,
    drawdown              REAL,
    halt_flag             INTEGER DEFAULT 0,
    halt_reason           TEXT,
    skip_reason           TEXT,
    signals_json          TEXT,
    vols_json             TEXT,
    targets_json          TEXT,
    planned_orders_json   TEXT,
    submitted_orders_json TEXT,
    positions_json        TEXT
)
"""

_CREATE_EQUITY_HISTORY = """
CREATE TABLE IF NOT EXISTS equity_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    ts_utc     TEXT NOT NULL,
    equity_usd REAL NOT NULL
)
"""

_CREATE_HALT_STATE = """
CREATE TABLE IF NOT EXISTS halt_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    halt_flag   INTEGER DEFAULT 0,
    halt_reason TEXT,
    updated_utc TEXT
)
"""

_CREATE_HALT_EVENTS = """
CREATE TABLE IF NOT EXISTS halt_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc       TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    run_id       TEXT,
    reason       TEXT NOT NULL,
    details_json TEXT
)
"""

_CREATE_ORDERS = """
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    side            TEXT NOT NULL,
    base_size       REAL,
    quote_size_usd  REAL,
    status          TEXT NOT NULL,
    submitted_at    TEXT NOT NULL,
    filled_at       TEXT
)
"""


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(db_path)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(_CREATE_RUNS)
            conn.execute(_CREATE_EQUITY_HISTORY)
            conn.execute(_CREATE_HALT_STATE)
            conn.execute(_CREATE_HALT_EVENTS)
            conn.execute(_CREATE_ORDERS)
            conn.execute("INSERT OR IGNORE INTO halt_state (id, halt_flag) VALUES (1, 0)")
            # Migrations: add new columns to existing DBs
            for migration in (
                "ALTER TABLE runs ADD COLUMN skip_reason TEXT",
                "ALTER TABLE runs ADD COLUMN submitted_orders_json TEXT",
                "ALTER TABLE runs ADD COLUMN force_btc_signal TEXT",
                "ALTER TABLE runs ADD COLUMN force_eth_signal TEXT",
                "ALTER TABLE runs ADD COLUMN force_gross_usd REAL",
            ):
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # column already exists
            conn.commit()

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def save_run(self, record: RunRecord) -> None:
        status = record.status.value if isinstance(record.status, RunStatus) else record.status
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs
                (run_id, ts_utc, status, dry_run, equity_usd, peak_equity_usd, drawdown,
                 halt_flag, halt_reason, skip_reason, signals_json, vols_json, targets_json,
                 planned_orders_json, submitted_orders_json, positions_json,
                 force_btc_signal, force_eth_signal, force_gross_usd)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.run_id,
                    record.ts_utc,
                    status,
                    int(record.dry_run),
                    record.equity_usd,
                    record.peak_equity_usd,
                    record.drawdown,
                    int(record.halt_flag),
                    record.halt_reason,
                    record.skip_reason,
                    json.dumps(record.signals),
                    json.dumps(record.vols),
                    json.dumps(record.targets),
                    json.dumps(record.planned_orders),
                    json.dumps(record.submitted_orders),
                    json.dumps(record.positions),
                    record.force_btc_signal,
                    record.force_eth_signal,
                    record.force_gross_usd,
                ),
            )
            conn.commit()

    def load_last_run(self) -> RunRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM runs ORDER BY ts_utc DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return RunRecord(
            run_id=row["run_id"],
            ts_utc=row["ts_utc"],
            status=RunStatus(row["status"]),
            dry_run=bool(row["dry_run"]),
            equity_usd=float(row["equity_usd"] or 0.0),
            peak_equity_usd=float(row["peak_equity_usd"] or 0.0),
            drawdown=float(row["drawdown"] or 0.0),
            halt_flag=bool(row["halt_flag"]),
            halt_reason=row["halt_reason"],
            skip_reason=row["skip_reason"],
            signals=json.loads(row["signals_json"] or "{}"),
            vols=json.loads(row["vols_json"] or "{}"),
            targets=json.loads(row["targets_json"] or "{}"),
            planned_orders=json.loads(row["planned_orders_json"] or "[]"),
            submitted_orders=json.loads(row["submitted_orders_json"] or "[]"),
            positions=json.loads(row["positions_json"] or "{}"),
            force_btc_signal=row["force_btc_signal"] or "COMPUTED",
            force_eth_signal=row["force_eth_signal"] or "COMPUTED",
            force_gross_usd=float(row["force_gross_usd"]) if row["force_gross_usd"] is not None else None,
        )

    # ------------------------------------------------------------------
    # Equity history
    # ------------------------------------------------------------------

    def save_equity_snapshot(self, snap: EquitySnapshot) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO equity_history (run_id, ts_utc, equity_usd) VALUES (?,?,?)",
                (snap.run_id, snap.ts_utc, snap.equity_usd),
            )
            conn.commit()

    def load_equity_history(self, days: int = 2) -> list[EquitySnapshot]:
        """Return up to `days` most recent equity snapshots, newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT run_id, ts_utc, equity_usd FROM equity_history ORDER BY ts_utc DESC LIMIT ?",
                (days,),
            ).fetchall()
        return [
            EquitySnapshot(run_id=r["run_id"], ts_utc=r["ts_utc"], equity_usd=float(r["equity_usd"]))
            for r in rows
        ]

    def load_peak_equity(self) -> float | None:
        with self._conn() as conn:
            row = conn.execute("SELECT MAX(peak_equity_usd) AS peak FROM runs").fetchone()
        if row is None or row["peak"] is None:
            return None
        return float(row["peak"])

    # ------------------------------------------------------------------
    # Halt state
    # ------------------------------------------------------------------

    def set_halt(
        self,
        reason: str,
        run_id: str | None = None,
        details_json: str | None = None,
    ) -> None:
        from redline.timeutil import utcnow_iso

        with self._conn() as conn:
            conn.execute(
                "UPDATE halt_state SET halt_flag=1, halt_reason=?, updated_utc=? WHERE id=1",
                (reason, utcnow_iso()),
            )
            conn.commit()
        self.log_halt_event("HALT_TRIGGERED", reason, run_id=run_id, details_json=details_json)

    def clear_halt(self) -> None:
        from redline.timeutil import utcnow_iso

        with self._conn() as conn:
            conn.execute(
                "UPDATE halt_state SET halt_flag=0, halt_reason=NULL, updated_utc=? WHERE id=1",
                (utcnow_iso(),),
            )
            conn.commit()

    def get_halt_state(self) -> tuple[bool, str | None]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT halt_flag, halt_reason FROM halt_state WHERE id=1"
            ).fetchone()
        if row is None:
            return False, None
        return bool(row["halt_flag"]), row["halt_reason"]

    def get_halt_state_full(self) -> tuple[bool, str | None, str | None]:
        """Return (halt_flag, halt_reason, halted_at) where halted_at is the updated_utc timestamp."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT halt_flag, halt_reason, updated_utc FROM halt_state WHERE id=1"
            ).fetchone()
        if row is None:
            return False, None, None
        return bool(row["halt_flag"]), row["halt_reason"], row["updated_utc"]

    def log_halt_event(
        self,
        event_type: str,
        reason: str,
        run_id: str | None = None,
        details_json: str | None = None,
    ) -> None:
        from redline.timeutil import utcnow_iso

        with self._conn() as conn:
            conn.execute(
                "INSERT INTO halt_events (ts_utc, event_type, run_id, reason, details_json) VALUES (?,?,?,?,?)",
                (utcnow_iso(), event_type, run_id, reason, details_json),
            )
            conn.commit()

    def load_halt_events(self, limit: int = 10) -> list[dict]:
        """Return halt_events rows, newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts_utc, event_type, run_id, reason, details_json FROM halt_events ORDER BY ts_utc DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Orders (Phase 2)
    # ------------------------------------------------------------------

    def save_order(self, order: SubmittedOrder) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO orders
                (order_id, client_order_id, run_id, product_id, side,
                 base_size, quote_size_usd, status, submitted_at, filled_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    order.order_id,
                    order.client_order_id,
                    order.run_id,
                    order.product_id,
                    order.side.value,
                    order.base_size,
                    order.quote_size_usd,
                    order.status,
                    order.submitted_at,
                    order.filled_at,
                ),
            )
            conn.commit()

    def update_order_status(
        self, order_id: str, status: str, filled_at: str | None = None
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE orders SET status=?, filled_at=? WHERE order_id=?",
                (status, filled_at, order_id),
            )
            conn.commit()

    def order_exists_for_run(self, run_id: str, product_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE run_id=? AND product_id=?",
                (run_id, product_id),
            ).fetchone()
        return row[0] > 0

    def load_orders_for_run(self, run_id: str) -> list[SubmittedOrder]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE run_id=? ORDER BY submitted_at",
                (run_id,),
            ).fetchall()
        return [
            SubmittedOrder(
                order_id=r["order_id"],
                client_order_id=r["client_order_id"],
                run_id=r["run_id"],
                product_id=r["product_id"],
                side=OrderSide(r["side"]),
                base_size=float(r["base_size"]) if r["base_size"] is not None else None,
                quote_size_usd=float(r["quote_size_usd"]) if r["quote_size_usd"] is not None else None,
                status=r["status"],
                submitted_at=r["submitted_at"],
                filled_at=r["filled_at"],
            )
            for r in rows
        ]
