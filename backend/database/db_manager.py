"""SQLite persistence for settings, trades, positions, tokens, and intents.

Connection hygiene (production hardening):
- WAL journal mode + 30s busy_timeout so API/worker concurrent writers wait
  instead of failing with SQLITE_BUSY.
- A single long-lived connection per DatabaseManager instance; ``close()``
  must be called by long-lived processes on shutdown (Windows cannot delete
  an open SQLite file, and open handles block clean cleanup).
- The ``positions.extra`` JSON column persists stop_loss/target/lot_size/
  trade_id so a worker restart after a fill can restore the full exit-risk
  state of a position. (Migration: column added on existing databases.)
- ``save_order_intent`` is INSERT OR IGNORE — a retried insert cannot
  overwrite an intent that already exists, so the pipeline's duplicate
  detection cannot be defeated by a retry.
- ``daily_counters`` holds durable per-day risk state (trades taken, realized
  P&L) so a worker restart cannot reset MAX_TRADES_PER_DAY / MAX_DAILY_LOSS.
- Trade metadata contract (backend/domain/trade_metadata.py): the ``trades``
  table carries the ONE common metadata model (underlying_symbol, option_type,
  strike_price, expiry, instrument_key, lot_size, capital_used, order_id,
  signal_id) written identically by PAPER, LIVE and BACKTEST execution paths.
  capital_used is ALWAYS entry_price × executed_quantity. Migration is purely
  additive (ALTER TABLE ADD COLUMN, PRAGMA-guarded, restart-safe, idempotent);
  historical rows keep NULL and the UI renders "N/A / Historical metadata
  unavailable" — no value is ever invented.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from backend.database.models import Position, Trade

# The ONE common trade metadata model — identical columns for paper, live and
# backtest trades. Kept in sync with backend/domain/trade_metadata.py.
TRADE_META_COLUMNS: tuple = (
    "underlying_symbol",
    "option_type",
    "strike_price",
    "expiry",
    "instrument_key",
    "lot_size",
    "capital_used",
    "order_id",
    "signal_id",
)


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
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            try:
                # WAL allows the API process and worker process to share the DB
                # without failing on concurrent write locks.
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass  # e.g. read-only or :memory: — default journal still works
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit atomic transaction: commit on success, rollback on error.

        SQLite autocommits every statement by default; multi-statement
        sequences must use this to stay all-or-nothing across a crash.
        """
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

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
                trade_duration_min REAL,
                -- ONE common trade metadata model (paper == live == backtest)
                underlying_symbol TEXT,
                option_type TEXT,
                strike_price REAL,
                expiry TEXT,
                instrument_key TEXT,
                lot_size INTEGER,
                capital_used REAL,
                order_id TEXT,
                signal_id TEXT
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
            CREATE TABLE IF NOT EXISTS daily_counters (
                day TEXT PRIMARY KEY,
                trades_taken INTEGER NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0.0
            );
            """
        )
        # Migrations: purely additive, idempotent, restart-safe. Guarded by
        # PRAGMA table_info so they run exactly once per database; existing
        # rows keep NULL for the new columns (never invented values).
        cols = [r["name"] for r in c.execute("PRAGMA table_info(positions)").fetchall()]
        if "extra" not in cols:
            c.execute("ALTER TABLE positions ADD COLUMN extra TEXT")
        trade_cols = {r["name"] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
        for col, decl in (
            ("underlying_symbol", "TEXT"),
            ("option_type", "TEXT"),
            ("strike_price", "REAL"),
            ("expiry", "TEXT"),
            ("instrument_key", "TEXT"),
            ("lot_size", "INTEGER"),
            ("capital_used", "REAL"),
            ("order_id", "TEXT"),
            ("signal_id", "TEXT"),
        ):
            if col not in trade_cols:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {decl}")
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
        """Insert a trade row including the common metadata model.

        Metadata arrives on the Trade dataclass as ``trade_metadata`` (the
        canonical dict from backend/domain/trade_metadata.py) or directly as
        extra keyword-style attributes; anything absent stays NULL. Existing
        callers that construct Trade without metadata are unaffected.
        """
        ts = trade.timestamp.isoformat() if isinstance(trade.timestamp, datetime) else str(trade.timestamp)
        meta = getattr(trade, "trade_metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        values = {
            "id": trade.id,
            "symbol": trade.symbol,
            "side": trade.side,
            "quantity": trade.quantity,
            "price": trade.price,
            "timestamp": ts,
            "strategy": trade.strategy,
            "status": trade.status,
            "pnl": trade.pnl,
            "notes": trade.notes,
            "underlying_symbol": meta.get("underlying_symbol"),
            "option_type": meta.get("option_type"),
            "strike_price": meta.get("strike_price"),
            "expiry": meta.get("expiry"),
            "instrument_key": meta.get("instrument_key"),
            "lot_size": meta.get("lot_size"),
            "capital_used": meta.get("capital_used"),
            "order_id": meta.get("order_id"),
            "signal_id": meta.get("signal_id"),
        }
        cols = list(values.keys())
        placeholders = ", ".join("?" for _ in cols)
        self._connect().execute(
            f"INSERT OR REPLACE INTO trades ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(values[c] for c in cols),
        )
        self._connect().commit()

    def list_trades(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        symbol: Optional[str] = None,
        mode: Optional[str] = None,
        exit_reason: Optional[str] = None,
    ) -> List[Trade]:
        """Return trades with optional filters.

        Filters use only columns that exist on the trades table.
        ``mode`` is accepted for API compatibility but is intentionally
        ignored for SQL (no mode column in the current schema; trading
        mode is owned by settings/environment and paper-mode event paths).
        """
        # mode is intentionally unused for SQL filtering — keep signature for callers.
        _ = mode

        clauses: List[str] = []
        params: List[Any] = []

        # Prefer entry_time when present; fall back to timestamp (both TEXT ISO-ish).
        time_expr = "COALESCE(NULLIF(entry_time, ''), timestamp)"

        if date_from:
            clauses.append(f"{time_expr} >= ?")
            params.append(str(date_from).strip())
        if date_to:
            end = str(date_to).strip()
            # Inclusive end date when only YYYY-MM-DD is provided
            if len(end) == 10 and "T" not in end:
                end = end + "T23:59:59.999999"
            clauses.append(f"{time_expr} <= ?")
            params.append(end)
        if symbol:
            clauses.append("UPPER(COALESCE(symbol, '')) = UPPER(?)")
            params.append(str(symbol).strip())
        if exit_reason:
            clauses.append("COALESCE(exit_reason, '') = ?")
            params.append(str(exit_reason))

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM trades{where} ORDER BY timestamp"
        rows = self._connect().execute(sql, params).fetchall()

        # Return full row dicts (not the lossy subset-Trade) so the API and UI
        # receive the complete common metadata model for every trade. Keys are
        # a superset of the previous Trade dataclass field names, so every
        # existing consumer (t.id, t.symbol, t.price, ...) keeps working.
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            pnl_val = d.get("pnl")
            if d.get("net_pnl") is not None:
                pnl_val = d["net_pnl"]
            d["pnl"] = pnl_val
            out.append(d)
        return out

    def get_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        row = self._connect().execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return dict(row) if row else None

    def record_trade_entry_details(self, trade_id: str, **fields: Any) -> None:
        allowed = {
            "entry_time", "entry_price", "initial_stop", "atr_at_entry", "rsi_at_entry",
            "choppiness_at_entry", "volume_ratio", "ema20_at_entry", "ema50_at_entry",
            "trend_bias", "conditions_checked",
            # common metadata model columns (entry-side)
            "underlying_symbol", "option_type", "strike_price", "expiry",
            "instrument_key", "lot_size", "capital_used", "signal_id",
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
            "order_id",  # exit-leg broker order id
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
        extra_json = json.dumps(position.extra or {}, default=str) if position.extra else None
        self._connect().execute(
            """INSERT INTO positions(symbol, quantity, average_price, entry_time, instrument_key, extra)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 quantity=excluded.quantity,
                 average_price=excluded.average_price,
                 entry_time=excluded.entry_time,
                 instrument_key=excluded.instrument_key,
                 extra=excluded.extra""",
            (position.symbol, position.quantity, position.average_price, ts, position.instrument_key, extra_json),
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
            extra_raw = r["extra"] if "extra" in r.keys() else None
            try:
                extra = json.loads(extra_raw) if extra_raw else {}
                if not isinstance(extra, dict):
                    extra = {}
            except Exception:
                extra = {}
            out.append(Position(
                symbol=r["symbol"], quantity=r["quantity"], average_price=r["average_price"],
                entry_time=tsv, instrument_key=r["instrument_key"] or "",
                extra=extra,
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

    def clear_token(self) -> None:
        """Remove persisted Upstox access token and verification metadata."""
        for key in (
            "upstox_access_token",
            "upstox_token_verified",
            "upstox_token_source",
            "upstox_token_verified_at",
            "upstox_token_exp",
        ):
            try:
                self.save_setting(key, "")
            except Exception:
                pass

    def load_token(self, require_valid: bool = False) -> str:
        """Load the persisted Upstox access token.

        Public contract (established and tested by the OAuth/V3 suites):
        - Returns "" (empty string) when no token is persisted — never None.
          Callers may safely use truthiness checks and `or` fallbacks.
        - With require_valid=True, an expired JWT returns "" (same as absent).
        - Invalid tokens are never stored here (save_token gates persistence),
          so anything returned came from an explicit save and is unchanged.
        """
        token = self.get_setting("upstox_access_token", "")
        if not token:
            return ""
        if require_valid:
            claims = self._decode_jwt_claims(token)
            exp = float(claims.get("expires_at") or 0)
            if exp and exp <= time.time():
                return ""
        return token

    def save_order_intent(self, signal_id: str, payload: Dict[str, Any], status: str = "INTENT") -> None:
        # INSERT OR IGNORE: an intent already recorded for this signal_id is
        # immutable — a retried submit must never refresh its timestamp or
        # payload, otherwise duplicate detection could be defeated by timing.
        self._connect().execute(
            """INSERT OR IGNORE INTO order_intents(signal_id, payload, status, created_at)
               VALUES (?, ?, ?, ?)""",
            (signal_id, json.dumps(payload), status, self._now().isoformat()),
        )
        self._connect().commit()

    def get_order_intent(self, signal_id: str) -> Optional[Dict[str, Any]]:
        row = self._connect().execute("SELECT * FROM order_intents WHERE signal_id=?", (signal_id,)).fetchone()
        return dict(row) if row else None

    def update_order_intent(self, signal_id: str, **fields: Any) -> None:
        if not fields:
            return
        # Never allow an update to clear a broker_order_id that is already
        # recorded — order-id attribution must be monotonic.
        if "broker_order_id" in fields and fields["broker_order_id"] in (None, ""):
            fields = {k: v for k, v in fields.items() if k != "broker_order_id"}
            if not fields:
                return
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [signal_id]
        self._connect().execute(f"UPDATE order_intents SET {sets} WHERE signal_id=?", vals)
        self._connect().commit()

    # ── durable daily risk counters ─────────────────────────────────────
    def get_daily_counters(self, day: str) -> Dict[str, Any]:
        row = self._connect().execute(
            "SELECT trades_taken, realized_pnl FROM daily_counters WHERE day=?", (day,)
        ).fetchone()
        if not row:
            return {"trades_taken": 0, "realized_pnl": 0.0}
        return {"trades_taken": int(row["trades_taken"] or 0), "realized_pnl": float(row["realized_pnl"] or 0.0)}

    def add_daily_trades(self, day: str, n: int) -> int:
        """Increment the day's trade count atomically; returns the new total."""
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO daily_counters(day, trades_taken, realized_pnl) VALUES (?, ?, 0)
                   ON CONFLICT(day) DO UPDATE SET trades_taken=trades_taken+excluded.trades_taken""",
                (day, int(n)),
            )
            row = conn.execute("SELECT trades_taken FROM daily_counters WHERE day=?", (day,)).fetchone()
        return int(row["trades_taken"]) if row else 0

    def add_daily_realized_pnl(self, day: str, pnl: float) -> None:
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO daily_counters(day, trades_taken, realized_pnl) VALUES (?, 0, ?)
                   ON CONFLICT(day) DO UPDATE SET realized_pnl=realized_pnl+excluded.realized_pnl""",
                (day, float(pnl)),
            )
