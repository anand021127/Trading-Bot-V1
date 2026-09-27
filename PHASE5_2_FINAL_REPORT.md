# PHASE 5.2 — FINAL REPORT
## AI Trading Reliability, Performance & Validation

**Date:** 2026-09-27 · **Baseline:** `Trading-Bot-V1-PHASE5.1-AI-TRADING-FINAL.zip` (SHA256 `6ed0d62844f3b03d8fac6de10feb65c80c2fa77af0e75f4a9d325984bb4e6e48`) — no earlier phase ZIP was merged. Phase 5 / 5.1 were not redone; their architecture is unchanged:

```
Market Data → V8_D_PULLBACK_ATM → AI Trading Decision → Hard Risk Validation
            → Position Sizing → ExecutionPipeline → PaperBroker
```

**Invariants verified at final state:** `TRADING_MODE=paper` · `TRADING_STRATEGY=V8_D_PULLBACK_ATM` · `UPSTOX_ORDER_PRODUCT=I` · `git diff HEAD -- backend/strategy/strategies/` is **empty** (V8-D parameters byte-identical) · no live order path · Copilot remains refuse-only (`COPILOT_EXECUTION_REMOVED` stub; static regression test asserts no module outside the execution/orders layer can place orders).

---

## A. Defects found (audit of Phase 5.1 implementation — not assumed from the report)

| # | Defect | Where | Impact |
|---|--------|-------|--------|
| 1 | `signal_id` embeds `generated_at` (changes every ~2s scan tick) and was part of the AI idempotency key → **the same continuing setup re-triggered Ollama inference on every scan** | `backend/paper/market_scan_loop.py` (5.1 gate) | Repeated inference, cost/latency waste, decision-table churn |
| 2 | Volatile `candle_age_seconds` (ticks every scan) was inside the AI snapshot → `input_snapshot_hash` poisoned → idempotency never stable | `backend/ai_decision/context.py` | Deduplication impossible; hash not per-setup deterministic |
| 3 | `save_decision` swallowed persistence failures (`except: return True`) while APPROVE proceeded → **an approval could execute with no stored decision** | `backend/ai_decision/store.py` | Unauditable approval — violates the core safety invariant |
| 4 | `reconciliation_ok=True` **hardcoded** at the scanner gate and in the trading-engine display path | `market_scan_loop.py:262`, `trading_engine.py:860` | AI could evaluate while the paper book was unreconciled; UI showed fabricated health |
| 5 | Backtest option-chain snapshot rebuilt everything per bar: full `_lookup_index` scan + full contract-history rescan/sort + full ATR(14) recompute → O(n²) | `backend/backtest/options_data_layer.py` | Backtest wall time dominated by redundant recomputation |
| 6 | `test_single_execution_path.py` fastapi stub condition (`if "fastapi" not in sys.modules`) broke standalone runs | `backend/tests` | Test isolation defect |
| 7 | *(found during 5.2)* The §21 rehearsal harness was first placed in `backend/ai_decision/` — its `paper_runtime` import violated the AST guard that keeps the AI decision core import-clean of broker/runtime machinery | `backend/ai_decision/rehearsal.py` | Architecture guard failure (full-suite catch) — moved to `backend/tests/rehearsal_p52.py` |
| 8 | *(found during 5.2)* Rehearsal `risk_rejection` scenario injected a malformed position → surfaced as `broker_positions_unavailable:KeyError` instead of a typed risk rejection | rehearsal scenario | Evidence quality defect — fixed to a well-formed open BLOCKER position → genuine `MAX_POSITIONS` |

## B. Fixes

- **§2/§3 Setup identity + two-layer dedup** — NEW `backend/ai_decision/setup_identity.py`: `build_setup_id()` = SHA-256[:32] of `SETUP_IDENTITY_VERSION|"v1"|strategy|symbol|direction|instrument_key|strike|expiry|last-candle-timestamp` — deliberately **excludes** `generated_at` and sizing. `decide()` dedup order: exact idempotency key (setup_id + snapshot hash + model identity) → `store.get_latest_setup_decision(setup_id)` → provider call. `setup_id` column added additively (PRAGMA check + ALTER TABLE + index). A genuinely new setup (new bar/contract/direction) still gets a fresh inference.
- **§4 Fail-closed persistence** — `save_decision()` STRICT: `True` only on commit or duplicate-key (already persisted); any `OperationalError`/exception → `False` + `store.available=False`. Engine `_store_and_return()`: APPROVE that cannot be durably stored becomes `AI_DECISION_PERSISTENCE_FAILED` → **NO TRADE**.
- **§5 Real reconciliation** — worker persists `paper_reconcile_ok` ("1"/"0") + `paper_reconcile_detail` in `_tick()`; scanner reads it once per scan; rec-fail short-circuits to `AI_NO_TRADE:RECONCILIATION_NOT_READY` **without calling AI**; V8-D `evaluate` receives the real flag; `trading_engine.py` display path reads the real setting (both §1 hardcodes removed; `v8d_shadow_mode.py` display-default left, labeled display-only).
- **§6/§7 Context + snapshot accuracy** — `candle_age_seconds` removed from hashed snapshot data (`candles_fresh` bool retained); no `datetime.now()` substitutes for market timestamps (remaining uses are contract timestamps/latency logging only); snapshot-hashing determinism property-tested (dict-order invariance; value/contract/bar changes flip the hash).
- **§8/§25 Ollama performance** — `OllamaDecisionProvider.warm_up()` (8-token preload, `keep_alive` default `30m` via `AI_DECISION_KEEP_ALIVE`, timeout ≥30s, never raises) + daemon warmup thread on engine init (startup never blocks) + `warmup_status()` observability + `keep_alive` on every decision call.
- **§16/§17 Backtest performance** — per-contract accelerators in `options_data_layer.py`: sorted per-contract timestamp table, day buckets + bisect lookup, incremental Wilder ATR table (matches `calculate_atr` 6-dp rounding), per-(underlying,option_type) chain-lookup cache; invalidation on new contract registration.
- **§33** — fastapi stub import-order fix. **§34** — static-audit fixes (TODO/FIXME = 0 in touched code).
- **§12/§13/§14 Observability** — `/api/ai-decision/status` now returns `decision_counters` + `rejection_breakdown` (top typed fail reasons); `why-not-traded` taxonomy covers every gate (AI_TIMEOUT, AI_PROVIDER_UNAVAILABLE, AI_MODEL_UNAVAILABLE, AI_INVALID_RESPONSE, AI_DECISION_PERSISTENCE_FAILED, RECONCILIATION_NOT_READY, AI_WAITING, AI_REJECTED, KILL_SWITCH, MAX_TRADES_REACHED, MAX_EXPOSURE_REACHED, RISK_REJECTED, INSUFFICIENT_EQUITY, STALE_OR_INSUFFICIENT_DATA, MARKET_CLOSED, NO_VALID_LOT_SIZE, NO_VALID_CONTRACT, EXECUTION_REJECTED, TRADEDED, V8D_REJECTED); frontend panel renders the counters.
- **§21 Rehearsal harness** — `backend/tests/rehearsal_p52.py`: TEST-labeled end-to-end matrix through the REAL chain (V8-D signal → AI decision → hard risk → sizing → pipeline → PaperBroker → ledger) with an ephemeral temp DB and scripted provider; 10 scenarios; every ledger trade prefixed `REHEARSAL-` and TEST-labeled.

## C. AI latency before/after (all measured, none assumed)

Baseline (Phase 5.1 §23, `analysis/ai_decision_latency.json`): cold first call **20 093.8 ms** → timed out (fail-closed); warm median **3 846.8 ms** (3 809–4 338).

Phase 5.2 live measurements (`analysis/ai_decision_latency_p52.json`, real Ollama `llama3.2:1b`, timeout 20s):

| Session | Cold (model unloaded) | `warm_up()` | Warm median | Warm p95 | Warm success |
|---|---|---|---|---|---|
| Run 1 | APPROVE **19 659.8 ms** — *succeeded just inside timeout* | 454.3 ms | 7 186.8 ms | 7 460.3 ms | 5/5 |
| Run 2 | REJECT / **AI_TIMEOUT** at **20 039.1 ms** — fail-closed | 730.2 ms | 8 231.6 ms | 8 278.9 ms | 5/5 |

Honest reading: warm latency is **machine-load dependent** (this session ran hotter than the 5.1 session — 3.8 s vs 7.2 s medians; both recorded). **No latency improvement is claimed.** What is claimed is architectural: (1) cold-start latency sits at/above the timeout and is non-deterministic — `warm_up()` at worker start removes that exposure (730 ms preload vs a 20 s cold decision); (2) `keep_alive=30m` kept the model resident after measurement (`/api/ps` verified); (3) every failure mode (timeout, unavailability, invalid response) is fail-closed to typed NO-TRADE.

## D. Backtest benchmark before/after

- Option-chain snapshot hot path (labeled fixture `BENCH_FIXTURE`, 2×4000-bar contracts): **0.6 ms → 0.2 ms per bar (~3.6×)**; the gap widens with larger caches.
- **Parity proof:** `analysis/bench_options_snapshot_before_chains.json` and `..._after_chains.json` are **byte-identical** — SHA256 `6a703f99c8dfbf3397e66190d7fb6ca288659fa83cbab8c461c426e96024cdea` for both. Outputs unchanged.
- Staged real-data benchmark (`analysis/bench_phase52.json`, real `NIFTY50_2024` 5-min candles, 17 322 bars): 5d = 0.16 s, 25d = 1.03 s. **Limitation stated honestly:** the local options cache is empty (0 contracts), so this run does not exercise the option hot path against production options data — the hot path is proven via the labeled fixture instead.
- 99 backtest/parity tests green on final code.

## E. AI inference count (duplicate prevention)

- Unit: same setup evaluated **10× → exactly 1 provider inference** (`test_ai_reliability_p52.py`); new candle → new inference; risk-only change → no retrigger; snapshot-hash changes exactly on value/contract/bar changes.
- Rehearsal `duplicate_setup`: `provider_calls_total = 1`, `one_inference_proven = true`, identical `decision_id` across two scans with different `generated_at`.

## F. Duplicate prevention evidence

Setup identity excludes every volatile input; `signal_id` (with `generated_at`) remains only as the pipeline/trade join key, never a dedup key. New-setup → new inference verified in both unit tests and rehearsal.

## G. Persistence failure evidence

SQLite failure injection ×4 (unavailable / locked / schema error / write timeout) → decision becomes `AI_DECISION_PERSISTENCE_FAILED` → **NO TRADE**, no crash loop; store marks itself unavailable; wait/already-persisted paths non-fatal. Provider-failure decisions are latency-logged without row fabrication (5 decision rows reconcile exactly with the storable scenarios).

## H. Reconciliation evidence

Injected ledger-mismatch (forced-reconcile DB wrapper) → scanner returns `AI_NO_TRADE:RECONCILIATION_NOT_READY` **without calling AI**; `RiskContext.reconciliation_ok` is `Optional[bool]` + `reconciliation_status` (UNKNOWN/OK/FAILED surfacing verified); UI display path reads the persisted real value.

## I. Restart evidence

Rehearsal `restart_after_approval`: a fresh engine over the same DB replays the stored approval with **0 provider calls** (`replayed_without_inference: true`). Additive migration survives an existing pre-5.2 DB (PRAGMA-guarded `ALTER TABLE`); restart matrix additionally covered by the idempotency/store tests.

## J. Paper AI rehearsal evidence (`analysis/ai_rehearsal_p52.json`, label **TEST**)

10 scenarios through the real chain: `ai_approve` → submitted, **1 TEST-labeled ledger trade** (`all_test_labeled: true`); `ai_reject`→`AI_NO_TRADE:BAD_SETUP`, `ai_wait`→`AI_NO_TRADE:AMBIGUOUS`, `ai_timeout`→`AI_NO_TRADE:AI_TIMEOUT`, `ai_provider_unavailable`→`AI_NO_TRADE:AI_PROVIDER_UNAVAILABLE`, `ai_malformed` (prose attack)→`AI_NO_TRADE:AI_INVALID_RESPONSE`; `risk_rejection` → AI APPROVE overridden by hard risk → **MAX_POSITIONS** (typed); `duplicate_setup` → 1 inference; `restart_after_approval` → 0 inference. No live-looking trade manufactured anywhere.

## K. Frontend results

`AIDecisionPanel` renders decision counters + rejection breakdown; "AI layer disabled — paper trading runs V8-D-only…" presentation verified for the default-off state. `tsc --noEmit` **0 errors**, `eslint` **0 errors** (21 pre-existing warnings), `npm run build` **✓**.

## L. Security scan

Value-shaped regex scan (Upstox token/LTpk, sk- keys, private keys, SMTP/SECRET values) over all text files + over the final ZIP members: **0 real hits** (2 self-matches are the scanner's own regex literals, excluded). `.env`, tokens, DBs, logs excluded from the ZIP. AST guard: `backend/ai_decision/` cannot import broker/execution/runtime machinery.

## M. Full test count (final code)

`backend/tests`: **884 passed, 0 failed** (116 s) · root `tests/`: **30 passed, 0 failed** → **914 passed, 0 failed total**. (Initial full run caught defect A-7; fixed and re-run to green.)

## N. Remaining limitations

1. **AI backtest remains unavailable** (see classifications) — roadmap: deterministic replay would require recording (context-snapshot → decision) pairs during paper operation and replaying the *stored* decisions against historical bars; a genuine "what would the model have said" replay of history is not reproducible and will not be fabricated. The legacy `backend/ai/predictor.py` stub is deterministic-only, defaults to `ai_mode="disabled"`, and never simulates LLM output.
2. **Live broker E2E blocked** — no safe broker sandbox; nothing fabricated.
3. **Local options cache empty** — production-data backtest hot path not exercised end-to-end; proven on labeled fixture with byte-identical parity.
4. Latency figures are machine-dependent; re-measure on the deployment host.
5. `keep_alive` is an Ollama extension (other OpenAI-compatible providers ignore it harmlessly).
6. AI decision layer ships **OFF** by default (`AI_DECISION_ENABLED=false`); enabling is an explicit env change + worker restart.

---

## FINAL CLASSIFICATIONS

| Classification | Result |
|---|---|
| **PAPER AI-ASSISTED** | **PASS** — V8-D → AI decision → hard risk → sizing → pipeline → PaperBroker; fail-closed on every AI failure; durable; setup-deduplicated; TEST-labeled rehearsal evidence |
| **BACKTEST** | **PASS** — outputs byte-identical under optimization; real-data staged benchmark recorded |
| **AI BACKTEST** | **UNAVAILABLE** — honestly labeled `AI_BACKTEST_UNAVAILABLE`; never simulated |
| **LIVE** | **DISABLED** — no live order path in this build |
| **LIVE BROKER E2E** | **BLOCKED** — no safe sandbox; nothing fabricated |
| **PROFITABILITY** | **NOT ESTABLISHED** — no profitability measured or claimed |

## FINAL ACCEPTANCE CRITERIA

- [x] AI duplicate evaluation controlled — setup identity + 2-layer dedup; 10×→1 inference proven
- [x] AI persistence failure fails closed — `AI_DECISION_PERSISTENCE_FAILED` → NO TRADE (4 injection modes)
- [x] Reconciliation state is real — persisted + read at all gates; rec-fail blocks AI
- [x] AI context verified — no invented/stale values; candle_age out of hash
- [x] AI latency measured — cold/warm/warm_up/p95 recorded honestly
- [x] Ollama warmup implemented/measured — non-blocking preload, keep_alive residency verified
- [x] AI queue bounded — single synchronous gate per scan; timeout bounded (20 s default)
- [x] AI timeout safe — AI_TIMEOUT → NO TRADE (proven vs real model)
- [x] AI provider failure safe — typed NO-TRADE, never auto-approve
- [x] Restart safe — replay without inference; additive migration
- [x] Paper AI path verified — TEST rehearsal through real chain
- [x] Backtest bottleneck identified — per-bar chain rebuild (O(n²))
- [x] Backtest performance improved — ~3.6× hot path with byte-identical outputs
- [x] Backtest results unchanged — SHA256-identical before/after chains
- [x] AI backtest remains honestly unavailable
- [x] Copilot remains explanation-only — refuse-only stub + static regression test
- [x] AI trading decision remains active — the gate is wired in the scan path
- [x] Frontend clean — tsc/lint/build green
- [x] API clean — 3 ai-decision routes; counters end-to-end
- [x] Security clean — value-pattern scan 0 real hits
- [x] Tests pass — 914/914 (884 backend + 30 root)
- [x] Test isolation clean — standalone suite runs green
- [x] V8-D parameters unchanged — `git diff HEAD -- backend/strategy/strategies/` empty
- [x] Paper mode unchanged — `TRADING_MODE=paper`, `UPSTOX_ORDER_PRODUCT=I`
- [x] Live remains disabled
- [x] No profitability claims
- [x] Final ZIP integrity verified — CRC per-member + SHA256 + secret scan (§38 below)

## §38 — Final ZIP

`Trading-Bot-V1-PHASE5.2-FINAL.zip` — excludes `.env`, tokens/credentials, DBs, logs, `node_modules`, `dist`, venvs, **all prior phase ZIPs** (`Trading-Bot-V1-*`, `trading-bot-copilot-*`; verified: zero `.zip`/`.env`/`.db`/`.log` members).

| Property | Value |
|---|---|
| File count | **383 files** (31.6 MB source) |
| ZIP size | **4 663 982 bytes (4.45 MB)** |
| SHA256 | `4f404f4c230dcd79b9fde0f5c4672d90e9dbe4a7a4f4de4db913a5b31244e70a` |
| CRC | **PASS** — every member verified (`zipfile.testzip`) |
| Secret scan | **PASS** — value-pattern scan over all text members: 0 real credentials |
| Created (UTC) | 2026-09-27T01:45:04Z |

Builder: `scripts/make_phase52_zip.py` (same safety pattern as 5.1; refuses >200 MB source sets). No commit or push was performed (§39); nothing outside the workspace was touched.
