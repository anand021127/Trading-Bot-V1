"""AITradingDecision — the ONE structured contract between the AI decision
layer and the execution pipeline (PHASE 5.1).

Hard rules enforced here:
  - The AI layer NEVER returns an execution command. It returns APPROVE |
    REJECT | WAIT plus context. The deterministic pipeline consumes this
    object; free-form LLM text can never become an order.
  - `confidence` is AI CONFIDENCE (0-100) — how strongly the model's stated
    analysis supports its own decision under the supplied context. It is
    never a probability of profit and must never be displayed as one.
  - APPROVE is contract-stamped: `strategy` must be the production strategy
    the pipeline is configured for. Any other strategy identity (e.g. a
    stray OPTION_PREMIUM decision) is coerced to REJECT by the gate.
  - Failure is a typed decision, never an exception to the caller: timeout
    -> AI_TIMEOUT, provider down -> AI_PROVIDER_UNAVAILABLE, bad JSON ->
    AI_INVALID_RESPONSE, schema violation -> AI_DECISION_INVALID. Every one
    of them means NO TRADE.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Decisions
APPROVE = "APPROVE"
REJECT = "REJECT"
WAIT = "WAIT"
VALID_DECISIONS = (APPROVE, REJECT, WAIT)

# Fail-closed typed reasons (each of these means NO TRADE, always)
R_APPROVED = "AI_APPROVED"
R_REJECTED = "AI_REJECTED"
R_WAIT = "AI_WAIT"
R_TIMEOUT = "AI_TIMEOUT"
R_PROVIDER_UNAVAILABLE = "AI_PROVIDER_UNAVAILABLE"
R_MODEL_UNAVAILABLE = "AI_MODEL_UNAVAILABLE"
R_INVALID_RESPONSE = "AI_INVALID_RESPONSE"
R_DECISION_INVALID = "AI_DECISION_INVALID"
R_STRATEGY_MISMATCH = "AI_STRATEGY_MISMATCH"

# Backtest-specific typed status (§17)
BACKTEST_UNAVAILABLE = "AI_BACKTEST_UNAVAILABLE"

# Failure reasons are terminal — they can never mean APPROVE.
FAILURE_REASONS = frozenset({
    R_TIMEOUT, R_PROVIDER_UNAVAILABLE, R_MODEL_UNAVAILABLE,
    R_INVALID_RESPONSE, R_DECISION_INVALID, R_STRATEGY_MISMATCH,
})

_ALLOWED_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: Any, lo: float, hi: float) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return max(lo, min(hi, f))


def new_decision_id() -> str:
    return f"aid_{uuid.uuid4().hex}"


def valid_reason_code(code: Any) -> bool:
    try:
        return bool(_ALLOWED_KEY_RE.match(str(code)))
        # strict machine-vocabulary code (UPPER_SNAKE, <=80 chars)
    except Exception:
        return False


@dataclass
class AITradingDecision:
    """Structured AI decision consumed by the deterministic pipeline.

    Execution requires V8-D signal + AI APPROVE + Risk PASS + PositionSizer
    PASS + ExecutionPipeline PASS. This object alone is never sufficient.
    """

    decision: str                      # APPROVE | REJECT | WAIT
    reason_codes: List[str]
    confidence: float = 0.0            # AI confidence 0-100 (never P(win))
    strategy: str = ""
    symbol: str = ""
    underlying_price: Optional[float] = None
    option_type: str = ""
    strike_price: Optional[float] = None
    expiry: str = ""
    instrument_key: str = ""
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    risk_reward: Optional[float] = None
    quantity: Optional[int] = None
    lot_size: Optional[int] = None
    capital_used: Optional[float] = None
    risk_amount: Optional[float] = None
    market_timestamp: str = ""
    decision_timestamp: str = field(default_factory=_utc_now_iso)
    model_provider: str = ""
    model_name: str = ""
    model_version: str = ""
    reasoning: str = ""
    input_snapshot_hash: str = ""
    decision_id: str = field(default_factory=new_decision_id)

    # ── validation ────────────────────────────────────────────────────
    def validate(self) -> List[str]:
        problems: List[str] = []
        if self.decision not in VALID_DECISIONS:
            problems.append(f"decision must be one of {VALID_DECISIONS}, got {self.decision!r}")
        conf = _clamp(self.confidence, 0.0, 100.0)
        if conf is None:
            problems.append("confidence must be a number 0-100")
        for rc in self.reason_codes:
            if not valid_reason_code(rc):
                problems.append(f"invalid reason code: {rc!r}")
        if self.decision == APPROVE:
            if not self.strategy:
                problems.append("APPROVE requires strategy identity")
            if not self.instrument_key:
                problems.append("APPROVE requires instrument_key")
        if self.decision != APPROVE and self.reason_codes and any(
            r == R_APPROVED for r in self.reason_codes
        ):
            problems.append("AI_APPROVED reason on a non-APPROVE decision")
        return problems

    # ── coercion from raw provider output ────────────────────────────
    @classmethod
    def from_model_output(
        cls,
        raw: Dict[str, Any],
        *,
        strategy: str,
        symbol: str,
        input_snapshot_hash: str,
        model_provider: str,
        model_name: str,
        model_version: str,
        market_timestamp: str,
        fallback_contract: Optional[Dict[str, Any]] = None,
    ) -> "AITradingDecision":
        """Coerce the model's JSON output into the strict contract.

        The model is allowed to state only: decision, confidence, reason
        codes, and reasoning prose. It may NOT invent prices, quantities,
        or a different strategy — those fields come from the verified
        signal/contract (`fallback_contract`), never from model text.
        """
        fallback = fallback_contract or {}
        allowed = str(raw.get("decision") or "").strip().upper()
        if allowed not in VALID_DECISIONS:
            # Unknown verdict — the strictest safe handling is REJECT with
            # an invalid-response marker; the gate treats any non-APPROVE
            # as no-trade anyway.
            return cls.fail_closed(
                R_DECISION_INVALID,
                strategy=strategy,
                symbol=symbol,
                input_snapshot_hash=input_snapshot_hash,
                model_provider=model_provider,
                model_name=model_name,
                model_version=model_version,
                market_timestamp=market_timestamp,
                reasoning=f"model returned unparseable decision value: {raw.get('decision')!r}",
            )

        codes_raw = raw.get("reason_codes")
        codes: List[str] = []
        if isinstance(codes_raw, list):
            for c in codes_raw:
                s = str(c).strip().upper().replace(" ", "_").replace("-", "_")
                # drop obvious schema-echo noise (the model repeating the
                # prompt's placeholders) — never part of the real vocabulary
                if s and s not in ("UPPER_SNAKE_CASE", "SHORT_CODE") and valid_reason_code(s) and s not in codes:
                    codes.append(s)
        if not codes:
            codes = [R_APPROVED if allowed == APPROVE else R_REJECTED if allowed == REJECT else R_WAIT]
        # Contract stamp: the machine marker AI_APPROVED is added by THIS
        # layer when the model returns a well-formed APPROVE verdict — the
        # model itself is never required to know internal reason codes.
        if allowed == APPROVE and R_APPROVED not in codes:
            codes = [R_APPROVED] + codes[:7]

        conf = _clamp(raw.get("confidence"), 0.0, 100.0)
        if conf is None:
            return cls.fail_closed(
                R_DECISION_INVALID,
                strategy=strategy, symbol=symbol,
                input_snapshot_hash=input_snapshot_hash,
                model_provider=model_provider, model_name=model_name,
                model_version=model_version, market_timestamp=market_timestamp,
                reasoning="confidence missing or not a number 0-100",
            )

        reasoning = str(raw.get("reasoning") or "")[:2000]
        return cls(
            decision=allowed,
            confidence=round(conf, 1),
            reason_codes=codes,
            strategy=strategy,           # contract-stamped, from the pipeline
            symbol=symbol,
            underlying_price=fallback.get("underlying_price"),
            option_type=fallback.get("option_type", ""),
            strike_price=fallback.get("strike_price"),
            expiry=fallback.get("expiry", ""),
            instrument_key=fallback.get("instrument_key", ""),
            entry_price=fallback.get("entry_price"),
            stop_loss=fallback.get("stop_loss"),
            target=fallback.get("target"),
            risk_reward=fallback.get("risk_reward"),
            quantity=fallback.get("quantity"),
            lot_size=fallback.get("lot_size"),
            capital_used=fallback.get("capital_used"),
            risk_amount=fallback.get("risk_amount"),
            market_timestamp=market_timestamp,
            model_provider=model_provider,
            model_name=model_name,
            model_version=model_version,
            reasoning=reasoning,
            input_snapshot_hash=input_snapshot_hash,
        )

    @classmethod
    def fail_closed(
        cls,
        reason: str,
        *,
        strategy: str = "",
        symbol: str = "",
        input_snapshot_hash: str = "",
        model_provider: str = "",
        model_name: str = "",
        model_version: str = "",
        market_timestamp: str = "",
        reasoning: str = "",
        fallback_contract: Optional[Dict[str, Any]] = None,
    ) -> "AITradingDecision":
        """Typed NO-TRADE decision for every failure mode (§13/§14)."""
        if reason not in FAILURE_REASONS and reason not in (R_REJECTED, R_WAIT):
            reason = R_DECISION_INVALID
        fb = fallback_contract or {}
        return cls(
            decision=REJECT,
            confidence=0.0,
            reason_codes=[reason],
            strategy=strategy,
            symbol=symbol,
            underlying_price=fb.get("underlying_price"),
            option_type=fb.get("option_type", ""),
            strike_price=fb.get("strike_price"),
            expiry=fb.get("expiry", ""),
            instrument_key=fb.get("instrument_key", ""),
            entry_price=fb.get("entry_price"),
            stop_loss=fb.get("stop_loss"),
            target=fb.get("target"),
            risk_reward=fb.get("risk_reward"),
            quantity=fb.get("quantity"),
            lot_size=fb.get("lot_size"),
            capital_used=fb.get("capital_used"),
            risk_amount=fb.get("risk_amount"),
            market_timestamp=market_timestamp,
            decision_timestamp=_utc_now_iso(),
            model_provider=model_provider,
            model_name=model_name,
            model_version=model_version,
            reasoning=reasoning[:2000],
            input_snapshot_hash=input_snapshot_hash,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def allows_execution(self) -> bool:
        """APPROVE is necessary — never sufficient. The pipeline still runs
        risk/sizing/validation after this returns True."""
        return self.decision == APPROVE and not (set(self.reason_codes) & FAILURE_REASONS)
