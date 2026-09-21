"""SQLite persistence for settings, trades, positions, tokens, and intents."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.database.models import Position, Trade


class DatabaseManager:
    def __init__(self, db_path: str = "data/trading_bot.db") -> None:
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        if db_path != ":memory:":
            import os
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def init_db(self) -> None:
        c = self._connect()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                symbol TEXT,
                side TEXT,
                quantity INTEGER,
                price REAL,
                timestamp TEXT,
                strategy TEXT,
                status TEXT,
                pnl REAL,
                notes TEXT,
                entry_time TEXT,
                entry_price REAL,
                exit_time TEXT,
                exit_price REAL,
                initial_stop REAL,
                final_stop REAL,
                atr_at_entry REAL,
                rsi_at_entry REAL,
                choppiness_at_entry REAL,
                volume_ratio REAL,
                ema20_at_entry REAL,
                ema50_at_entry REAL,
                trend_bias TEXT,
                conditions_checked TEXT,
                exit_reason TEXT,
                gross_pnl REAL,
                net_pnl REAL,
                brokerage REAL,
                stt REAL,
                pnl_r REAL,
                trade_duration_min REAL
            );
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                quantity INTEGER,
                average_price REAL,
                entry_time TEXT,
                instrument_key TEXT
            );
            CREATE TABLE IF NOT EXISTS order_intents (
                signal_id TEXT PRIMARY KEY,
                payload TEXT,
                broker_order_id TEXT,
                status TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS performance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT,
                payload TEXT
            );
            """
        )
        c.commit()

    def save_setting(self, key: str, value: str) -> None:
        self._connect().execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self._connect().commit()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._connect().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def load_settings_blob(self) -> Optional[Dict[str, Any]]:
        raw = self.get_setting("settings_blob", "")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def save_settings_blob(self, blob: Dict[str, Any]) -> None:
        self.save_setting("settings_blob", json.dumps(blob))

    def insert_trade(self, trade: Trade) -> None:
        ts = trade.timestamp.isoformat() if isinstance(trade.timestamp, datetime) else str(trade.timestamp)
        self._connect().execute(
            """INSERT OR REPLACE INTO trades
               (id, symbol, side, quantity, price, timestamp, strategy, status, pnl, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.id, trade.symbol, trade.side, trade.quantity, trade.price, ts,
                trade.strategy, trade.status, trade.pnl, trade.notes,
            ),
        )
        self._connect().commit()

    def list_trades(self) -> List[Trade]:
        rows = self._connect().execute("SELECT * FROM trades ORDER BY timestamp").fetchall()
        out: List[Trade] = []
        for r in rows:
            ts = r["timestamp"]
            try:
                tsv = datetime.fromisoformat(ts) if ts else self._now()
            except Exception:
                tsv = self._now()
            out.append(Trade(
                id=r["id"], symbol=r["symbol"], side=r["side"], quantity=r["quantity"],
                price=r["price"], timestamp=tsv, strategy=r["strategy"] or "",
                status=r["status"] or "", pnl=r["pnl"], notes=r["notes"] or "",
            ))
        return out

    def get_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        row = self._connect().execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return dict(row) if row else None

    def record_trade_entry_details(self, trade_id: str, **fields: Any) -> None:
        allowed = {
            "entry_time", "entry_price", "initial_stop", "atr_at_entry", "rsi_at_entry",
            "choppiness_at_entry", "volume_ratio", "ema20_at_entry", "ema50_at_entry",
            "trend_bias", "conditions_checked",
        }
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        vals = list(cols.values()) + [trade_id]
        self._connect().execute(f"UPDATE trades SET {sets} WHERE id=?", vals)
        self._connect().commit()

    def update_trade_exit(self, trade_id: str, **fields: Any) -> None:
        allowed = {
            "exit_time", "exit_price", "exit_reason", "gross_pnl", "net_pnl",
            "brokerage", "stt", "pnl_r", "trade_duration_min", "final_stop",
        }
        cols = {k: v for k, v in fields.items() if k in allowed}
        if "net_pnl" in cols:
            cols["pnl"] = cols["net_pnl"]
            cols["status"] = "closed"
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        vals = list(cols.values()) + [trade_id]
        self._connect().execute(f"UPDATE trades SET {sets} WHERE id=?", vals)
        self._connect().commit()

    def upsert_position(self, position: Position) -> None:
        ts = position.entry_time.isoformat() if isinstance(position.entry_time, datetime) else str(position.entry_time)
        self._connect().execute(
            """INSERT INTO positions(symbol, quantity, average_price, entry_time, instrument_key)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 quantity=excluded.quantity,
                 average_price=excluded.average_price,
                 entry_time=excluded.entry_time,
                 instrument_key=excluded.instrument_key""",
            (position.symbol, position.quantity, position.average_price, ts, position.instrument_key),
        )
        self._connect().commit()

    def list_positions(self) -> List[Position]:
        rows = self._connect().execute("SELECT * FROM positions").fetchall()
        out: List[Position] = []
        for r in rows:
            ts = r["entry_time"]
            try:
                tsv = datetime.fromisoformat(ts) if ts else self._now()
            except Exception:
                tsv = self._now()
            out.append(Position(
                symbol=r["symbol"], quantity=r["quantity"], average_price=r["average_price"],
                entry_time=tsv, instrument_key=r["instrument_key"] or "",
            ))
        return out

    def get_open_positions(self) -> List[Position]:
        return [p for p in self.list_positions() if p.quantity != 0]

    def delete_position(self, symbol: str) -> None:
        self._connect().execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self._connect().commit()

    def list_performance_snapshots(self) -> List[Dict[str, Any]]:
        rows = self._connect().execute("SELECT * FROM performance_snapshots ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ── tokens ──────────────────────────────────────────────
    def _decode_jwt_claims(self, token: str) -> Dict[str, Any]:
        try:
            from backend.broker.token_resolver import decode_jwt_safe
            return decode_jwt_safe(token)
        except Exception:
            return {}

    def save_token(
        self,
        token: str,
        verified: bool = False,
        source: str = "",
        verified_at: Optional[str] = None,
    ) -> bool:
        token = (token or "").strip().strip('"').strip("'")
        if not token:
            return False
        claims = self._decode_jwt_claims(token)
        exp = float(claims.get("expires_at") or 0)
        iat = float(claims.get("issued_at") or 0)
        now = time.time()
        existing = self.get_setting("upstox_access_token", "")
        if existing:
            old = self._decode_jwt_claims(existing)
            old_exp = float(old.get("expires_at") or 0)
            old_iat = float(old.get("issued_at") or 0)
            old_valid = old_exp == 0 or old_exp > now
            new_expired = exp and exp <= now
            if old_valid and new_expired:
                return False
            if not verified and old_iat and iat and iat < old_iat:
                return False
        self.save_setting("upstox_access_token", token)
        self.save_setting("upstox_token_verified", "true" if verified else "false")
        self.save_setting("upstox_token_source", source or "")
        if verified_at:
            self.save_setting("upstox_token_verified_at", verified_at)
        if exp:
            self.save_setting("upstox_token_exp", str(exp))
        return True

    def load_token(self, require_valid: bool = False) -> Optional[str]:
        token = self.get_setting("upstox_access_token", "")
        if not token:
            return None
        if require_valid:
            claims = self._decode_jwt_claims(token)
            exp = float(claims.get("expires_at") or 0)
            if exp and exp <= time.time():
                return None
        return token

    def save_order_intent(self, signal_id: str, payload: Dict[str, Any], status: str = "INTENT") -> None:
        self._connect().execute(
            """INSERT INTO order_intents(signal_id, payload, status, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(signal_id) DO UPDATE SET payload=excluded.payload""",
            (signal_id, json.dumps(payload), status, self._now().isoformat()),
        )
        self._connect().commit()

    def get_order_intent(self, signal_id: str) -> Optional[Dict[str, Any]]:
        row = self._connect().execute("SELECT * FROM order_intents WHERE signal_id=?", (signal_id,)).fetchone()
        return dict(row) if row else None

    def update_order_intent(self, signal_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [signal_id]
        self._connect().execute(f"UPDATE order_intents SET {sets} WHERE signal_id=?", vals)
        self._connect().commit()
