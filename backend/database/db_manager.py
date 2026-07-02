"""Simple SQLite-backed database manager for trading bot state.

The manager provides basic persistence helpers for trades and positions so the
later strategy and execution modules can persist their state without a full ORM.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from backend.database.models import PerformanceSnapshot, Position, Trade


class DatabaseManager:
    """Handle storage for trades, positions, and simple performance snapshots."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or str(Path("/data") / "trading_bot.db")

    def _now(self) -> datetime:
        """Return the current timestamp in UTC."""
        return datetime.now(timezone.utc)

    def _connect(self) -> sqlite3.Connection:
        """Create a SQLite connection to the configured database path."""
        if self.db_path == ":memory:":
            connection = sqlite3.connect(
                "file::memory:?cache=shared",
                uri=True,
                check_same_thread=False,
            )
        else:
            db_path = Path(self.db_path)
            if not db_path.is_absolute():
                db_path = Path.cwd() / db_path

            parent_dir = db_path.parent
            if parent_dir and not parent_dir.exists():
                try:
                    parent_dir.mkdir(parents=True, exist_ok=True)
                except PermissionError:
                    fallback_path = Path.cwd() / db_path.name
                    print(
                        f"WARNING: unable to create database directory {parent_dir}; "
                        f"falling back to local path {fallback_path}"
                    )
                    db_path = fallback_path
                    parent_dir = db_path.parent
                    if not parent_dir.exists():
                        parent_dir.mkdir(parents=True, exist_ok=True)
            elif parent_dir and not os.access(parent_dir, os.W_OK):
                fallback_path = Path.cwd() / db_path.name
                print(
                    f"WARNING: database directory {parent_dir} is not writable; "
                    f"falling back to local path {fallback_path}"
                )
                db_path = fallback_path
                parent_dir = db_path.parent
                if not parent_dir.exists():
                    parent_dir.mkdir(parents=True, exist_ok=True)

            connection = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def init_db(self) -> None:
        """Create storage tables if they do not already exist."""
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pnl REAL,
                    notes TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    quantity INTEGER NOT NULL,
                    average_price REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    side TEXT NOT NULL,
                    unrealized_pnl REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS performance_snapshots (
                    date TEXT PRIMARY KEY,
                    net_pnl REAL NOT NULL,
                    trades_count INTEGER NOT NULL,
                    win_rate REAL NOT NULL,
                    equity REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def insert_trade(self, trade: Trade) -> None:
        """Insert a trade record into the database."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trades (
                    id, symbol, side, quantity, price, timestamp, strategy, status, pnl, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.id,
                    trade.symbol,
                    trade.side,
                    trade.quantity,
                    trade.price,
                    trade.timestamp.isoformat(),
                    trade.strategy,
                    trade.status,
                    trade.pnl,
                    trade.notes,
                ),
            )
            connection.commit()

    def list_trades(self) -> List[Trade]:
        """Retrieve all trades from the database."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, symbol, side, quantity, price, timestamp, strategy, status, pnl, notes FROM trades ORDER BY timestamp"
            ).fetchall()

        return [
            Trade(
                id=row["id"],
                symbol=row["symbol"],
                side=row["side"],
                quantity=row["quantity"],
                price=row["price"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                strategy=row["strategy"],
                status=row["status"],
                pnl=row["pnl"],
                notes=row["notes"],
            )
            for row in rows
        ]

    def upsert_position(self, position: Position) -> None:
        """Insert or update a position entry."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO positions (
                    symbol, quantity, average_price, entry_time, side, unrealized_pnl
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    quantity=excluded.quantity,
                    average_price=excluded.average_price,
                    entry_time=excluded.entry_time,
                    side=excluded.side,
                    unrealized_pnl=excluded.unrealized_pnl
                """,
                (
                    position.symbol,
                    position.quantity,
                    position.average_price,
                    position.entry_time.isoformat(),
                    position.side,
                    position.unrealized_pnl,
                ),
            )
            connection.commit()

    def list_positions(self) -> List[Position]:
        """Retrieve all stored positions."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT symbol, quantity, average_price, entry_time, side, unrealized_pnl FROM positions ORDER BY symbol"
            ).fetchall()

        return [
            Position(
                symbol=row["symbol"],
                quantity=row["quantity"],
                average_price=row["average_price"],
                entry_time=datetime.fromisoformat(row["entry_time"]),
                side=row["side"],
                unrealized_pnl=row["unrealized_pnl"],
            )
            for row in rows
        ]

    def save_performance_snapshot(self, snapshot: PerformanceSnapshot) -> None:
        """Persist a performance summary snapshot."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO performance_snapshots (
                    date, net_pnl, trades_count, win_rate, equity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    net_pnl=excluded.net_pnl,
                    trades_count=excluded.trades_count,
                    win_rate=excluded.win_rate,
                    equity=excluded.equity,
                    created_at=excluded.created_at
                """,
                (
                    snapshot.date,
                    snapshot.net_pnl,
                    snapshot.trades_count,
                    snapshot.win_rate,
                    snapshot.equity,
                    snapshot.created_at.isoformat(),
                ),
            )
            connection.commit()

    def list_performance_snapshots(self) -> List[PerformanceSnapshot]:
        """Retrieve stored performance snapshots."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT date, net_pnl, trades_count, win_rate, equity, created_at FROM performance_snapshots ORDER BY date"
            ).fetchall()

        return [
            PerformanceSnapshot(
                date=row["date"],
                net_pnl=row["net_pnl"],
                trades_count=row["trades_count"],
                win_rate=row["win_rate"],
                equity=row["equity"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]
