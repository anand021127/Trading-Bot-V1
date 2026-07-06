"""Production SQLite database manager for the Upstox trading bot."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.database.models import PerformanceSnapshot, Position, Trade


class DatabaseManager:
    """Handle all storage for trades, positions, and performance data."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or str(Path("/data") / "trading_bot.db")

    # ─── Connection ───────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            conn = sqlite3.connect("file::memory:?cache=shared", uri=True, check_same_thread=False)
        else:
            db_path = Path(self.db_path)
            if not db_path.is_absolute():
                db_path = Path.cwd() / db_path
            parent = db_path.parent
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except (PermissionError, OSError):
                db_path = Path.cwd() / db_path.name
                db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    # ─── Schema ───────────────────────────────────────────────────────────────

    def init_db(self) -> None:
        """Create all tables if they do not already exist."""
        with self._connect() as conn:
            # Legacy trades table (simple — used by original tests)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    strategy TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'filled',
                    pnl REAL,
                    notes TEXT NOT NULL DEFAULT '',
                    mode TEXT DEFAULT 'paper',
                    entry_time TEXT,
                    exit_time TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    initial_stop REAL,
                    final_stop REAL,
                    exit_reason TEXT,
                    gross_pnl REAL,
                    net_pnl REAL,
                    brokerage REAL,
                    stt REAL,
                    pnl_r REAL,
                    trade_duration_min INTEGER,
                    stage_at_exit INTEGER,
                    orb_high REAL,
                    orb_low REAL,
                    atr_at_entry REAL,
                    rsi_at_entry REAL,
                    choppiness_at_entry REAL,
                    volume_ratio REAL,
                    ema20_at_entry REAL,
                    ema50_at_entry REAL,
                    trend_bias TEXT,
                    max_favorable REAL,
                    max_adverse REAL,
                    conditions_checked TEXT,
                    created_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    quantity INTEGER NOT NULL,
                    average_price REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    side TEXT NOT NULL DEFAULT 'long',
                    unrealized_pnl REAL NOT NULL DEFAULT 0.0,
                    initial_stop REAL,
                    trailing_stop REAL,
                    stage INTEGER DEFAULT 1,
                    trade_id TEXT,
                    mode TEXT DEFAULT 'paper'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS performance_snapshots (
                    date TEXT PRIMARY KEY,
                    net_pnl REAL NOT NULL,
                    trades_count INTEGER NOT NULL,
                    win_rate REAL NOT NULL,
                    equity REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_test_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    test_name TEXT,
                    status TEXT,
                    response_time_ms REAL,
                    error_message TEXT,
                    tested_at TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades(mode)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)
            """)
            conn.commit()

    # ─── Trades ───────────────────────────────────────────────────────────────

    def insert_trade(self, trade: Trade) -> None:
        """Insert a trade record."""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO trades
                   (id, symbol, side, quantity, price, timestamp, strategy, status, pnl, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade.id, trade.symbol, trade.side, trade.quantity,
                    trade.price, trade.timestamp.isoformat(),
                    trade.strategy, trade.status, trade.pnl, trade.notes,
                ),
            )
            conn.commit()

    def list_trades(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        symbol: Optional[str] = None,
        mode: Optional[str] = None,
        exit_reason: Optional[str] = None,
    ) -> List[Any]:
        """List trades with optional filters. Returns sqlite3.Row objects."""
        clauses = []
        params: List[Any] = []

        if date_from:
            clauses.append("(entry_time >= ? OR timestamp >= ?)")
            params.extend([date_from, date_from])
        if date_to:
            clauses.append("(entry_time <= ? OR timestamp <= ?)")
            params.extend([date_to + "T23:59:59", date_to + "T23:59:59"])
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if mode:
            clauses.append("mode = ?")
            params.append(mode)
        if exit_reason:
            clauses.append("exit_reason = ?")
            params.append(exit_reason)

        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"SELECT * FROM trades {where} ORDER BY COALESCE(entry_time, timestamp) DESC"

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return rows

    def get_trade(self, trade_id: str) -> Optional[Any]:
        """Get a single trade by ID."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return row

    # ─── Positions ────────────────────────────────────────────────────────────

    def upsert_position(self, position: Position) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO positions
                   (symbol, quantity, average_price, entry_time, side, unrealized_pnl)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET
                       quantity=excluded.quantity,
                       average_price=excluded.average_price,
                       entry_time=excluded.entry_time,
                       side=excluded.side,
                       unrealized_pnl=excluded.unrealized_pnl""",
                (
                    position.symbol, position.quantity, position.average_price,
                    position.entry_time.isoformat(), position.side, position.unrealized_pnl,
                ),
            )
            conn.commit()

    def list_positions(self) -> List[Any]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM positions ORDER BY symbol").fetchall()

    def delete_position(self, symbol: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
            conn.commit()

    # ─── Performance ──────────────────────────────────────────────────────────

    def save_performance_snapshot(self, snapshot: PerformanceSnapshot) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO performance_snapshots
                   (date, net_pnl, trades_count, win_rate, equity, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                       net_pnl=excluded.net_pnl, trades_count=excluded.trades_count,
                       win_rate=excluded.win_rate, equity=excluded.equity,
                       created_at=excluded.created_at""",
                (
                    snapshot.date, snapshot.net_pnl, snapshot.trades_count,
                    snapshot.win_rate, snapshot.equity, snapshot.created_at.isoformat(),
                ),
            )
            conn.commit()

    def list_performance_snapshots(self) -> List[Any]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM performance_snapshots ORDER BY date"
            ).fetchall()
