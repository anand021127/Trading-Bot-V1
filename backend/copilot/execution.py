"""Paper-mode execution for approved TradePlans.

PHASE 9 safety design: this function does NOT re-implement order
simulation, position tracking, or exit management — it converts a
TradePlan back into a `StrategySignal` and calls the EXISTING
`TradingEngine.execute_multi_signal()`, which already does
RiskManager -> PositionSizer -> OrderManager -> paper fill simulation ->
position tracking (see backend/strategy/trading_engine.py). That is the
same method the live scanner path uses for every other signal in this
bot — nothing here is a second execution engine.

The double-gate below is deliberate and non-negotiable: `execute_multi_signal`
places whatever the bot's GLOBAL `settings.mode` says (paper or live) —
it does not know or care that the caller is "the Copilot in paper mode."
So this function refuses to call it at all unless the bot's real global
mode is ALSO "paper" — COPILOT_MODE=paper alone is not sufficient and
must never be treated as sufficient, because if the global bot is
actually in live mode, this call would place a REAL order.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from backend.copilot.config import CopilotSettings, load_copilot_settings


@dataclass
class ExecutionResult:
    submitted: bool
    trade_id: Optional[str]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"submitted": self.submitted, "trade_id": self.trade_id, "reason": self.reason}


def _refuse(reason: str) -> ExecutionResult:
    return ExecutionResult(submitted=False, trade_id=None, reason=reason)


def submit_trade_plan_for_paper_execution(
    tools: Any,
    trade_plan_dict: Dict[str, Any],
    validation_dict: Dict[str, Any],
    copilot_settings: Optional[CopilotSettings] = None,
) -> ExecutionResult:
    copilot_settings = copilot_settings or load_copilot_settings()

    # ── Gate 1: Copilot itself must be enabled and in paper mode ──────
    if not copilot_settings.enabled:
        return _refuse("Copilot is disabled (COPILOT_ENABLED=false) — refusing to execute anything.")
    if copilot_settings.mode != "paper":
        return _refuse(f"COPILOT_MODE is '{copilot_settings.mode}', not 'paper' — refusing paper execution.")

    # ── Gate 2: the bot's GLOBAL mode must ALSO be paper — this is the
    #    setting OrderManager/execute_multi_signal actually obeys, and it
    #    is checked independently of Copilot's own config on purpose. ──
    try:
        from backend.strategy.trading_engine import settings as bot_settings
    except Exception as e:
        return _refuse(f"Could not read the bot's global settings to verify mode — refusing to execute: {e}")
    if bot_settings.mode.lower() != "paper":
        return _refuse(
            f"Bot's global settings.mode is '{bot_settings.mode}', not 'paper'. "
            f"Refusing to execute — COPILOT_MODE=paper alone is never sufficient; "
            f"execute_multi_signal() obeys the global mode, and this call would "
            f"otherwise risk a REAL order if the global mode were 'live'."
        )

    # ── Gate 3: the plan must have actually passed deterministic validation ──
    if not validation_dict or not validation_dict.get("approved"):
        return _refuse("TradePlan was not approved by validate_trade_plan() — refusing to execute an unapproved plan.")

    if tools.engine is None or not hasattr(tools.engine, "execute_multi_signal"):
        return _refuse("No trading engine (with execute_multi_signal) attached — cannot execute.")

    # ── Convert the TradePlan back into a real StrategySignal ─────────
    try:
        from backend.strategy.signal import StrategySignal
        entry_mid = (trade_plan_dict["entry_price_low"] + trade_plan_dict["entry_price_high"]) / 2.0
        signal = StrategySignal(
            strategy_name="OPTION_PREMIUM",
            symbol=trade_plan_dict["symbol"],
            signal="BUY",
            confidence=trade_plan_dict.get("ai_confidence") is not None and 100.0 or 80.0,
            entry_price=entry_mid,
            stop_loss=trade_plan_dict["stop_loss"],
            target=trade_plan_dict["target_1"],
            entry_reason=trade_plan_dict.get("reason", "AI Copilot TradePlan, paper mode"),
            setup_name="", factor_scores={}, conditions={"copilot_trade_plan": True},
            indicators={
                "selected_contract": {
                    "strike": trade_plan_dict.get("strike"),
                    "option_type": trade_plan_dict.get("option_type"),
                    "instrument_key": trade_plan_dict.get("instrument_key"),
                    "oi": trade_plan_dict.get("open_interest"),
                    "bid_price": trade_plan_dict.get("bid_price"),
                    "ask_price": trade_plan_dict.get("ask_price"),
                    "delta": trade_plan_dict.get("delta"),
                    "theta": trade_plan_dict.get("theta"),
                    "iv": trade_plan_dict.get("iv"),
                    # execute_multi_signal() requires these two for its
                    # lot-rounding/freeze-cap step — found missing via the
                    # end-to-end paper-execution test, not assumed present.
                    "lot_size": trade_plan_dict.get("lot_size"),
                    "freeze_quantity": trade_plan_dict.get("freeze_quantity"),
                },
                "directional_intent": trade_plan_dict.get("option_type"),
                "option_type": trade_plan_dict.get("option_type"),
                "expiry_date": trade_plan_dict.get("expiry"),
            },
        )
    except Exception as e:
        return _refuse(f"Could not reconstruct a StrategySignal from the TradePlan — refusing to execute: {e}")

    try:
        trade_id = tools.engine.execute_multi_signal(signal)
    except Exception as e:
        return _refuse(f"execute_multi_signal() raised — treating as a failed/refused execution: {e}")

    if trade_id is None:
        return _refuse("execute_multi_signal() returned None — the existing RiskManager/engine rejected this trade "
                        "at execution time (see engine logs for the exact reason).")
    return ExecutionResult(submitted=True, trade_id=trade_id, reason="Submitted to the existing paper-mode order pipeline.")
