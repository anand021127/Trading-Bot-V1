# PRODUCTION QA REPORT — Trading-Bot-V1

Date: 2026-09-24 · Runner: true `pytest` (the repo-root `pytest.py` shim was
shadowing the real pytest package and was renamed — see audit C5)

## 1. Headline numbers

| Metric | Before hardening | After hardening |
|---|---|---|
| Collected | 629 | 651 |
| **Passed** | 620 | **650** |
| **Failed** | 11 | **0** |
| **Errors** | 8 | **0** |
| Skipped | 0 | 0 |
| New regression tests | — | +22 (`test_production_hardening_regression.py`) |

Full-suite evidence: `python -m pytest backend/tests -q --tb=no -p no:cacheprovider -o addopts=`
→ `650 passed, 1 warning in 111.55s`.

## 2. Regression tests added (22) — mapped to production bugs

| Test | Bug class covered |
|---|---|
| `test_restart_restores_realized_equity_and_daily_counters` | C2 — restart reset risk limits |
| `test_restart_new_day_resets_daily_counters_but_keeps_equity` | C2 — day-rollover semantics |
| `test_restart_restores_position_risk_state_from_extra` | C3 — SL/target/lot lost on restart |
| `test_exit_after_restart_uses_restored_stop_and_persists_once` | C3 + duplicate-exit guard |
| `test_manual_exit_queue_deduplicates_and_executes_once` | C4 — fake exit endpoint |
| `test_manual_exit_for_unknown_position_is_safe_noop` | C4 — unknown symbol safety |
| `test_trading_router_exit_endpoint_queues_when_runtime_attached` | C4 — endpoint actually works |
| `test_paper_place_never_reaches_real_upstox_place_order` | paper/live contamination |
| `test_offline_guard_blocks_real_upstox_place_order_even_when_called_directly` | H4 — offline guard |
| `test_order_intent_insert_is_idempotent_under_retry` | C9 — retry overwrote intent |
| `test_duplicate_signal_100x_never_creates_two_positions` | idempotency ×100 |
| `test_sqlite_wal_and_busy_timeout_applied` | H1 — SQLITE_BUSY |
| `test_close_is_idempotent_and_releases_file` | H3 — leaked handles |
| `test_daily_counters_atomic_increment` | H2 — atomic counters |
| `test_pid_liveness_current_process_true` | C6 — Windows PID probe |
| `test_pid_liveness_bogus_and_invalid` | C6 |
| `test_worker_status_reports_spawned_worker_alive` | C6 — false "dead" report |
| `test_worker_started_after_eod_refuses_new_entries` | startup-after-EOD |
| `test_startup_before_market_open_does_not_trade_via_cutoff_rule` | pre-open determinism |
| `test_reconcile_orphan_broker_position_fails_closed` | fail-closed reconcile |
| `test_reconcile_self_heals_ledger_orphan_by_hydration` | the NSE_FO\|69780 incident class |
| `test_eod_square_off_closes_restored_position_at_valid_mark` | EOD after restart |

## 3. Failure-injection coverage (offline)

* Duplicate signal ×100 → 100/100 rejected, exactly one position/one trade row.
* Order-intent retry overwrite → impossible (`INSERT OR IGNORE`).
* Broker order-id clear attempt → blocked (monotonic attribution).
* Expired/missing token at paper entry → rejected (existing + hardened tests).
* Broker positions API failure → `broker_positions_unavailable` fail-closed (existing).
* Ledger↔broker quantity mismatch → `STOP_NEW_ENTRIES` latched (existing).
* Broker-only orphan → fail closed (new).
* Ledger-only orphan (restart incident) → self-heals via hydration (new).
* Worker spawn / duplicate start / stop / kill (subprocess, real) → 10/10 e2e tests.
* EOD cutoff rejection at 15:30 IST start → `EOD_CUTOFF`.
* Stale candles (>900 s), future candles, insufficient bars → scan refuses (existing).
* Corrupt OHLC → rejected before strategy evaluation (existing).
* Contract metadata missing lot size → `INVALID_LOT_SIZE` rejection (existing).
* Notional above allocation cap → rejected (proved live during this pass: the
  regression fixture itself was rejected at ₹19,305 > ₹18,000 before being fixed).
* `TRADING_BOT_OFFLINE_TESTS=1` → `UpstoxClient._get` and `place_order` both raise
  before any socket use; mocked `requests.Session.post` proves zero network writes.

## 4. Restart / recovery drills

* Paper worker subprocess lifecycle: spawn → heartbeat → duplicate-start guard →
  SIGTERM stop → kill switch → reset (`test_paper_worker_e2e.py`, 6 tests) — all pass.
* Position hydration after restart with full risk state (new).
* Equity/counters restore after restart (new) + new-day rollover (new).
* Kill switch persistence across a brand-new DB handle (existing).
* Node bridge status/start/stop/kill/reset round-trip (existing, 4 tests).

## 5. Paper-safety verification

* Zero calls to real Upstox order placement from any paper path — proven by
  mocking `UpstoxClient.place_order`, `Session.post`, `Session.request` with
  trip-wires while executing entry + intraday exit end-to-end.
* Paper reconcile never contacts Upstox positions (`PaperBroker` type enforced).
* Strategy gate: any strategy other than `V8_D_PULLBACK_ATM` refuses to start
  the paper runtime, refuses pipeline submission, refuses scanner evaluation.

## 6. Live-safety verification (static + mocked; live NOT enabled)

* `bot_control.start` refuses `TRADING_MODE=live` (tested).
* `node_bridge.cmd_start` refuses live (tested).
* `worker_manager` preflight refuses live (tested via bridge).
* `TradingEngine` live construction refuses `paper_mode=False` mismatch and
  requires explicit product (existing tests).
* Live-order path additionally blocked by the offline guard under test env.

## 7. Frontend verification

* `npm ci` clean install: 325 packages, registry-clean lockfile (no private URLs).
* `npm run build` (Vite production): success, ~31.6 s, dist emitted.
* `npm run typecheck` (`tsc --noEmit`): clean.
* Contract checks: paper positions now expose SL/target/lot + `paper_runtime_attached`;
  manual exit endpoint performs a real queue+execute; health exposes worker PID,
  heartbeat age, pipeline_ok, broker auth, market-feed state.

## 8. Clean-install verification

* Backend: CI installs `backend/requirements.txt` on Python 3.11 (`.github/workflows/python-tests.yml`)
  and runs true pytest. Local suite executed against Python 3.14 too — 650 pass.
* Frontend: clean `npm ci` + build + typecheck verified locally on Node 22.
* `.env.example` rewritten: safe paper defaults, precedence rules, test switches documented.

## 9. Deployment checks

* New `deploy/systemd/upstox-bot.service` (paper-mode enforced, hardened unit).
* Nginx config present (`deploy/nginx/upstoxbot.conf`, TLS + WS upgrade).
* Vercel frontend config present (`frontend/vercel.json`, SPA rewrites + API proxy).
* Deployment command sequence documented in PRODUCTION_DEPLOYMENT.md.

## 10. Known-good environment notes

* Windows dev machine: PID liveness and handle-hygiene fixes make the suite green
  here too; worker e2e tests pass (~72 s).
* pytest must be invoked with `-p no:cacheprovider` against OneDrive-synced paths
  to avoid watcher contention (documented in runbook troubleshooting).

---

# ADDENDUM — Phase 3 (Copilot + Backtest) verification pass, 2026-09-25

Scope: final audit of the async Copilot chat + background backtest phase that was
already implemented on disk when this pass started (root cause of the reported
"timeout of 30000ms exceeded" class of UI failures: synchronous provider/loop
work inside single HTTP requests — replaced by 202 + job-id + short-poll on both
the Copilot chat and backtest paths).

## What this pass verified (all evidence from real runs)

| Check | Result | Evidence |
|---|---|---|
| Async chat jobs: submit→202→poll→complete/cancel | PASS | `backend/tests/test_copilot_api_regression.py` (24 tests: job lifecycle, typed provider errors, secret redaction end-to-end, honest PROVIDER_NOT_CONFIGURED path — no canned fake answers) |
| Provider failure contract is TYPED, never a fake answer | PASS | `provider_errors.py` + `llm_adapter.py` raise `AIProviderError` subclasses (unreachable/timeout/auth/rate-limit/model-missing); `RuleBasedFallbackAdapter` remains explicit via `COPILOT_LLM_BACKEND=none` only |
| Context grounding + secret redaction before provider | PASS | `context.py` builds question-relevant Bot/Trade/Backtest contexts from real tool outputs; `secret_guard.py` value+key redaction applied to context AND final answer |
| Backtest async job lifecycle (QUEUED→FETCHING_DATA→RUNNING→COMPLETED/FAILED/CANCELLED) | PASS | `backend/tests/test_backtest_async_regression.py` + `test_backtest_cancel_and_restart.py` + `tests/test_backtest_lifecycle.py` (35 tests) |
| Backtest cancellation actually stops work | PASS | cooperative `_cancelled` flag checked at fetch/symbol/simulation boundaries + `asyncio` task cancel; status flips to CANCELLED and work returns |
| Completed/failed/cancelled states render truthfully in frontend | PASS | `Backtest.tsx` polls `/api/backtest/jobs/{id}`, maps COMPLETED/FAILED/CANCELLED to distinct UI states and shows the backend's real `error_details` message; retries transient poll failures only |
| No synthetic data / no silent OPTION_PREMIUM fallback in backtest | PASS | missing symbols → `DATA_UNAVAILABLE` fail-closed; incomplete bars → `BACKTEST_INCOMPLETE` fail-closed; strategy list must come from the registry or explicitly named strategies (400 otherwise) |
| Transient historical-API failures retried without data substitution | PASS | `historical_fetch.py` bounded retry/backoff (3 attempts, 429/5xx/timeout only; 401/400 fail fast); re-issues the SAME request — no alternate source |
| Copilot page wired to async job API | **FIXED** | `Copilot.tsx` created the assistant placeholder with a client-generated UUID but matched poll updates by the SERVER's job id — the answer would never render (stuck "Thinking…"). Placeholder is now re-keyed to the server job id before polling |
| Stale docstring contradicting failure contract | **FIXED** | `LocalOpenAICompatibleAdapter` docstring claimed "falls back to rule-based on error" — code raises typed errors; docstring corrected, no behavior change |
| ONE test runner | **FIXED** | `run_all_tests.py` was a second, diverging framework (probed a nonexistent `_is_fixture` attr, crashed on real pytest fixtures: `Failed: Fixture "memory_db" called directly`). Now a thin wrapper over true pytest with the same offline/paper isolation envs; `python run_all_tests.py` → 721 passed |
| Full backend suite | PASS | `python -m pytest backend/tests -q` → **721 passed, 0 failed, 0 errors** (85 s) |
| Root integration suite | PASS | `python run_all_tests.py tests` → 30 passed |
| Frontend typecheck + build | PASS | `tsc --noEmit` clean; `vite build` ✓ 8.56 s |
| Strategy/mode/product invariants untouched | PASS | `TRADING_MODE=paper`, `TRADING_STRATEGY=V8_D_PULLBACK_ATM`, `UPSTOX_ORDER_PRODUCT=I`; `v8d_strategy.py` not modified in this pass |
| Secrets in deliverable | PASS | tracked-files scan + full ZIP scan: no tokens/keys/.env/token-store/db; ZIP hits were prose false positives ("risk-capped") and variable-passing (`request_token_approval(client_secret=client_secret)`) |

## Copilot decision authority (re-verified)

* Every chat job resolves context deterministically FIRST (`route_question` +
  `build_context` over real tool outputs); the provider receives already-computed
  data and returns prose. It has no tool access, no execution path, and cannot
  flip a decision — LLM output is explanation only.
* No Copilot endpoint places, modifies, or cancels orders; the async job worker
  only formats context and calls the provider.

## Remaining limitations (unchanged, honest)

* NSE holiday calendar not modeled (stale-data gate is the paper-mode protection;
  required before live — same as phase 1 finding).
* Chat/backtest jobs are in-memory in the API process (single-process deployment;
  a backend restart loses job history — reported truthfully via 404 + UI guidance).
* Live trading remains BLOCKED by design pending the dedicated live release.

---

# ADDENDUM 2 — Phase 4 (infrastructure gaps) delivery, 2026-09-26

Closes the three gaps this report previously listed as known limitations:
the exchange holiday calendar, in-memory backtest job state, and the missing
full-stack CI. All evidence below is from real runs on 2026-09-26.

## Gap 1 — Authoritative exchange session/holiday calendar

`backend/market/calendar.py` is the ONE authoritative source for trading-day,
session-time, and expiry decisions. All production modules now delegate to it:
paper scan gate (`market_scan_loop.py`), live/paper session manager (which also
gained the previously-missing `is_market_open` — a latent `AttributeError` in
the live path — and a working `is_entry_window`), market-data websocket client,
overview/trading/websocket routers, diagnostics, backtest coverage math, and
expiry resolution (`get_nearest_expiry_for_date` now holiday-shifts). Copilot
market-status answers are grounded via the same tools.

* Verified holiday tables for **2024 (NSE circular CMTR59722), 2025, 2026** —
  corroborated across independent published calendars; no guessed dates.
* Special sessions: Muhurat trading 2024-11-01 / 2025-10-21 / 2026-11-08
  (18:15–19:15 IST, published timings) and the 2025-02-01 Budget Saturday.
* Expiry holiday-shift rule: shift BACKWARD to the previous trading day.
* IST is the only trading timezone; naive timestamps treated as IST; UTC
  boundary tests pin the date-flip behavior.
* **FAIL-CLOSED**: an unverified year raises `CalendarDataError` — never
  silently weekend-only. Extending the calendar = append one table.
* The 32-test suite (`test_exchange_calendar.py`) pins weekends, holidays for
  all three years, expiry shifts, month/year boundaries, IST/UTC boundaries,
  special sessions, and the fail-closed rule.
* Effect on results: trading results are bitwise unchanged; coverage math is
  corrected (holidays no longer counted as requested trading days — see
  benchmark verdict below). Legacy `weekday()` checks remain ONLY as
  documented fail-safe fallbacks inside try/except blocks.

## Gap 2 — Durable backtest job state

`backend/backtest/job_store.py` (SQLite, WAL, busy_timeout, per-update
transactions, idempotent upserts) persists every contract field: job_id,
status, phase, strategies, symbols, dates, interval, capital, timestamps,
progress, processed/total bars, bars_per_second, ETA, current symbol/timestamp,
result, error, error_details, cancel flag.

* **Restart recovery**: at startup (and via `POST /api/backtest/jobs/recover`)
  any job left active is marked `INTERRUPTED_BY_RESTART` — never COMPLETED.
* Completed/failed/cancelled results remain retrievable after restart; status
  polling and `/jobs/active` fall back to the durable row when memory is empty.
* Duplicate jobs are DB-enforced across restarts (partial-unique index on the
  active-state set).
* New states honored end-to-end: `RESOLVING_CONTRACTS` (fetch→simulate
  bracket) and `FINALIZING`; bars/second + ETA computed from the row's own
  `started_at` and persisted.
* 15-test restart suite (`test_backtest_job_store.py`) + 9 contract tests.

## Gap 3 — Full-stack CI

`.github/workflows/ci.yml` (YAML-validated) runs on push/PR to main:
backend suite **and** root integration tests under enforced paper/offline env
(`ALLOW_LIVE_UPSTOX=0`, empty token — CI can never place an order); frontend
`npm ci` → `tsc --noEmit` → eslint → `vite build` → output existence check;
repo hygiene (`git diff --check`, conflict markers, tracked secret-file scan,
credential-literal scan, JWT-shape scan, packaging exclusion rule).
Fails on test/type/build errors and on any secret detection. The legacy
partial workflows remain for their specific deploy roles; CI is the gate.

## Status enum contract

`backend/backtest/status.py` defines the 9 authoritative values
(QUEUED, FETCHING_DATA, RESOLVING_CONTRACTS, RUNNING, FINALIZING, COMPLETED,
FAILED, CANCELLED, INTERRUPTED_BY_RESTART) with terminal/active partitioning
and lowercase normalization. The frontend `BacktestStatus` union in
`frontend/src/types/index.ts` must mirror it exactly — enforced by a test that
parses the TS source and diffs it against the backend set.

## Backtest UI

`Backtest.tsx` now recovers the latest durable job on mount (auto-resumes
polling an active job; re-hydrates a COMPLETED result; shows FAILED/CANCELLED/
INTERRUPTED banners with the backend's real reason), labels every state from
the shared map (including INTERRUPTED and RESOLVING_CONTRACTS), and can never
show "Running…" when the backend reports a terminal state (statuses are
compared as exact enum values).

## No-regression evidence

| Check | Result |
|---|---|
| Backend suite | **777 passed, 0 failed, 0 errors, 0 skipped** (89 s) — includes 56 new Phase 4 tests |
| Root integration suite | 30 passed |
| Frontend typecheck | clean |
| Frontend production build | ✓ 7.3 s |
| Frontend lint | 0 errors (23 pre-existing warnings, untouched) |
| Benchmarks NIFTY50 V8-D 5min 1d/5d/25d | trades/net_pnl/accuracy/PF/validity/signals **identical to pre-change baseline**; 25d coverage 95.7→100.0 solely from the holiday correction (PASS by design: coverage may only improve via verified-holiday removal, never degrade) |
| Safety invariants | TRADING_MODE=paper · TRADING_STRATEGY=V8_D_PULLBACK_ATM · UPSTOX_ORDER_PRODUCT=I · `v8d_strategy.py` byte-identical to HEAD · no synthetic data paths · no silent OPTION_PREMIUM fallback · no LLM execution authority |
