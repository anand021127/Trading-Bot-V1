# Final Production Audit — AI Trading Decision, Scanner, Paper Execution

## 1. Is AI really part of the trade decision? (traced in code, proven by tests)
Paper scan path, in order (`backend/paper/market_scan_loop.py::PaperMarketScanner._scan_once_impl`):

market data (Upstox REST) → freshness → expiry → option chain → **V8-D** (`evaluate_v8d_signal`) →
duplicate-position guard → *kill-switch / reconciliation stop* → **AI Trading Decision**
(`AITradingDecisionEngine.decide_with_budget` → `apply_ai_decision_gate`) → `runtime.submit_entry`
(kill switch, EOD, lot size) → `ExecutionPipeline` (risk, sizing, duplicate-signal) → `PaperBroker` → SQLite → API → UI.

* The AI receives the V8-D signal + verified context snapshot (contract, candles freshness, risk state,
  reconciliation, session) and returns a strict-contract decision: APPROVE / REJECT / WAIT.
* **APPROVE is necessary, never sufficient**: risk, sizing, kill switch, daily limit and the pipeline still run.
* REJECT, WAIT, timeout (bounded by `AI_DECISION_BUDGET_SECONDS`), provider down, model missing, invalid JSON,
  strategy mismatch or failure to persist the decision ⇒ **no trade** (fail-closed).
* The AI cannot change price, stop, target, quantity or instrument — the gate only reads the verdict; the order
  payload comes from V8-D + the sizer (test: hostile AI output with `quantity=999999`, `instrument_key=EVIL`).
* The AI decision and the trade share one `signal_id` (auditable from the trade row).
* The `backend/ai_decision` package cannot reach brokers/orders (AST test).
* **Default is `AI_DECISION_ENABLED=false`** (`.env.example`); enable with the env var or the Operations-page toggle
  (re-read every scan). When disabled the dashboard says **AI DISABLED**, never "evaluated".
* **Copilot is a different component**: read-only explanations. Its answers about AI architecture are now
  deterministic (live state), after a small local model told the operator "no other AI … you are the sole AI
  decision-maker" (false).

### Where the AI gate does NOT apply (stated, not hidden)
* **Live mode** (`trading_engine.py`) uses the older ML `ai_predictor` filter (shadow by default), **not** the LLM
  decision engine. Live order placement stays gated and was not touched. Do not arm live until the same gate is
  added and validated in paper.
* **Backtest** cannot replay an LLM; it runs V8-D (+ the optional ML filter). The AI layer can only *remove*
  trades, so backtest/paper strategy identity (V8-D) is unaffected.

## 2. Bugs found and fixed
| # | Problem | Fix | Regression test |
|---|---|---|---|
| 1 | When AI was off / not consulted, nothing was recorded: the UI could not say "AI NOT EVALUATED/DISABLED". | Every scan records AI state; new `build_pipeline` view (Scanner · Market · Strategy · Latest Signal · AI Decision · AI Reason · Risk · Execution). | `test_ai_pipeline_e2e.py` (28) |
| 2 | AI/LLM was consulted (and a decision persisted) even when the kill switch blocked entries. | Stop before the AI call with the runtime's own reason. | `test_kill_switch_stops_before_the_ai_is_even_consulted` |
| 3 | A second worker losing the lock race overwrote the shared pid/status/error rows and left a permanent "Paper worker already running" banner. | Loser exits touching nothing; stale error cleared on clean start / clean scan; Copilot shows worker errors only when unhealthy. | `test_second_worker_losing_the_lock_changes_nothing`, `…stale_worker_error…` |
| 4 | Copilot answered AI-architecture questions by model guesswork (wrong). | Deterministic live-state answer, routed first. | `test_copilot_answers_ai_architecture_…` |
| 5 | 106 double-encoded characters (`â€”`, `â‚¹`) in `llm_adapter.py` (Copilot text + system prompt). | Repaired (round-trip-verified). | scanned in QA |
| 6 | Dashboard had no single "why was/wasn't a trade taken" view. | Decision Pipeline panel on Overview + Copilot Live Context. | `DecisionPipeline.test.tsx` (7) |

(Carried from the previous deliverable: same-bar daily-cap parity in the backtest, scanner always records scans.)

## 3. Verified, no defect
Scanner loop/scheduling, stale-data rejection, market-closed vs scanner-not-running, duplicate-position guard
(before the AI, so no second inference), AI timeout/WAIT replay (one inference per setup), AI persistence fail-closed,
operator toggle, ExecutionPipeline/PaperBroker/risk untouched.

## 4. Not verified here
No live Upstox/Ollama in the QA sandbox: real candle freshness, option-chain size and real model latency/decisions
must be observed on your machine during market hours (09:20–14:45 IST). V8-D BUY scenarios use the deterministic
interface-identical stand-in used across the suite (no synthetic data on production paths).
