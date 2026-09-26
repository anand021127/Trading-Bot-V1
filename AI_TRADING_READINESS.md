# AI_TRADING_READINESS.md — PHASE 5.1

**Scope:** restore AI participation in the actual trading decision, safely. Phase 5 safety architecture is untouched; the unsafe Copilot → `execute_multi_signal()` path remains removed. **No live trading. No profitability claims — none of this is evidence that AI improves returns.**

---

## 1. Current AI provider & model (§1/§12 — audited, not invented)

| Item | Value |
|---|---|
| Decision-layer provider | **Ollama** (local, OpenAI-compatible `http://localhost:11434/v1`) |
| Model | **`llama3.2:1b`** — verified present locally: 1.2B params, Q8_0, 128k ctx, tools-capable |
| Decoding | `temperature=0` (deterministic), `max_tokens=128`, `response_format=json_object` |
| Timeout | 20 s default (`AI_DECISION_TIMEOUT_SECONDS`), measured warm median ≈ **3.8 s**, cold start ≈ 14–20 s |
| Chat (Copilot) provider | Same local Ollama via `COPILOT_LLM_BACKEND=local_openai_compatible` — **separate responsibility**, chat explains, never executes |

Pre-existing AI components, audited honestly:
- `backend/ai/predictor.py` (`AIPredictor`) — **stub/passthrough** (never loads a model, disabled by default). Untouched; it is not the decision layer.
- `backend/copilot/llm_adapter.py` — real OpenAI-compatible adapter, **chat-only** (`explain()` → string).
- No AI decision layer existed before Phase 5.1. The only model that can actually perform the decision is the local Ollama `llama3.2:1b`. **No model was invented.**

## 2. Architecture (§0/§6/§19)

```
REAL MARKET DATA (Upstox)
   ↓
V8_D_PULLBACK_ATM  (deterministic signal — parameters unchanged)
   ↓
AI TRADING DECISION   ← backend/ai_decision/ (LLM, temperature 0, strict JSON)
   ↓                   returns APPROVE|REJECT|WAIT — never an order
HARD RISK VALIDATION  ← kill switch → strategy identity → MAX_POSITIONS /
   ↓                   MAX_DAILY_TRADES / MAX_DAILY_LOSS → equity
POSITION SIZING       ← lot-multiple sizing from contract metadata
   ↓
EXECUTION PIPELINE    ← contract validation + pretrade guard + idempotent intent
   ↓
PAPER BROKER → TRADE LEDGER        (live-future variant ends at Upstox — disabled)
```

The gate lives in `backend/paper/market_scan_loop.py::scan_once()` between the V8-D BUY signal and `runtime.submit_entry()`, wired from `backend/paper/paper_worker.py` (`AI_DECISION_ENABLED` controls it; default **off** = V8-D-only, clearly reported — never a silent fake decision).

**Hard guarantees:**
- AI approval is **necessary but never sufficient**. Final execution requires V8-D signal + AI APPROVE + Risk PASS + PositionSizer PASS + ExecutionPipeline PASS.
- The AI layer **never calls** Upstox, PaperBroker, LiveBroker, OrderManager, `execute_multi_signal`, and never imports `backend/execution/` or `backend/orders/` — enforced by an AST test (`test_ai_layer_never_imports_broker_or_execution`).
- Free-form LLM text can never become an execution command: only the structured `AITradingDecision` crosses the boundary (§2).
- The AI layer receives **no broker credentials, ever** (§19/§22).

## 3. Decision contract (§2)

`backend/ai_decision/contract.py` — `AITradingDecision`, all user-specified fields:
`decision` (APPROVE|REJECT|WAIT), `confidence` (0–100 — **AI confidence**, never a probability of winning), `strategy` (contract-stamped), `symbol`, `underlying_price`, `option_type`, `strike_price`, `expiry`, `instrument_key`, `entry_price`, `stop_loss`, `target`, `risk_reward`, `quantity`, `lot_size`, `capital_used`, `risk_amount`, `market_timestamp`, `decision_timestamp`, `model_provider`, `model_name`, `model_version`, `reason_codes` (strict UPPER_SNAKE vocabulary), `reasoning`, `input_snapshot_hash`, `decision_id`.

Contract prices/quantities come from the **verified signal/contract, never from model text** — the model states only verdict/confidence/reasons. Non-APPROVE never executes; an APPROVE carrying a failure reason can never execute (`allows_execution`).

## 4. Deterministic context + input snapshot (§3/§15)

`backend/ai_decision/context.py` builds the AI's entire view from the same candles/chain V8-D used: underlying price, candles count, EMA20/EMA50, RSI, ATR, VWAP, option LTP/bid/ask/spread, option ATR, strike, expiry, lot size, instrument key, risk state (equity, exposure, daily P&L, trades today, open positions, kill switch, reconciliation), market session, data freshness. Missing critical data → explicit **WAIT** policy (stale candles, incomplete contract) — nothing is fabricated.

`input_snapshot_hash` = SHA-256 over the canonical JSON (sorted keys, 6-dp floats) of exactly what was sent. `assert_no_secrets()` hard-fails if any token/secret/password/credential key ever reaches the payload (tested).

## 5. Hard risk gates (§5) — AI never overrides

Every scenario proven by test: AI APPROVE + kill switch → NO TRADE; AI APPROVE + `INSUFFICIENT_EQUITY` → NO TRADE; AI REJECT/WAIT with risk PASS → NO TRADE; AI APPROVE + stale data → WAIT/NO TRADE. Kill switch, reconciliation and daily-loss gates remain upstream and authoritative inside `runtime.submit_entry` → `ExecutionPipeline`.

## 6. Failure behavior (§13/§14) — fail closed, always

| Condition | Typed reason | Result |
|---|---|---|
| Provider unreachable | `AI_PROVIDER_UNAVAILABLE` | REJECT → NO TRADE |
| Timeout (20 s) | `AI_TIMEOUT` | REJECT → NO TRADE |
| Model missing | `AI_MODEL_UNAVAILABLE` | REJECT → NO TRADE |
| Non-JSON / prose answer | `AI_INVALID_RESPONSE` | REJECT → NO TRADE |
| Schema violation / unknown verdict | `AI_DECISION_INVALID` | REJECT → NO TRADE |
| Strategy identity mismatch (incl. any OPTION_PREMIUM decision) | `AI_STRATEGY_MISMATCH` | REJECT → NO TRADE |

No fallback to auto-BUY, no fallback to V8-D-only execution, no fabricated response — the trading loop stays healthy (typed failures measured ≤ timeout, never blocking indefinitely). Verified **against the real model**: a genuine cold-start timeout produced `AI_TIMEOUT → REJECT` live.

## 7. Durability & idempotency (§7/§16)

`backend/ai_decision/store.py` — additive `ai_decisions` table in the **same SQLite DB** (no second idempotency system; `IdempotentOrderStore` still owns orders). Every decision stores decision_id, model identity, decision, confidence, reason codes, `input_snapshot_hash`, timestamps, latency. Idempotency key = SHA-256(signal_id + input_snapshot_hash + provider/model/version); identical evaluation replays the stored decision — no duplicate approvals. Scan-side signal ids use the exact `make_signal_id` scheme the pipeline uses, so AI decisions join to executed trades by `signal_id`.

## 8. Copilot chat vs AI decision (§8)

Unchanged separation: **Copilot Chat** (backend/copilot/, grounded, refuse-only execution stub intact, 122 tests passing) explains state; **AI Trading Decision Engine** (backend/ai_decision/, new) participates in trading through the pipeline. They may share the local Ollama provider; responsibilities never mix. The Copilot UI now hosts a read-only **AI Trading Decision panel** fed by `/api/ai-decision/*` (status, why-not-traded breakdown, recent decisions with model + latency).

## 9. Paper path (§6) — AI actually affects paper trading

`PaperMarketScanner.scan_once()` consumes the structured decision via `apply_ai_decision_gate()`: REJECT/WAIT/failure → **no paper order**; APPROVE → payload stamped with AI decision metadata and submitted through the normal `submit_entry` (kill switch → pipeline → sizer → PaperBroker). Proven end-to-end by `test_paper_path_ai_approve_flows_to_submission` (real runtime, real pipeline, real SQLite trade row) and `test_paper_path_respects_ai_reject` (`submit_entry` provably never reached).

## 10. Backtest (§17/§18)

The AI decision layer **cannot** produce reproducible historical decisions (LLM inference at temperature 0 is deterministic per exact prompt, but the historical context builder + per-signal LLM latency make genuine replay impractical), and the legacy ML filter is a stub. Therefore every backtest result now carries `ai_backtest_status = "AI_BACKTEST_UNAVAILABLE"` (engine field + task-manager payload) and the UI shows "Backtest AI layer: AI BACKTEST UNAVAILABLE". **No backtest is labeled AI-assisted when AI was not evaluated. V8-D-only vs AI-assisted remain clearly distinguished.** Also fixed here: latent `ai_decision.probability` attribute bug in the engine's shadow-log line (would have crashed any future `ai_mode != disabled` run).

## 11. Latency (§23)

Measured against the real local Ollama `llama3.2:1b` (see `analysis/ai_decision_latency.json`):

| Run | Phase | Latency | Outcome |
|---|---|---|---|
| 1 | cold (model idle) | 20,094 ms | timeout → `AI_TIMEOUT` REJECT (fail-closed observed live) |
| 2–6 | warm | 3,809–4,338 ms (median ≈ 3.8 s) | valid structured APPROVE |

Per-decision provider/model/latency/timeout/success/error-code telemetry is stored (`ai_decision_latency_log`, surfaced via `/api/ai-decision/status`). The 10 s default was raised to 20 s after live measurement; `max_tokens=128` bounds worst-case repetition loops (~15 s) that previously burned 31 s at 256 tokens.

## 12. Tests (§21)

**31 new tests** in `backend/tests/test_ai_decision_layer.py` — all 20 required scenarios plus contract/context/security units:

1. V8-D + AI APPROVE + Risk PASS → execution permitted ✓
2. AI REJECT → no execution ✓
3. AI WAIT → no execution ✓
4. AI APPROVE + Risk FAIL → no execution ✓
5. AI APPROVE + stale data → no execution ✓
6. AI APPROVE + kill switch → no execution ✓
7. AI timeout → no execution ✓
8. AI provider unavailable → no execution ✓
9. Malformed AI response → no execution ✓
10. Wrong strategy → rejected ✓
11. OPTION_PREMIUM decision under V8-D → rejected ✓
12. Duplicate AI decision → no duplicate execution ✓
13. Restart after AI approval → safe recovery (stored decision replay) ✓
14. Restart after submission → Phase 5 order-intent scheme shared (test 14: scan id == pipeline id; Phase 5 restart attribution tests still green) ✓
15. AI cannot call broker directly (AST test) ✓
16/17/18. AI cannot bypass RiskManager / PositionSizer / ExecutionPipeline (pipeline's own gate rejects even a forged AI stamp) ✓
19. Copilot chat cannot execute (refuse-only stub intact) ✓
20. Paper trading actually respects AI rejection (`submit_entry` never reached) ✓

**Full regression:** `backend/tests` **859 passed** (Phase 5: 828 → +31), `tests/` 30 passed — **0 failed, 0 errors, 0 unexpected skips**. Frontend: `npm ci` clean (Phase 5), `npx tsc --noEmit` clean, `npm run build` ✓ 14.9 s, `npm run lint` 0 errors (21 pre-existing warnings).

## 13. Configuration & invariants (§24)

- `TRADING_MODE=paper`, `TRADING_STRATEGY=V8_D_PULLBACK_ATM`, `UPSTOX_ORDER_PRODUCT=I` — **unchanged** (verified).
- V8-D strategy parameters — **unchanged** (verified; no edits to `v8d_strategy.py`).
- New env (all in `.env.example`, all default-safe): `AI_DECISION_ENABLED=false`, `AI_DECISION_PROVIDER=ollama`, `AI_DECISION_MODEL=llama3.2:1b`, `AI_DECISION_BASE_URL=http://localhost:11434/v1`, `AI_DECISION_TIMEOUT_SECONDS=20`, `AI_DECISION_TEMPERATURE=0`, `AI_DECISION_MAX_TOKENS=128`.
- Enabling the AI layer is an env change + worker restart — **no runtime API toggle exists**.

## 14. Remaining limitations

1. **llama3.2:1b is a small model**: its reasoning is shallow (it echoes context, gives high confidence, and needed contract-side guardrails against schema echo and repetition loops). It is a *demonstration-grade* decision gate, not a proven alpha source. A larger local model (e.g. llama3.1:8b) works via the same adapter but is slower.
2. **Cold-start latency** can exceed the timeout after model unload; the first decision may fail closed (`AI_TIMEOUT`). Mitigations: `ollama keep_alive`, or a warm-up call at worker start (future work).
3. **AI backtest remains unavailable** (see §10) — no AI-assisted performance numbers exist or are claimed.
4. **Confidence is not calibrated** — displayed as "AI confidence" only, never probability of profit.
5. **No profitability claims** of any kind are made — this phase establishes architecture and correctness only.

## 15. Verdicts

| Area | Verdict |
|---|---|
| **PAPER AI-ASSISTED TRADING** | **PASS** — V8-D → AI decision → hard risk → sizing → pipeline → PaperBroker, fully gated, fail-closed, durable, idempotent, tested (AI layer OFF by default; ON via `AI_DECISION_ENABLED=true`) |
| **AI BACKTEST** | **UNAVAILABLE** — honestly labeled `AI_BACKTEST_UNAVAILABLE`; never silently degraded |
| **LIVE** | **DISABLED** — no live order path exists in this build |
| **LIVE BROKER E2E** | **BLOCKED** — no safe broker sandbox is actually available in this environment; nothing was fabricated |

---

*Phase 5 safety preserved in full: order state machine, partial-fill aggregation, ambiguous-response handling, idempotency, restart persistence, reconciliation, risk-before-broker, kill switch, control-token auth, Copilot grounding, secret handling, calendar, durable backtest jobs — all untouched and green (859 backend + 30 root tests).*
