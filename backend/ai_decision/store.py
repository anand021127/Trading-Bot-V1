"""Durable AI decision store (PHASE 5.1 §7/§16).

Persists every AI trading decision (approved, rejected, waiting, or
fail-closed) in the SAME SQLite database the runtime already uses — a new
additive `ai_decisions` table, NOT a second idempotency system:

  - Idempotency: the same (signal_id + input_snapshot_hash + model
    provider/name/version) key maps to exactly one stored decision. A
    retry of an identical evaluation replays the stored decision instead
    of producing a second independent approval. The ORDER-level
    duplicate protection remains IdempotentOrderStore/ExecutionPipeline
    (Phase 5) — this store never gates orders, only AI evaluations.
  - Durability / reproducibility: decision_id, model identity, decision,
    confidence, reason codes, input_snapshot_hash and timestamps are
    stored so "why did the AI approve this trade?" is answerable from
    stored state alone.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from typing import Any, Dict, Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_decisions (
    decision_id         TEXT PRIMARY KEY,
    idempotency_key     TEXT NOT NULL,
    signal_id           TEXT,
    strategy            TEXT NOT NULL,
    symbol              TEXT,
    decision            TEXT NOT NULL,
    confidence          REAL NOT NULL DEFAULT 0,
    reason_codes        TEXT,
    reasoning           TEXT,
    model_provider      TEXT,
    model_name          TEXT,
    model_version       TEXT,
    input_snapshot_hash TEXT,
    market_timestamp    TEXT,
    created_at          TEXT,
    latency_ms          REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_decisions_idem
    ON ai_decisions (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_ai_decisions_signal
    ON ai_decisions (signal_id);
"""

# PHASE 5.2 §2: additive migration — setup identity column for dedup
# lookups (signal_id stays the pipeline signal id, so decisions still join
# to executed trades).
_MIGRATE = """
ALTER TABLE ai_decisions ADD COLUMN setup_id TEXT;
CREATE INDEX IF NOT EXISTS ix_ai_decisions_setup
    ON ai_decisions (setup_id);
"""


def make_decision_idempotency_key(
    *, signal_id: str, input_snapshot_hash: str, model_provider: str, model_name: str, model_version: str
) -> str:
    raw = f"{signal_id}|{input_snapshot_hash}|{model_provider}|{model_name}|{model_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


class AIDecisionStore:
    """SQLite-backed store for AI trading decisions + latency telemetry.

    PHASE 5.2 §4: persistence is STRICT — save_decision returns False when
    the row could not be durably written (DB unavailable, locked, schema
    error, write failure). The engine turns any such failure into
    AI_DECISION_PERSISTENCE_FAILED → NO TRADE: an AI approval may NEVER
    exist only in memory (a trade must remain auditable from stored state).
    Writes are serialized with a lock (API threads + worker share the DB).
    """

    def __init__(self, db: Any) -> None:
        # `db` is the runtime's DatabaseManager (settings table carrier). We
        # create the additive table on the same connection.
        self.db = db
        self._lock = threading.Lock()
        self.available: bool = True   # False after any hard store failure
        try:
            conn = db._connect()
            conn.executescript(_SCHEMA)
            conn.commit()
        except Exception:
            # Schema creation failure does not crash construction, but the
            # store is marked unavailable — the engine will fail closed.
            self.available = False
            return
        try:
            # Idempotent additive migration for pre-5.2 databases.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(ai_decisions)").fetchall()}
            if "setup_id" not in cols:
                conn.executescript(_MIGRATE)
                conn.commit()
        except Exception:
            self.available = False

    # ── idempotent decision persistence ───────────────────────────────
    def save_decision(self, decision_dict: Dict[str, Any], idempotency_key: str,
                      signal_id: str, latency_ms: Optional[float] = None,
                      setup_id: str = "") -> bool:
        """Durably insert a decision. STRICT result semantics:

        True  → row committed; the decision may proceed (subject to the
                normal hard risk gates).
        False → NOT persisted (already-stored duplicate, OR any storage
                failure: DB unavailable / locked / schema error / write
                failure). The caller must treat False as unsaved: an
                APPROVE that could not be persisted MUST NOT execute
                (AI_DECISION_PERSISTENCE_FAILED → NO TRADE).
        """
        with self._lock:
            try:
                conn = self.db._connect()
                conn.execute(
                    """INSERT INTO ai_decisions (
                         decision_id, idempotency_key, signal_id, setup_id, strategy,
                         symbol, decision, confidence, reason_codes, reasoning,
                         model_provider, model_name, model_version,
                         input_snapshot_hash, market_timestamp, created_at, latency_ms
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(decision_dict.get("decision_id") or ""),
                        idempotency_key,
                        str(signal_id or ""),
                        str(setup_id or ""),
                        str(decision_dict.get("strategy") or ""),
                        str(decision_dict.get("symbol") or ""),
                        str(decision_dict.get("decision") or ""),
                        float(decision_dict.get("confidence") or 0.0),
                        json.dumps(list(decision_dict.get("reason_codes") or [])),
                        str(decision_dict.get("reasoning") or "")[:4000],
                        str(decision_dict.get("model_provider") or ""),
                        str(decision_dict.get("model_name") or ""),
                        str(decision_dict.get("model_version") or ""),
                        str(decision_dict.get("input_snapshot_hash") or ""),
                        str(decision_dict.get("market_timestamp") or ""),
                        str(decision_dict.get("decision_timestamp") or ""),
                        float(latency_ms) if latency_ms is not None else None,
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Same idempotency key already stored — treat as already
                # persisted (the caller replays the stored row).
                return True
            except sqlite3.OperationalError:
                # locked / unavailable / timeout / malformed schema
                self.available = False
                return False
            except Exception:
                self.available = False
                return False

    def get_decision_by_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        """Fetch a stored decision. Read failures return None (the engine
        then treats the evaluation as unseen and re-evaluates; a write will
        surface any hard store failure)."""
        try:
            row = self.db._connect().execute(
                "SELECT * FROM ai_decisions WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        try:
            codes = json.loads(row["reason_codes"] or "[]")
        except Exception:
            codes = []
        return {
            "decision_id": row["decision_id"],
            "signal_id": row["signal_id"],
            "strategy": row["strategy"],
            "symbol": row["symbol"],
            "decision": row["decision"],
            "confidence": row["confidence"],
            "reason_codes": codes,
            "reasoning": row["reasoning"],
            "model_provider": row["model_provider"],
            "model_name": row["model_name"],
            "model_version": row["model_version"],
            "input_snapshot_hash": row["input_snapshot_hash"],
            "market_timestamp": row["market_timestamp"],
            "created_at": row["created_at"],
            "latency_ms": row["latency_ms"],
        }

    def get_decision(self, decision_id: str) -> Optional[Dict[str, Any]]:
        try:
            row = self.db._connect().execute(
                "SELECT * FROM ai_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        return self.get_decision_by_key(row["idempotency_key"])

    def get_latest_setup_decision(self, setup_id: str) -> Optional[Dict[str, Any]]:
        """Most recent stored decision for a SETUP identity (PHASE 5.2 §2).

        The setup id is stored in the signal_id column (the engine keys
        idempotency on setup identity). Used to reuse an earlier verdict for
        the same continuing opportunity without re-calling the provider.
        Read failures return None (the engine then re-evaluates).
        """
        try:
            row = self.db._connect().execute(
                """SELECT idempotency_key FROM ai_decisions
                   WHERE setup_id=? ORDER BY created_at DESC LIMIT 1""",
                (str(setup_id or ""),),
            ).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        return self.get_decision_by_key(row["idempotency_key"])

    def get_decisions_for_signal(self, signal_id: str) -> list:
        try:
            rows = self.db._connect().execute(
                "SELECT idempotency_key FROM ai_decisions WHERE signal_id=? ORDER BY created_at",
                (str(signal_id or ""),),
            ).fetchall()
        except Exception:
            return []
        out = []
        for r in rows:
            d = self.get_decision_by_key(r["idempotency_key"])
            if d:
                out.append(d)
        return out

    # ── latency telemetry (§23) ───────────────────────────────────────
    def record_latency(self, *, provider: str, model: str, latency_ms: float,
                       timeout_seconds: float, success: bool, error_code: str = "") -> None:
        try:
            raw = self.db.get_setting("ai_decision_latency_log", "[]") or "[]"
            entries = json.loads(raw)
            if not isinstance(entries, list):
                entries = []
            entries.append({
                "provider": provider,
                "model": model,
                "latency_ms": round(float(latency_ms), 1),
                "timeout_seconds": float(timeout_seconds),
                "success": bool(success),
                "error_code": str(error_code or ""),
                "ts": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            })
            self.db.save_setting("ai_decision_latency_log", json.dumps(entries[-200:]))
        except Exception:
            pass

    def latency_stats(self, limit: int = 200) -> Dict[str, Any]:
        try:
            raw = self.db.get_setting("ai_decision_latency_log", "[]") or "[]"
            entries = json.loads(raw)
            if not isinstance(entries, list):
                entries = []
        except Exception:
            entries = []
        recent = entries[-int(limit):]
        latencies = [e.get("latency_ms") for e in recent if isinstance(e.get("latency_ms"), (int, float))]
        successes = [e for e in recent if e.get("success")]
        errors: Dict[str, int] = {}
        for e in recent:
            if not e.get("success") and e.get("error_code"):
                errors[str(e["error_code"])] = errors.get(str(e["error_code"]), 0) + 1
        latencies.sort()
        n = len(latencies)
        return {
            "samples": n,
            "success_count": len(successes),
            "failure_count": n - len(successes),
            "error_counts": errors,
            "latency_ms_min": latencies[0] if n else None,
            "latency_ms_median": latencies[n // 2] if n else None,
            "latency_ms_p95": latencies[max(0, int(n * 0.95) - 1)] if n else None,
            "latency_ms_max": latencies[-1] if n else None,
        }
