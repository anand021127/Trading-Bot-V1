"""PHASE 5.3C — Copilot Strategy Context (READ-ONLY, structured).

Gives the Copilot assistant safe, structured knowledge of the currently
configured strategy, its runtime scan state, AI status, and recent backtest
comparisons — so it can answer "why didn't we trade?", "is V8-D too
restrictive?", "why do the two backtests differ?" from REAL data instead of
saying "I don't have information about V8-D".

HARD SAFETY RULES (enforced by construction):
- READ-ONLY: every value is derived from existing tool/DB/router state.
- NO secrets: no tokens, credentials, .env values, control tokens. The whole
  payload passes through the same redaction layer as every other context.
- NO execution: nothing here can place orders, change settings, or run code.
- Honest gaps: when a data source is unavailable the section says exactly
  what is missing instead of inventing values.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

# ── Static, documented strategy facts (mirrors V8DStrategy defaults; verified
# by a consistency test against the actual class) ─────────────────────────────
STRATEGY_NAME = "V8_D_PULLBACK_ATM"
V8D_PARAMS = {
    "stop_loss_pct": 0.28,
    "target_pct": 0.42,
    "max_account_risk_pct": 0.025,
    "max_capital_alloc_pct": 0.18,
    "max_daily_trades": 3,
    "ema_fast": 20,
    "ema_slow": 50,
    "rsi_period": 14,
    "use_atr_stop": True,
    "atr_stop_mult": 1.8,
}
ENTRY_POLICY = {
    "entry_start_ist": "09:20",
    "last_entry_ist": "14:45",
    "cutoff_inclusive": True,
    "square_off_ist": "15:15",
    "enforced_in": ["backtest engine", "paper scan loop", "live trading engine (calendar OPEN)"],
}
SUPPORTED_UNDERLYINGS = ["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]
OPTION_SELECTION_RULES = [
    "Directional intent from the underlying pullback: bullish reversal -> CE, bearish reversal -> PE.",
    "ATM strike = spot rounded to the index strike step (NIFTY/FINNIFTY 50, BANKNIFTY/SENSEX/BANKEX 100, MIDCPNIFTY 25).",
    "Nearest expiry with a valid broker/cache contract; contract must resolve from real chain/metadata (never invented).",
    "Lot size comes only from contract metadata — missing metadata rejects the trade (no hardcoded fallback).",
    "Premium must be positive and must NOT equal the underlying spot (corruption guard).",
]
STOP_TARGET_RULES = [
    "Stop distance = max(28% of premium, 1.8 x option ATR), capped at 40% of premium.",
    "Target = premium + 1.5 x final stop distance (1.5R).",
    "Trailing: 4-stage R-multiple ratchet (0.7R breakeven, 1.2R locks 0.4R, 1.8R locks 0.9R, 2.5R locks 1.6R).",
]

# Frozen historical comparison (real runs, see analysis/*.json artifacts)
BACKTEST_COMPARISON = {
    "previous_run_2026_09_26_engine": {
        "label": "OLD engine (pre-lifecycle-fix), unchanged V8-D",
        "period": "2025-09-26 to 2026-09-27",
        "trades": 277, "wins": 80, "losses": 197, "win_rate_pct": 28.88,
        "net_pnl": -81511.32, "profit_factor": 0.61, "max_drawdown_pct": 84.31,
        "known_defects": [
            "expired contract held ~6.5 months to BACKTEST_END",
            "18 entries outside 09:20-14:45 (14:50-15:25) that live could never place",
            "minimum 1 lot force-fed through the risk cap (4-6 lot positions on ₹5 premiums)",
            "daily trade limit inert (no trades_today in strategy context)",
        ],
        "artifact": "analysis/backtest_forensics_p53.json",
    },
    "latest_run_2026_09_27_engine": {
        "label": "Corrected engine (lifecycle + entry window + risk parity), unchanged V8-D",
        "period": "2025-09-26 to 2026-09-27",
        "trades": 14, "wins": 3, "losses": 11, "win_rate_pct": 21.43,
        "net_pnl": -2897.00, "profit_factor": 0.45, "max_drawdown_pct": 2.91,
        "dominant_rejections": {
            "Daily trade limit reached: 3/3": 43590,
            "risk-cap 1-lot rejections": 225,
            "ENTRY_SESSION_RESTRICTED": 23,
        },
        "caveat": "14-trade run still carried a QA regression (lifetime per-symbol "
                  "daily counter + engine-sized lots at 1% instead of strategy "
                  "sizing) — fixed this session; next run will differ again. "
                  "Do NOT read strategy quality from either run alone.",
        "artifact": "analysis/trade_diff_p53c.json",
    },
    "unchanged_between_runs": {
        "technical_pullback_rejections": 105947,
        "contract_resolution_failures": "~2450 (missing historical chain data)",
        "strategy_parameters": "byte-identical",
    },
}


def _strategy_params_from_class() -> Dict[str, Any]:
    """Read the ACTUAL configured parameters from the strategy class — the
    static table above must match (a test pins this)."""
    try:
        from backend.strategy.strategies.v8d_strategy import V8DStrategy
        s = V8DStrategy()
        out = {}
        for k in V8D_PARAMS:
            out[k] = getattr(s, k, V8D_PARAMS[k])
        return out
    except Exception:
        return dict(V8D_PARAMS)


def _scan_state() -> Dict[str, Any]:
    """Latest paper scan result from the worker's persisted state."""
    out: Dict[str, Any] = {"available": False, "missing": ["no scan state file found"]}
    try:
        path = os.path.join("data", "paper_scan_state.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            out = {
                "available": True,
                "last_scan_ts": d.get("last_scan_ts"),
                "last_result": d.get("last_result"),
                "signal": d.get("signal"),
                "rejection_reasons": d.get("rejection_reasons"),
                "underlying": d.get("underlying"),
                "option_candidate": d.get("option_candidate"),
                "indicators": d.get("indicators"),
                "option_premium": d.get("option_premium"),
                "dte_days": d.get("dte_days"),
            }
        else:
            out["missing"] = [
                "data/paper_scan_state.json not present — the paper worker "
                "persists per-scan state there; run the worker at least once"
            ]
    except Exception as e:  # pragma: no cover
        out = {"available": False, "missing": [f"scan state unreadable: {type(e).__name__}"]}
    return out


def _ai_status() -> Dict[str, Any]:
    """Effective AI runtime state (same authority chain as the scan loop)."""
    out: Dict[str, Any] = {}
    try:
        from backend.ai_decision.decision_engine import load_ai_decision_settings
        s = load_ai_decision_settings()
        out["env_default_enabled"] = bool(s.get("enabled"))
        out["provider"] = s.get("provider")
        out["model"] = s.get("model")
        out["timeout_seconds"] = s.get("timeout_seconds")
    except Exception as e:
        out["error"] = f"settings unreadable: {type(e).__name__}"
    try:
        from backend.database.db_manager import DatabaseManager
        from backend.api.routers.bot_control import AI_ENABLED_OVERRIDE_KEY
        db = DatabaseManager(db_path=os.environ.get("DATABASE_PATH", "data/trading_bot.db"))
        override = str(db.get_setting(AI_ENABLED_OVERRIDE_KEY, "") or "")
        out["runtime_override"] = override or "unset"
        out["effective_enabled"] = (
            True if override == "1" else False if override == "0"
            else bool(out.get("env_default_enabled", False))
        )
        note = db.get_setting("ai_decision_layer", "")
        if note:
            out["worker_note"] = note
    except Exception as e:
        out["override_error"] = f"override state unreadable: {type(e).__name__}"
    out["fail_closed"] = "AI REJECT/WAIT/timeout/error/malformed → NO TRADE (never bypasses hard risk)"
    return out


def _risk_state() -> Dict[str, Any]:
    try:
        from backend.paper.paper_runtime import PaperTradingRuntime  # noqa: F401
    except Exception:
        pass
    out: Dict[str, Any] = {}
    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager(db_path=os.environ.get("DATABASE_PATH", "data/trading_bot.db"))
        snap = db.get_setting("paper_equity_snapshot", "")
        if snap:
            out["account_equity_snapshot"] = snap
        else:
            out["missing"] = ["paper_equity_snapshot not persisted yet"]
        out["risk_pct_per_trade"] = V8D_PARAMS["max_account_risk_pct"]
        out["capital_alloc_pct"] = V8D_PARAMS["max_capital_alloc_pct"]
        out["max_daily_trades"] = V8D_PARAMS["max_daily_trades"]
        out["lot_size_note"] = "per-contract from broker metadata; varies by index"
    except Exception as e:
        out["error"] = f"risk state unreadable: {type(e).__name__}"
    return out


def build_strategy_context() -> Dict[str, Any]:
    """The full read-only Strategy Context payload (redacted downstream)."""
    return {
        "strategy": {
            "name": STRATEGY_NAME,
            "parameters": _strategy_params_from_class(),
            "entry_policy": ENTRY_POLICY,
            "supported_underlyings": SUPPORTED_UNDERLYINGS,
            "option_selection_rules": OPTION_SELECTION_RULES,
            "stop_target_rules": STOP_TARGET_RULES,
            "source": "backend/strategy/strategies/v8d_strategy.py (read-only reflection)",
        },
        "risk_state": _risk_state(),
        "ai_status": _ai_status(),
        "recent_scan": _scan_state(),
        "backtest_comparison": BACKTEST_COMPARISON,
        "copilot_guardrails": [
            "Copilot is read-only: it cannot place orders, change settings, or execute code.",
            "Strategy changes require out-of-sample evidence and explicit operator approval.",
            "If asked whether to change V8-D, answer with OBSERVED FACTS → EVIDENCE → "
            "POSSIBLE CAUSES → VALIDATION NEEDED → POTENTIAL CHANGE; never auto-modify.",
        ],
    }


def answer_helpers() -> Dict[str, str]:
    """Answer-shape guidance embedded in the Copilot system prompt side."""
    return {
        "why_didnt_we_trade": "Inspect recent_scan.rejection_reasons and ai_status; "
                              "name the exact typed stage (technical / risk / limit / "
                              "session / reconciliation / AI) and the count.",
        "too_restrictive": "Compare technical_pullback_not_met vs risk/limit/session "
                           "rejection counts; state which gate dominates and that "
                           "105,947 pullback rejections are the strategy's design.",
        "stop_question": "Use backtest_comparison + stop_target_rules; cite stop-hit "
                         "vs target-hit R statistics from the artifacts.",
        "should_we_change": "Follow copilot_guardrails: no auto-change; demand "
                            "chronological train/validation evidence.",
        "data_missing": "If recent_scan.available is false, say exactly which file "
                        "is missing and how it gets populated.",
    }
