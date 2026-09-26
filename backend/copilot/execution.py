"""Copilot execution — REMOVED (PHASE 5, items 1 & 24).

The Copilot is OBSERVATION + EXPLANATION ONLY. It never places, modifies,
or cancels any order, in paper or live mode, through any path.

History: this module used to reconstruct a StrategySignal
(strategy_name="OPTION_PREMIUM" — the wrong identity for the configured
V8_D_PULLBACK_ATM production strategy) and submit it through
`TradingEngine.execute_multi_signal()` from the live scanner's Copilot
hook. That was a real duplicate execution path: a second way for orders to
enter the paper runtime from an analysis component, with a mismatched
strategy identity attached. It is deleted, not gated — the scanner hook now
only LOGS trade plans (see scan_loop._process_trade_plan_result) and no
code path reaches execution from Copilot.

`submit_trade_plan_for_paper_execution` remains as an explicit refuse-only
stub so any stale importer fails safely with a loud reason instead of
silently executing. A static check in the regression suite asserts no
module outside the execution/orders layer can reach order placement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ExecutionResult:
    submitted: bool
    trade_id: Optional[str] = None
    reason: str = ""


def _refuse(reason: str) -> ExecutionResult:
    return ExecutionResult(submitted=False, trade_id=None, reason=reason)


def submit_trade_plan_for_paper_execution(
    tools: Any,
    trade_plan_dict: Dict[str, Any],
    validation_dict: Dict[str, Any],
    copilot_settings: Optional[Any] = None,
) -> ExecutionResult:
    """Refuse-only stub — Copilot execution was removed in PHASE 5.

    Previously this reconstructed a StrategySignal
    (strategy_name="OPTION_PREMIUM") and called
    TradingEngine.execute_multi_signal() from the scanner's Copilot hook.
    That violated the one-execution-path architecture and the strategy
    identity contract. The scanner hook now only records alerts/shadow logs;
    actual trading happens exclusively through:
      scanner → PaperTradingRuntime → ExecutionPipeline → PaperBroker (paper)
      scanner → TradingEngine → ExecutionPipeline → OrderManager → broker (live, disabled)
    """
    return _refuse(
        "COPILOT_EXECUTION_REMOVED — the Copilot is observation-only and "
        "cannot execute trades (PHASE 5). Use the production scanner/runtime "
        "path instead."
    )
