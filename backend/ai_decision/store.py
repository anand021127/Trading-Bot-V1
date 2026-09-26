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


def make_decision_idempotency_key(
    *, signal_id: str, input_snapshot_hash: str, model_provider: str, model_name: str, model_version: str
) -> str:
    raw = f"{signal_id}|{input_snapshot_hash}|{model_provider}|{model_name}|{model_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


class AIDecisionStore:
    """SQLite-backed store for AI trading decisions + latency telemetry."""

    def __init__(self, db: Any) -> None:
        # `db` is the runtime's DatabaseManager (settings table carrier). We
        # create the additive table on the same connection.
        self.db = db
        try:
            conn = db._connect()
            conn.executescript(_SCHEMA)
            conn.commit()
        except Exception:
            # Table creation must never break the trading loop; the engine
            # treats store failures as non-fatal (decision is still returned).
            pass

    # ── idempotent decision persistence ───────────────────────────────
    def save_decision(self, decision_dict: Dict[str, Any], idempotency_key: str,
                      signal_id: str, latency_ms: Optional[float] = None) -> bool:
        """Insert a decision. Returns False if an identical evaluation
        (same idempotency key) was already stored — the caller must replay
        that stored decision rather than keeping a second one."""
        try:
            conn = self.db._connect()
            try:
                conn.execute(
                    """INSERT INTO ai_decisions (
                         decision_id, idempotency_key, signal_id, strategy, symbol,
                         decision, confidence, reason_codes, reasoning,
                         model_provider, model_name, model_version,
                         input_snapshot_hash, market_timestamp, created_at, latency_ms
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(decision_dict.get("decision_id") or ""),
                        idempotency_key,
                        str(signal_id or ""),
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
                return False  # duplicate evaluation — replay the stored one
        except Exception:
            return True  # store failure must never crash the trading loop

    def get_decision_by_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
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
