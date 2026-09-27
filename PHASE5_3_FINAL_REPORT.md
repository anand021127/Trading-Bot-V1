# PHASE 5.3 — FINAL PRODUCTION ENGINEERING REPORT

## FINAL STATUS

| Area | Verdict |
|---|---|
| **Software QA** | **PASS** — 922 backend + 30 root tests = **952 passed, 0 failed** (`pytest -q`, order-independent; includes 8 new expiry-lifecycle regression tests + 2 new paper entry-window tests); frontend `npm run build` clean (tsc/lint clean as of the 5.3 pass) |
| **Paper** | **PASS** — unchanged architecture, exercised by the full regression suite; reconciliation staleness + current-equity fixes verified in the real scan path |
| **AI** | **PASS (paper, fail-closed)** — new bounded-budget decide, token-capped inference (−42% warm latency, measured), runtime UI toggle, persistence fail-closed preserved |
| **BANKEX** | **PASS (code + data path)** — end-to-end as the 6th index with dynamic broker-metadata resolution; historical OPTIONS backtest for BANKEX (and all indices) remains data-blocked, honestly refused |
| **Upstox API** | **BLOCKED (live network)** — all integration code tested offline by design guard; no live API/order call was made or fabricated |
| **Live Broker E2E** | **BLOCKED** — no safe broker sandbox available; no real-money order placed per the phase rules |
| **Live Execution** | **BLOCKED (engineering complete, execution unproven)** — the live path is fully engineered and gated (§3–§5 below), but LIVE readiness cannot be claimed without a real broker exercise |
| **Performance** | **PASS (measured)** — p50/p95/max recorded for the AI decision path and backtest engine; live signal→order p50/p95/p99 **NOT MEASURABLE** without a live broker (BLOCKED, not skipped) |
| **Security** | **PASS** — secret scan 373 files: 0 hits; control-plane auth covers the new mode/AI/kill endpoints; tokens never logged; live arming is two-step, backend-enforced |
| **Profitability** | **NOT ESTABLISHED** — no claim in any direction |
| **Backtest lifecycle (5.3B)** | **PASS (fixed + regression-tested)** — real-data forensics found an expired option held ~6.5 months to BACKTEST_END; engine now force-closes at actual contract expiry (`EXPIRY_FORCED_CLOSE`); 8 regression tests pin it |
| **V8-D strategy (5.3B)** | **UNCHANGED (frozen, evidence-based)** — OLD vs NEW train/validation evaluation on the real 277-trade run adopted NO parameter change; every in-sample improvement failed held-out validation (`analysis/v8d_old_vs_new_p53.json`) |

Every non-PASS item is explained in **§7 Verdict detail** below.

**Baseline:** Phase 5.2 final (`Trading-Bot-V1-PHASE5.2-FINAL.zip`, 914 tests green). V8-D parameters byte-identical throughout (`git diff HEAD -- backend/strategy/strategies/` = empty). Nothing was committed or pushed.

---

## 1. What this pass changed (and why)

This was the final engineering pass: make the existing bot production-grade for real Upstox live trading, add BANKEX correctly, make execution/API paths fast, expose operational controls in the UI, and eliminate remaining reliability/security issues — without redesigning the strategy or touching V8-D.

### §2 BANKEX — 6th supported index (end-to-end)

- `backend/config/universe_config.py`: `VALID_OPTION_INDICES` is now exactly `NIFTY50, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX`, plus shared `INDEX_EXCHANGE` (BANKEX/SENSEX → BSE) and nominal `INDEX_STRIKE_STEP` tables (cross-checks, never sources). Every gate that imports `VALID_OPTION_INDICES` (universe API, strategy API, options API, trading API, backtest API, scanner, Upstox client, chain summarizer) accepts BANKEX immediately.
- NEW `backend/broker/contract_metadata.py` — live contract resolution for all six indices from **broker metadata only**: exchange segment must match the underlying (BANKEX → `BSE_FO`, mismatch = typed `ContractResolutionError: exchange_mismatch`), lot size from the chain row or the daily-refreshed instrument master (never hardcoded — a BANKEX lot change at the exchange is picked up automatically), tick size from the master when present (never invented), expiry must be broker-provided and strictly future, zero-LTP rows flagged as non-tradable. A failed resolution is a typed refusal — **never a substitute contract, never a guessed one**.
- BANKEX static fallback key `BSE_INDEX|BANKEX` was already in `INDEX_TO_KEY`; live resolution prefers the current instrument master.
- Backtest: BANKEX real underlying data (`real_data/BANKEX_2024_5min.json`, 12 607 candles) runs through the production engine; the historical contract resolver already carried BANKEX (BSE_FO, Monday-expiry, step-100). Missing historical OPTIONS data is refused honestly (`BACKTEST_INCOMPLETE` / `FAILED_INCOMPLETE_COVERAGE`, "Not padded with synthetic data") — no manufactured BANKEX option history.
- Tests: 8 dedicated BANKEX tests (universe, fallback key, metadata resolution incl. lot-size-from-master, chain-lot precedence, wrong-segment refusal, unresolved-lot refusal, expired/guessed-expiry refusal, unsupported-underlying refusal, backtest whitelist).

### §3–§5 Live path — engineered, gated, and guarded

- **§5 Order safety (critical duplicate-order fix):** `_build_session()` excluded **POST** from urllib3 `Retry.allowed_methods`. Previously a 5xx/timeout *after broker acceptance* would be auto-retried — a real duplicate-order risk. Now: order placement is single-attempt; ambiguous outcomes are classified (`classify_broker_exception` → `ResponseKind.UNKNOWN` → durable `SUBMISSION_UNKNOWN` intent → reconcile, never blind-resubmit). Tested (`test_upstox_session_does_not_retry_post`).
- **§10 LIVE readiness gate** — NEW `backend/execution/live_gate.py::evaluate_live_readiness()`: read-only evaluation of ten mandatory conditions against REAL state (env mode, Upstox profile auth, broker funds > 0, instrument master fresh+loaded, reconciliation fresh-OK ≤ 15 min, risk config resolvable, kill switch clear, strategy == `V8_D_PULLBACK_ATM`, all six underlyings resolvable, pipeline armed). Returns `ready` + exact `blocked_reasons` + per-check detail. `POST /api/bot/mode {"mode":"live"}` refuses with those reasons unless every check passes; even then, arming execution requires a worker restart with `TRADING_MODE=live` — **two-step arming; a UI click alone never arms live**.
- **§6 Reconciliation staleness:** worker now persists `checked_at` (UTC) with every reconcile verdict; the scan gate reads it and returns typed `AI_NO_TRADE:RECONCILIATION_STALE` when an OK verdict is older than `RECONCILE_MAX_AGE_SECONDS` (15 min) — no new trades on stale state. Never-checked and FAILED states were already no-trade (5.2); FAILED now additionally proven to skip the AI call entirely (new test).
- **§7 Current equity:** the scan path now evaluates V8-D with `runtime.realized_equity` (persisted, P&L-adjusted, restart-safe) instead of the frozen `TRADING_CAPITAL` startup constant; the worker seeds the scanner from the persisted equity snapshot. Verified by a test where realized P&L moves equity to 104 250 and the strategy must see exactly that. The same runtime state flows into the AI `RiskContext` (5.2) and the pipeline's state provider.
- **§17 Contract resolution:** every live entry already validated instrument key/segment, future expiry, lot-multiple quantity, positive premium, spot/LTP sanity, quote age ≤ 30 s (`validate_option_contract`); the new metadata resolver adds master-backed lot/tick/segment authority on top.

### §4/§13/§16 Speed — measured, not asserted

- **Pooled keep-alive HTTP everywhere:** the Upstox client already shared one `requests.Session` (pooled, retries for idempotent GET/DELETE); `instrument_master.py` now fetches the daily master through a shared pooled session instead of a per-call connection; the AI decision provider now posts through **one shared keep-alive session per process** (pooled across the 2 s scan cadence).
- **Token handling:** kept per-call authoritative resolution (local-file cost, but rotates/invalidates safely — a TTL cache was prototyped and **rejected** because it broke immediate rotation, verified against the token-lifecycle regression tests). A 401 hook (`invalidate_token_cache`) remains for future caching. Tokens are never logged (fingerprint-only).
- **AI latency — the real bottleneck, found by A/B:** tiny-prompt (8-token) calls take ~0.43 s while decision calls took ~12–15 s with a *small* payload — latency is dominated by **generated tokens** (repetition loops burn the whole `max_tokens` budget), not prompt size or HTTP. Fixes: `AI_DECISION_MAX_TOKENS` 128 → **64** (the valid decision envelope needs <50 tokens; truncated JSON → typed `AI_INVALID_RESPONSE` → NO TRADE, never a guess) plus a compacted system prompt preserving every safety rule. **Measured result (same session, same load): warm median 15 932 ms → 9 171 ms (−42%).** Remaining bottleneck is CPU inference speed of the local model — honestly recorded, with the mitigation below.
- **§12/§13 Bounded AI in the trading loop:** NEW `decide_with_budget(max_wait_seconds)` — the scan never blocks longer than `AI_DECISION_BUDGET_SECONDS` (default 10, ≤ provider timeout) while holding a live signal. A decision finishing inside the budget is used normally; a slow one returns typed **WAIT (`AI_WAITING`) → NO TRADE this tick** while inference completes on its worker thread and the stored verdict replays via setup dedup on a later scan of the same setup (no second inference). `AI: TOO SLOW FOR LIVE` is effectively expressed by the measured latency + the UI's AI OFF switch — V8-D behavior is unchanged when AI is off.
- **§16 Market data:** the scan path already fetches candles + one chain + one expiry per tick (3 idempotent GETs, now all pooled); the instrument master is 24 h-TTL cached; option ATR enrichment is capped to nearest-ATM contracts. No per-tick structure rebuilds were found in the live path.

### §8/§9/§10/§30/§31 Operations control plane (UI + API)

- **NEW `/api/bot/operations`** — one payload for the whole dashboard: mode, strategy, broker, market session (authoritative exchange calendar), API/data health, reconciliation state + age, kill switch level, AI status (enabled/override/provider/model/timeout/worker note), LIVE readiness verdict with blocked reasons, bot running flag.
- **`POST /api/bot/ai-toggle`** — runtime AI ON/OFF via DB override read every scan tick (no .env edit, no restart; `ai_effectively_enabled()` defines the authority: override wins over env default, fail-closed to OFF). Test-proven: override OFF → zero AI calls; override ON → AI called. AI ON never bypasses hard risk — it sits before RiskManager in the chain, never instead of it.
- **`POST /api/bot/mode`** — server-validated PAPER/LIVE switch with the readiness gate; frontend can only request, never bypass.
- **NEW `/operations` page (TRADING CONTROL)** — the §30 box exactly: Mode / Strategy / AI / Broker / Market / API / Data / Reconcile / Kill Switch / Live Readiness rows, the blocked-reason list, and four controls with confirmation on dangerous ones: `[ENABLE/DISABLE AI] [PAPER MODE] [LIVE MODE] [🔴 KILL SWITCH]`. All state lives in the backend; the UI is display + request only. Registered at `/operations` with nav entry.
- **§31 why-not-traded** — taxonomy extended: `RECONCILIATION_STALE`, `AI_WAITING` (budget), `BROKER_UNAVAILABLE` (submit/candle/chain/expiry fetch errors), `BROKER_REJECTED` — every normal no-trade now has a distinct typed stage; no "something went wrong".
- All control endpoints inherit the `require_control_token` router guard (`X-Control-Token` / Bearer, timing-safe, no-op unless `CONTROL_TOKEN` is set, Upstox token never accepted).

### §22/§23 Parity + §26/§27 durability

- Paper and live share one architecture (strategy → AI → hard risk → sizer → ExecutionPipeline → broker adapter); only the broker implementation differs. NEW parity test: identical signal/account/contract through `ExecutionPipeline` with paper vs live broker client produces the **same acceptance, same signal_id, same quantity, same instrument key** before any broker submission.
- Order state machine verified against the required states (`CREATED…UNKNOWN`, `CANCEL_REQUESTED` included); `UNKNOWN` is non-terminal and never auto-promoted without broker evidence; broker status normalization maps unknown strings to `UNKNOWN`, never guesses.
- Restart/crash safety: existing suite covers AI evaluation/approval/persistence, order submission, partial fills, position open/close, ledger hydration (idempotent start, durable intents `SUBMISSION_UNKNOWN`, equity snapshot restore). New 5.3 tests add reconciliation-age and override restarts.
- §26 SQLite: WAL + 30 s busy_timeout + NORMAL sync confirmed in code; `order_intents.signal_id` PRIMARY KEY + `ux_ai_decisions_idem` UNIQUE index + signal/setup indexes confirmed; ai-decision latency rows are the only per-decision writes (already batched per call); no per-tick writes added.

## 2. Performance measurements (real, not fabricated)

| Measurement | p50 | p95 | max | Source |
|---|---|---|---|---|
| AI warm decision (max_tokens=128, pooled) | 15 932 ms | 16 002 ms | 16 819 ms | `analysis/ai_decision_latency_p52.json` |
| AI warm decision (**max_tokens=64**, pooled) | **9 171 ms** | 9 411 ms | 9 434 ms | same, phase53_optimization block |
| AI `warm_up()` preload | 388–730 ms | — | 730 ms | same |
| AI cold (model unloaded) | timeout-adjacent: 17 560–20 040 ms, fail-closed | — | 20 040 ms (`AI_TIMEOUT` → NO TRADE) | same |
| Raw A/B: 8-token vs decision prompt | 423 ms vs 11 797–15 153 ms | — | — | phase53_optimization.ab_evidence |
| Scan AI budget (new) | ≤ 10 000 ms blocking, then typed WAIT | — | — | `AI_DECISION_BUDGET_SECONDS` |
| Backtest engine, BANKEX real data | 25d = 1 200 bars in 1.32 s (~905 bars/s) | — | — | `analysis/bench_phase53_bankex.json` |
| Backtest engine, NIFTY50 real data | 25d = 1 200 bars in 1.88 s | — | — | same |
| **Live signal→order (broker RTT)** | **NOT MEASURABLE** | NOT MEASURABLE | — | **BLOCKED** — requires live broker; will be recorded in the runbook at enablement |

Component breakdown for the live path (by construction): market data 3 pooled GETs, strategy evaluation (in-process, ms-scale, measured in scan tests), AI ≤ 10 s budget (only when ON), risk+sizing in-process (µs–ms), DB writes short WAL transactions, broker RTT unmeasured (BLOCKED). No safety check was removed for speed; the token cap and pooling are the only latency changes, and both preserve fail-closed behavior.

## 3. Test & verification evidence (§33/§36)

- `pytest -q` (`backend/tests` + `tests`): **922 + 30 = 952 passed, 0 failed** in one combined order-independent run (~1:45). Includes the Phase 5.3 additions (`test_live_readiness_p53.py`, 26 tests) and the Phase 5.3B additions: `test_backtest_expiry_lifecycle.py` (8) + 2 entry-window tests in `test_market_scan_loop.py`.
- Frontend: `npm ci` ✓, `npm run build` ✓, `npx tsc --noEmit` 0 errors, `npm run lint` 0 errors (21 pre-existing warnings).
- Static architecture audit (§34): `reconciliation_ok=True` hardcodes — **0** in production code; `datetime.now` in `backend/ai_decision|orders|execution` only for decision/latency timestamps and staleness math (legitimate, never substituting market data); `OPTION_PREMIUM` references are the guarded legacy strategy (refuse-only Copilot stub, explicit refusals on silent fallback, identity tests) — no execution path; `execute_multi_signal` reachable only inside `TradingEngine` (the configured live path) — Copilot cannot reach it (AST-guarded); order placement reachable only via `orders/ | execution/ | paper_runtime | upstox_client` implementation.
- Secret scan (§35): 373 text files with value-shaped patterns (Upstox token/LTpk, sk- keys, private keys, SMTP/SECRET values, bearer literals) — **0 hits**. ZIP scan repeats it per-member.
- BANKEX historical options data: cache directory `real_data/options_cache` is EMPTY (0 files) — options-required backtests are refused honestly on this machine; classified BLOCKED (environmental), never simulated.

## 4. Deliverables (§38)

| File | Content |
|---|---|
| `PHASE5_3_FINAL_REPORT.md` | this report (FINAL STATUS at top per §39) |
| `LIVE_RELEASE_READINESS.md` | updated 5.3 readiness matrix (PASS/FAIL/BLOCKED/NOT TESTED per item) |
| `PRODUCTION_ENGINEERING_AUDIT.md` | updated static/architecture audit findings incl. the POST-retry defect |
| `PRODUCTION_RUNBOOK.md` | updated: operations dashboard, ai-toggle/mode/kill control plane, AI budget env |
| `AI_TRADING_READINESS.md` | updated: runtime toggle, token-cap latency findings |
| `Trading-Bot-V1-FINAL-PRODUCTION.zip` | final source ZIP (excludes `.env`, secrets, production DBs, logs, prior ZIPs, temp files; includes `deploy/systemd/upstox-bot.service`, `deploy/nginx/upstoxbot.conf`, migrations/docs/tests) with CRC, SHA256, secret scan (facts below) |

## 5. Acceptance checklist

- [x] V8-D parameters/signal logic untouched — byte-identical
- [x] Paper behavior unchanged (full suite green through the same path)
- [x] BANKEX end-to-end (universe, resolution, metadata, validation, tests, UI lists, honest data refusal)
- [x] Live path engineered: pooled HTTP, no-POST-retry, idempotency, order state machine, reconciliation freshness
- [x] Live readiness gate with exact reasons + two-step arming
- [x] Reconciliation: OK/FAILED/STALE/never — all no-trade unless fresh OK; age exposed
- [x] Current equity flows to scanner/strategy/AI/risk; startup capital no longer frozen into decisions
- [x] AI: UI toggle (no .env), bounded budget, token-capped latency (−42% measured), fail-closed everywhere, dedup preserved
- [x] Operations dashboard (§30 box) + full why-not-traded taxonomy (§31)
- [x] Control endpoints authenticated; tokens never logged/exposed
- [x] Copilot explanation-only; refuse-only guard intact
- [x] 952 tests green via `pytest -q`; frontend build clean (tsc/lint clean as of the 5.3 pass)
- [x] Secret scan clean; no .env/tokens/DBs/logs in the ZIP
- [x] No profitability claim; no fabricated results; nothing committed or pushed
- [ ] **LIVE BROKER E2E — intentionally NOT checked**: cannot pass without a real broker exercise (BLOCKED, §7)

## 6. Remaining limitations (honest)

1. **Live broker E2E is unproven** — the order path, funds/profile reads, and reconciliation are tested against doubles and offline guards only; Upstox offers no sandbox here and real-money orders were forbidden by the phase rules.
2. **Live latency p50/p95/p99** requires that same exercise; the runbook documents exactly what to record at enablement.
3. **AI decision latency (~9 s warm) is real** — CPU-bound for llama3.2:1b. With the 10 s budget the scan is safe, but operators who need faster loops should run AI OFF (one click) or a faster model/host; the UI communicates both.
4. **Historical options cache is empty locally** — options backtests (all indices incl. BANKEX) are BLOCKED until an operator populates real cached option candles; nothing was synthesized.
5. **AI backtest remains unavailable** (`AI_BACKTEST_UNAVAILABLE`), unchanged and honest.

## 7. Verdict detail (per §36/§37 — every non-PASS explained)

- **UPSTOX API — BLOCKED (live network):** all client behavior (pooling, retry policy, 401 semantics, order payload, funds/profile parsing, token rotation) is tested; the offline guard deliberately blocks real HTTP in tests. Verdict would become PASS only after live read-only exercises (profile/funds/chain) against a real token.
- **LIVE BROKER E2E / LIVE EXECUTION — BLOCKED:** no sandbox exists in this environment; placing a real-money order merely to test was prohibited. The engineering (gate, state machine, idempotency, reconciliation, kill switch) is complete and unit/integration tested; execution itself is unproven. The mode endpoint therefore refuses LIVE with exact reasons on any real deployment until an operator supplies and verifies a real token, funds, and fresh reconciliation.
- **BANKEX historical options backtest — BLOCKED (environmental):** the engine/refusal path is tested and the real underlying data runs; the missing piece is real historical option candles, which were not fabricated.
- **PROFITABILITY — NOT ESTABLISHED:** correctness ≠ profitability; no backtest of AI-assisted performance exists; nothing is claimed.

---

## 8. PHASE 5.3B — Backtest forensics & position-lifecycle fix (2026-09-27)

Trigger: the user-supplied REAL Upstox v3 backtest CSV
`upstox_backtest_nifty50_banknifty_finnifty_+3_20250926_20260927.csv`
(2025-09-26 → 2026-09-27, 6 indices, V8_D_PULLBACK_ATM: 277 trades, 28.88% WR,
net −₹81,511.32, PF 0.61, maxDD 84.31%, 808 signals / 110,137 rejections).
Every number below was parsed from that CSV by
`analysis/backtest_forensics_p53.py` (artifacts: `analysis/backtest_forensics_p53.{json,txt}`)
— nothing was re-simulated or fabricated.

### §8.1 The suspicious trade, confirmed and explained

`SENSEX2630579900CE` — entry 2026-03-05 **15:25 IST**, contract expiry
**2026-03-05**, exit 2026-09-25 15:25 with reason `BACKTEST_END`, gross P&L
exactly **0.0** (the option was never priced again after entry). An expired
contract was held ~6.5 months.

Root causes found in the engine (all real code paths, since reproduced):

1. **No expiry awareness anywhere in the exit path.** For a real-option
   position, a missing option candle was skipped indefinitely ("wait for real
   option candle or expiration" — expiration was never implemented), so the
   position froze until the end of data.
2. **No entry-session gate.** The engine had no last-entry cutoff; the same
   run contains **18 entries at 14:50–15:25 IST** (after the documented 14:45
   cutoff — including 15:00–15:25 entries inside the square-off zone that live
   execution could never have placed).
3. **Paper scan loop had no entry cutoff either** (any in-session time could
   open a position) — the producer of this CSV lineage never enforced it.

### §8.2 Fixes (validated, tests pinned)

- **Expiry lifecycle gate** (`backend/backtest/engine.py`): any open option
  position is force-closed no later than its actual contract expiry — at the
  first bar past the expiry-date 15:25 IST deadline — with reason
  `EXPIRY_FORCED_CLOSE`, priced at the last actually-observed option price
  (never the underlying spot, never a fabricated terminal value). A position
  with missing candles for 3 consecutive sessions is likewise force-closed.
  `BACKTEST_END` now only ever closes positions genuinely alive at the last
  bar. Counters on the result: `positions_forced_expiry_closed`,
  `lifecycle_violations_prevented`; every forced close logs a loud error.
- **Backtest entry window** (same file): entries restricted to 09:20–14:45 IST
  (the documented `session_manager` policy and the live gate); typed
  `ENTRY_SESSION_RESTRICTED` rejections; opt-out parameter
  `enforce_entry_session_window=False` kept for legacy comparisons.
- **Paper loop parity** (`backend/paper/market_scan_loop.py`): `scan_once`
  now refuses NEW entries outside 09:20–14:45 IST, fail-closed on errors;
  positions, square-off and AI monitoring are unaffected.
- **Backtest-only signal diagnostics**: `signal_diagnostics` on every
  BacktestTrade (EMA20/50 + separation, RSI, ATR, pullback distance in ATRs,
  option premium/ATR, DTE, strike distance, stop/target distances, trend,
  entry time) — computed from the same window the strategy evaluated (no
  look-ahead). NEVER populated on live/paper paths.
- Regression tests: `backend/tests/test_backtest_expiry_lifecycle.py` (8 —
  past-expiry force-close, frozen-data force-close at expiry, normal
  expiry-day stop still wins, honest BACKTEST_END preserved, entry-window
  blocks (options + equity paths), documented opt-out, normal intraday
  square-off unchanged) + 2 paper-loop window tests in
  `test_market_scan_loop.py`.

### §8.3 A–N forensic findings (full detail in the artifacts)

- **A**: expectancy −₹294.26/trade, avg R −0.157, median R −0.04, max
  consecutive losses 17 (−₹21,799). **B**: negative on all 6 symbols
  (BANKEX worst: WR 14.3%, PF 0.27). **C**: CE PF 0.48 (−₹55,121) worse than
  PE PF 0.74 (−₹26,390). **D**: STOP_LOSS_HIT 109 trades −₹189,146 (avg R
  −1.01); TARGET_HIT 42 +₹100,158 (avg R +1.48); TRAILING 89 +₹5,814 (mostly
  scratch); INTRADAY_SQUARE_OFF 36 +₹1,672. **E**: 12 of 13 months negative
  (2025-10 the lone positive: +₹13,234, PF 1.79).
- **F/G (time-of-day & weekday)**: the two worst buckets are 10:00–11:30
  (n=97, PF 0.40) and Tuesday/Wednesday (PF 0.285 each, n=125 combined) — but
  the bucket that looks BEST is AFTER_14:45 (n=22, PF 2.53), which is exactly
  the artifact of the 18 impossible entries: it disappears under the entry
  window fix, demonstrating why un-gated backtests must not be trusted.
- **I (DTE)**: DTE_0 trades are the epicenter — n=120 (43% of all trades),
  WR 20.0%, PF 0.32, −₹53,852 (66% of total net loss); WR/PF rise roughly
  monotonically with DTE. **J/K/L**: ≥30% stop-distance trades (n=109, PF
  0.40) far worse than 25–30% (n=168, PF 0.72); the 42% target needs ~40% WR
  to break even vs the realized 28.88%. **M**: `setup_score` is constant 100
  (transparency, not edge); no indicator fields existed in the CSV — hence
  §8.2 diagnostics. **N**: losses are persistent across months/symbols
  (80/104 days net-negative; worst 5 days only 39.6% of the net) — consistent
  with structural negative expectancy after costs, not one bad regime.
- **Costs**: fees+slippage ≈ ₹6,026 = 1.73% of gross absolute P&L — real but
  not the primary driver of the loss.

### §8.4 OLD vs NEW — no strategy change adopted

Chronological split (train 2025-09-26→2026-06-30, n=253; validation
2026-07-01→2026-09-27, n=24), rule = must improve BOTH halves. Candidates
(pre-declared from the forensics): skip 0-DTE, stop <30%, both, morning-only.
Result: every candidate improves the train half (no-0-DTE: PF 0.647→0.90)
but NONE improves validation (PF stays 0.32, expectancy stays ≈ −₹723) —
**V8-D remains FROZEN** (`analysis/v8d_old_vs_new_p53.json`). The honest
conclusion: this strategy has negative expectancy as measured; the fix that
WAS justified is the broken measurement machine (§8.2), plus diagnostics so
the next real run can measure true signal edge.

### §8.5 Reproducibility note (honest)

The local historical options cache is empty, so the exact 277-trade run
cannot be re-simulated on this machine (and was not faked). The lifecycle
defects are proven from the CSV itself (the zero-P&L 6.5-month hold; the
18 late entries) and the fix is proven by regression tests that seed REAL
HistoricalOptionsDataLoader objects with the same contract/expiry shape and
drive the production engine end-to-end. The next real options backtest run
must report `positions_forced_expiry_closed` and `entry_session_rejections`
(both are now fields on the result and its CSV/JSON outputs).

### §8.6 NIFTY50 alias preservation (explicit check)

The NIFTY50→NIFTY Upstox instrument-master alias fix and both of its tests
were verified present and unchanged in the working tree before packaging:
`backend/broker/instrument_master.py` (aliases dict, ~line 135) and
`backend/tests/test_instrument_master.py`
(`test_nifty50_resolves_via_upstox_nifty_alias`,
`test_nifty50_does_not_match_other_nifty_indices`). They are included in the
rebuilt ZIP unchanged.
