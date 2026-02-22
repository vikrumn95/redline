"""SQLite persistence layer."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from redline.core.types import EquitySnapshot, RunRecord, RunStatus

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    ts_utc            TEXT NOT NULL,
    status            TEXT NOT NULL,
    dry_run           INTEGER NOT NULL,
    equity_usd        REAL,
    peak_equity_usd   REAL,
    drawdown          REAL,
    halt_flag         INTEGER DEFAULT 0,
    halt_reason       TEXT,
    signals_json      TEXT,
    vols_json         TEXT,
    targets_json      TEXT,
    planned_orders_json TEXT,
    positions_json    TEXT
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
            conn.execute("INSERT OR IGNORE INTO halt_state (id, halt_flag) VALUES (1, 0)")
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
                 halt_flag, halt_reason, signals_json, vols_json, targets_json,
                 planned_orders_json, positions_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    json.dumps(record.signals),
                    json.dumps(record.vols),
                    json.dumps(record.targets),
                    json.dumps(record.planned_orders),
                    json.dumps(record.positions),
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
            signals=json.loads(row["signals_json"] or "{}"),
            vols=json.loads(row["vols_json"] or "{}"),
            targets=json.loads(row["targets_json"] or "{}"),
            planned_orders=json.loads(row["planned_orders_json"] or "[]"),
            positions=json.loads(row["positions_json"] or "{}"),
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

    def set_halt(self, reason: str) -> None:
        from redline.timeutil import utcnow_iso

        with self._conn() as conn:
            conn.execute(
                "UPDATE halt_state SET halt_flag=1, halt_reason=?, updated_utc=? WHERE id=1",
                (reason, utcnow_iso()),
            )
            conn.commit()

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
