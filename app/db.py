"""Tiny SQLite storage layer: settings, runtime state, cycles, positions, logs."""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Optional

import sqlite3

from .config import BotSettings
from .models import Cycle, Position, now_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cycles (
    id TEXT PRIMARY KEY,
    side TEXT NOT NULL,
    signal_ts INTEGER,
    opened_ts INTEGER,
    closed_ts INTEGER,
    status TEXT,
    swing_high REAL,
    swing_low REAL,
    goal_pct REAL,
    target REAL,
    avg_entry REAL,
    close_price REAL,
    close_reason TEXT,
    positions_count INTEGER,
    qty REAL,
    volume_usd REAL,
    pnl REAL,
    pnl_pct REAL,
    fees REAL,
    max_adverse_pnl_usd REAL NOT NULL DEFAULT 0,
    max_adverse_pnl_pct REAL NOT NULL DEFAULT 0,
    peak_notional_usd REAL NOT NULL DEFAULT 0,
    peak_margin_usd REAL NOT NULL DEFAULT 0,
    max_deposit_load_pct REAL NOT NULL DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    side TEXT NOT NULL,
    ts INTEGER,
    price REAL,
    qty REAL,
    volume_usd REAL,
    fee REAL,
    multiplier REAL,
    close_ts INTEGER,
    close_price REAL,
    close_fee REAL,
    pnl REAL
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    params TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_positions_cycle ON positions(cycle_id);
CREATE INDEX IF NOT EXISTS idx_cycles_closed_ts ON cycles(closed_ts);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            cycle_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(cycles)")}
            for name in ("max_adverse_pnl_usd", "max_adverse_pnl_pct", "peak_notional_usd",
                         "peak_margin_usd", "max_deposit_load_pct"):
                if name not in cycle_columns:
                    self.conn.execute(f"ALTER TABLE cycles ADD COLUMN {name} REAL NOT NULL DEFAULT 0")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------- kv
    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), now_ms()))
            self.conn.commit()

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    # --------------------------------------------------------------- settings
    def load_settings(self, default: Optional[BotSettings] = None) -> BotSettings:
        base = default or BotSettings()
        data = self.kv_get("settings") or {}
        try:
            return BotSettings(**{**base.model_dump(), **{k: v for k, v in data.items()
                                                          if k in BotSettings.model_fields}})
        except Exception:
            return base

    def save_settings(self, settings: BotSettings) -> None:
        self.kv_set("settings", settings.model_dump())

    def save_runtime_state(self, state: Dict[str, Any]) -> None:
        self.kv_set("runtime_state", state)

    def load_runtime_state(self) -> Dict[str, Any]:
        return self.kv_get("runtime_state") or {}

    # ----------------------------------------------------------------- cycles
    def save_cycle(self, cycle: Cycle) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO cycles(id, side, signal_ts, opened_ts, closed_ts, status, swing_high,
                       swing_low, goal_pct, target, avg_entry, close_price, close_reason,
                       positions_count, qty, volume_usd, pnl, pnl_pct, fees,
                       max_adverse_pnl_usd, max_adverse_pnl_pct, peak_notional_usd,
                       peak_margin_usd, max_deposit_load_pct, note)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       closed_ts=excluded.closed_ts, status=excluded.status, target=excluded.target,
                       swing_low=excluded.swing_low, swing_high=excluded.swing_high,
                       avg_entry=excluded.avg_entry, close_price=excluded.close_price,
                       close_reason=excluded.close_reason, positions_count=excluded.positions_count,
                       qty=excluded.qty, volume_usd=excluded.volume_usd, pnl=excluded.pnl,
                       pnl_pct=excluded.pnl_pct, fees=excluded.fees,
                       max_adverse_pnl_usd=excluded.max_adverse_pnl_usd,
                       max_adverse_pnl_pct=excluded.max_adverse_pnl_pct,
                       peak_notional_usd=excluded.peak_notional_usd,
                       peak_margin_usd=excluded.peak_margin_usd,
                       max_deposit_load_pct=excluded.max_deposit_load_pct""",
                (cycle.id, cycle.side, cycle.signal_ts, cycle.opened_ts, cycle.closed_ts, cycle.status,
                 cycle.swing_high, cycle.swing_low, cycle.goal_pct, cycle.target, cycle.avg_entry,
                 cycle.close_price, cycle.close_reason, len(cycle.positions), cycle.total_qty,
                 cycle.invested_volume_usd, cycle.pnl, cycle.pnl_pct, cycle.fees,
                 cycle.max_adverse_pnl_usd, cycle.max_adverse_pnl_pct, cycle.peak_notional_usd,
                 cycle.peak_margin_usd, cycle.max_deposit_load_pct, cycle.note))
            self.conn.commit()

    def save_position(self, cycle: Cycle, pos: Position) -> None:
        with self._lock:
            row = self.conn.execute(
                "SELECT id FROM positions WHERE cycle_id=? AND idx=?", (cycle.id, pos.idx)).fetchone()
            if row is None:
                self.conn.execute(
                    """INSERT INTO positions(cycle_id, idx, side, ts, price, qty, volume_usd, fee,
                           multiplier, close_ts, close_price, close_fee, pnl)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cycle.id, pos.idx, cycle.side, pos.ts, pos.price, pos.qty, pos.volume_usd,
                     pos.fee, pos.multiplier, pos.close_ts, pos.close_price, pos.close_fee, pos.pnl))
            else:
                self.conn.execute(
                    """UPDATE positions SET close_ts=?, close_price=?, close_fee=?, pnl=?, qty=?,
                           price=?, volume_usd=?, fee=? WHERE id=?""",
                    (pos.close_ts, pos.close_price, pos.close_fee, pos.pnl, pos.qty, pos.price,
                     pos.volume_usd, pos.fee, row["id"]))
            self.conn.commit()

    def list_cycles(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM cycles ORDER BY COALESCE(closed_ts, opened_ts) DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def cycle_stats_since(self, since_ts: int) -> Dict[str, Any]:
        """Aggregate closed-cycle results from an inclusive timestamp."""
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS cycles,
                          SUM(CASE WHEN pnl >= 0 THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,
                          COALESCE(SUM(pnl), 0) AS pnl
                   FROM cycles WHERE status='closed' AND closed_ts >= ?""",
                (int(since_ts),)).fetchone()
        cycles = int(row["cycles"] or 0)
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        pnl = float(row["pnl"] or 0.0)
        return {"cycles": cycles, "wins": wins, "losses": losses, "pnl": pnl,
                "win_rate": round(wins / cycles * 100.0, 2) if cycles else 0.0}

    def all_positions(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM positions ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------- logs
    def log(self, level: str, message: str) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO logs(ts, level, message) VALUES(?,?,?)",
                              (now_ms(), level, message))
            self.conn.commit()

    def tail_logs(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def trim_logs(self, keep: int = 5000) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
                (keep,))
            self.conn.commit()

    # -------------------------------------------------------------- backtests
    def save_backtest(self, params: Dict[str, Any], result: Dict[str, Any]) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO backtests(created_at, params, result) VALUES(?,?,?)",
                (now_ms(), json.dumps(params, ensure_ascii=False), json.dumps(result, ensure_ascii=False)))
            self.conn.commit()
            return int(cur.lastrowid)

    def list_backtests(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, created_at, params FROM backtests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["params"] = json.loads(d["params"])
            except Exception:
                d["params"] = {}
            out.append(d)
        return out

    def get_backtest(self, bt_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM backtests WHERE id=?", (bt_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["params"] = json.loads(d["params"])
        d["result"] = json.loads(d["result"])
        return d
