"""AI Trading Decision Engine (PHASE 5.1).

Architecture (the ONE AI decision layer):

    Market Scanner → V8-D (deterministic signal) → AI TRADING DECISION (here)
        → hard risk / sizing / ExecutionPipeline → PaperBroker

The AI layer NEVER calls Upstox, PaperBroker, LiveBroker, OrderManager, or
execute_multi_signal, and never imports the execution/ or orders/ modules.
It returns a structured AITradingDecision; the deterministic pipeline
consumes it. AI approval is NECESSARY but NEVER sufficient — hard risk
always overrides AI, and any provider failure fails closed to NO TRADE.

Provider: local Ollama (OpenAI-compatible /v1/chat/completions), model
llama3.2:1b, temperature 0, strict JSON schema, typed timeout/unavailable/
invalid-response handling — the same typed provider-error taxonomy the
Copilot chat adapter uses, deliberately kept separate from it (chat
explains; this engine decides).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from backend.ai_decision.contract import (
    APPROVE,
    BACKTEST_UNAVAILABLE,
    R_APPROVED,
    R_DECISION_INVALID,
    R_INVALID_RESPONSE,
    R_MODEL_UNAVAILABLE,
    R_PROVIDER_UNAVAILABLE,
    R_REJECTED,
    R_STRATEGY_MISMATCH,
    R_TIMEOUT,
    R_WAIT,
    AITradingDecision,
)
from backend.ai_decision.context import (
    MarketSession,
    RiskContext,
    build_ai_snapshot,
    build_market_context,
    snapshot_hash,
)
from backend.ai_decision.store import AIDecisionStore, make_decision_idempotency_key
from backend.copilot.provider_errors import (
    AIProviderError,
    AIModelUnavailableError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
    classify_provider_exception,
)


# ── settings ──────────────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_bool(name: str, default: str) -> bool:
    return (os.environ.get(name, default) or default).strip().lower() in ("1", "true", "yes", "on")


def load_ai_decision_settings() -> Dict[str, Any]:
    """AI trading-decision configuration. Defaults: DISABLED — the AI layer
    must be explicitly enabled; disabled means V8-D-only trading, clearly
    reported (never a silent fake decision)."""
    provider = os.environ.get("AI_DECISION_PROVIDER", "ollama").strip().lower()
    model = os.environ.get("AI_DECISION_MODEL", "llama3.2:1b").strip()
    return {
        "enabled": _env_bool("AI_DECISION_ENABLED", "false"),
        "provider": provider,
        "model": model,
        "base_url": os.environ.get("AI_DECISION_BASE_URL", "http://localhost:11434/v1").strip(),
        "timeout_seconds": _env_float("AI_DECISION_TIMEOUT_SECONDS", 20.0),
        # Ollama supports temperature 0 = deterministic decoding.
        "temperature": _env_float("AI_DECISION_TEMPERATURE", 0.0),
        "max_tokens": int(_env_float("AI_DECISION_MAX_TOKENS", 128)),
    }


# ── provider client (the ONLY outbound call in this package) ─────────────

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract a JSON object carrying a string `decision` field from model
    text. The model is instructed to answer with JSON only; prose is never
    parsed into a decision (§12)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and isinstance(obj.get("decision"), str):
            return obj
    except ValueError:
        pass
    if "```" in text:
        for chunk in text.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("{"):
                try:
                    obj = json.loads(chunk)
                    if isinstance(obj, dict) and isinstance(obj.get("decision"), str):
                        return obj
                except ValueError:
                    continue
    start = text.find("{")
    if start != -1:
        end = text.rfind("}")
        if end > start:
            try:
                obj = json.loads(text[start:end + 1])
                if isinstance(obj, dict) and isinstance(obj.get("decision"), str):
                    return obj
            except ValueError:
                pass
    return None


_DECISION_SYSTEM_PROMPT = (
    "You are the AI decision layer of an options trading bot. You evaluate ONE "
    "candidate trade signal produced by a deterministic strategy (V8-D Pullback ATM) "
    "using ONLY the verified market data provided. You cannot fetch data, place "
    "orders, or change anything.\n"
    "Respond with JSON ONLY — exactly this one object and nothing else:\n"
    '{"decision": "APPROVE" | "REJECT" | "WAIT", "confidence": <number 0-100>, '
    '"reason_codes": ["SHORT_CODE", "SHORT_CODE"]}\n'
    "Guidance:\n"
    "- APPROVE only when trend, pullback quality, momentum (RSI), volatility (ATR), "
    "option spread and risk/reward all support the entry.\n"
    "- REJECT for clearly poor setups (bad risk/reward, wide spread, momentum against "
    "the direction, exhausted move, risk limits nearly exhausted).\n"
    "- WAIT when data is ambiguous, indicators conflict, or freshness/margin context "
    "makes the entry unsafe to judge right now.\n"
    "- confidence is your confidence in your own analysis under this data — NOT a "
    "probability of profit. You have no such statistic.\n"
    "- Use only values present in the data. If something critical is missing, answer WAIT.\n"
    "- Keep it SHORT: at most 3 reason codes, UPPER_SNAKE_CASE, and no explanation "
    "text outside the JSON object. Never repeat keys or values."
)


class OllamaDecisionProvider:
    """Minimal OpenAI-compatible chat client for a local Ollama server.

    It knows how to do exactly one thing: POST a chat completion and return
    the message text. It holds no broker credentials, never receives them,
    and has no order-placement capability (§19)."""

    def __init__(self, *, base_url: str, model: str, timeout_seconds: float,
                 temperature: float = 0.0, max_tokens: int = 256) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.max_tokens = max_tokens

    def chat_json(self, snapshot: Dict[str, Any]) -> str:
        """Send the snapshot and return raw assistant text. Raises typed
        AIProviderError subclasses for every failure mode — never a canned
        answer, never a fabricated decision."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _DECISION_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False, default=str)},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            # Grammar-constrained JSON (supported by Ollama's OpenAI-compatible
            # endpoint; harmless if a provider ignores it).
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise classify_provider_exception(exc)
        except ValueError as exc:
            raise AIProviderError(f"malformed provider response envelope: {exc}")
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderError(f"malformed provider response envelope: {exc}")


# ── the engine ────────────────────────────────────────────────────────────

class AITradingDecisionEngine:
    """Builds the deterministic context, calls the model, validates the
    response into the strict contract, enforces idempotency, and returns the
    structured decision. All failures are typed NO-TRADE decisions."""

    def __init__(self, db: Any = None, provider: Optional[OllamaDecisionProvider] = None) -> None:
        self.settings = load_ai_decision_settings()
        self.provider = provider or OllamaDecisionProvider(
            base_url=self.settings["base_url"],
            model=self.settings["model"],
            timeout_seconds=self.settings["timeout_seconds"],
            temperature=self.settings["temperature"],
            max_tokens=self.settings["max_tokens"],
        )
        self.store = AIDecisionStore(db) if db is not None else None

    @property
    def enabled(self) -> bool:
        return bool(self.settings["enabled"])

    # ── context + snapshot ────────────────────────────────────────────
    def build_snapshot(
        self,
        *,
        signal: Any,
        contract: Dict[str, Any],
        expiry: str,
        candles: List[Dict[str, Any]],
        candles_fresh: bool,
        candle_age_seconds: Optional[float],
        risk: RiskContext,
        session: MarketSession,
    ) -> Dict[str, Any]:
        context = build_market_context(
            symbol=str(getattr(signal, "symbol", "")),
            candles=candles,
            signal=signal,
            contract=contract,
            expiry=expiry,
            risk=risk,
            session=session,
            candles_fresh=candles_fresh,
            candle_age_seconds=candle_age_seconds,
        )
        return build_ai_snapshot(context, strategy=str(getattr(signal, "strategy_name", "")))

    # ── core decision ─────────────────────────────────────────────────
    def decide(
        self,
        *,
        signal_id: str,
        signal: Any,
        contract: Dict[str, Any],
        expiry: str,
        candles: List[Dict[str, Any]],
        candles_fresh: bool,
        candle_age_seconds: Optional[float],
        risk: RiskContext,
        session: MarketSession,
        pipeline_strategy: str = "",
    ) -> AITradingDecision:
        """Produce the structured AI decision for one V8-D BUY signal.

        Fail-closed guarantees: provider unavailable → AI_PROVIDER_UNAVAILABLE,
        timeout → AI_TIMEOUT, model missing → AI_MODEL_UNAVAILABLE, bad
        envelope → AI_INVALID_RESPONSE, schema violation → AI_DECISION_INVALID.
        Every failure is decision=REJECT (NO TRADE). Never raises to the
        trading loop."""
        strategy_name = str(getattr(signal, "strategy_name", ""))
        provider_id = f"ollama:{self.settings['provider']}" if self.settings["provider"] else "ollama"
        model_name = str(self.settings["model"])
        model_version = "local-ollama"
        started = time.monotonic()

        # 1. Deterministic snapshot of exactly what the AI will receive.
        try:
            snapshot = self.build_snapshot(
                signal=signal, contract=contract, expiry=expiry, candles=candles,
                candles_fresh=candles_fresh, candle_age_seconds=candle_age_seconds,
                risk=risk, session=session,
            )
        except Exception as exc:  # noqa: BLE001 — fail closed
            return self._finish_fail(
                R_DECISION_INVALID, strategy_name, provider_id, model_name, model_version,
                "", started, signal_id,
                reasoning=f"context build failed: {type(exc).__name__}",
            )
        input_hash = snapshot_hash(snapshot)

        # 2. Idempotency: identical evaluation replays the stored decision.
        idem_key = make_decision_idempotency_key(
            signal_id=signal_id, input_snapshot_hash=input_hash,
            model_provider=provider_id, model_name=model_name, model_version=model_version,
        )
        if self.store is not None:
            existing = self.store.get_decision_by_key(idem_key)
            if existing is not None:
                return self._from_stored(existing, signal, contract, expiry)

        # 3. Explicit data policy — critical gaps are WAIT, never invented.
        ctx = snapshot["context"]
        if not ctx["data_freshness"]["candles_fresh"]:
            decision = self._wait(
                strategy_name, provider_id, model_name, model_version, input_hash, started,
                signal_id, signal, contract, expiry,
                reasoning="Market candle data is stale — refusing to judge the signal on old data.",
                codes=[R_WAIT, "STALE_DATA"],
            )
            return self._store_and_return(decision, idem_key, signal_id, started)
        if not ctx["option"]["instrument_key"] or not ctx["option"]["ltp"]:
            decision = self._wait(
                strategy_name, provider_id, model_name, model_version, input_hash, started,
                signal_id, signal, contract, expiry,
                reasoning="Resolved option contract is incomplete (no instrument key or LTP).",
                codes=[R_WAIT, "INCOMPLETE_CONTRACT"],
            )
            return self._store_and_return(decision, idem_key, signal_id, started)

        # 4. Provider call.
        try:
            raw_text = self.provider.chat_json(snapshot)
        except AIProviderTimeoutError as exc:
            return self._finish_fail(
                R_TIMEOUT, strategy_name, provider_id, model_name, model_version,
                input_hash, started, signal_id, reasoning=str(exc),
            )
        except AIModelUnavailableError as exc:
            return self._finish_fail(
                R_MODEL_UNAVAILABLE, strategy_name, provider_id, model_name, model_version,
                input_hash, started, signal_id, reasoning=str(exc),
            )
        except (AIProviderUnavailableError, AIProviderError) as exc:
            code = R_INVALID_RESPONSE if not isinstance(exc, AIProviderUnavailableError) else R_PROVIDER_UNAVAILABLE
            return self._finish_fail(
                code, strategy_name, provider_id, model_name, model_version,
                input_hash, started, signal_id, reasoning=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — never crash the trading loop
            return self._finish_fail(
                R_PROVIDER_UNAVAILABLE, strategy_name, provider_id, model_name, model_version,
                input_hash, started, signal_id,
                reasoning=f"unexpected provider error: {type(exc).__name__}",
            )

        # 5. Strict JSON → contract coercion (never parse prose into verdicts).
        parsed = _extract_json(raw_text)
        if parsed is None:
            return self._finish_fail(
                R_INVALID_RESPONSE, strategy_name, provider_id, model_name, model_version,
                input_hash, started, signal_id,
                reasoning=f"model output was not parseable JSON with a decision field: {raw_text[:200]!r}",
            )
        decision = AITradingDecision.from_model_output(
            parsed,
            strategy=strategy_name,
            symbol=str(getattr(signal, "symbol", "")),
            input_snapshot_hash=input_hash,
            model_provider=provider_id,
            model_name=model_name,
            model_version=model_version,
            market_timestamp=str(ctx.get("market_timestamp") or ""),
            fallback_contract=self._contract_fields(signal, contract, expiry),
        )
        problems = decision.validate()
        if problems:
            return self._finish_fail(
                R_DECISION_INVALID, strategy_name, provider_id, model_name, model_version,
                input_hash, started, signal_id,
                fallback_contract=self._contract_fields(signal, contract, expiry),
                reasoning="; ".join(problems),
            )

        # 6. Strategy-identity guard: an AI decision can never approve a trade
        #    for a strategy other than the configured production strategy.
        if pipeline_strategy and decision.decision == APPROVE and strategy_name != pipeline_strategy:
            decision = AITradingDecision.fail_closed(
                R_STRATEGY_MISMATCH,
                strategy=strategy_name,
                symbol=str(getattr(signal, "symbol", "")),
                input_snapshot_hash=input_hash,
                model_provider=provider_id, model_name=model_name,
                model_version=model_version,
                market_timestamp=decision.market_timestamp,
                fallback_contract=self._contract_fields(signal, contract, expiry),
                reasoning=f"AI decision strategy {strategy_name!r} != configured pipeline strategy {pipeline_strategy!r}",
            )

        latency_ms = (time.monotonic() - started) * 1000.0
        if self.store is not None:
            self.store.record_latency(
                provider=provider_id, model=model_name, latency_ms=latency_ms,
                timeout_seconds=float(self.settings["timeout_seconds"]),
                success=True,
            )
        return self._store_and_return(decision, idem_key, signal_id, started)

    # ── helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _contract_fields(signal: Any, contract: Dict[str, Any], expiry: str) -> Dict[str, Any]:
        ind = getattr(signal, "indicators", None) or {}
        sizing = ind.get("sizing") or {}
        try:
            qty = int(sizing.get("quantity") or sizing.get("qty") or 0) or None
        except (TypeError, ValueError):
            qty = None
        entry = getattr(signal, "entry_price", None)
        try:
            entry = float(entry) if entry else None
        except (TypeError, ValueError):
            entry = None
        stop = getattr(signal, "stop_loss", None)
        try:
            stop = float(stop) if stop else None
        except (TypeError, ValueError):
            stop = None
        tgt = getattr(signal, "target", None)
        try:
            tgt = float(tgt) if tgt else None
        except (TypeError, ValueError):
            tgt = None
        try:
            lot = int(contract.get("lot_size") or ind.get("lot_size") or 0) or None
        except (TypeError, ValueError):
            lot = None
        rr = None
        if entry and stop and tgt and entry > stop and tgt > entry:
            rr = round((tgt - entry) / (entry - stop), 2)
        return {
            "underlying_price": ind.get("underlying_spot"),
            "option_type": str(contract.get("option_type") or ind.get("option_type") or ""),
            "strike_price": contract.get("strike") or ind.get("atm_strike"),
            "expiry": str(expiry or ""),
            "instrument_key": str(contract.get("instrument_key") or ""),
            "entry_price": entry,
            "stop_loss": stop,
            "target": tgt,
            "risk_reward": rr,
            "quantity": qty,
            "lot_size": lot,
            "capital_used": round(entry * qty, 2) if entry and qty else None,
            "risk_amount": round((entry - stop) * qty, 2) if entry and stop and qty and entry > stop else None,
        }

    def _wait(self, strategy_name, provider_id, model_name, model_version, input_hash,
              started, signal_id, signal, contract, expiry, *, reasoning: str, codes: List[str]) -> AITradingDecision:
        from backend.ai_decision.contract import WAIT
        latency_ms = (time.monotonic() - started) * 1000.0
        if self.store is not None:
            self.store.record_latency(
                provider=provider_id, model=model_name, latency_ms=latency_ms,
                timeout_seconds=float(self.settings["timeout_seconds"]), success=True,
            )
        return AITradingDecision(
            decision=WAIT,
            confidence=0.0,
            reason_codes=codes,
            strategy=strategy_name,
            symbol=str(getattr(signal, "symbol", "")),
            market_timestamp="",
            model_provider=provider_id,
            model_name=model_name,
            model_version=model_version,
            reasoning=reasoning,
            input_snapshot_hash=input_hash,
            **self._contract_fields(signal, contract, expiry),  # type: ignore[arg-type]
        )

    def _finish_fail(
        self, reason: str, strategy_name: str, provider_id: str, model_name: str,
        model_version: str, input_hash: str, started: float, signal_id: str,
        reasoning: str = "", fallback_contract: Optional[Dict[str, Any]] = None,
    ) -> AITradingDecision:
        latency_ms = (time.monotonic() - started) * 1000.0
        if self.store is not None:
            self.store.record_latency(
                provider=provider_id, model=model_name, latency_ms=latency_ms,
                timeout_seconds=float(self.settings["timeout_seconds"]),
                success=False, error_code=reason,
            )
        return AITradingDecision.fail_closed(
            reason,
            strategy=strategy_name,
            input_snapshot_hash=input_hash,
            model_provider=provider_id,
            model_name=model_name,
            model_version=model_version,
            reasoning=reasoning,
            fallback_contract=fallback_contract,
        )

    def _store_and_return(self, decision: AITradingDecision, idem_key: str,
                          signal_id: str, started: float) -> AITradingDecision:
        if self.store is not None:
            latency_ms = (time.monotonic() - started) * 1000.0
            self.store.save_decision(decision.to_dict(), idem_key, signal_id, latency_ms)
        return decision

    def _from_stored(self, stored: Dict[str, Any], signal: Any, contract: Dict[str, Any],
                     expiry: str) -> AITradingDecision:
        """Replay a stored decision (idempotency). Contract fields are
        refreshed from the current signal so downstream validation still sees
        real prices; the VERDICT is the stored one."""
        fb = self._contract_fields(signal, contract, expiry)
        return AITradingDecision(
            decision=str(stored.get("decision") or "REJECT"),
            confidence=float(stored.get("confidence") or 0.0),
            reason_codes=list(stored.get("reason_codes") or []),
            strategy=str(stored.get("strategy") or ""),
            symbol=str(stored.get("symbol") or ""),
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
            market_timestamp=str(stored.get("market_timestamp") or ""),
            decision_timestamp=str(stored.get("created_at") or ""),
            model_provider=str(stored.get("model_provider") or ""),
            model_name=str(stored.get("model_name") or ""),
            model_version=str(stored.get("model_version") or ""),
            reasoning=str(stored.get("reasoning") or ""),
            input_snapshot_hash=str(stored.get("input_snapshot_hash") or ""),
            decision_id=str(stored.get("decision_id") or ""),
        )


# ── the gate on the paper path (§6) ──────────────────────────────────────

def apply_ai_decision_gate(
    payload: Dict[str, Any],
    decision: AITradingDecision,
    *,
    pipeline_strategy: str,
) -> Optional[str]:
    """Deterministic gate between the AI decision and runtime.submit_entry.

    Returns None when the normal pipeline may continue (AI APPROVE with the
    correct strategy identity), or a no-trade reason string otherwise. This
    is the ONLY consumption of the AI decision — free-form model text can
    never reach the pipeline, and hard risk still runs after this gate.

    Example outcomes (§5):
      AI APPROVE  + risk later FAILS  → pipeline rejects (NO TRADE)
      AI APPROVE  + kill switch ON    → runtime rejects (NO TRADE)
      AI REJECT / WAIT / any failure → NO TRADE here, before risk
    """
    if decision.decision == APPROVE and decision.allows_execution:
        if pipeline_strategy and decision.strategy != pipeline_strategy:
            return f"AI_STRATEGY_MISMATCH:{decision.strategy}"
        # Stamp AI approval metadata onto the payload for durability (§7).
        payload["ai_decision"] = {
            "decision_id": decision.decision_id,
            "model_provider": decision.model_provider,
            "model_name": decision.model_name,
            "model_version": decision.model_version,
            "decision": decision.decision,
            "confidence": decision.confidence,
            "reason_codes": list(decision.reason_codes),
            "input_snapshot_hash": decision.input_snapshot_hash,
            "decision_timestamp": decision.decision_timestamp,
        }
        return None
    reason = (decision.reason_codes or [R_REJECTED])[0]
    return f"AI_NO_TRADE:{reason}"


def ai_decision_status(engine: Optional[AITradingDecisionEngine]) -> Dict[str, Any]:
    """Read-only status for the API/UI (§9/§18): what is configured, whether
    the AI layer is enabled, and measured latency — never a fake decision."""
    settings = load_ai_decision_settings()
    stats: Dict[str, Any] = {}
    if engine is not None and engine.store is not None:
        try:
            stats = engine.store.latency_stats()
        except Exception:
            stats = {}
    return {
        "ai_decision_enabled": bool(settings["enabled"]) and engine is not None,
        "provider": settings["provider"],
        "model": settings["model"],
        "base_url": settings["base_url"],
        "timeout_seconds": settings["timeout_seconds"],
        "temperature": settings["temperature"],
        "backtest_status": BACKTEST_UNAVAILABLE,
        "latency": stats,
        "note": (
            "AI layer enabled — APPROVE is necessary but never sufficient; "
            "hard risk always overrides AI."
            if settings["enabled"] and engine is not None
            else "AI layer disabled — paper trading runs V8-D-only; no AI "
                 "decisions are being consulted."
        ),
    }
