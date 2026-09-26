# LIVE RELEASE READINESS — Trading-Bot-V1 (PHASE 5 FINAL)

Date: 2026-09-26 · Baseline: Phase 4 FINAL · Verdicts: **PASS / FAIL / BLOCKED / NOT TESTABLE**, each with evidence.

## Headline verdicts

> **PAPER READY — YES.** Hardened, deterministic, restart-safe, fully tested (828 backend + 30 integration tests, 0 failures).
>
> **LIVE READY — NO. LIVE TRADING REMAINS DISABLED** (`TRADING_MODE=paper`, unchanged).
> Live-broker order behavior has never been exercised against the real Upstox
> order API; those items are marked **BLOCKED — REAL BROKER E2E NOT EXECUTED**.
>
> **STRATEGY PROFITABILITY — NOT CLAIMED.** The Oct-2024 reference windows
> resolve zero trades (no cached option-premium data → honestly INCONCLUSIVE).
> No profitability statement is made in either direction.

---

## 1. Architecture — one execution path

| Item | Verdict | Evidence |
|---|---|---|
| Single canonical entry path (scanner → runtime → ExecutionPipeline → broker/paper) | **PASS** | `backend/execution/pipeline.py` is the only submit gate; strategy identity check at entry; test `test_single_execution_path.py` |
| Copilot cannot execute | **PASS (fixed this phase — CRITICAL)** | `backend/copilot/execution.py` previously reconstructed `StrategySignal(strategy_name="OPTION_PREMIUM")` and called `execute_multi_signal()` from the live-scanner hook — a real duplicate execution path with the wrong strategy identity. **REMOVED**: module is now a refuse-only stub; the scanner hook only logs (`COPILOT_OBSERVED_NOT_EXECUTED`). Tests: `TestCopilotExecutionRemoved`, `TestScannerCopilotHookObservationOnly` (hook creates no position in paper/shadow/live-global-mode), AST static check proving no order-placement call survives in Copilot code |
| No direct broker calls from strategy/Copilot | **PASS** | static audit: `place_order` reachable only from `orders/`, `execution/`, `paper/paper_runtime` (via OrderManager), `broker/upstox_client` (implementation) |

## 2. Order state machine

| Item | Verdict | Evidence |
|---|---|---|
| 13-state machine, legal transitions enforced | **PASS** | `backend/orders/order_state.py`; 16 tests incl. FILLED→CREATED, REJECTED→FILLED, CANCELLED→FILLED, CREATED→FILLED all rejected |
| UNKNOWN only resolvable via RECONCILING | **PASS** | `UNKNOWN → {RECONCILING}` is the only outgoing edge; 16/16 tests green |
| Broker status normalization, never guessed | **PASS** | `broker_state()` maps known Upstox strings; unknowns → UNKNOWN (tested, incl. whitespace variants) |

## 3. Ambiguous broker responses (CRITICAL)

| Item | Verdict | Evidence |
|---|---|---|
| Timeout / reset / 5xx / DNS / malformed → UNKNOWN, reconcile before retry | **PASS (deterministic adapter level)** | `backend/orders/broker_responses.py`; `classify_broker_exception` + `classify_place_response`; 7 classification tests |
| No blind resubmit | **PASS** | pipeline marks intent `SUBMISSION_UNKNOWN` and returns `ORDER_STATE_UNKNOWN`; same-signal retry is a hard duplicate — `test_timeout_then_retry_does_not_double_submit` (exactly 1 broker attempt), `test_unknown_intent_resolved_by_reconciliation` |
| Real Upstox behavior verification | **BLOCKED — REAL BROKER E2E NOT EXECUTED** | no sandbox credentials; classifier behavior against the REAL API's exact exception shapes is unverified |
| Duplicate responses / delayed responses | **PASS (by construction)** | idempotency key is content-derived (strategy+timestamp+instrument+direction); a duplicate response carries the same order id and cannot create a second intent — tested ×100 |

## 4. Idempotency

| Item | Verdict | Evidence |
|---|---|---|
| Same signal ⇒ one order, durable across restarts | **PASS** | `test_order_intent_restart.py` (3 tests): intent / SUBMISSION_UNKNOWN / SUBMITTED+order-id all survive a DB-handle restart and keep gating |
| Failed validation does not poison the store | **PASS** | validation failures return BEFORE `remember_intent`; `clear_intent` only fires on PROVEN pre-acceptance rejection — `test_known_rejection_clears_intent_for_legitimate_retry` (corrected retry succeeds, still one real order) |
| Intent created at the correct lifecycle point | **PASS** | created after strategy/risk/contract checks, immediately before the broker call (`pipeline.py` ordering) |

## 5. Partial fills

| Item | Verdict | Evidence |
|---|---|---|
| Fill aggregation (25/50/25 → filled/remaining/avg/capital) | **PASS (adapter level)** | `FillAggregator`: 7 tests — first partial never treated as complete; capital_used = actual avg × actual filled; overfill/duplicate-fill rejected |
| Cancel-with-partial keeps fills | **PASS** | `test_cancel_with_partial_fill_keeps_fills` — position truth = 30/100, capital = 30×55 |
| Broker-reported FILLED without fills → RECONCILING | **PASS** | never trusts status without fill evidence |
| Real partial-fill flow against live broker | **BLOCKED — REAL BROKER E2E NOT EXECUTED** | order book/fill polling against real Upstox unverified |

## 6. Exits

| Item | Verdict | Evidence |
|---|---|---|
| Exactly one effective exit; duplicate exit gated | **PASS** | pipeline `submit_exit` uses the same intent store (`duplicate_exit`); paper runtime has a duplicate-exit guard (phase-1 suite, `test_exit_after_restart_uses_restored_stop_and_persists_once`) |
| Exit ambiguity → reconcile, not blind retry | **PASS** | `EXIT_STATE_UNKNOWN` gate added to `submit_exit` this phase |
| SL/target/trailing/EOD exits | **PASS (paper)** | phase-1/4 paper suites (SL, target, trailing, EOD square-off, restart-restored stop) |

## 7. Reconciliation & restart

| Item | Verdict | Evidence |
|---|---|---|
| Ledger↔broker mismatch → fail-closed | **PASS** | `recover_and_reconcile_positions` (orphan broker/local, qty mismatch → trading_halted); paper reconcile never touches Upstox; phase-1 orphan self-heal tests |
| Restart at order-lifecycle points | **PASS (local-layer)** | no duplicate order (durable intents), no duplicate exit, no lost position/SL/lot (phase-1 hydration suite), counters survive (daily_counters), metadata survives (`positions.extra`, trades metadata contract) |
| Restart mid-broker-call (crash between ack and DB commit) | **BLOCKED — REAL BROKER E2E NOT EXECUTED** | the UNKNOWN-intent design covers it deterministically, but the actual broker-side outcome (does the order exist?) can only be confirmed against the real API |

## 8. Risk & kill switch

| Item | Verdict | Evidence |
|---|---|---|
| Risk BEFORE broker; rejection ⇒ broker calls = 0 | **PASS** | spy tests: `test_max_positions_rejection_never_reaches_broker`, `test_kill_switch_blocks_submission_broker_never_called`, `test_invalid_strategy_identity_rejected_before_broker` |
| Risk coverage (capital/exposure/trades/positions/daily loss/lot/allocation) | **PASS** | `execution_guard.evaluate_pretrade_guard` + pipeline limits; phase-1 failure-injection matrix (notional > cap rejected with real numbers) |
| Kill switch semantics | **PASS** | 3 levels (STOP_NEW_ENTRIES / CLOSE_EXISTING / FULL_STOP); entries blocked at pipeline entry; exits still permitted (`submit_exit` works under FULL_SYSTEM_STOP) — never leaves a position unmanaged |

## 9. Security

| Item | Verdict | Evidence |
|---|---|---|
| State-changing endpoints protected | **PASS (opt-in, default no-op)** | `backend/api/control_auth.py`: when `CONTROL_TOKEN` is set, bot-control/settings/paper/trading/backtest routers require it (header or bearer, timing-safe, 401s tested); **Upstox token never accepted as control token** (tested). Default (unset) = no-op for local/paper operation — **MEDIUM finding**: production should set CONTROL_TOKEN and keep the VPN/allowlist posture documented in the runbook |
| CORS | **PASS** | explicit allowlist + vercel regex; no wildcard |
| Token hygiene | **PASS** | no token in Git/ZIP/API responses/logs (phase-1 + phase-5 scans); OAuth callback & expired-token paths tested (phase-1 suites) |

## 10. Calendar / session / contract integrity

| Item | Verdict | Evidence |
|---|---|---|
| One authoritative calendar used everywhere | **PASS** | phase-4 integration; remaining `weekday()` code paths are documented in-function fail-safe fallbacks reachable only if the calendar itself fails |
| Holidays / special sessions / expiry shift / boundaries / IST | **PASS** | 32 calendar tests (2024 NSE circular CMTR59722 + 2025/2026 verified tables, Muhurat, Budget Saturday, fail-closed for unverified years) |
| Contract fields coherent, lot from metadata only, no strike-interval-as-lot | **PASS** | `contract_validator` + phase-1 `INVALID_LOT_SIZE` rejection tests; backtest resolver uses expired-instruments API |

## 11. Data / database / observability

| Item | Verdict | Evidence |
|---|---|---|
| Stale/future/corrupt data rejected; wrong-symbol/expiry/strike rejected | **PASS** | candles freshness gate, quote-age 30s cap, contract validator checks (phase-1 suites) |
| SQLite durability | **PASS** | WAL + busy_timeout + transactions + atomic counters + idempotent intents (phase-1/4 tests); jobs DB survives restart (phase-4/5 tests) |
| Observability fields on every order/fill/exit | **PASS** | ORDER/EXIT_ORDER logs carry strategy, signal_id, instrument, qty, order_id, status; intent rows carry broker_order_id + status; secrets never logged (phase-1 test) |

## 12. Copilot safety & grounding

| Item | Verdict | Evidence |
|---|---|---|
| Observation/explanation only | **PASS (fixed this phase)** | see §1; refuse-only stub + hook observation-only + AST check |
| Grounding guard | **PASS** | `grounding_guard.py`: fabricated-activity/P&L, false no-data denial, execution-authority claims detected; contradicting LLM answers are **discarded** and the deterministic fallback (verified numbers only) is served — `test_lying_adapter_answer_is_discarded` proves the end-to-end discard |
| V8-D identity, no OPTION_PREMIUM fallback | **PASS** | `build_trade_plan_for_symbol_v8d` uses `evaluate_configured_strategy` and stamps the payload with the configured strategy; legacy `evaluate_option_premium` path remains explicitly labeled as the research strategy |
| Freshness from source timestamps | **PASS** | `validate_context_freshness` uses candle/chain timestamps; STALE flagged; unparseable source ts fails closed |
| ML vs LLM separation | **PASS (existing)** | AI layer reports `ai_mode` honestly; disabled by default — no fake predictions (phase-4 audit) |

## 13. Backtest integrity & performance

| Item | Verdict | Evidence |
|---|---|---|
| No lookahead / real contracts / real expiry / real lot / real premium | **PASS** | phase-1/4 suites (lookahead, expired-instruments resolver, DATA_UNAVAILABLE fail-closed) |
| Durable jobs; restart in any state → INTERRUPTED_BY_RESTART, never COMPLETED | **PASS** | phase-4 job-store + contract suites (15+9 tests) |
| Cancellation stops work | **PASS** | phase-4 cancel tests |
| Benchmark equality vs Phase 4 | **PASS** | `analysis/bench_phase5.json`: 1d/5d/25d trades/net_pnl/validity/coverage/signals **identical**; timings 0.10/0.20/2.91 s (25s-window variance is OneDrive I/O noise — same code path; bars/sec ≈ 566) |
| Backtest correctness re-verified post-changes | **PASS** | deterministic re-run equality (above) |

## 14. CI / deployment / frontend

| Item | Verdict | Evidence |
|---|---|---|
| CI gates (backend, integration, frontend build+tsc+lint, secret scans, packaging, git diff --check) | **PASS** | `.github/workflows/ci.yml` (phase 4); paper/offline env enforced; branch protection must be enabled on GitHub (operator action — MEDIUM) |
| Frontend consistency | **PASS** | npm ci clean, tsc clean, vite build ✓ 9.5 s, eslint 0 errors (21 pre-existing warnings, documented) |
| systemd/deployment docs | **PASS** | `deploy/systemd/upstox-bot.service` (paper-enforced) matches runbook; DB path/log dir/restart policy documented |
| Health API honesty | **PASS** | component-level health (scanner/ws/engine/db/worker/pipeline); "HEALTHY" requires component checks, not just FastAPI responding |

---

## Remaining issues (severity-classified)

| # | Issue | Severity | Status |
|---|---|---|---|
| 1 | Copilot execution path existed (wrong strategy identity + duplicate path) | **BLOCKER (fixed)** | **FIXED this phase** — removed & tested |
| 2 | No live-broker E2E (order lifecycle, partial fills, real ambiguity) | **HIGH (for LIVE only)** | **BLOCKED — REAL BROKER E2E NOT EXECUTED**; does not block paper |
| 3 | State-changing endpoints unauthenticated by default | **MEDIUM** | Mitigated: optional control token shipped & tested; operator must set `CONTROL_TOKEN` in production |
| 4 | Branch protection / required CI checks not verifiable from repo alone | **MEDIUM** | Operator action on GitHub settings |
| 5 | 21 eslint warnings (no-explicit-any, hook deps) in pre-existing frontend files | **LOW** | Not functional; scheduled cleanup |
| 6 | NSE holiday tables require annual manual update (fail-closed if missing) | **LOW** | By design: extend `BUILTIN_HOLIDAYS` from the official circular |
| 7 | 25-day benchmark wall-time variance on OneDrive-synced disk | **INFORMATIONAL** | I/O noise; results identical |

**No BLOCKER or HIGH issue remains open for PAPER operation.**
**HIGH issue #2 (real-broker E2E) remains open for LIVE — therefore LIVE READY = NO.**

## Final classification

* **PAPER READY: YES** — deploy with `TRADING_MODE=paper`, `CONTROL_TOKEN` set, VPN/allowlist per runbook.
* **LIVE READY: NO — LIVE TRADING REMAINS DISABLED.** Enabling live requires: real-broker order-lifecycle E2E (ambiguous response, partial fill, cancel-with-fills), authenticated control plane verified on the production host, and operator sign-off. The deterministic scaffolding (state machine, ambiguity gate, idempotency, reconciliation) is in place and tested at the adapter level.
* **STRATEGY PROFITABILITY: NOT CLAIMED** — reference windows are honestly INCONCLUSIVE (zero cached option-premium data); no optimization was performed, per instructions.
